"""Import the human-produced scene media and open the gate to rendering.

PLAN-001 issue #8, SPEC-001 §7. One entry point::

    job = import_assets(job, store)                      # runs in AWAITING_ASSETS
    job = import_assets(job, store, creator_profile=p)   # ... with a real person in it

The §6.1 generation manifest is the contract: it already told the operator
which file to produce and exactly where to put it, so this stage reads it back
and checks what actually landed there. **Files are never moved.** An imported
asset's ``storage_key`` is the manifest's own
``<import_dir>/<expected_filename>``, job-dir-relative and POSIX-separated —
e.g. ``scenes/scene-001/images/scene-001.png``. (The two frozen fixtures carry
``assets/asset-NNN.png``; those are hand-written placeholders pointing at files
that do not exist, and are not the convention.) Like every other persisted
``storage_key``, it is data: paths are always resolved through
:class:`~app.services.jobs.store.JobStore` helpers, never by joining the
recorded string.

**Preflight gates real-person assets only.** PRD-001 FR-005 scopes the
creator-profile check to 真人 voice/avatar material, and issue #6 records a
synthesised Master Voice as ``consent_status="not_applicable"`` /
``manual_review_status="not_required"``. Demanding ``explicit_granted`` from
every asset would park every V0 job at this gate. So an asset is *subject* to
preflight when its ``consent_status`` is anything other than
``"not_applicable"``, **or** when a creator profile references it by
``asset_ref``. Everything else passes through untouched.

Not billable: like issue #5 and issue #7, this stage runs entirely locally,
makes no provider call, and therefore calls neither ``check_budget`` nor
``record_usage``. (``check_budget`` refuses whenever ``actual_cost_usd`` is
``"unknown"``, so calling it here would block a free operation.) There is no
CLI entry point either — ``run --job`` belongs to issue #11.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.models.content_job import (
    AssetRecord,
    ContentJob,
    GenerationManifestEntry,
    JobStatus,
)
from app.services.creator_profile import CreatorProfileError, validate_creator_profile
from app.services.jobs import media_probe
from app.services.jobs.state_machine import (
    TRANSITIONS,
    UnauthorizedAssetError,
    classify_error,
    decision_record,
    transition,
    utc_now,
)
from app.services.jobs.store import SCENES_DIR, JobStore

#: SPEC-001 §4.5 values this stage writes. ``asset_type`` is an unvalidated
#: string on the model, so these constants are the only place the convention
#: lives — same arrangement as ``captions.SUBTITLE_ASSET_TYPE``.
IMAGE_ASSET_TYPE = "image"
VIDEO_ASSET_TYPE = "video"
#: §7 rule 7: where the bytes came from. A human produced them outside the
#: pipeline and dropped them at the manifest's import path.
SOURCE_MODE = "human_import"
LICENSE_OPERATOR_SUPPLIED = "operator_supplied_asset"
#: §4.1 / PRD-001 FR-005 vocabulary, shared with ``creator_profile``.
CONSENT_NOT_APPLICABLE = "not_applicable"
CONSENT_EXPLICIT_GRANTED = "explicit_granted"
REVIEW_APPROVED = "approved"
REVIEW_NOT_REQUIRED = "not_required"

#: The Master Voice's ``asset_type``, written by ``master_voice`` — what
#: ``creator_profile.voice.asset_ref`` must resolve to (SPEC-001 §4.1:121).
MASTER_VOICE_ASSET_TYPE = "audio"


class AssetImportError(RuntimeError):
    """A manifest entry's file is missing or does not pass §7.

    Always non-retryable: a wrong file does not become the right file by
    trying again. ``missing`` counts the entries whose file was simply not
    there, which is the 缺件 case a human resolves and resumes.
    """

    def __init__(self, message: str, *, missing: int = 0) -> None:
        super().__init__(message)
        self.retryable = False
        self.missing = missing


def asset_id_for(scene_id: str) -> str:
    """The ``asset_id`` this stage mints for a scene's imported media.

    Derived, not random, for two reasons: re-running must be able to tell "this
    entry is already imported" from "this entry produced a second record"
    against a store that has no dedupe of its own, and a creator profile has to
    be able to name the avatar asset in ``avatar.asset_ref`` before the import
    that creates it has run.
    """
    return f"asset-{scene_id}"


# -- §7 rules 1-6: is this file the file the manifest asked for? -------------


def _entry_path(store: JobStore, job_id: str, entry: GenerationManifestEntry):
    """Resolve one entry's expected file through the store's guarded helpers.

    ``import_dir`` and ``expected_filename`` are persisted manifest data, so
    they are parsed rather than joined: the directory is rebuilt from
    ``scene_media_dir`` (which validates the scene id and the media kind and
    proves the result sits under the store root), and the filename must be a
    bare name.
    """
    parts = PurePosixPath(entry.import_dir).parts
    if len(parts) != 3 or parts[0] != SCENES_DIR or parts[1] != entry.scene_id:
        raise AssetImportError(
            f"scene {entry.scene_id}: manifest import_dir is not this scene's "
            f"media directory"
        )
    name = entry.expected_filename
    if not name or os.path.basename(name) != name or name in (".", ".."):
        raise AssetImportError(
            f"scene {entry.scene_id}: manifest expected_filename is not a plain "
            f"file name"
        )
    path = store.scene_media_dir(job_id, entry.scene_id, parts[2]) / name
    if os.path.islink(path):
        # ``store`` proves the *directory* sits under its root, but the file is
        # opened directly and ``open`` follows links. Measured: a symlink at
        # the import path imports bytes from outside the job tree while the
        # recorded ``storage_key`` still claims the manifest's path.
        raise AssetImportError(
            f"scene {entry.scene_id}: the import path is a symlink; the media "
            f"must be the file itself"
        )
    return path


def _validated_facts(
    path, entry: GenerationManifestEntry
) -> media_probe.MediaFacts:
    """§7 rules 2, 3, 5 and 6 for one file that is known to exist."""
    scene = entry.scene_id
    facts = media_probe.probe(path)

    # Rule 2, both directions: the bytes must be a type the manifest accepts,
    # and the name the manifest chose must agree with the bytes.
    if facts.mime not in entry.accepted_mime_types:
        raise AssetImportError(
            f"scene {scene}: the file is {facts.mime} but the manifest accepts "
            f"{', '.join(entry.accepted_mime_types)}"
        )
    extension = os.path.splitext(entry.expected_filename)[1].lower()
    if media_probe.EXTENSION_MIME_TYPES.get(extension) != facts.mime:
        raise AssetImportError(
            f"scene {scene}: the file is {facts.mime} but is named {extension!r}"
        )

    # Rule 5. ``decoded is False`` means this host has no decoder at all —
    # ``probe`` raises when a decoder is present and refuses the file. Refuse
    # either way: an unverifiable asset must not be worth the same as a
    # verified one.
    if not facts.decoded:
        raise AssetImportError(
            f"scene {scene}: no decoder is available on this host, so the file "
            f"cannot be proven readable"
        )

    # Rule 3. The byte ceiling is enforced by ``media_probe.probe`` instead,
    # which can refuse on the ``stat`` alone rather than after hashing and
    # decoding a file it is about to reject for being too big.
    for label, value in (("width", facts.width), ("height", facts.height)):
        if not media_probe.MIN_DIMENSION <= value <= media_probe.MAX_DIMENSION:
            raise AssetImportError(
                f"scene {scene}: {label} {value} is outside "
                f"{media_probe.MIN_DIMENSION}-{media_probe.MAX_DIMENSION} px"
            )
    if facts.duration_ms is not None and not (
        media_probe.MIN_VIDEO_DURATION_MS
        <= facts.duration_ms
        <= media_probe.MAX_VIDEO_DURATION_MS
    ):
        raise AssetImportError(
            f"scene {scene}: duration {facts.duration_ms} ms is outside "
            f"{media_probe.MIN_VIDEO_DURATION_MS}-"
            f"{media_probe.MAX_VIDEO_DURATION_MS} ms"
        )
    return facts


def _asset_record(
    *,
    job_id: str,
    entry: GenerationManifestEntry,
    facts: media_probe.MediaFacts,
    consent: Mapping[str, Any],
    created_at: str,
) -> AssetRecord:
    """§7 rules 7 and 10: the record, with its provenance and its consent."""
    return AssetRecord(
        asset_id=asset_id_for(entry.scene_id),
        content_job_id=job_id,
        scene_id=entry.scene_id,
        asset_type=(
            IMAGE_ASSET_TYPE if facts.mime == media_probe.IMAGE_PNG else VIDEO_ASSET_TYPE
        ),
        storage_key=f"{entry.import_dir}/{entry.expected_filename}",
        original_filename=entry.expected_filename,
        mime_type=facts.mime,
        bytes=facts.bytes,
        width=facts.width,
        height=facts.height,
        duration_ms=facts.duration_ms,
        sha256=facts.sha256,
        source_mode=SOURCE_MODE,
        provider=entry.provider,
        model="",
        **consent,
        created_at=created_at,
    )


def _unowned_consent() -> Dict[str, str]:
    """The consent block for media nobody's likeness or voice appears in.

    Same reasoning as the synthesised Master Voice (#6) and the derived
    subtitle track (#7): there is no person to have consented, so there is no
    consent to record and no human review to wait for. A real person's asset
    gets :func:`_profile_consent` instead.
    """
    return {
        "license_or_consent": LICENSE_OPERATOR_SUPPLIED,
        "consent_status": CONSENT_NOT_APPLICABLE,
        "usage_scope": "",
        "consent_source": "",
        "consent_expires_at": "",
        "consent_revoked_at": None,
        "manual_review_status": REVIEW_NOT_REQUIRED,
    }


def _profile_consent(profile: Mapping[str, Any], section: Mapping[str, Any]) -> Dict:
    """§7 rules 10 and 11 carried from the creator profile onto the asset.

    ``license_or_consent`` records *which* profile authorised these bytes.
    ``ContentJob.creator_profile_id`` itself is left alone: it is part of the
    §3.1 request contract, the pipeline hard-codes it to ``""``, and widening
    that is a spec change rather than this slice's work — see the handoff.
    """
    return {
        "license_or_consent": f"creator_profile:{profile['creator_profile_id']}",
        "consent_status": section["consent_status"],
        "usage_scope": section["usage_scope"],
        "consent_source": section["source"],
        "consent_expires_at": section["expires_at"],
        "consent_revoked_at": section["revoked_at"],
        "manual_review_status": section["manual_review_status"],
    }


# -- §7 rules 11 and 13: creator profile preflight ---------------------------


def _as_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise UnauthorizedAssetError(f"{label} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise UnauthorizedAssetError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalized_profile(
    profile: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """``validate_creator_profile`` plus the expiry §7 rule 11 actually requires.

    Measured 2026-08-29: the upstream validator treats ``expires_at`` as
    optional — deleting the key, or passing ``""`` or ``None``, passes and
    stores ``""``, so a profile with no expiry never expires. SPEC-001 §7 rule
    11 (line 506) lists expiry alongside consent, scope, source and review as
    required. ``creator_profile.py`` is shared with the legacy task pipeline
    and is not edited here, so the missing half of the rule is enforced on top
    of it.
    """
    try:
        result = validate_creator_profile(profile, now=now)
    except CreatorProfileError as error:
        raise UnauthorizedAssetError(f"creator profile is not usable: {error}") from error
    for section in ("voice", "avatar"):
        if not result[section]["expires_at"]:
            raise UnauthorizedAssetError(
                f"creator profile {section} reference has no expiry; SPEC-001 §7 "
                f"rule 11 requires one, and a consent that never expires is not "
                f"a consent this pipeline will act on"
            )
    return result


def _resolve_reference(
    assets: Sequence[AssetRecord], reference: str, label: str
) -> AssetRecord:
    """SPEC-001 §4.1:121, which nothing implemented before this slice."""
    matches = [asset for asset in assets if asset.asset_id == reference]
    if not matches:
        raise UnauthorizedAssetError(
            f"creator profile {label}.asset_ref resolves to no asset of this job"
        )
    if len(matches) > 1:
        raise UnauthorizedAssetError(
            f"creator profile {label}.asset_ref resolves to {len(matches)} assets; "
            f"it must name exactly one"
        )
    return matches[0]


def _refuse_consent_defects(asset: AssetRecord, now: datetime) -> None:
    """PRD-001 FR-005: expired、revoked、未明確同意或未通過 manual review 不得進入 render."""
    label = f"asset {asset.asset_id}"
    if asset.consent_status != CONSENT_EXPLICIT_GRANTED:
        raise UnauthorizedAssetError(
            f"{label} carries consent_status {asset.consent_status!r}, not "
            f"{CONSENT_EXPLICIT_GRANTED!r}"
        )
    if asset.manual_review_status != REVIEW_APPROVED:
        raise UnauthorizedAssetError(
            f"{label} has not passed manual review "
            f"({asset.manual_review_status!r})"
        )
    if not asset.usage_scope.strip():
        raise UnauthorizedAssetError(f"{label} records no usage scope")
    if not asset.consent_source.strip():
        raise UnauthorizedAssetError(f"{label} records no consent source")
    if asset.consent_revoked_at:
        raise UnauthorizedAssetError(f"{label} consent has been revoked")
    if not str(asset.consent_expires_at).strip():
        raise UnauthorizedAssetError(f"{label} consent has no expiry")
    if _as_datetime(asset.consent_expires_at, f"{label} consent_expires_at") <= now:
        raise UnauthorizedAssetError(f"{label} consent has expired")


def preflight(
    assets: Sequence[AssetRecord],
    profile: Optional[Mapping[str, Any]],
    *,
    now: datetime,
) -> None:
    """Refuse the job entry to ``READY_TO_RENDER`` if any consent is unusable.

    Scope, restated because it is the decision this function embodies: an asset
    is checked when it claims a consent status at all, or when the profile
    names it. A synthetic-TTS voice and a machine-generated still are
    ``not_applicable`` and are not checked.
    """
    references: Dict[str, str] = {}
    if profile is not None:
        for section in ("voice", "avatar"):
            references[profile[section]["asset_ref"]] = section
        voice = _resolve_reference(assets, profile["voice"]["asset_ref"], "voice")
        if voice.asset_type != MASTER_VOICE_ASSET_TYPE:
            raise UnauthorizedAssetError(
                "creator profile voice.asset_ref must resolve to the Master Voice"
            )
        avatar = _resolve_reference(assets, profile["avatar"]["asset_ref"], "avatar")
        if not avatar.scene_id:
            raise UnauthorizedAssetError(
                "creator profile avatar.asset_ref must resolve to a scene asset"
            )
    for asset in assets:
        if asset.consent_status == CONSENT_NOT_APPLICABLE and (
            asset.asset_id not in references
        ):
            continue
        _refuse_consent_defects(asset, now)


# -- the stage ---------------------------------------------------------------


def _park(job_id: str, store: JobStore, error: BaseException) -> None:
    """Move an unrecoverable job to ``MANUAL_ACTION_REQUIRED``.

    Same shape and same reasoning as ``captions._park``: ``AWAITING_ASSETS`` is
    not a 可重試階段, so §5.2 gives it no ``RETRYABLE_FAILED`` edge and a
    retryable failure simply stays put for the caller to try again.

    The guard is "does §5.2 allow this state to be parked" rather than "is this
    state ``AWAITING_ASSETS``". A re-run that refuses an asset on a job already
    at ``READY_TO_RENDER`` must still park it — measured, the narrower guard
    left an unauthorised real-person asset sitting in an open render gate while
    the call raised.

    The reason string names scene ids and rule violations only. §7 rule 12
    keeps raw filenames and narration out of the audit trail, and
    ``budget.redact`` is not usable on them — measured, it eats an identifier
    whole (``"my secret plan.png"`` -> ``"my <redacted>"``).
    """
    if classify_error(error).is_retryable:
        return
    current = store.load(job_id).job
    if JobStatus.MANUAL_ACTION_REQUIRED not in TRANSITIONS[current.status]:
        return
    reason = f"asset import refused: {error}"
    parked = transition(current, JobStatus.MANUAL_ACTION_REQUIRED, reason=reason)
    store.save(parked)
    store.append_decision(job_id, decision_record(current.status, parked, reason))


def _advance_to_ready_to_render(job_id: str, store: JobStore, reason: str) -> ContentJob:
    """Open the render gate, at most once.

    Conditional on the *persisted* status, exactly like
    ``master_voice._advance_to_awaiting_assets``, so the idempotent
    short-circuit advances a job whose crash landed between the last asset
    write and the status write.
    """
    current = store.load(job_id).job
    if current.status is not JobStatus.AWAITING_ASSETS:
        return current
    ready = transition(current, JobStatus.READY_TO_RENDER, reason=reason)
    store.save(ready)
    store.append_decision(job_id, decision_record(current.status, ready, reason))
    return ready


def import_assets(
    job: ContentJob,
    store: JobStore,
    *,
    creator_profile: Optional[Mapping[str, Any]] = None,
    now: str = "",
) -> ContentJob:
    """Validate every manifest entry's file, record it, and open the render gate.

    Idempotent, and "this job already has some assets" is not "this job is
    done": a re-run imports whatever is still missing and appends no second
    record for an entry already imported. The store has no dedupe of its own,
    so that is a read-then-write against ``store.load(...).assets``.

    Returns the job as persisted. Raises :class:`AssetImportError` when a file
    is missing or fails §7 (the job is parked at ``MANUAL_ACTION_REQUIRED``
    first, so a human can fix the file and resume through
    ``state_machine.resume_target``), or
    :class:`~app.services.jobs.state_machine.UnauthorizedAssetError` when
    consent does not hold.
    """
    job_id = job.content_job_id
    record = store.load(job_id)
    timestamp = now or utc_now()
    moment = _as_datetime(timestamp, "now")
    profile = None if creator_profile is None else normalized_profile(
        creator_profile, now=moment
    )

    manifest = store.read_generation_manifest(job_id)
    if manifest is None:
        raise AssetImportError(
            "asset import needs the §6.1 generation manifest; run plan_scenes first"
        )

    scene_ids = {scene.scene_id for scene in record.scenes}
    named = [entry.scene_id for entry in manifest.entries]
    if len(set(named)) != len(named):
        # ``asset_id`` is derived from the scene id, so a repeated scene would
        # mint two records sharing one id — measured: the job reaches
        # READY_TO_RENDER with a duplicate, and every later run then fails the
        # checksum re-verification against the wrong record, permanently.
        raise AssetImportError("the generation manifest names a scene more than once")
    known: Dict[str, AssetRecord] = {asset.asset_id: asset for asset in record.assets}
    digests = {asset.sha256: asset.asset_id for asset in record.assets}

    pending: List[GenerationManifestEntry] = []
    for entry in manifest.entries:
        existing = known.get(asset_id_for(entry.scene_id))
        if existing is None:
            pending.append(entry)
            continue
        # Already imported. Prove the bytes are still the bytes that were
        # recorded rather than assuming a record implies a file — cheap, and it
        # is the difference between "finished" and "someone replaced it".
        try:
            path = _entry_path(store, job_id, entry)
            if not os.path.isfile(path):
                raise AssetImportError(
                    f"scene {entry.scene_id}: an asset is recorded but its file is gone"
                )
            if media_probe.file_sha256(path) != existing.sha256:
                raise AssetImportError(
                    f"scene {entry.scene_id}: the imported file no longer matches "
                    f"the checksum on its asset record"
                )
            # A profile cannot authorise an asset after the fact: the store is
            # append-only and the record's consent was written at import time.
            # Say so here rather than letting ``preflight`` report the symptom.
            if profile is not None and any(
                profile[section]["asset_ref"] == existing.asset_id
                and existing.consent_status != profile[section]["consent_status"]
                for section in ("voice", "avatar")
            ):
                raise UnauthorizedAssetError(
                    f"asset {existing.asset_id} was imported without this creator "
                    f"profile and recorded consent_status "
                    f"{existing.consent_status!r}; consent is written when the "
                    f"asset is imported, so the profile must be supplied then"
                )
        except (
            AssetImportError,
            UnauthorizedAssetError,
            media_probe.MediaProbeError,
        ) as error:
            _park(job_id, store, error)
            raise

    if pending and record.job.status is not JobStatus.AWAITING_ASSETS:
        raise AssetImportError(
            f"import_assets requires AWAITING_ASSETS, got {record.job.status.value}"
        )

    imported: List[AssetRecord] = []
    missing: List[str] = []
    try:
        for entry in pending:
            # Rule 1: the entry must name a scene of *this* job.
            if entry.scene_id not in scene_ids:
                raise AssetImportError(
                    f"manifest entry names scene {entry.scene_id}, which is not a "
                    f"scene of job {job_id}"
                )
            path = _entry_path(store, job_id, entry)
            # Rule 6: no usable file is not a validation failure, it is 缺件 —
            # the human has not produced this one yet, or the copy was cut off
            # partway and left nothing behind.
            if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                missing.append(entry.scene_id)
                continue
            facts = _validated_facts(path, entry)
            # Rule 4: one file, one record.
            owner = digests.get(facts.sha256)
            if owner is not None:
                raise AssetImportError(
                    f"scene {entry.scene_id}: these exact bytes are already "
                    f"recorded as {owner}"
                )
            consent = _unowned_consent()
            if profile is not None:
                for section in ("voice", "avatar"):
                    if profile[section]["asset_ref"] == asset_id_for(entry.scene_id):
                        consent = _profile_consent(profile, profile[section])
            asset = _asset_record(
                job_id=job_id,
                entry=entry,
                facts=facts,
                consent=consent,
                created_at=timestamp,
            )
            store.append_event(job_id, asset)
            digests[facts.sha256] = asset.asset_id
            imported.append(asset)

        if missing:
            raise AssetImportError(
                f"{len(missing)} of {len(manifest.entries)} manifest entries have "
                f"no usable file yet: {', '.join(sorted(missing))}",
                missing=len(missing),
            )
        preflight(store.load(job_id).assets, profile, now=moment)
    except (AssetImportError, UnauthorizedAssetError, media_probe.MediaProbeError) as error:
        _park(job_id, store, error)
        raise

    return _advance_to_ready_to_render(
        job_id,
        store,
        f"{len(manifest.entries)} manifest assets validated "
        f"({len(imported)} imported this run)",
    )
