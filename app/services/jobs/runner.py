"""PLAN-001 issue #11: drive one job through the whole V0 arc, resumably.

This is the only module that knows the *order* of the stages. Every stage it
calls already owns its own idempotency, its own budget gate and its own parking
behaviour; the runner adds three things none of them can:

1. **It dispatches on the persisted** ``job.json`` **status, not on**
   ``resume_target``. Measured 2026-08-30 by killing a process between
   ``store.save`` and ``store.append_decision`` at three stages: a crash leaves
   the job sitting in a *stage* (``SCRIPTING``, ``SCENE_PLANNING``,
   ``AWAITING_ASSETS``) with its decision line missing, and
   ``resume_target`` answers only for ``RETRYABLE_FAILED`` and
   ``MANUAL_ACTION_REQUIRED`` — for anything else it raises
   ``"X is not a parked status"``. So ``decisions.jsonl`` is advisory here and
   ``job.json`` is the truth. ``resume_target`` is still the only way a *parked*
   job is resumed, and it is allowed to raise rather than be second-guessed.

2. **The three edges nothing else implements.**
   ``TECHNICAL_QA → CONTENT_QA → READY_FOR_REVIEW → POSTIZ_DRAFTING`` exist in
   SPEC-001 §5.2 and, before this module, in no code at all — every Postiz test
   force-set the status to get past them. They are implemented here, with
   ``CONTENT_QA`` as an explicit **human** gate: PRD-001 FR-008's content half
   (繁中／Hook／核心觀點／結論／CTA／來源標記／人工否決能力) is not something this
   pipeline can judge, so the runner stops at ``TECHNICAL_QA`` unless the caller
   states a human verdict, and it honours a refusal as well as an approval.

3. **A loop that terminates.** "Did the log grow" is not a stop condition — a
   resume always appends at least two lines, and a job that re-parks identically
   grows the log forever (7 rounds, +302 bytes each, zero progress, measured).
   :func:`_made_progress` therefore treats "the status is unchanged and the only
   lines this round were ``park → stage`` then ``stage → park``" as no progress.

What it deliberately does **not** do:

* **It never invents a caption.** ``Script`` has no caption field; ``hook`` and
  ``cta`` are the two that exist, so :func:`postiz_caption` is exactly those two
  joined, and a caller who wants something else passes ``caption=``.
* **It never reads a credential.** ``PostizSettings`` has no config keys and
  this module adds none: the caller constructs the publisher, or the run stops
  at ``POSTIZ_DRAFTING`` and says so.
* **It never auto-passes content QA**, and it never resumes ``BUDGET_EXCEEDED``:
  §5.2 gives that state no return row on purpose, so recovery is two hops with a
  human in between.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Mapping, Optional, Sequence

from app.models.content_job import ContentJob, JobStatus, Script
from app.services.jobs import (
    asset_import,
    captions,
    master_voice,
    pipeline,
    postiz,
    renderer,
    scene_planner,
)
from app.services.jobs.state_machine import (
    PARKING_STATUSES,
    TERMINAL_STATUSES,
    ResumeError,
    decision_record,
    resume_target,
    transition,
)
from app.services.jobs.store import JobRecord, JobStore

#: The extension ``renderer`` writes. The leading dot is mandatory —
#: ``store.render_output_path(job_id, "mp4")`` raises.
RENDER_EXTENSION = ".mp4"

#: Backstop only. The loop terminates on its own through :func:`_made_progress`;
#: this exists so a future stage that appends lines forever fails loudly instead
#: of hanging. The full arc is ten rounds.
MAX_ROUNDS = 60


#: One runner per job. ``cli.py run --job`` is a repeatable automated entry
#: point, so a cron run overlapping a manual one is the ordinary case, not the
#: exotic one — and two runners on one job were measured creating two Postiz
#: drafts and making two paid LLM calls, each believing it had succeeded.
LOCK_FILE = ".runner.lock"


class RunnerError(RuntimeError):
    """The runner cannot take another step, and guessing one would be wrong."""


class JobBusyError(RunnerError):
    """Another runner holds this job. Re-spending on it would be the bug."""


@contextmanager
def _job_lock(job_dir: Path) -> Iterator[Path]:
    """``O_EXCL`` lock file in the job directory, released on the way out.

    A stale lock left by a killed process is cleared by deleting the file — the
    runner does not time it out, because guessing that a peer is dead is exactly
    the guess that produces the duplicate draft.
    """
    path = job_dir / LOCK_FILE
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise JobBusyError(
            f"another runner already holds {job_dir.name} ({path}); "
            "delete the lock file if you are certain no runner is alive"
        ) from None
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        yield path
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class RunResult:
    """Where the job ended up, and why the runner stopped there."""

    job: ContentJob
    stopped_because: str
    rounds: int
    draft: Optional[dict] = None
    error: Optional[BaseException] = None

    @property
    def status(self) -> JobStatus:
        return self.job.status

    @property
    def needs_a_human(self) -> bool:
        return self.job.status in PARKING_STATUSES


@dataclass
class _Options:
    content_qa_approved: Optional[bool] = None
    publisher: Any = None
    caption: str = ""
    creator_profile: Optional[Mapping[str, Any]] = None
    resume: bool = True
    drafts: List[dict] = field(default_factory=list)


def postiz_caption(script: Optional[Script]) -> str:
    """The Postiz caption derived from the Script.

    The rule, stated so it is not mistaken for a guess: **the caption is the
    script's hook followed by its call to action, separated by a blank line.**
    ``Script`` (SPEC-001 §4.3) has no caption field and nothing else in the
    repository produces one; ``hook`` and ``cta`` are the two fields that carry
    what a social post needs. A caller who wants different text passes
    ``caption=`` to :func:`run_job` — the runner never silently invents one.
    """
    if script is None:
        raise RunnerError(
            "a Postiz draft needs a caption and this job has no script to derive "
            "one from; pass caption= explicitly"
        )
    parts = [script.hook.strip(), script.cta.strip()]
    caption = "\n\n".join(part for part in parts if part)
    if not caption:
        raise RunnerError(
            "the script's hook and cta are both empty, so no caption can be "
            "derived; pass caption= explicitly"
        )
    return caption


def _advance(
    store: JobStore, job: ContentJob, target: JobStatus, reason: str
) -> ContentJob:
    """Transition, persist, and record — in the order every other stage uses."""
    updated = transition(job, target, reason=reason)
    store.save(updated)
    store.append_decision(
        job.content_job_id, decision_record(job.status, updated, reason)
    )
    return updated


def _made_progress(
    before: JobStatus, after: JobStatus, appended: Sequence[Mapping[str, Any]]
) -> bool:
    """Did the last round move the job forward?

    Not "did ``decisions.jsonl`` grow": it grows every round of a resume loop.
    The one shape that means *nothing happened* is a round that ended where it
    started having written exactly ``park → stage`` and then ``stage → park``.

    ``from == to`` lines are dropped first, for the reason ``resume_target``
    drops them too: they are refusal traces, not movement. Counting them as
    progress is not hypothetical — the Postiz draft-only guard writes one on
    every refusal, and a run over a job whose media file is missing span 60
    rounds before this line existed.
    """
    appended = [line for line in appended if line.get("from") != line.get("to")]
    if after is not before:
        return True
    if len(appended) == 2:
        out, back = appended
        if (
            out.get("from") == before.value
            and back.get("to") == before.value
            and out.get("to") == back.get("from")
        ):
            return False
    return bool(appended)


def _step(record: JobRecord, store: JobStore, options: _Options) -> Optional[str]:
    """Take one step. Return a stop reason, or ``None`` to keep going."""
    job = record.job
    job_id = job.content_job_id
    status = job.status

    if status is JobStatus.DRAFT:
        pipeline.start_scripting(job, store)
        return None

    if status is JobStatus.SCRIPTING:
        # Returns the persisted script untouched when one exists, so a crash
        # after the script was written costs no second LLM call.
        pipeline.generate_script(job, store)
        scene_planner.start_scene_planning(store.load(job_id).job, store)
        return None

    if status is JobStatus.SCENE_PLANNING:
        scene_planner.plan_scenes(job, store)
        master_voice.start_voice_generating(store.load(job_id).job, store)
        return None

    if status is JobStatus.VOICE_GENERATING:
        master_voice.generate_master_voice(job, store)
        return None

    if status is JobStatus.AWAITING_ASSETS:
        # ``generate_captions`` makes no transition; ``import_assets`` is what
        # opens the render gate, and it refuses until every manifest entry has
        # a file a human placed there.
        captions.generate_captions(job, store)
        asset_import.import_assets(
            store.load(job_id).job, store, creator_profile=options.creator_profile
        )
        return None

    if status in (JobStatus.READY_TO_RENDER, JobStatus.RENDERING):
        # ``render_job`` runs the budget gate and opens ``RENDERING`` itself, so
        # ``start_rendering`` is deliberately not called here.
        renderer.render_job(job, store)
        return None

    if status is JobStatus.TECHNICAL_QA:
        if options.content_qa_approved is None:
            return (
                "technical QA passed and content QA is a human gate: PRD-001 "
                "FR-008 asks a person to check 繁中／Hook／核心觀點／結論／CTA／"
                "來源標記, so the runner will not pass it. Re-run with an explicit "
                "content_qa_approved verdict."
            )
        _advance(
            store,
            job,
            JobStatus.CONTENT_QA,
            "a human opened content QA review (PRD-001 FR-008)",
        )
        return None

    if status is JobStatus.CONTENT_QA:
        if options.content_qa_approved:
            _advance(
                store,
                job,
                JobStatus.READY_FOR_REVIEW,
                "a human approved content QA (PRD-001 FR-008)",
            )
            return None
        # FR-008 「人工否決能力」. The refusal is recorded and the run ends here:
        # nothing automatic may undo a human's no, and ``CONTENT_QA`` is not a
        # §5.2 return target, so this park is for a person to clear.
        _advance(
            store,
            job,
            JobStatus.MANUAL_ACTION_REQUIRED,
            "a human refused content QA (PRD-001 FR-008 人工否決能力)",
        )
        return (
            "a human refused content QA. Nothing automatic undoes that: "
            "CONTENT_QA is not a SPEC-001 §5.2 return target, so resuming this "
            "park needs a person to set job.json back to a stage themselves"
        )

    if status is JobStatus.READY_FOR_REVIEW:
        _advance(
            store,
            job,
            JobStatus.POSTIZ_DRAFTING,
            "content QA approved; creating the Postiz draft",
        )
        return None

    if status is JobStatus.POSTIZ_DRAFTING:
        if options.publisher is None:
            return (
                "the job is ready for a Postiz draft but no publisher was "
                "supplied; PostizSettings has no config keys in V0, so the "
                "caller constructs it"
            )
        # A publisher persists the draft only when a store was injected at
        # construction (postiz.PostizPublisher.__init__), and it is the caller
        # who constructs it. Without one the POST still goes out and nothing is
        # written: the job stays at POSTIZ_DRAFTING with no provider event, so
        # the duplicate-draft guard below sees nothing next round and posts
        # again. Refuse before the socket — an unrecorded irreversible call is
        # the one failure this whole branch exists to prevent. Same store, too:
        # one bound elsewhere would write the draft into another job tree.
        if getattr(options.publisher, "_store", None) is not store:
            return (
                "the Postiz publisher is not bound to this job store, so a "
                "draft it creates would not be recorded and the next run would "
                "create a second one; construct it with store=<this store>"
            )
        # ``state_machine.RESUMABLE_STAGES`` excludes POSTIZ_DRAFTING precisely
        # so a resume cannot create a second Draft; the runner reaches this
        # status by the front door instead, so it needs the same guard. Postiz
        # sends no idempotency key on the wire (postiz.py records it locally
        # only), so a re-run after a POST the server accepted but whose reply we
        # could not read would draft twice.
        # *Any* postiz event, not only one carrying a draft id: the case that
        # needs the guard most is a POST the server accepted whose reply we
        # could not read, and that one records an empty ``external_job_id``.
        # Refusing is the conservative side of an irreversible external call.
        drafted = [
            event
            for event in record.provider_events
            if event.provider == postiz.PROVIDER
        ]
        if drafted:
            return (
                "a Postiz draft may already exist for this job "
                f"(external_job_id={drafted[-1].external_job_id!r}); the API "
                "carries no idempotency key, so the runner will not POST again. "
                "Check Postiz, then clear the attempt or draft by hand"
            )
        output_path = store.render_output_path(job_id, RENDER_EXTENSION)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            return (
                "the rendered file is missing or empty, so there is nothing to "
                "draft; re-render before drafting"
            )
        caption = options.caption or postiz_caption(record.script)
        options.drafts.append(
            options.publisher.create_draft(job, str(output_path), caption)
        )
        return None

    if status is JobStatus.POSTIZ_DRAFTED:
        return "the Postiz draft exists; V0 stops at a draft and never publishes"

    if status is JobStatus.BUDGET_EXCEEDED:
        return (
            "the budget gate refused this job. SPEC-001 §5.2 gives "
            "BUDGET_EXCEEDED no return row on purpose: recovery is two hops, "
            "BUDGET_EXCEEDED -> MANUAL_ACTION_REQUIRED and then a resume, so a "
            "human clears the spend before anything generates again"
        )

    if status in (JobStatus.RETRYABLE_FAILED, JobStatus.MANUAL_ACTION_REQUIRED):
        if not options.resume:
            return f"the job is parked at {status.value} and resume is disabled"
        try:
            target = resume_target(status, record.decisions)
        except ResumeError as error:
            return f"the job is parked at {status.value} and cannot be resumed: {error}"
        _advance(store, job, target, f"resuming {status.value} at {target.value}")
        # The resumed stage runs in the *same* round, on purpose. A park and the
        # stage it resumes into are one unit of progress: split across two
        # rounds, every park/resume pair looks like two status changes and
        # :func:`_made_progress` could never see the ``park -> stage`` followed
        # by ``stage -> park`` shape that means the round achieved nothing.
        return _step(store.load(job_id), store, options)

    return f"{status.value} has no step in the V0 runner"


def run_job(
    job_id: str,
    store: JobStore,
    *,
    content_qa_approved: Optional[bool] = None,
    publisher: Any = None,
    caption: str = "",
    creator_profile: Optional[Mapping[str, Any]] = None,
    resume: bool = True,
) -> RunResult:
    """Drive ``job_id`` as far as it can go, and say where it stopped.

    Dispatches on the status persisted in ``job.json`` — see the module
    docstring for the measurement behind that. Every stage is idempotent, so a
    run over an already-finished stage costs no provider call and rewrites no
    bytes; a resumed run therefore picks up where the last one died rather than
    redoing it.

    ``content_qa_approved`` is a human's verdict on PRD-001 FR-008's content
    half. ``None`` (the default) means *nobody has looked*, and the run stops at
    ``TECHNICAL_QA``. ``True`` walks ``CONTENT_QA → READY_FOR_REVIEW →
    POSTIZ_DRAFTING``. ``False`` records the refusal and stops.

    ``publisher`` is an already-constructed
    :class:`~app.services.jobs.postiz.PostizPublisher`. Without one the run
    stops at ``POSTIZ_DRAFTING``: this module reads no credential and invents no
    config key.

    Raises :class:`RunnerError` only if the loop hits :data:`MAX_ROUNDS`, or
    :class:`JobBusyError` if another runner holds the job. A stage failure is
    *not* raised: the stage parks the job itself, the runner gives the park
    exactly one resume, and the result carries the exception on ``.error``.
    """
    # ``_job_dir`` is the store's own id validation and existence check: an
    # unknown job raises JobStoreError here, before anything is locked.
    job_dir = store._job_dir(job_id)
    with _job_lock(job_dir):
        return _run_locked(
            job_id,
            store,
            content_qa_approved=content_qa_approved,
            publisher=publisher,
            caption=caption,
            creator_profile=creator_profile,
            resume=resume,
        )


def _run_locked(
    job_id: str,
    store: JobStore,
    *,
    content_qa_approved: Optional[bool],
    publisher: Any,
    caption: str,
    creator_profile: Optional[Mapping[str, Any]],
    resume: bool,
) -> RunResult:
    """:func:`run_job` with the job lock already held."""
    options = _Options(
        content_qa_approved=content_qa_approved,
        publisher=publisher,
        caption=caption,
        creator_profile=creator_profile,
        resume=resume,
    )
    rounds = 0
    error: Optional[BaseException] = None
    while True:
        record = store.load(job_id)
        before_status = record.job.status
        before_lines = len(record.decisions)

        if before_status in TERMINAL_STATUSES:
            reason = f"{before_status.value} is terminal"
            break

        rounds += 1
        if rounds > MAX_ROUNDS:
            raise RunnerError(
                f"{job_id} is still at {before_status.value} after {MAX_ROUNDS} "
                "rounds; the runner refuses to keep spending on a job that is "
                "not converging"
            )

        error = None
        try:
            reason = _step(record, store, options)
        except Exception as exc:  # noqa: BLE001 - every stage parks itself first
            # Deliberately broad, for the reason ``renderer.render_job`` states:
            # the stages raise JobStoreError, pydantic ValidationError, ffmpeg
            # OSError and provider errors, and each has already recorded what it
            # did. The runner's job is to let the resume have its one attempt
            # and then stop, not to re-classify anything.
            error = exc
            reason = None

        after = store.load(job_id)
        if reason is not None:
            break
        if not _made_progress(
            before_status, after.job.status, after.decisions[before_lines:]
        ):
            reason = (
                f"the last round made no progress: the job is still at "
                f"{after.job.status.value}"
            )
            if error is not None:
                reason += f" after {type(error).__name__}: {error}"
            break

    return RunResult(
        job=store.load(job_id).job,
        stopped_because=reason,
        rounds=rounds,
        draft=options.drafts[-1] if options.drafts else None,
        error=error,
    )
