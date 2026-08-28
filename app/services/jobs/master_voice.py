"""Synthesise the one Master Voice for a job and lay out its timeline.

PLAN-001 issue #6. Two entry points, in order::

    voicing = start_voice_generating(job, store)   # SCENE_PLANNING -> VOICE_GENERATING
    asset = generate_master_voice(voicing, store)  # -> AWAITING_ASSETS

``start_voice_generating`` exists for the same reason
``scene_planner.start_scene_planning`` did: issue #5 leaves the job sitting in
``SCENE_PLANNING`` once the scenes and the generation manifest are on disk, and
§5.2 allows exactly one step from there.

SPEC-001 §6.3 and PRD-001 FR-004A both say a job gets **one** Master Voice.
That is enforced here by a document-level short circuit, not by the store:
``assets.jsonl`` is append-only and deduplicates nothing.

Unlike issue #5 this stage spends money, so it is gated: ``check_budget``
before the call, one ``ProviderEvent`` and one ledger row after it, and the
provider is reached through :mod:`app.services.jobs.voice_adapter` rather than
directly — see that module for the measured reasons.

Both functions re-read the job from the store rather than trusting the
argument, matching ``budget.check_budget``: a caller holding a stale
:class:`~app.models.content_job.ContentJob` must not be able to re-run a stage
or spend past the limit.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Sequence
from uuid import uuid4

from app.config import config
from app.models.content_job import AssetRecord, ContentJob, JobStatus, ProviderEvent, Scene
from app.services.jobs import voice_adapter
from app.services.jobs.budget import (
    build_idempotency_key,
    check_budget,
    record_usage,
    summarize,
)
from app.services.jobs.state_machine import (
    classify_error,
    decision_record,
    transition,
    utc_now,
)
from app.services.jobs.store import JobStore

#: One ``voice_adapter.synthesize`` call authorises up to **three** real
#: provider requests, because every provider but Gemini retries three times
#: inside ``voice.py`` and there is no way to ask it how many it made. This
#: ceiling is therefore sized for the whole invocation, not for one request.
#: Conservative placeholder, like ``pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD``:
#: replace it with real per-provider pricing, never with 0 or an unknown
#: sentinel.
VOICE_TTS_CALL_COST_CEILING_USD = 0.05

#: Total synthesis attempts across all calls for one job, derived from the
#: ledger the same way ``pipeline`` derives the script attempts. A resumed job
#: continues the count; it does not restart it.
MAX_VOICE_GENERATION_ATTEMPTS = 3

#: The repository-wide default (``app/models/schema.py``). SPEC-001 §3.1's
#: request contract has no voice field and ``ContentJob`` is ``extra="forbid"``,
#: so the job cannot carry one yet — see the handoff for that gap.
DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"

#: Language prefix -> default voice. Without this a ``zh-TW`` job is read aloud
#: by a mainland voice. Only the languages the V0 fixtures actually use are
#: listed; anything else falls back to ``DEFAULT_VOICE_NAME``.
_LANGUAGE_VOICES = {
    "zh-tw": "zh-TW-HsiaoChenNeural-Female",
    "zh-hk": "zh-HK-HiuMaanNeural-Female",
    "zh": DEFAULT_VOICE_NAME,
    "en": "en-US-JennyNeural-Female",
    "ja": "ja-JP-NanamiNeural-Female",
}

_ESTIMATED_COST_SOURCE = (
    "conservative whole-invocation TTS ceiling; app.services.voice exposes no "
    "provider pricing and retries up to three times internally per call"
)
_FREE_PROVIDER_COST_SOURCE = (
    "edge-tts and the silent no-voice path are unmetered: this is a known zero, "
    "not an unknown recorded as zero"
)


class MasterVoiceError(RuntimeError):
    """The Master Voice could not be produced for this job."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _ordered_scenes(scenes: Sequence[Scene]) -> List[Scene]:
    return sorted(scenes, key=lambda scene: scene.scene_index)


def narration_text(scenes: Sequence[Scene]) -> str:
    """The whole script as the voice will speak it, in scene order.

    Joined raw. Do not run this through ``budget.redact`` — measured, it
    rewrites ordinary prose (the handoff records ``token economy ...`` becoming
    ``<redacted> ...``), and this is the text a human will hear. Do not strip a
    trailing ``，`` either: scene splitting can cut mid-phrase and a test pins
    that the concatenated narration reproduces the script exactly.
    """
    return "".join(scene.narration for scene in _ordered_scenes(scenes))


def resolve_voice_name(job: ContentJob) -> str:
    """The configured voice, else one matching the job's language."""
    configured = str(config.app.get("voice_name", "") or "").strip()
    if configured:
        return configured
    language = str(job.language or "").strip().lower()
    for candidate in (language, language.split("-")[0]):
        if candidate in _LANGUAGE_VOICES:
            return _LANGUAGE_VOICES[candidate]
    return DEFAULT_VOICE_NAME


def _voice_generation_attempts(record) -> set:
    """Attempts already recorded for this job's voice synthesis."""
    attempts = set()
    keys = [event.idempotency_key for event in record.provider_events] + [
        entry.idempotency_key for entry in record.usage_ledger
    ]
    for key in keys:
        parts = key.split(":")
        if len(parts) != 4:
            continue
        content_job_id, slot, operation, attempt_part = parts
        if (
            content_job_id != record.job.content_job_id
            or slot != "voice"
            or operation != "generate"
            or not attempt_part.startswith("attempt-")
        ):
            continue
        attempt_text = attempt_part.removeprefix("attempt-")
        if attempt_text.isdecimal() and int(attempt_text) >= 1:
            attempts.add(int(attempt_text))
    return attempts


def timeline_document(
    *,
    content_job_id: str,
    asset_id: str,
    take: voice_adapter.VoiceTake,
) -> Dict[str, Any]:
    """The ``audio/master-voice-timestamps.json`` contract.

    Invented here: SPEC-001 §3.2 names the file but nothing in §6.3 or FR-004
    defines its contents, and no model or example exists in the repository.
    Kept to what issue #7 (字幕生成) needs and no more — integer milliseconds
    throughout, so no consumer has to know that half of ``voice.py`` speaks in
    100-nanosecond ticks.
    """
    return {
        "content_job_id": content_job_id,
        "master_voice_asset_id": asset_id,
        "total_duration_ms": take.duration_ms,
        "duration_source": take.duration_source,
        "segments": [
            {
                "index": segment.index,
                "text": segment.text,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
            }
            for segment in take.segments
        ],
    }


def _existing_voice_assets(record) -> List[AssetRecord]:
    return [asset for asset in record.assets if asset.asset_type == "audio"]


def _park(job: ContentJob, store: JobStore, error: BaseException) -> None:
    """Move an unrecoverable job to ``MANUAL_ACTION_REQUIRED``.

    A retryable failure deliberately stays put: §5.2 gives ``RETRYABLE_FAILED``
    no edge back into any generating stage, so transitioning there would make a
    recoverable failure permanent. Same product decision as
    ``pipeline._persist_failed_status``.
    """
    classification = classify_error(error)
    if classification.is_retryable:
        return
    current = store.load(job.content_job_id).job
    if current.status is not JobStatus.VOICE_GENERATING:
        return
    reason = (
        f"master voice failed ({classification.value}): {type(error).__name__}"
    )
    parked = transition(current, JobStatus.MANUAL_ACTION_REQUIRED, reason=reason)
    store.save(parked)
    store.append_decision(
        job.content_job_id, decision_record(current.status, parked, reason)
    )


def _advance_to_awaiting_assets(job_id: str, store: JobStore) -> None:
    """Hand a finished Master Voice over to asset import, at most once.

    Conditional on the persisted status so it is safe to call from the
    idempotency short circuit as well as from the happy path: a job already in
    ``AWAITING_ASSETS`` is left alone, and one still in ``VOICE_GENERATING``
    because a crash landed between the asset write and the status write is
    finished rather than abandoned.
    """
    current = store.load(job_id).job
    if current.status is not JobStatus.VOICE_GENERATING:
        return
    reason = "master voice and timeline created"
    awaiting = transition(current, JobStatus.AWAITING_ASSETS, reason=reason)
    store.save(awaiting)
    store.append_decision(job_id, decision_record(current.status, awaiting, reason))


def start_voice_generating(job: ContentJob, store: JobStore) -> ContentJob:
    """Move a planned job from ``SCENE_PLANNING`` to ``VOICE_GENERATING``."""
    record = store.load(job.content_job_id)
    persisted = record.job
    if not record.scenes:
        raise MasterVoiceError(
            "voice generation needs planned scenes; run plan_scenes first",
            retryable=False,
        )
    total_ms = sum(scene.duration_target_ms for scene in record.scenes)
    if total_ms <= 0:
        raise MasterVoiceError(
            "planned scenes carry no duration to voice", retryable=False
        )
    if persisted.status is not JobStatus.SCENE_PLANNING:
        raise MasterVoiceError(
            f"voice generation starts from SCENE_PLANNING, got {persisted.status.value}",
            retryable=False,
        )
    reason = f"{len(record.scenes)} scenes, duration total {total_ms} ms"
    voicing = transition(persisted, JobStatus.VOICE_GENERATING, reason=reason)
    store.save(voicing)
    store.append_decision(
        job.content_job_id, decision_record(persisted.status, voicing, reason)
    )
    return voicing


def _audit(
    *,
    job: ContentJob,
    store: JobStore,
    provider_id: str,
    model: str,
    attempt: int,
    started_at: str,
    narration: str,
    take: voice_adapter.VoiceTake = None,
    error: BaseException = None,
) -> None:
    """Write exactly one ProviderEvent and one ledger row for one call."""
    free = voice_adapter.is_free(provider_id)
    estimated = 0.0 if free else VOICE_TTS_CALL_COST_CEILING_USD
    classification = classify_error(error) if error is not None else None
    event = ProviderEvent(
        provider_event_id=f"provider-event-{uuid4().hex}",
        content_job_id=job.content_job_id,
        scene_id=None,
        provider=provider_id,
        model=model,
        request_id="",
        external_job_id="",
        idempotency_key=build_idempotency_key(
            job.content_job_id, "voice", "generate", attempt
        ),
        attempt_count=attempt,
        estimated_cost_usd=estimated,
        # A known-free provider records a real 0.0, the way postiz does for a
        # draft. Everything else records "unknown": §10 forbids presenting an
        # unknown cost as zero, and voice.py reports no cost at all.
        actual_cost_usd=0.0 if free else "unknown",
        request_summary=summarize(
            "master voice request",
            {"voice": model, "characters": len(narration)},
        ),
        response_summary=summarize(
            "master voice response",
            {
                "duration_ms": take.duration_ms if take else 0,
                "bytes": take.bytes if take else 0,
                "segments": len(take.segments) if take else 0,
            },
        ),
        error_class=type(error).__name__ if error is not None else None,
        retryable=classification.is_retryable if classification else False,
        created_at=started_at,
        completed_at=utc_now(),
    )
    record_usage(
        store,
        job,
        event,
        estimated_cost_source=(
            _FREE_PROVIDER_COST_SOURCE if free else _ESTIMATED_COST_SOURCE
        ),
    )


def generate_master_voice(job: ContentJob, store: JobStore) -> AssetRecord:
    """Synthesise the job's single Master Voice and hand it to asset import.

    Idempotent: a job that already has a complete Master Voice keeps it and
    makes no provider call. "Complete" means the AssetRecord, the audio bytes
    and the timeline document are all present — an artifact set missing one of
    the three is a crash, not a finished stage, and re-running must not paper
    over it by appending a second voice.
    """
    job_id = job.content_job_id
    record = store.load(job_id)
    existing = _existing_voice_assets(record)
    if existing:
        if len(existing) > 1:
            raise MasterVoiceError(
                f"job carries {len(existing)} voice assets; SPEC-001 6.3 allows one",
                retryable=False,
            )
        asset = existing[0]
        audio_path = store.master_voice_path(job_id, os.path.splitext(asset.storage_key)[1])
        timeline = store.read_master_voice_timestamps(job_id)
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise MasterVoiceError(
                "master voice asset is recorded but its audio is missing or empty",
                retryable=False,
            )
        if timeline is None:
            raise MasterVoiceError(
                "master voice asset is recorded but its timeline is missing",
                retryable=False,
            )
        # The artifacts are complete, so finish the handover if the crash
        # landed between appending the asset and saving the new status —
        # otherwise this short circuit would return happily forever while the
        # job sat wedged in VOICE_GENERATING, which start_voice_generating
        # refuses to re-enter.
        _advance_to_awaiting_assets(job_id, store)
        return asset

    if record.job.status is not JobStatus.VOICE_GENERATING:
        raise MasterVoiceError(
            f"generate_master_voice requires VOICE_GENERATING, "
            f"got {record.job.status.value}",
            retryable=False,
        )
    if not record.scenes:
        raise MasterVoiceError("voice generation needs planned scenes", retryable=False)

    narration = narration_text(record.scenes)
    if not narration.strip():
        error = MasterVoiceError(
            "planned scenes carry no narration to speak", retryable=False
        )
        _park(record.job, store, error)
        raise error

    used = _voice_generation_attempts(record)
    attempt = max(used, default=0) + 1
    if attempt > MAX_VOICE_GENERATION_ATTEMPTS:
        error = MasterVoiceError(
            f"master voice attempt limit of {MAX_VOICE_GENERATION_ATTEMPTS} reached",
            retryable=False,
        )
        _park(record.job, store, error)
        raise error

    voice_name = resolve_voice_name(record.job)
    provider_id, model = voice_adapter.resolve_identity(voice_name)
    estimated = (
        0.0 if voice_adapter.is_free(provider_id) else VOICE_TTS_CALL_COST_CEILING_USD
    )
    # The gate runs against what is on disk, and it runs before the provider is
    # reached. A refusal persists BUDGET_EXCEEDED and raises.
    current_job = check_budget(record.job, estimated, store=store)

    audio_path = store.master_voice_path(job_id, voice_adapter.AUDIO_EXTENSION)
    started_at = utc_now()
    try:
        take = voice_adapter.synthesize(
            text=narration,
            voice_name=voice_name,
            voice_file=str(audio_path),
        )
    except voice_adapter.VoiceTransportError as error:
        failure = MasterVoiceError(str(error), retryable=error.retryable)
        _audit(
            job=current_job,
            store=store,
            provider_id=provider_id,
            model=model,
            attempt=attempt,
            started_at=started_at,
            narration=narration,
            error=failure,
        )
        # Leave nothing half-written behind: the next attempt must not mistake
        # a truncated file for a finished one.
        if audio_path.exists():
            audio_path.unlink()
        _park(current_job, store, failure)
        raise failure from error

    _audit(
        job=current_job,
        store=store,
        provider_id=provider_id,
        model=model,
        attempt=attempt,
        started_at=started_at,
        narration=narration,
        take=take,
    )

    asset = AssetRecord(
        asset_id=f"asset-{uuid4().hex}",
        content_job_id=job_id,
        scene_id=None,
        asset_type="audio",
        storage_key=store.master_voice_relative_path(voice_adapter.AUDIO_EXTENSION),
        original_filename=audio_path.name,
        mime_type=voice_adapter.AUDIO_MIME_TYPE,
        bytes=take.bytes,
        width=0,
        height=0,
        duration_ms=take.duration_ms,
        sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
        source_mode="improved_tts",
        provider=provider_id,
        model=model,
        # A synthesised voice has no creator to consent: SPEC-001 6.3's consent
        # rules govern a *reference* to a real person's voice, and this path
        # never clones or imitates one. FR-005's preflight is likewise scoped to
        # 真人 voice/avatar. Issue #8 owns making that scoping explicit.
        license_or_consent="synthetic_tts_no_creator_reference",
        consent_status="not_applicable",
        usage_scope="",
        consent_source="",
        consent_expires_at="",
        consent_revoked_at=None,
        manual_review_status="not_required",
        created_at=utc_now(),
    )
    store.write_master_voice_timestamps(
        job_id,
        timeline_document(content_job_id=job_id, asset_id=asset.asset_id, take=take),
    )
    store.append_event(job_id, asset)
    _advance_to_awaiting_assets(job_id, store)
    return asset
