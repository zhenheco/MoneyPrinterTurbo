"""SPEC-001 §5: which job state may follow which, and which errors may be retried.

Pure module: it decides, it never persists. :func:`transition` hands back a new
:class:`~app.models.content_job.ContentJob`; writing ``job.json`` and appending
the decision to ``decisions.jsonl`` is the caller's job via the job store::

    record = store.load(job_id)
    updated = transition(record.job, JobStatus.RENDERING, reason="manifest passed")
    store.save(updated)
    store.append_decision(job_id, decision_record(record.job.status, updated, reason))

The transition table is the §5.2 table and nothing else. In particular
``PUBLISHED`` is not rejected by a guard — no row in the table points at it, so
there is no edge to reject. ``APPROVED`` and ``SCHEDULED`` are unreachable for
the same reason: §5.2 reserves all three for the controlled publish flow of a
later version.

Several rows of §5.2 name a *class* of states rather than a single state
("任一可重試階段", "任一生成階段", "該次失敗的可恢復階段", "該次中斷的可恢復階段").
Those classes are spelled out as :data:`RETRYABLE_STAGES`,
:data:`GENERATING_STAGES`, :data:`RESUMABLE_STAGES` and
:data:`MANUAL_RETURN_STAGES` below.

For the two return rows the table is a necessary but not a sufficient condition:
it says a job *may* go back to one of several stages, and :func:`resume_target`
says which one. A caller must never pick any legal target.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Dict, FrozenSet, Mapping, Sequence, Union

from app.models.content_job import ContentJob, JobStatus

StatusLike = Union[JobStatus, str]

#: The timestamp key of a decision record. ``at`` rather than ``timestamp``
#: because the frozen job fixtures from slice 1 already use ``at``.
DECISION_TIMESTAMP_KEY = "at"

#: A job in one of these is finished; §5.2 has no row leading out of them.
TERMINAL_STATUSES: FrozenSet[JobStatus] = frozenset(
    {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
)

#: §5.2 "任一可重試階段": the stages that run work which can fail transiently —
#: a provider call, or the local render.
RETRYABLE_STAGES: FrozenSet[JobStatus] = frozenset(
    {
        JobStatus.RESEARCHING,
        JobStatus.SCRIPTING,
        JobStatus.SCENE_PLANNING,
        JobStatus.VOICE_GENERATING,
        JobStatus.IMAGE_GENERATING,
        JobStatus.VIDEO_GENERATING,
        JobStatus.RENDERING,
        JobStatus.POSTIZ_DRAFTING,
    }
)

#: §5.2 "任一生成階段": the stages that spend provider budget, so the ones a
#: budget gate can trip in. Rendering itself is local and free, so it is not here.
#: ``READY_TO_RENDER`` is, even though it generates nothing: its §5.2 row reads
#: "Render Manifest 通過**且預算閘門通過** → RENDERING", so the gate is evaluated
#: while the job sits in that state and needs somewhere to send it when it refuses.
GENERATING_STAGES: FrozenSet[JobStatus] = frozenset(
    {
        JobStatus.RESEARCHING,
        JobStatus.SCRIPTING,
        JobStatus.SCENE_PLANNING,
        JobStatus.VOICE_GENERATING,
        JobStatus.IMAGE_GENERATING,
        JobStatus.VIDEO_GENERATING,
        JobStatus.READY_TO_RENDER,
    }
)

#: The states a job parks in instead of progressing. A derivation that walks
#: ``decisions.jsonl`` backwards must walk *past* these: a job can chain them
#: (BUDGET_EXCEEDED then MANUAL_ACTION_REQUIRED), and none of them is a stage.
PARKING_STATUSES: FrozenSet[JobStatus] = frozenset(
    {
        JobStatus.RETRYABLE_FAILED,
        JobStatus.MANUAL_ACTION_REQUIRED,
        JobStatus.BUDGET_EXCEEDED,
    }
)

#: §5.2 "該次失敗的可恢復階段": where RETRYABLE_FAILED may return to.
#: ``POSTIZ_DRAFTING`` is excluded: §5.3 requires resume to go through an
#: idempotency key, and the Postiz publisher never reads one, so re-entering it
#: would create a second Draft. Add it here once it does.
#: ``SCENE_PLANNING`` is excluded for the reason given on
#: :data:`MANUAL_RETURN_STAGES` — the hazard is the planner, not which row
#: leads back into it, so both return sets exclude it or neither does.
RESUMABLE_STAGES: FrozenSet[JobStatus] = RETRYABLE_STAGES - {
    JobStatus.POSTIZ_DRAFTING,
    JobStatus.SCENE_PLANNING,
}

#: §5.2 "該次中斷的可恢復階段": where MANUAL_ACTION_REQUIRED may return to.
#: The three stages that actually park there today, plus ``READY_TO_RENDER``,
#: which is where the budget gate sends a refusal and which generates nothing on
#: re-entry (the render is local and free). ``SCENE_PLANNING`` is excluded: the
#: planner rebuilds its scene list whenever it differs from a freshly derived
#: one, and a freshly derived scene always carries an empty ``reference_assets``,
#: so re-entry discards exactly the human-imported assets these rows exist to
#: preserve. Add it to both sets once the planner can merge instead of replace.
MANUAL_RETURN_STAGES: FrozenSet[JobStatus] = frozenset(
    {
        JobStatus.SCRIPTING,
        JobStatus.VOICE_GENERATING,
        JobStatus.AWAITING_ASSETS,
        JobStatus.READY_TO_RENDER,
    }
)

#: The eleven explicit rows of §5.2, in table order.
LINEAR_TRANSITIONS = (
    (JobStatus.DRAFT, JobStatus.SCRIPTING),
    (JobStatus.SCRIPTING, JobStatus.SCENE_PLANNING),
    (JobStatus.SCENE_PLANNING, JobStatus.VOICE_GENERATING),
    (JobStatus.VOICE_GENERATING, JobStatus.AWAITING_ASSETS),
    (JobStatus.AWAITING_ASSETS, JobStatus.READY_TO_RENDER),
    (JobStatus.READY_TO_RENDER, JobStatus.RENDERING),
    (JobStatus.RENDERING, JobStatus.TECHNICAL_QA),
    (JobStatus.TECHNICAL_QA, JobStatus.CONTENT_QA),
    (JobStatus.CONTENT_QA, JobStatus.READY_FOR_REVIEW),
    (JobStatus.READY_FOR_REVIEW, JobStatus.POSTIZ_DRAFTING),
    (JobStatus.POSTIZ_DRAFTING, JobStatus.POSTIZ_DRAFTED),
)


def _build_table() -> Mapping[JobStatus, FrozenSet[JobStatus]]:
    table: dict = {status: set() for status in JobStatus}
    for source, target in LINEAR_TRANSITIONS:
        table[source].add(target)
    for source in RETRYABLE_STAGES:
        table[source].add(JobStatus.RETRYABLE_FAILED)
    for source in GENERATING_STAGES:
        table[source].add(JobStatus.BUDGET_EXCEEDED)
    table[JobStatus.RETRYABLE_FAILED].update(RESUMABLE_STAGES)
    # §5.2 "RETRYABLE_FAILED → FAILED": the row above is bounded by
    # "未超過上限", and a bound needs a state that says the bound was passed.
    table[JobStatus.RETRYABLE_FAILED].add(JobStatus.FAILED)
    table[JobStatus.MANUAL_ACTION_REQUIRED].update(MANUAL_RETURN_STAGES)
    # BUDGET_EXCEEDED deliberately gets no return row: recovery is two hops,
    # through the MANUAL_ACTION_REQUIRED edge it already has.
    for source in JobStatus:
        if source in TERMINAL_STATUSES:
            continue
        # §5.2 "任一未完成階段 → CANCELLED" and "任一階段 → MANUAL_ACTION_REQUIRED";
        # the `continue` above is what leaves the terminal states with no edge.
        table[source].add(JobStatus.CANCELLED)
        if source is not JobStatus.MANUAL_ACTION_REQUIRED:
            table[source].add(JobStatus.MANUAL_ACTION_REQUIRED)
    return MappingProxyType(
        {source: frozenset(targets) for source, targets in table.items()}
    )


#: source state -> every state §5.2 allows it to move to. Read-only: a caller
#: that could assign into it could quietly make PUBLISHED reachable.
TRANSITIONS: Mapping[JobStatus, FrozenSet[JobStatus]] = _build_table()


class IllegalTransitionError(ValueError):
    """The requested state change is not a row of SPEC-001 §5.2."""


class ResumeError(ValueError):
    """The stage a parked job should return to cannot be derived. Never guessed."""


class BudgetExceededError(ValueError):
    """A call would push the job past its budget limit (§10). Never retryable."""


class UnauthorizedAssetError(ValueError):
    """An asset lacks usable consent or licence (§7). Never retryable."""


class ErrorClass(str, Enum):
    """SPEC-001 §5.3. ``UNKNOWN`` is not retried: only a positively identified
    transient failure earns another provider call."""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        return self is ErrorClass.RETRYABLE


# §5.3: schema, file format, permission, budget and unauthorized-asset failures.
# ``ValueError`` is the common base of pydantic's ValidationError and the store's
# JobStoreError, so bad data of any shape lands here.
_NON_RETRYABLE_ERRORS = (
    ValueError,
    PermissionError,
    BudgetExceededError,
    UnauthorizedAssetError,
)


def _retryable_error_types():
    # requests is a hard dependency, but keep the import local so this module
    # stays importable in a stripped environment.
    try:
        import requests
    except ImportError:  # pragma: no cover - requests ships in requirements.txt
        return (ConnectionError, TimeoutError)
    return (
        ConnectionError,
        TimeoutError,
        requests.ConnectionError,
        requests.Timeout,
    )


_RETRYABLE_ERRORS = _retryable_error_types()

TOO_MANY_REQUESTS = 429


def as_status(value: StatusLike) -> JobStatus:
    """Accept an enum member or its name/value; reject anything else loudly."""
    if isinstance(value, JobStatus):
        return value
    return JobStatus(value)


def is_legal(from_status: StatusLike, to_status: StatusLike) -> bool:
    """True when SPEC-001 §5.2 has a row from ``from_status`` to ``to_status``."""
    return as_status(to_status) in TRANSITIONS[as_status(from_status)]


def transition(
    job: ContentJob,
    to_status: StatusLike,
    reason: str,
    now: str = "",
) -> ContentJob:
    """Return a copy of ``job`` in ``to_status``, or raise if §5.2 forbids it.

    ``now`` is an ISO-8601 timestamp; it defaults to the current UTC time and is
    injectable so tests do not have to freeze the clock.
    """
    target = as_status(to_status)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"a transition to {target.value} needs a non-empty reason for decisions.jsonl"
        )
    if not is_legal(job.status, target):
        raise IllegalTransitionError(
            f"illegal transition: {job.status.value} -> {target.value} "
            "is not in SPEC-001 §5.2"
        )
    return job.model_copy(
        update={"status": target, "updated_at": now or _utc_now()}
    )


def _return_stages(status: JobStatus) -> FrozenSet[JobStatus]:
    """The stages ``status`` may be resumed into — its §5.2 return row only."""
    if status is JobStatus.RETRYABLE_FAILED:
        return RESUMABLE_STAGES
    return MANUAL_RETURN_STAGES


def resume_target(
    status: StatusLike, decisions: Sequence[Mapping[str, str]]
) -> JobStatus:
    """The stage a parked job returns to, derived from its ``decisions.jsonl``.

    ``decisions`` is the already-loaded log, oldest first — this module never
    reads a file. Answers 回哪裡, not 可不可以回: whether a resume is allowed at all
    is the caller's decision (§5.3 retry limits for ``RETRYABLE_FAILED``, a human
    trigger for ``MANUAL_ACTION_REQUIRED``). Raises :class:`ResumeError` rather
    than returning a plausible stage — a wrong resume re-spends budget.

    Two implementation rules that look like they could be simplified, and cannot:

    *The log is walked in file order, never sorted by* ``at``. That field is
    ``job.updated_at``, which :func:`transition` lets a caller stamp through its
    ``now`` argument, so it is neither guaranteed monotonic nor a total order
    (both frozen fixtures already carry three lines sharing one value). File
    order is the only real record of what happened first.

    *The candidate is the park line's* ``from``\\ *, not the previous line's*
    ``to``. Every writer does ``store.save(job)`` and *then*
    ``store.append_decision(...)``; a crash between the two leaves ``job.json``
    advanced with its decision line missing. Reading the previous line's ``to``
    then silently returns an older stage — a legal edge and a wrong answer.

    Two known blind spots, so they are not rediscovered the hard way:

    (a) The ``from == to`` skip makes a budget refusal invisible here: those
        no-op lines record that a refusal happened, not that anything moved, so
        a job that resumes will meet the same gate with no trace in the answer.
    (b) The rule is total but not terminating. A job that re-parks identically
        is handed the same stage forever. A resume always appends at least two
        lines (its own ``park -> stage`` and whatever follows), so "did the log
        grow" is not the stop condition; an automated runner must stop when the
        last resume appended *only* those two — ``park -> stage`` immediately
        followed by ``stage -> park`` — because that round made no progress.
    """
    current = as_status(status)
    if current is JobStatus.BUDGET_EXCEEDED:
        raise ResumeError(
            "BUDGET_EXCEEDED has no return row in SPEC-001 §5.2: recovery is two "
            "hops, BUDGET_EXCEEDED -> MANUAL_ACTION_REQUIRED and then resume from "
            "there, so a human clears the spend before anything generates again"
        )
    if current not in (JobStatus.RETRYABLE_FAILED, JobStatus.MANUAL_ACTION_REQUIRED):
        raise ResumeError(
            f"{current.value} is not a parked status; there is nothing to resume"
        )
    if not decisions:
        raise ResumeError(
            f"{current.value} has an empty decision log, so no return stage can be "
            "derived; the job needs a human to say where it belongs"
        )
    for record in reversed(decisions):
        try:
            source = as_status(record["from"])
            target = as_status(record["to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResumeError(
                f"decisions.jsonl holds a record that is not a usable transition: {exc}"
            ) from exc
        if source is target:
            continue  # a refusal trace, not a movement
        candidate = source if target is current else target
        if candidate in PARKING_STATUSES:
            continue  # parks chain; keep walking back to a stage
        # Not ``TRANSITIONS[current]``: that also holds CANCELLED (and FAILED,
        # for RETRYABLE_FAILED), which are legal moves but not stages to resume
        # into. Handing one back would let a caller quietly cancel a job it was
        # asked to restart, so the return set is what this must be checked against.
        if candidate not in _return_stages(current):
            raise ResumeError(
                f"{current.value} -> {candidate.value} is not a SPEC-001 §5.2 "
                f"return target, so the job cannot go back to the stage it came from"
            )
        return candidate
    raise ResumeError(
        f"{current.value} has no non-parking stage anywhere in its "
        f"{len(decisions)} decision records, so no return stage can be derived"
    )


def decision_record(
    from_status: StatusLike, job: ContentJob, reason: str
) -> Dict[str, str]:
    """The ``decisions.jsonl`` line for the transition that produced ``job``.

    Takes the already-transitioned job rather than returning a tuple from
    :func:`transition`, so the record's ``to`` and timestamp can only ever be
    the ones actually written to ``job.json``.
    """
    return {
        "from": as_status(from_status).value,
        "to": job.status.value,
        "reason": reason,
        DECISION_TIMESTAMP_KEY: job.updated_at,
    }


def classify_error(exc: BaseException) -> ErrorClass:
    """Classify a provider or pipeline failure per SPEC-001 §5.3."""
    if isinstance(exc, _NON_RETRYABLE_ERRORS):
        return ErrorClass.NON_RETRYABLE
    flag = getattr(exc, "retryable", None)
    if isinstance(flag, bool):
        # A provider adapter that judged its own failure outranks the guesses below.
        return ErrorClass.RETRYABLE if flag else ErrorClass.NON_RETRYABLE
    status = _status_code(exc)
    if status is not None:
        return (
            ErrorClass.RETRYABLE
            if status == TOO_MANY_REQUESTS
            else ErrorClass.NON_RETRYABLE
        )
    if isinstance(exc, _RETRYABLE_ERRORS):
        return ErrorClass.RETRYABLE
    # §5.3 file-format errors: ffmpeg/ffprobe and moviepy surface unreadable or
    # malformed media as OSError. Checked *after* the retryable branch above,
    # because ConnectionError and TimeoutError are OSError subclasses.
    if isinstance(exc, OSError):
        return ErrorClass.NON_RETRYABLE
    return ErrorClass.UNKNOWN


def _status_code(exc: BaseException):
    for candidate in (exc, getattr(exc, "response", None)):
        status = getattr(candidate, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def utc_now() -> str:
    """The timestamp format every job document and decision record uses."""
    return datetime.now(timezone.utc).isoformat()


#: Kept so the existing private call sites in this module keep working.
_utc_now = utc_now
