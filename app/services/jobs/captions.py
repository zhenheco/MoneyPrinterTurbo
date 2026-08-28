"""Turn the Master Voice timeline into the job's subtitle track.

PLAN-001 issue #7. One entry point::

    asset = generate_captions(job, store)   # runs in AWAITING_ASSETS

Row 7's parenthetical says 「沿用 subtitle.py」. That is not followed, and the
reason is measured, not stylistic:

* ``subtitle.create()`` is faster-whisper **ASR over the audio**. It returns
  ``""`` when the dependency is missing, returns ``None`` on *both* a
  model-load failure and success, and on an empty transcript writes a one-byte
  file containing ``"\\n"`` while logging that it created the subtitle. It also
  re-transcribes speech we already have the script for, so its output drifts
  from the text the voice was synthesised from.
* ``subtitle.correct()`` fed an empty SRT **fabricates** one
  ``00:00:00,000 --> 00:00:00,000`` cue per script line, which then passes the
  legacy pipeline's only sanity check.
* ``voice.create_subtitle()`` needs the live ``SubMaker`` that
  :mod:`app.services.jobs.voice_adapter` deliberately discards, and speaks in
  100-nanosecond ticks — re-importing exactly the unit trap issue #6 removed.
* ``utils.text_to_srt`` takes float **seconds**, truncates (8123 ms renders as
  ``00:00:08,122``), appends eight trailing spaces to every block, and has no
  sign guard.

Row 7's own scope clause — 由 master voice timestamps 產 — is the mechanism
used here. The timeline is already on disk in integer milliseconds; this stage
is pure derivation with no provider call, so like issue #5 it has no budget
gate, writes no ``ProviderEvent`` and makes no state transition on success.
``AWAITING_ASSETS -> READY_TO_RENDER`` belongs to issue #8.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence
from uuid import uuid4

from app.models.content_job import AssetRecord, ContentJob, JobStatus, Scene
from app.services.jobs.state_machine import (
    classify_error,
    decision_record,
    transition,
    utc_now,
)
from app.services.jobs.store import JobStore

SUBTITLE_ASSET_TYPE = "subtitle"
SUBTITLE_EXTENSION = ".srt"
#: The frozen fixtures' value. ``AssetRecord.asset_type`` is an unvalidated
#: string, so this constant is the only place the convention lives.
SUBTITLE_MIME_TYPE = "application/x-subrip"

_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


class CaptionsError(RuntimeError):
    """The subtitle track could not be derived for this job."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class CaptionCue:
    """One subtitle cue, and the scene it belongs to."""

    srt_index: int
    caption_ref: str
    scene_id: str
    scene_index: int
    text: str
    start_ms: int
    end_ms: int


def caption_ref(scene_index: int) -> str:
    """The id ``RenderSceneEntry.caption_ref`` dereferences.

    Both frozen render manifests pin ``caption-001..caption-00N`` against the
    1-based ``scene_index``.
    """
    return f"caption-{scene_index:03d}"


def srt_timestamp(ms: int) -> str:
    """``HH:MM:SS,mmm`` from integer milliseconds.

    Integer arithmetic throughout. ``utils.time_convert_seconds_to_hmsm`` is
    not reused: it takes float seconds and truncates, so 8123 ms round-tripped
    through it renders as ``00:00:08,122``.
    """
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise CaptionsError(f"a cue time must be an int, got {ms!r}", retryable=False)
    if ms < 0:
        raise CaptionsError(f"a cue time cannot be negative: {ms}", retryable=False)
    hours, rest = divmod(ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, milliseconds = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _cue_body(text: str) -> str:
    """Cue text with every line stripped and blank lines removed.

    A blank line is what *separates* cues in SubRip, so one inside a cue's text
    silently truncates the block: the reader keeps what came before it and
    discards the rest, while ``captions.json`` — written by this same stage —
    still carries the whole narration. ``Scene.narration`` is unvalidated model
    output, and ``scene_planner._sentences`` strips only the outer whitespace,
    so an ordinary two-paragraph hook arrives here intact.

    ``splitlines`` covers ``\\n``, ``\\r``, ``\\r\\n`` and the vertical-tab and
    form-feed family in one call. The per-line strip also removes the trailing
    space a positional narration cut can leave behind.
    """
    body = "\n".join(line for line in (raw.strip() for raw in text.splitlines()) if line)
    if not body:
        raise CaptionsError("a cue has no text to render", retryable=False)
    return body


def render_srt(cues: Sequence[CaptionCue]) -> str:
    """The SubRip document for ``cues``, with no trailing whitespace.

    Ends with a blank line after the final cue. Several SubRip readers —
    moviepy's ``file_to_subtitles`` among them, which issue #9 feeds this file
    to — flush the cue they are accumulating only when they meet a blank line,
    and drop the last one without it.
    """
    if not cues:
        raise CaptionsError("a subtitle track needs at least one cue", retryable=False)
    blocks = [
        f"{cue.srt_index}\n"
        f"{srt_timestamp(cue.start_ms)} --> {srt_timestamp(cue.end_ms)}\n"
        f"{_cue_body(cue.text)}\n"
        for cue in cues
    ]
    return "\n".join(blocks) + "\n"


def _validated_timeline(timeline: Any) -> Dict[str, Any]:
    """``read_master_voice_timestamps`` only proves it is a JSON object."""
    if not isinstance(timeline, Mapping):
        raise CaptionsError(
            "the master voice timeline is missing; run generate_master_voice first",
            retryable=False,
        )
    total = timeline.get("total_duration_ms")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise CaptionsError(
            f"the timeline's total_duration_ms is not a positive int: {total!r}",
            retryable=False,
        )
    if not str(timeline.get("duration_source") or "").strip():
        raise CaptionsError("the timeline has no duration_source", retryable=False)
    segments = timeline.get("segments")
    if not isinstance(segments, list) or not segments:
        raise CaptionsError("the timeline has no segments", retryable=False)
    previous_end = 0
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise CaptionsError("a timeline segment is not an object", retryable=False)
        start, end = segment.get("start_ms"), segment.get("end_ms")
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CaptionsError(
                    f"a timeline segment time is not a non-negative int: {value!r}",
                    retryable=False,
                )
        if start > end:
            raise CaptionsError(
                f"a timeline segment ends before it starts: {start} > {end}",
                retryable=False,
            )
        if start < previous_end:
            raise CaptionsError("timeline segments are not in order", retryable=False)
        previous_end = end
    return dict(timeline)


def _ordered_scenes(scenes: Sequence[Scene]) -> List[Scene]:
    ordered = sorted(scenes, key=lambda scene: scene.scene_index)
    if not ordered:
        raise CaptionsError("a subtitle track needs planned scenes", retryable=False)
    for scene in ordered:
        if not scene.narration.strip():
            raise CaptionsError(
                f"scene {scene.scene_id} has no narration to caption",
                retryable=False,
            )
    return ordered


def _scene_edges(
    scenes: Sequence[Scene], timeline: Mapping[str, Any]
) -> List[int]:
    """Where each scene starts, in ms, snapped to real word boundaries.

    The timeline carries no scene id — ``master_voice.narration_text`` joins the
    scene narrations with no separator, so the only link back is position. Each
    scene boundary is located by cumulative narration-character share and then
    snapped to the ``start_ms`` of the first segment that reaches it, so every
    cue edge is a boundary the provider actually reported rather than an
    interpolation between them.

    # ponytail: character share is the ceiling here. It holds because the
    # segments are a segmentation of the same narration. If a provider ever
    # returns segment text that does not track the narration, upgrade this to
    # matching on text offsets rather than counting characters.
    """
    total_ms = int(timeline["total_duration_ms"])
    segments = timeline["segments"]
    last_segment_end = max(int(segment["end_ms"]) for segment in segments)
    # The take is allowed up to 25% drift before voice_adapter rejects it, and
    # the total is then the *measured* duration, so a segment may legitimately
    # end past it. The subtitle track may not.
    ceiling = min(last_segment_end, total_ms)

    if ceiling < len(scenes):
        raise CaptionsError(
            f"the voice is too short to caption {len(scenes)} scenes "
            f"({ceiling} ms)",
            retryable=False,
        )

    lengths = [len(scene.narration) for scene in scenes]
    total_chars = sum(lengths)
    if total_chars <= 0:
        raise CaptionsError("scenes carry no narration characters", retryable=False)

    # Proportional first, because it is always defined and always strictly
    # increasing once the ceiling covers one millisecond per scene.
    edges = [0]
    consumed = 0
    for length in lengths[:-1]:
        consumed += length
        edges.append(int(round(consumed / total_chars * ceiling)))
    edges.append(ceiling)

    # Then pull each interior edge onto a real segment start — but only when
    # that start is already close to where the proportional estimate put it.
    #
    # The cap is the point. On the Edge path the segments are word boundaries,
    # so the nearest one is milliseconds away and snapping genuinely improves
    # the edge. On every other path they are clause-level, and the nearest
    # clause start can be seconds away: taking it then replaces this scene's
    # share of the narration with that clause's, which adds no information and
    # only moves the edge. Fuzzed over 3000 realistic scripts, the uncapped
    # version displaced an edge by 10.25 s and left a 26-character scene on
    # screen for 36 ms. Half the smaller adjacent gap keeps it a nudge.
    starts = sorted({int(segment["start_ms"]) for segment in segments})
    for index in range(1, len(edges) - 1):
        nearest = min(starts, key=lambda start: (abs(start - edges[index]), start))
        slack = min(
            edges[index] - edges[index - 1], edges[index + 1] - edges[index]
        ) // 2
        if (
            edges[index - 1] < nearest < edges[index + 1]
            and abs(nearest - edges[index]) <= slack
        ):
            edges[index] = nearest

    for index in range(1, len(edges)):
        if edges[index] <= edges[index - 1]:
            edges[index] = edges[index - 1] + 1
    if edges[-1] > ceiling:
        # The ``ceiling >= len(scenes)`` guard is a feasibility bar, not a
        # proof: the fix-up above only nudges edges forward, so a lopsided
        # narration against a tiny ceiling can still walk the last edge past
        # it. Not reachable at realistic parameters, but reachable.
        raise CaptionsError(
            f"the voice is too short to caption {len(scenes)} scenes "
            f"({ceiling} ms)",
            retryable=False,
        )
    return edges


def scene_cues(
    *, scenes: Sequence[Scene], timeline: Mapping[str, Any]
) -> List[CaptionCue]:
    """One cue per scene, carrying that scene's narration verbatim.

    One cue per *timeline segment* is not an option: on the default Edge path
    those segments are word boundaries, which would produce one-word captions
    and make ``caption_ref`` impossible to keep 1:1 with a scene the way both
    frozen render manifests require.
    """
    validated = _validated_timeline(timeline)
    ordered = _ordered_scenes(scenes)
    edges = _scene_edges(ordered, validated)
    return [
        CaptionCue(
            srt_index=position + 1,
            caption_ref=caption_ref(scene.scene_index),
            scene_id=scene.scene_id,
            scene_index=scene.scene_index,
            text=scene.narration,
            start_ms=edges[position],
            end_ms=edges[position + 1],
        )
        for position, scene in enumerate(ordered)
    ]


def captions_document(
    *,
    content_job_id: str,
    master_voice_asset_id: str,
    subtitle_asset_id: str,
    timeline: Mapping[str, Any],
    cues: Sequence[CaptionCue],
) -> Dict[str, Any]:
    """The ``subtitles/captions.json`` contract.

    Invented here, like the timeline document: SPEC-001 §3.2 names the file and
    nothing defines its contents. A ``caption_ref`` in the render manifest
    dereferences to one entry in ``captions``, whose ``srt_index`` is that
    cue's 1-based position in ``captions.srt``.

    ``voice_duration_source`` is carried verbatim from the timeline so issue
    #9's QA can tell a decoded duration from one the provider merely claimed.
    """
    return {
        "content_job_id": content_job_id,
        "master_voice_asset_id": master_voice_asset_id,
        "subtitle_asset_id": subtitle_asset_id,
        "voice_total_duration_ms": int(timeline["total_duration_ms"]),
        "voice_duration_source": timeline["duration_source"],
        "captions": [
            {
                "caption_ref": cue.caption_ref,
                "scene_id": cue.scene_id,
                "scene_index": cue.scene_index,
                "srt_index": cue.srt_index,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "text": cue.text,
            }
            for cue in cues
        ],
    }


def _subtitle_assets(record) -> List[AssetRecord]:
    return [
        asset for asset in record.assets if asset.asset_type == SUBTITLE_ASSET_TYPE
    ]


def _master_voice_asset(record) -> AssetRecord:
    voices = [asset for asset in record.assets if asset.asset_type == "audio"]
    if not voices:
        raise CaptionsError(
            "captions need the Master Voice; run generate_master_voice first",
            retryable=False,
        )
    return voices[0]


def _park(job: ContentJob, store: JobStore, error: BaseException) -> None:
    """Move an unrecoverable job to ``MANUAL_ACTION_REQUIRED``.

    A retryable failure stays put: ``AWAITING_ASSETS`` is not a 可重試階段, so
    §5.2 gives it no ``RETRYABLE_FAILED`` edge to take in the first place. (It
    would no longer be a dead end if it did — RETRYABLE_FAILED now has return
    rows — but there is still no edge into it from here.) The caller retries in
    place. Same shape as ``master_voice._park``.
    """
    classification = classify_error(error)
    if classification.is_retryable:
        return
    current = store.load(job.content_job_id).job
    if current.status is not JobStatus.AWAITING_ASSETS:
        return
    reason = f"caption generation failed ({classification.value}): {type(error).__name__}"
    parked = transition(current, JobStatus.MANUAL_ACTION_REQUIRED, reason=reason)
    store.save(parked)
    store.append_decision(
        job.content_job_id, decision_record(current.status, parked, reason)
    )


def generate_captions(job: ContentJob, store: JobStore) -> AssetRecord:
    """Derive the subtitle track from the Master Voice timeline.

    Idempotent by completeness, like ``generate_master_voice``: a job that
    already has a subtitle AssetRecord *and* both files keeps them; one that
    has the record but lost a file is a crash, not a finished stage, and is
    refused rather than papered over with a second record.

    Makes no state transition on success. ``AWAITING_ASSETS`` is where issue #8
    picks the job up.
    """
    job_id = job.content_job_id
    record = store.load(job_id)

    existing = _subtitle_assets(record)
    if existing:
        if len(existing) > 1:
            raise CaptionsError(
                f"job carries {len(existing)} subtitle assets; one is expected",
                retryable=False,
            )
        asset = existing[0]
        expected_key = store.captions_relative_path(SUBTITLE_EXTENSION)
        if asset.storage_key != expected_key:
            raise CaptionsError(
                f"subtitle asset points at {asset.storage_key!r}, "
                f"not at {expected_key!r}",
                retryable=False,
            )
        # Through the store's guarded resolver rather than from the persisted
        # string: a storage_key is data, and data is not a path to open.
        srt_path = store.captions_srt_path(job_id)
        document = store.read_captions_document(job_id)
        if not srt_path.is_file() or srt_path.stat().st_size <= 0:
            raise CaptionsError(
                "subtitle asset is recorded but captions.srt is missing or empty",
                retryable=False,
            )
        if document is None:
            raise CaptionsError(
                "subtitle asset is recorded but captions.json is missing",
                retryable=False,
            )
        if hashlib.sha256(srt_path.read_bytes()).hexdigest() != asset.sha256:
            raise CaptionsError(
                "captions.srt no longer matches the checksum on its asset record",
                retryable=False,
            )
        return asset

    if record.job.status is not JobStatus.AWAITING_ASSETS:
        raise CaptionsError(
            f"generate_captions requires AWAITING_ASSETS, got {record.job.status.value}",
            retryable=False,
        )

    try:
        voice_asset = _master_voice_asset(record)
        timeline = _validated_timeline(store.read_master_voice_timestamps(job_id))
        cues = scene_cues(scenes=record.scenes, timeline=timeline)
        srt_path = store.write_captions_srt(job_id, render_srt(cues))
    except CaptionsError as error:
        _park(record.job, store, error)
        raise

    srt_bytes = srt_path.read_bytes()
    asset = AssetRecord(
        asset_id=f"asset-{uuid4().hex}",
        content_job_id=job_id,
        scene_id=None,
        asset_type=SUBTITLE_ASSET_TYPE,
        storage_key=store.captions_relative_path(SUBTITLE_EXTENSION),
        original_filename=srt_path.name,
        mime_type=SUBTITLE_MIME_TYPE,
        bytes=len(srt_bytes),
        width=0,
        height=0,
        duration_ms=int(timeline["total_duration_ms"]),
        sha256=hashlib.sha256(srt_bytes).hexdigest(),
        source_mode="post_production",
        provider="local_render",
        model="",
        # Derived from bytes this job already owns: nobody's voice, image or
        # likeness is referenced, so there is no consent to record and no
        # human review to wait for. Same reasoning as the synthesised Master
        # Voice in issue #6.
        license_or_consent="owner_generated",
        consent_status="not_applicable",
        usage_scope="",
        consent_source="",
        consent_expires_at="",
        consent_revoked_at=None,
        manual_review_status="not_required",
        created_at=utc_now(),
    )
    store.write_captions_document(
        job_id,
        captions_document(
            content_job_id=job_id,
            master_voice_asset_id=voice_asset.asset_id,
            subtitle_asset_id=asset.asset_id,
            timeline=timeline,
            cues=cues,
        ),
    )
    store.append_event(job_id, asset)
    return asset
