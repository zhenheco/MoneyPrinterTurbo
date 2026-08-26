"""SPEC-001 §10: the budget gate that runs before a provider call, and the
ledger that records what the call cost afterwards.

Two halves, in the order a call site uses them::

    job = store.load(content_job_id).job                 # spend to date, re-read
    check_budget(job, estimated_cost_usd, store=store)   # raises, or returns
    event = call_the_provider(...)                       # only reached if allowed
    record_usage(store, job, event, adopted_video_seconds=6.0)

The loop is closed by :func:`record_usage`, which adds what was spent to
``job.actual_cost_usd`` — the very number :func:`check_budget` reads. A call
site that skipped it would leave the gate comparing every estimate against a
figure that never moves, so the ``job`` held in memory is stale from that point
on and the next round re-reads it.

:func:`check_budget` is the whole of the §10 predicate and nothing else: it
refuses on a strict ``>`` so a call that lands exactly on the limit still goes
through. It raises rather than returning a verdict, because a verdict can be
ignored by a caller and an exception cannot — the provider line is simply never
reached.

Money is compared as :class:`~decimal.Decimal` built from the decimal *text* of
each amount. ``0.1 + 0.2`` is ``0.30000000000000004`` in binary floating point,
which would refuse a call §10 allows against a ``0.30`` limit.

Nothing here ever writes a credential. Summaries are produced by
:func:`summarize`, which describes the shape of a payload and never a value, and
*every* string field of an event reaching :func:`record_usage` — not only the
two summary columns — is put through :func:`redact` before it is appended to a
job file (§4.6: 完整憑證、API Key、Authorization header 與敏感回應不得寫入摘要
欄位). :func:`redact` recognises a credential by its key, by the auth scheme in
front of it, and by the shape of the token itself, because a provider quoting
the key it rejected supplies none of the first two.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Sequence, Union

from app.models.content_job import (
    ContentJob,
    CostUsd,
    JobStatus,
    ProviderEvent,
    UsageLedgerEntry,
)
from app.services.jobs.state_machine import (
    BudgetExceededError,
    decision_record,
    is_legal,
    transition,
)

#: The §4.6 idempotency key joins its parts with this, so no part may contain it.
KEY_SEPARATOR = ":"

#: §10's explicit marker for a cost the provider did not report.
UNKNOWN = "unknown"

REDACTED = "<redacted>"

#: Credential shapes that can end up inside a summary string a caller hands us.
#: Deliberately greedy about what counts as a secret: a redacted summary is
#: merely less useful, a leaked one is an incident.
#: The credential words a key or an auth scheme is built from, shared by the
#: two patterns below so a word added here is caught in both shapes.
_CREDENTIAL_WORDS = (
    r"authorization|bearer|basic|api[-_]?key|apikey|token|secret|password|"
    r"passwd|credential|cookie|session"
)

_CREDENTIAL_PATTERNS = (
    # ``Bearer <token>``, ``Basic <base64>``, ``access_token <value>``: the
    # scheme word is not the secret, the run after the whitespace is. This runs
    # first so the ``key: value`` pattern below cannot "redact" the word
    # ``Basic`` and leave the credential it introduced sitting on the line.
    re.compile(rf"(?i)\b[\w-]*(?:{_CREDENTIAL_WORDS})[\w-]*\s+[^\s,;}}\]\"']+"),
    # A ``key: value`` or ``key=value`` pair whose key names a credential, with
    # the quotes a JSON payload puts around either side optional.
    re.compile(
        rf"(?i)[\"']?\b[\w-]*(?:{_CREDENTIAL_WORDS})[\w-]*\b[\"']?\s*[:=]\s*"
        r"[\"']?[^\s,;}\]\"']+[\"']?"
    ),
    # Tokens that announce themselves by their own shape. A provider message
    # quoting the key it rejected carries no ``key:`` in front of it, so the
    # two patterns above never see it.
    re.compile(r"(?i)\b(?:sk|pk|rk|ak)-[A-Za-z0-9_-]{12,}"),
    re.compile(
        r"\b(?:gh[pousr]_|xox[abposr]-|AKIA|ASIA|glpat-|ya29\.)[A-Za-z0-9_.-]{10,}"
    ),
)


def build_idempotency_key(
    content_job_id: str, scene_id: str, operation: str, attempt: int
) -> str:
    """``<content_job_id>:<scene_id>:<operation>:attempt-<n>`` per §4.6.

    A part carrying the separator or whitespace would make two different calls
    share a key, so those are rejected rather than escaped: the key is the only
    thing standing between a resumed job and a double charge (§5.3).
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError(f"attempt must be a positive integer, got {attempt!r}")
    parts = (content_job_id, scene_id, operation)
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise ValueError(f"idempotency key part must be a non-empty string: {part!r}")
        if KEY_SEPARATOR in part or part.strip() != part or any(
            character.isspace() for character in part
        ):
            raise ValueError(
                f"idempotency key part must not contain {KEY_SEPARATOR!r} "
                f"or whitespace: {part!r}"
            )
    return KEY_SEPARATOR.join((*parts, f"attempt-{attempt}"))


def check_budget(
    job: ContentJob,
    estimated_cost_usd: float,
    store: Any = None,
    *,
    now: str = "",
) -> ContentJob:
    """Run the §10 gate. Return ``job`` if the call may proceed, else raise.

    ::

        if actual_cost_usd + estimated_cost_usd > budget_limit_usd:
            transition(BUDGET_EXCEEDED)
            do_not_call_provider()

    On refusal the job is moved to ``BUDGET_EXCEEDED`` and, when ``store`` is
    given, that status and one decision line are persisted before the
    :class:`BudgetExceededError` leaves. The raised error carries the
    transitioned job on ``.job``.

    A job whose spend to date is ``"unknown"`` is refused: §10 forbids treating
    an unknown cost as zero, and an unprovable budget is not an affordable one.
    §5.2 gives some states no ``BUDGET_EXCEEDED`` edge; from those the call is
    still refused, the status simply stays where it is.
    """
    estimate = _amount(estimated_cost_usd, "estimated_cost_usd")
    limit = _amount(job.budget_limit_usd, "budget_limit_usd")
    spent = (
        None if job.actual_cost_usd == UNKNOWN
        else _amount(job.actual_cost_usd, "actual_cost_usd")
    )

    if spent is not None and spent + estimate <= limit:
        return job

    if spent is None:
        detail = (
            f"spend to date is {UNKNOWN}, so a limit of {limit} cannot be proven"
        )
    else:
        detail = f"{spent} spent + {estimate} estimated exceeds a limit of {limit}"

    reason = f"budget guard refused the call: {detail}"
    refused = job
    if is_legal(job.status, JobStatus.BUDGET_EXCEEDED):
        refused = transition(job, JobStatus.BUDGET_EXCEEDED, reason=reason, now=now)
        if store is not None:
            store.save(refused)
    if store is not None:
        # Outside the branch above: a refusal from a state §5.2 gives no
        # BUDGET_EXCEEDED edge is still a refusal, and a call that was blocked
        # without leaving a trace is indistinguishable from one never made.
        # ``from`` and ``to`` are then the same state — nothing moved, and the
        # reason says why.
        store.append_decision(
            job.content_job_id, decision_record(job.status, refused, reason)
        )
    error = BudgetExceededError(reason)
    error.job = refused
    raise error


def summarize(label: str, payload: Any) -> str:
    """Describe ``payload`` without reproducing any of it.

    Neither values nor key names survive — an ``Authorization`` header cannot
    leak through a key name any more than through its value — so the result is
    a shape, not a redaction of the content: ``"video request: 3 fields,
    142 chars withheld"``.
    """
    fields, characters = _shape(payload)
    return f"{redact(str(label))}: {fields} fields, {characters} chars withheld"


def redact(text: str) -> str:
    """Blank out credential-shaped runs in a summary string a caller built."""
    result = str(text)
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def normalize_actual_cost(reported: Any) -> CostUsd:
    """Turn what a provider reported into a §10 cost: a number, or ``"unknown"``.

    Nothing reported becomes ``"unknown"``; it never becomes ``0`` (§10:
    不可假裝為零).
    """
    if reported is None or reported == UNKNOWN:
        return UNKNOWN
    if isinstance(reported, str) and not reported.strip():
        return UNKNOWN
    return float(_amount(reported, "actual_cost_usd"))


def record_usage(
    store: Any,
    job: ContentJob,
    event: ProviderEvent,
    *,
    estimated_cost_source: str = "",
    discarded_asset_ids: Sequence[str] = (),
    adopted_video_seconds: float = 0.0,
) -> Optional[UsageLedgerEntry]:
    """Append one provider event and one ledger entry for a call that happened.

    Returns the entry that was written, or ``None`` when ``event`` carries an
    ``idempotency_key`` the ledger already holds: a resumed job re-records its
    calls, and §5.3 says that must not produce a second charge.

    ``estimated_cost_source`` is mandatory when the provider reported no cost —
    §10 requires the estimate's provenance to survive alongside the ``unknown``.

    Writing the entry also updates ``job.actual_cost_usd`` on disk to the
    ledger's running total, so the next :func:`check_budget` sees this call. The
    ``job`` passed in is therefore stale on return: re-read it from ``store``.
    """
    if event.content_job_id != job.content_job_id:
        raise ValueError(
            f"provider event belongs to {event.content_job_id!r}, "
            f"not to {job.content_job_id!r}"
        )
    actual = normalize_actual_cost(event.actual_cost_usd)
    if actual == UNKNOWN and not str(estimated_cost_source).strip():
        raise ValueError(
            "actual_cost_usd is unknown, so §10 requires an estimated_cost_source"
        )
    seconds = _amount(adopted_video_seconds, "adopted_video_seconds")

    if any(
        entry.idempotency_key == event.idempotency_key
        for entry in store.load(job.content_job_id).usage_ledger
    ):
        return None

    store.append_event(job.content_job_id, _scrubbed(event, actual))
    entry = UsageLedgerEntry(
        provider_event_id=event.provider_event_id,
        content_job_id=event.content_job_id,
        scene_id=event.scene_id,
        provider=event.provider,
        model=event.model,
        idempotency_key=event.idempotency_key,
        attempt_count=event.attempt_count,
        estimated_cost_usd=event.estimated_cost_usd,
        actual_cost_usd=actual,
        created_at=event.created_at,
        estimated_cost_source=redact(estimated_cost_source),
        discarded_asset_ids=list(discarded_asset_ids),
        adopted_video_seconds=float(seconds),
        effective_cost_per_adopted_second_usd=_per_second(actual, seconds),
    )
    store.append_event(job.content_job_id, entry)
    _write_back_spend(store, job.content_job_id)
    return entry


# -- internals ------------------------------------------------------------


def _scrubbed(event: ProviderEvent, actual: CostUsd) -> ProviderEvent:
    """``event`` with every string it carries put through :func:`redact`.

    Not just the two summary columns. A provider that answers with
    ``error_class="upstream 401: token sk-... rejected"`` writes the credential
    into ``provider_events.jsonl`` exactly as effectively as one that puts it in
    ``response_summary``, and ``request_id``/``external_job_id`` are echoed
    provider text too.
    """
    scrubbed = {
        name: redact(value) for name, value in event if isinstance(value, str)
    }
    scrubbed["actual_cost_usd"] = actual
    return event.model_copy(update=scrubbed)


def _write_back_spend(store: Any, job_id: str) -> None:
    """Put what this job has now spent where :func:`check_budget` reads it.

    Without this the gate compares each estimate against a number that never
    moves, so every call looks like the first one and a loop of individually
    affordable calls spends without limit. The total is recomputed from the
    whole ledger rather than incremented, so it is the same figure whether the
    job ran straight through or was resumed, and a duplicate key — which never
    reaches here — cannot inflate it.

    An entry whose cost the provider never reported contributes its estimate,
    not zero: §10 forbids treating an unknown cost as free, and it makes
    ``estimated_cost_source`` mandatory on exactly those entries so the
    stand-in has provenance.
    """
    record = store.load(job_id)
    total = sum(
        (
            _amount(
                entry.estimated_cost_usd
                if entry.actual_cost_usd == UNKNOWN
                else entry.actual_cost_usd,
                "usage ledger cost",
            )
            for entry in record.usage_ledger
        ),
        Decimal(0),
    )
    store.save(record.job.model_copy(update={"actual_cost_usd": float(total)}))


def _amount(value: Any, name: str) -> Decimal:
    """A money or duration amount as an exact decimal, or a loud rejection.

    Built from the decimal text of ``value`` so ``0.1`` is one tenth, not
    ``0.1000000000000000055511151231257827021181583404541015625``. ``bool`` is
    excluded on purpose: ``True`` is not a cost.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a real number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the checks above
        raise ValueError(f"{name} is not a usable amount: {value!r}") from exc
    if amount < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return amount


def _per_second(actual: CostUsd, seconds: Decimal) -> CostUsd:
    """§10's effective cost per adopted second, or ``"unknown"``.

    Unknown in, unknown out — and nothing adopted also means unknown rather
    than zero, because zero would read as "these seconds were free".
    """
    if actual == UNKNOWN or seconds <= 0:
        return UNKNOWN
    return float(Decimal(str(actual)) / seconds)


def _shape(payload: Any) -> tuple:
    """Count the leaves of ``payload`` and the characters they hold."""
    if isinstance(payload, dict):
        return _merge(_shape(value) for value in payload.values())
    if isinstance(payload, (list, tuple, set)):
        return _merge(_shape(item) for item in payload)
    return 1, len(str(payload))


def _merge(shapes: Iterable) -> tuple:
    fields = characters = 0
    for count, size in shapes:
        fields += count
        characters += size
    return fields, characters
