"""The render stage: manifest, render, technical QA, ``TECHNICAL_QA``.

PLAN-001 issue #9, second half. Two entry points::

    job = start_rendering(job, store)   # READY_TO_RENDER -> RENDERING, budget gate
    job = render_job(job, store)        # render + QA, -> TECHNICAL_QA on a pass

``render_job`` calls ``start_rendering`` itself when the job is still at
``READY_TO_RENDER``, so a caller has one entry point and the gate cannot be
skipped by forgetting to call it.

Technical QA runs *inside* ``RENDERING``, and that is the least obvious decision
in this slice
-------------------------------------------------------------------------------

The obvious arrangement — advance to ``TECHNICAL_QA`` when the file is written,
then judge it there — **permanently strands every job that fails**. Measured
2026-08-29 against a real store:

* ``TRANSITIONS[TECHNICAL_QA]`` is ``{CANCELLED, CONTENT_QA,
  MANUAL_ACTION_REQUIRED}``. ``TECHNICAL_QA`` is in none of
  ``RETRYABLE_STAGES``, ``RESUMABLE_STAGES`` or ``MANUAL_RETURN_STAGES``.
* ``TECHNICAL_QA -> RETRYABLE_FAILED`` and ``TECHNICAL_QA -> RENDERING`` both
  raise ``IllegalTransitionError``.
* ``TECHNICAL_QA -> MANUAL_ACTION_REQUIRED`` is legal, and then
  ``resume_target`` raises ``ResumeError``, because ``TECHNICAL_QA`` is not a
  return target. The job is stuck with no legal move except ``CANCELLED``.

So the file is judged while the job is still ``RENDERING``, and the state only
moves on a pass::

    READY_TO_RENDER --budget gate--> RENDERING --render--> [QA in place]
                                                    pass --> TECHNICAL_QA
                                                    fail --> RETRYABLE_FAILED

``RENDERING -> RETRYABLE_FAILED`` is legal and ``resume_target`` answers
``RENDERING``, so a QA failure converges instead of stranding. For the same
reason a *non-retryable* render failure also parks at ``RETRYABLE_FAILED``
rather than ``MANUAL_ACTION_REQUIRED``: ``RENDERING`` is not in
``MANUAL_RETURN_STAGES``, so that edge is a dead end too, and the retry-limit
judgement (§5.3) belongs to the runner, which can still send an exhausted job to
``FAILED`` from ``RETRYABLE_FAILED``.

The budget gate is wired here, at ``READY_TO_RENDER -> RENDERING``
-----------------------------------------------------------------

SPEC-001 §5.2's row for that edge reads 「Render Manifest 通過**且預算閘門通過**」,
and ``READY_TO_RENDER`` is in ``GENERATING_STAGES`` for exactly this reason —
it is the one state in that set that generates nothing and is there only so the
gate has a ``BUDGET_EXCEEDED`` exit. Rendering is local and free, so the
estimate is ``0.0``: the gate is a total-spend assertion before committing, not
a charge. (``asset_import`` declined the gate, correctly — its §5.2 row has no
預算閘門 clause and ``AWAITING_ASSETS`` has no ``BUDGET_EXCEEDED`` edge.)

One consequence is deliberate and worth stating loudly: ``check_budget``
refuses whenever ``actual_cost_usd`` is ``"unknown"``, **regardless of the
estimate, including 0.0** — §10 forbids treating an unknown spend as zero. Such
a job parks at ``BUDGET_EXCEEDED`` without rendering. That is the rule working,
and recovery is the two hops PR #13 built: ``BUDGET_EXCEEDED ->
MANUAL_ACTION_REQUIRED -> READY_TO_RENDER`` (which *is* in
``MANUAL_RETURN_STAGES``, so it terminates).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from app.models.content_job import AssetRecord, ContentJob, JobStatus, RenderManifest
from app.services.jobs import media_probe, render_adapter, render_manifest
from app.services.jobs.budget import check_budget
from app.services.jobs.render_adapter import RenderError, StreamFacts
from app.services.jobs.render_manifest import RenderManifestError
from app.services.jobs.state_machine import (
    classify_error,
    decision_record,
    transition,
    utc_now,
)
from app.services.jobs.store import JobRecord, JobStore

#: Rendering is local: no provider call, nothing to charge. The gate still runs;
#: see the module docstring.
RENDER_ESTIMATED_COST_USD = 0.0

RENDER_EXTENSION = ".mp4"
RENDER_MIME_TYPE = "video/mp4"
RENDER_ASSET_TYPE = "video"
#: Derived, not random, for the same reason ``asset_import.asset_id_for`` is: a
#: re-run has to be able to tell "this job is already rendered" from "this run
#: produced a second record", against a store with no dedupe of its own.
RENDER_ASSET_ID = "asset-render-final"

#: How far the decoded duration may fall short of the manifest timeline. The
#: concat step trims to the Master Voice's duration and the encoder rounds to a
#: frame boundary, so an exact equality would fail on arithmetic rather than on
#: content. One second is well under the shortest usable cut
#: (``media_probe.MIN_VIDEO_DURATION_MS`` is 500 ms).
DURATION_TOLERANCE_MS = 1000

#: Upper bound on the failure text quoted into a decision reason. Wide enough
#: to carry a full technical-QA failure list — decisions.jsonl is where
#: SPEC-001:642's readable QA report lives — narrow enough to keep an ffmpeg
#: banner from turning the audit log into a transcript.
_MAX_REASON_DETAIL_CHARS = 400


@dataclass(frozen=True)
class TechnicalQaResult:
    """What the rendered file was measured to be, and what QA made of it."""

    facts: StreamFacts
    failures: List[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def start_rendering(job: ContentJob, store: JobStore, *, now: str = "") -> ContentJob:
    """Run the §10 gate and open ``RENDERING``. Idempotent.

    Raises ``BudgetExceededError`` (with the job parked at ``BUDGET_EXCEEDED``)
    when the gate refuses.
    """
    job_id = job.content_job_id
    current = store.load(job_id).job
    if current.status is JobStatus.RENDERING:
        return current
    if current.status is not JobStatus.READY_TO_RENDER:
        raise RenderManifestError(
            f"start_rendering requires READY_TO_RENDER, got {current.status.value}"
        )
    # Judged against what is on disk, and refusing raises rather than returning
    # a verdict a caller could ignore.
    check_budget(current, RENDER_ESTIMATED_COST_USD, store=store, now=now)
    reason = "render manifest and budget gate passed"
    rendering = transition(current, JobStatus.RENDERING, reason=reason, now=now)
    store.save(rendering)
    store.append_decision(job_id, decision_record(current.status, rendering, reason))
    return rendering


def technical_qa(
    path,
    manifest: RenderManifest,
    *,
    subtitle_text: Optional[str] = None,
) -> TechnicalQaResult:
    """PRD-001 FR-008's technical half, read off the encoded file.

    Every check is against what ffmpeg found in the bytes, never against what
    the render was *asked* for: ``video._runtime_disabled_video_codecs`` is a
    mutable module global, so the second render in a process can silently use a
    different encoder than the first and the caller is never told which one ran.

    FR-008 asks for 影片可解碼、ffprobe metadata、1080×1920、音訊、字幕範圍 and
    Asset Record. The first five are here; the Asset Record is written by
    :func:`render_job` from these same measured facts. "ffprobe metadata" is
    satisfied through ffmpeg — see :mod:`app.services.jobs.render_adapter` for
    the measurement behind that substitution.
    """
    facts = render_adapter.inspect(path)  # 可解碼: raises if it does not decode
    failures: List[str] = []

    canvas = manifest.canvas
    if (facts.width, facts.height) != (canvas.width, canvas.height):
        failures.append(
            f"the render is {facts.width}x{facts.height}, not "
            f"{canvas.width}x{canvas.height}"
        )
    if facts.video_codec != manifest.output.video_codec:
        failures.append(
            f"the video codec is {facts.video_codec!r}, not "
            f"{manifest.output.video_codec!r}"
        )
    if facts.pixel_format != canvas.pixel_format:
        failures.append(
            f"the pixel format is {facts.pixel_format!r}, not {canvas.pixel_format!r}"
        )
    if facts.fps is None or abs(facts.fps - canvas.fps) > 0.5:
        failures.append(f"the frame rate is {facts.fps!r}, not {canvas.fps}")

    if facts.audio_codec is None:
        failures.append("the render carries no audio stream at all")
    elif facts.audio_codec != manifest.audio.codec:
        failures.append(
            f"the audio codec is {facts.audio_codec!r}, not {manifest.audio.codec!r}"
        )
    elif (
        facts.audio_sample_rate is not None
        and facts.audio_sample_rate != manifest.audio.sample_rate
    ):
        failures.append(
            f"the audio sample rate is {facts.audio_sample_rate}, not "
            f"{manifest.audio.sample_rate}"
        )

    timeline_end = render_manifest.timeline_end_ms(manifest)
    if facts.duration_ms is None:
        failures.append("the render reports no duration")
    elif facts.duration_ms + DURATION_TOLERANCE_MS < timeline_end:
        failures.append(
            f"the render is {facts.duration_ms} ms long but the manifest timeline "
            f"runs to {timeline_end} ms"
        )
    elif facts.duration_ms > timeline_end + DURATION_TOLERANCE_MS:
        # The check is symmetric on purpose. ``combine_videos`` loops clips to
        # the Master Voice's length, so a voice longer than the caption timeline
        # yields seconds of content that no scene entry and no subtitle cue
        # covers. Measured 2026-08-30: a 3000 ms manifest with an 8 s voice
        # shipped an 8000 ms render straight through QA.
        failures.append(
            f"the render is {facts.duration_ms} ms long but the manifest timeline "
            f"ends at {timeline_end} ms"
        )

    # SPEC-001:623 「Subtitle Timing 不超出 Master Voice 與影片長度」.
    if subtitle_text is not None:
        last_cue = render_adapter.subtitle_end_ms(subtitle_text)
        if last_cue is None:
            failures.append("the subtitle track holds no cue")
        elif facts.duration_ms is not None and (
            last_cue > facts.duration_ms + DURATION_TOLERANCE_MS
        ):
            failures.append(
                f"the last subtitle cue ends at {last_cue} ms, past the "
                f"{facts.duration_ms} ms render"
            )
    return TechnicalQaResult(facts=facts, failures=failures)


def technical_qa_report(
    job_id: str,
    manifest: RenderManifest,
    result: TechnicalQaResult,
    checked_at: str,
) -> dict:
    """SPEC-001 §12:644's 「可讀的 technical QA report」, as a JSON document.

    Says what was checked *and* what was found, not just pass/fail: every field
    :class:`~app.services.jobs.render_adapter.StreamFacts` measured sits next to
    the manifest value it was compared against, so a reader can tell a failure
    apart from a wrong expectation without re-running anything.

    ``pixel_format``, ``fps`` and ``audio_sample_rate`` are here for exactly
    that reason — before this report they were measured and thrown away.

    **The numbers come from ffmpeg, not ffprobe.** §12:644 names ffprobe;
    :mod:`app.services.jobs.render_adapter` explains why this repository has
    none, and why a full decode is the stricter check. The document says so in
    ``measured_with`` so the report cannot imply a tool that never ran.
    """
    facts = result.facts
    return {
        "content_job_id": job_id,
        "checked_at": checked_at,
        "passed": result.passed,
        "failures": list(result.failures),
        "measured_with": "ffmpeg",
        "measured_with_note": (
            "SPEC-001 §12:644 says ffprobe; this host has none (see "
            "app/services/jobs/render_adapter.py). These facts were read from a "
            "full ffmpeg decode of the rendered file, which is stricter than a "
            "container-header read."
        ),
        "measured": {
            "duration_ms": facts.duration_ms,
            "width": facts.width,
            "height": facts.height,
            "video_codec": facts.video_codec,
            "pixel_format": facts.pixel_format,
            "fps": facts.fps,
            "audio_codec": facts.audio_codec,
            "audio_sample_rate": facts.audio_sample_rate,
        },
        "expected": {
            "duration_ms": render_manifest.timeline_end_ms(manifest),
            "duration_tolerance_ms": DURATION_TOLERANCE_MS,
            "width": manifest.canvas.width,
            "height": manifest.canvas.height,
            "video_codec": manifest.output.video_codec,
            "pixel_format": manifest.canvas.pixel_format,
            "fps": manifest.canvas.fps,
            "audio_codec": manifest.audio.codec,
            "audio_sample_rate": manifest.audio.sample_rate,
            "container": manifest.output.container,
        },
    }


def _write_qa_report(
    store: JobStore,
    job_id: str,
    manifest: RenderManifest,
    result: TechnicalQaResult,
    checked_at: str,
) -> TechnicalQaResult:
    """Persist the report and hand the result straight back, pass or fail.

    A failing render is exactly when the report is worth having, so it is
    written before the caller decides what to do with ``result``.
    """
    store.write_technical_qa(
        job_id, technical_qa_report(job_id, manifest, result, checked_at)
    )
    return result


def _render_asset(
    job_id: str,
    store: JobStore,
    path,
    facts: StreamFacts,
    created_at: str,
) -> AssetRecord:
    """§4.5 for the rendered file, measured from the file that was just written."""
    return AssetRecord(
        asset_id=RENDER_ASSET_ID,
        content_job_id=job_id,
        scene_id=None,
        asset_type=RENDER_ASSET_TYPE,
        storage_key=store.render_output_relative_path(RENDER_EXTENSION),
        original_filename=os.path.basename(str(path)),
        mime_type=RENDER_MIME_TYPE,
        bytes=os.path.getsize(path),
        width=facts.width,
        height=facts.height,
        duration_ms=facts.duration_ms,
        sha256=media_probe.file_sha256(path),
        source_mode="local_render",
        provider="local_render",
        model="",
        # Assembled from bytes this job already owns; no new person's voice,
        # image or likeness enters here. Same reasoning as the derived subtitle
        # track in issue #7.
        license_or_consent="owner_generated",
        consent_status="not_applicable",
        usage_scope="",
        consent_source="",
        consent_expires_at="",
        consent_revoked_at=None,
        manual_review_status="not_required",
        created_at=created_at,
    )


def _park_retryable(job_id: str, store: JobStore, error: BaseException) -> None:
    """Send a failed render back to ``RETRYABLE_FAILED``, from ``RENDERING`` only.

    Both retryable and non-retryable failures land here on purpose; the module
    docstring says why ``MANUAL_ACTION_REQUIRED`` would be a dead end. The
    classification is still recorded in the reason, so a runner applying the
    §5.3 retry limit can tell the two apart without re-raising anything.
    """
    current = store.load(job_id).job
    if current.status is not JobStatus.RENDERING:
        return
    # ffmpeg/moviepy messages carry the absolute path of the job tree, which on
    # a developer or operator machine sits under $HOME. The audit log gets the
    # store-relative form, bounded, so decisions.jsonl stays a summary.
    detail = str(error).replace(str(store.root), "<store>")
    if len(detail) > _MAX_REASON_DETAIL_CHARS:
        detail = detail[:_MAX_REASON_DETAIL_CHARS] + "..."
    reason = (
        f"render failed ({classify_error(error).value}): "
        f"{type(error).__name__}: {detail}"
    )
    parked = transition(current, JobStatus.RETRYABLE_FAILED, reason=reason)
    store.save(parked)
    store.append_decision(job_id, decision_record(current.status, parked, reason))


def _advance_to_technical_qa(job_id: str, store: JobStore, reason: str) -> ContentJob:
    """Move on, at most once, and only from the *persisted* ``RENDERING``."""
    current = store.load(job_id).job
    if current.status is not JobStatus.RENDERING:
        return current
    passed = transition(current, JobStatus.TECHNICAL_QA, reason=reason)
    store.save(passed)
    store.append_decision(job_id, decision_record(current.status, passed, reason))
    return passed


def _persist_manifest(store: JobStore, record: JobRecord, manifest: RenderManifest) -> None:
    """Write ``render_manifest.json`` through the whole-document-set path.

    ``JobStore`` has no single-manifest writer, so this goes through
    :meth:`JobStore.replace` — which is safe here precisely because ``record``
    is a complete ``load()`` and not a patch.
    """
    record.render_manifest = manifest
    store.replace(record)


def render_job(job: ContentJob, store: JobStore, *, now: str = "") -> ContentJob:
    """Build the manifest, render it, verify the file, and advance on a pass.

    Idempotent, and **"a final.mp4 exists" is not "this job is rendered"**: an
    existing render is re-verified against the manifest built from the job as it
    stands now, so a job whose scenes or voice changed after a render is
    rendered again rather than shipped stale. The same lesson ``master_voice``
    and ``scene_planner`` both had to learn.

    Returns the job as persisted. Raises
    :class:`~app.services.jobs.render_manifest.RenderManifestError` or
    :class:`~app.services.jobs.render_adapter.RenderError` on failure, with the
    job parked at ``RETRYABLE_FAILED``; or ``BudgetExceededError`` from the gate,
    with the job parked at ``BUDGET_EXCEEDED`` and nothing rendered.
    """
    job_id = job.content_job_id
    current = store.load(job_id).job
    if current.status is JobStatus.READY_TO_RENDER:
        current = start_rendering(job, store, now=now)
    if current.status not in (JobStatus.RENDERING, JobStatus.TECHNICAL_QA):
        raise RenderManifestError(
            f"render_job requires READY_TO_RENDER or RENDERING, got "
            f"{current.status.value}"
        )

    timestamp = now or utc_now()
    try:
        record = store.load(job_id)
        manifest = render_manifest.build_render_manifest(record.job, store)
        _persist_manifest(store, record, manifest)
        record = store.load(job_id)

        output_path = store.render_output_path(job_id, RENDER_EXTENSION)
        subtitle_path = render_manifest.subtitle_source_path(manifest, store, record)
        subtitle_text = (
            subtitle_path.read_text(encoding="utf-8", errors="replace")
            if subtitle_path is not None and subtitle_path.is_file()
            else None
        )

        # ``assets.jsonl`` is append-only, so a re-render leaves the previous
        # record behind it. The *last* line wins, exactly as a log implies.
        existing = None
        for asset in record.assets:
            if asset.asset_id == RENDER_ASSET_ID:
                existing = asset
        reuse = (
            existing is not None
            and output_path.is_file()
            and output_path.stat().st_size > 0
            and media_probe.file_sha256(output_path) == existing.sha256
        )
        result = None
        if reuse:
            # The bytes are the bytes that were recorded — but "recorded" is not
            # "still correct". Re-run QA against the manifest built from the job
            # as it stands *now*; a job whose scenes, voice or captions changed
            # since the render is re-rendered rather than shipped stale. Failing
            # here instead would be a loop: the retry would reuse the same file
            # and fail on the same check forever.
            result = _write_qa_report(
                store,
                job_id,
                manifest,
                technical_qa(output_path, manifest, subtitle_text=subtitle_text),
                timestamp,
            )
            reuse = result.passed
        if not reuse:
            render_adapter.render(
                manifest,
                scene_sources=render_manifest.scene_source_paths(manifest, store, record),
                voice_path=store.asset_path(
                    job_id,
                    next(
                        asset.storage_key
                        for asset in record.assets
                        if asset.asset_id == manifest.audio.master_voice_asset_id
                    ),
                ),
                subtitle_path=subtitle_path,
                output_path=output_path,
            )
            result = _write_qa_report(
                store,
                job_id,
                manifest,
                technical_qa(output_path, manifest, subtitle_text=subtitle_text),
                timestamp,
            )
            if not result.passed:
                raise RenderError(
                    "technical QA refused the render: " + "; ".join(result.failures)
                )
            store.append_event(
                job_id, _render_asset(job_id, store, output_path, result.facts, timestamp)
            )
    except Exception as error:
        # Deliberately every exception, not a tuple. Measured 2026-08-30: a
        # corrupt scene image raises ``av.error.InvalidDataError`` and a bad
        # ``storage_key`` raises ``JobStoreError`` — both plain ``ValueError``
        # subclasses that a narrow tuple misses, leaving the job unparked in
        # ``RENDERING``, where ``resume_target`` raises ``ResumeError`` and no
        # ``RETRYABLE_FAILED`` line exists for the §5.3 retry limit to count.
        # ``classify_error`` already answers UNKNOWN for types it cannot judge,
        # and ``_park_retryable`` no-ops unless the persisted status is
        # ``RENDERING``, so widening this costs nothing and closes the leak.
        _park_retryable(job_id, store, error)
        raise

    return _advance_to_technical_qa(
        job_id,
        store,
        f"render passed technical QA: {result.facts.width}x{result.facts.height} "
        f"{result.facts.video_codec}/{result.facts.audio_codec}, "
        f"{result.facts.duration_ms} ms",
    )
