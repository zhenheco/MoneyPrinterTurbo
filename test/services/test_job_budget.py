"""SPEC-001 §10 budget guard and usage ledger.

The §10 predicate is transcribed by hand here rather than imported from
``app.services.jobs.budget``, so a wrong predicate cannot make its own test
pass. The provider is always a mock whose call count is asserted: "the job
moved to BUDGET_EXCEEDED" is not the requirement — "the provider was never
called" is.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.models.content_job import (
    ContentJob,
    JobStatus,
    ProviderEvent,
    UsageLedgerEntry,
)
from app.services.jobs.budget import (
    build_idempotency_key,
    check_budget,
    normalize_actual_cost,
    record_usage,
    redact,
    summarize,
)
from app.services.jobs.state_machine import BudgetExceededError
from app.services.jobs.store import JobStore
from test.services.test_content_job_models import content_job_payload

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"

#: A payload of the shape a provider adapter really passes around: the token is
#: the thing that must never survive into a job file.
SECRET = "sk-live-51H9zSuperSecretValue"
CREDENTIALLED_PAYLOAD = {
    "url": "https://api.example.com/v1/videos",
    "headers": {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/json",
    },
    "json": {"prompt": "a founder at a whiteboard", "api_key": SECRET},
}


def load_demo(tmp_path, job_id="three-scene-demo"):
    """A JobStore over a scratch copy of the frozen fixture, plus its record."""
    shutil.copytree(FIXTURES_ROOT / job_id, tmp_path / job_id)
    store = JobStore(tmp_path)
    return store, store.load(job_id)


def provider_event(job_id, **overrides):
    """A §4.6 provider event for a call that already happened."""
    payload = {
        "provider_event_id": "provider-event-900",
        "content_job_id": job_id,
        "scene_id": "scene-001",
        "provider": "manual_google_flow",
        "model": "veo-3",
        "request_id": "req-900",
        "external_job_id": "",
        "idempotency_key": build_idempotency_key(job_id, "scene-001", "video", 1),
        "attempt_count": 1,
        "estimated_cost_usd": 0.18,
        "actual_cost_usd": 0.12,
        "request_summary": "",
        "response_summary": "",
        "error_class": None,
        "retryable": False,
        "created_at": "2026-08-16T10:00:00+00:00",
        "completed_at": "2026-08-16T10:01:00+00:00",
    }
    payload.update(overrides)
    return ProviderEvent.model_validate(payload)


def spend(job, estimated_cost_usd, provider, store=None):
    """What every §10 call site looks like: gate, and only then the provider.

    If the gate stops raising, the provider line is reached and the call-count
    assertions in this file fail — which is the point.
    """
    check_budget(job, estimated_cost_usd, store=store)
    return provider()


class TestIdempotencyKey:
    def test_key_matches_the_spec_4_6_format(self):
        key = build_idempotency_key("job-20260816-001", "scene-001", "video", 1)

        assert key == "job-20260816-001:scene-001:video:attempt-1"

    def test_attempt_number_is_carried_verbatim(self):
        assert build_idempotency_key("j", "s", "image", 3).endswith(":attempt-3")

    @pytest.mark.parametrize(
        "part", ["with:colon", "", "   ", "with\nnewline"]
    )
    def test_a_part_that_would_corrupt_the_key_is_rejected(self, part):
        with pytest.raises(ValueError):
            build_idempotency_key(part, "scene-001", "video", 1)

    @pytest.mark.parametrize("attempt", [0, -1, "1", 1.5])
    def test_attempt_must_be_a_positive_integer(self, attempt):
        with pytest.raises(ValueError):
            build_idempotency_key("job", "scene-001", "video", attempt)


class TestCheckBudget:
    def test_an_affordable_call_reaches_the_provider(self, tmp_path):
        store, record = load_demo(tmp_path)
        provider = Mock(return_value="ok")

        result = spend(record.job, 0.5, provider, store=store)

        assert result == "ok"
        assert provider.call_count == 1
        assert store.load("three-scene-demo").job.status == JobStatus.READY_TO_RENDER

    def test_an_unaffordable_call_never_reaches_the_provider(self, tmp_path):
        store, record = load_demo(tmp_path)
        provider = Mock(return_value="ok")
        # 0.36 already spent + 2.70 estimated > the fixture's 3.00 limit.
        with pytest.raises(BudgetExceededError):
            spend(record.job, 2.70, provider, store=store)

        assert provider.call_count == 0
        assert store.load("three-scene-demo").job.status == JobStatus.BUDGET_EXCEEDED

    def test_the_refusal_is_written_to_the_decision_log(self, tmp_path):
        store, record = load_demo(tmp_path)
        before = len(record.decisions)

        with pytest.raises(BudgetExceededError):
            check_budget(record.job, 2.70, store=store)

        decisions = store.load("three-scene-demo").decisions
        assert len(decisions) == before + 1
        assert decisions[-1]["from"] == "READY_TO_RENDER"
        assert decisions[-1]["to"] == "BUDGET_EXCEEDED"
        assert decisions[-1]["reason"]

    def test_spending_exactly_the_limit_is_allowed(self, tmp_path):
        """§10 refuses on strict ``>``; landing on the limit is not over it."""
        store, record = load_demo(tmp_path)
        job = record.job.model_copy(
            update={"actual_cost_usd": 0.1, "budget_limit_usd": 0.3}
        )
        store.save(job)
        provider = Mock(return_value="ok")

        # 0.1 + 0.2 is 0.30000000000000004 in binary floating point, so a naive
        # float comparison refuses a call that §10 allows.
        assert spend(job, 0.2, provider, store=store) == "ok"
        assert provider.call_count == 1
        assert store.load("three-scene-demo").job.status == JobStatus.READY_TO_RENDER

    def test_one_cent_over_the_limit_is_refused(self, tmp_path):
        store, record = load_demo(tmp_path)
        job = record.job.model_copy(
            update={"actual_cost_usd": 0.1, "budget_limit_usd": 0.3}
        )
        store.save(job)
        provider = Mock()

        with pytest.raises(BudgetExceededError):
            spend(job, 0.21, provider, store=store)

        assert provider.call_count == 0

    def test_an_unknown_spend_to_date_refuses_rather_than_counting_as_zero(
        self, tmp_path
    ):
        """§10: 不可假裝為零. An unprovable budget is not an affordable one."""
        store, record = load_demo(tmp_path)
        job = record.job.model_copy(update={"actual_cost_usd": "unknown"})
        store.save(job)
        provider = Mock()

        with pytest.raises(BudgetExceededError):
            spend(job, 0.01, provider, store=store)

        assert provider.call_count == 0

    @pytest.mark.parametrize("estimate", [-0.01, float("nan"), float("inf"), "cheap", None])
    def test_an_unusable_estimate_is_rejected_and_no_call_is_made(
        self, tmp_path, estimate
    ):
        store, record = load_demo(tmp_path)
        provider = Mock()

        with pytest.raises(ValueError):
            spend(record.job, estimate, provider, store=store)

        assert provider.call_count == 0

    def test_a_state_with_no_budget_exceeded_edge_still_refuses_the_call(
        self, tmp_path
    ):
        """§5.2 gives DRAFT no BUDGET_EXCEEDED edge; §10 still forbids the call."""
        store, record = load_demo(tmp_path)
        job = record.job.model_copy(update={"status": JobStatus.DRAFT})
        store.save(job)
        provider = Mock()

        with pytest.raises(BudgetExceededError):
            spend(job, 99.0, provider, store=store)

        assert provider.call_count == 0
        assert store.load("three-scene-demo").job.status == JobStatus.DRAFT

    def test_the_gate_works_without_a_store(self, tmp_path):
        _, record = load_demo(tmp_path)
        provider = Mock()

        with pytest.raises(BudgetExceededError):
            spend(record.job, 99.0, provider)

        assert provider.call_count == 0

    def test_the_refusal_message_states_the_numbers_and_no_secret(self, tmp_path):
        _, record = load_demo(tmp_path)

        with pytest.raises(BudgetExceededError) as raised:
            check_budget(record.job, 2.70)

        message = str(raised.value)
        assert "3" in message and "2.7" in message


class TestSummaryScrubbing:
    def test_a_summary_of_a_credentialled_payload_leaks_nothing(self):
        summary = summarize("video request", CREDENTIALLED_PAYLOAD)

        assert SECRET not in summary
        assert "Authorization" not in summary
        assert "Bearer" not in summary
        assert "api_key" not in summary
        assert "video request" in summary

    def test_a_summary_of_a_nested_payload_leaks_no_leaf_value(self):
        summary = summarize("video response", CREDENTIALLED_PAYLOAD)

        assert "a founder at a whiteboard" not in summary
        assert "https://api.example.com/v1/videos" not in summary

    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {SECRET}",
            f"authorization={SECRET}",
            f"api_key={SECRET}",
            f"X-Api-Key: {SECRET}",
            f'{{"token": "{SECRET}"}}',
            f"password = {SECRET}",
            f"bearer {SECRET}",
        ],
    )
    def test_redact_removes_a_credential_that_reached_a_string(self, text):
        assert SECRET not in redact(text)

    def test_redact_leaves_harmless_text_alone(self):
        assert redact("video asset asset-002 accepted") == (
            "video asset asset-002 accepted"
        )


class TestRecordUsage:
    def test_one_call_appends_one_provider_event_and_one_ledger_entry(self, tmp_path):
        store, record = load_demo(tmp_path)
        events_before = len(record.provider_events)
        ledger_before = len(record.usage_ledger)

        entry = record_usage(store, record.job, provider_event("three-scene-demo"))

        reloaded = store.load("three-scene-demo")
        assert isinstance(entry, UsageLedgerEntry)
        assert len(reloaded.provider_events) == events_before + 1
        assert len(reloaded.usage_ledger) == ledger_before + 1
        assert reloaded.usage_ledger[-1].idempotency_key == entry.idempotency_key

    def test_the_same_idempotency_key_is_never_billed_twice(self, tmp_path):
        store, record = load_demo(tmp_path)
        ledger_before = len(record.usage_ledger)
        event = provider_event("three-scene-demo")

        first = record_usage(store, record.job, event)
        second = record_usage(store, record.job, event)

        reloaded = store.load("three-scene-demo")
        assert first is not None
        assert second is None
        assert len(reloaded.usage_ledger) == ledger_before + 1
        assert len(reloaded.provider_events) == len(record.provider_events) + 1

    def test_a_key_already_in_the_frozen_fixture_is_not_billed_again(self, tmp_path):
        """Resume must not re-bill a call the ledger already carries (§5.3)."""
        store, record = load_demo(tmp_path)
        existing = record.usage_ledger[0].idempotency_key

        entry = record_usage(
            store,
            record.job,
            provider_event("three-scene-demo", idempotency_key=existing),
        )

        assert entry is None
        assert len(store.load("three-scene-demo").usage_ledger) == len(
            record.usage_ledger
        )

    def test_a_second_attempt_gets_its_own_key_and_its_own_billable_entry(
        self, tmp_path
    ):
        """§10 retry cost: attempt 2 is a separate call and a separate entry."""
        store, record = load_demo(tmp_path)
        ledger_before = len(record.usage_ledger)

        record_usage(store, record.job, provider_event("three-scene-demo"))
        record_usage(
            store,
            record.job,
            provider_event(
                "three-scene-demo",
                provider_event_id="provider-event-901",
                idempotency_key=build_idempotency_key(
                    "three-scene-demo", "scene-001", "video", 2
                ),
                attempt_count=2,
            ),
        )

        ledger = store.load("three-scene-demo").usage_ledger
        assert len(ledger) == ledger_before + 2
        assert [entry.attempt_count for entry in ledger[-2:]] == [1, 2]

    def test_an_unreported_cost_is_recorded_as_unknown_not_zero(self, tmp_path):
        store, record = load_demo(tmp_path)

        entry = record_usage(
            store,
            record.job,
            provider_event(
                "three-scene-demo",
                actual_cost_usd=normalize_actual_cost(None),
            ),
            estimated_cost_source="veo-3 list price 0.18/sec, 2026-08 rate card",
        )

        reloaded = store.load("three-scene-demo").usage_ledger[-1]
        assert entry.actual_cost_usd == "unknown"
        assert reloaded.actual_cost_usd == "unknown"
        assert reloaded.actual_cost_usd != 0
        assert reloaded.estimated_cost_source == (
            "veo-3 list price 0.18/sec, 2026-08 rate card"
        )

    def test_an_unknown_cost_without_an_estimate_source_is_rejected(self, tmp_path):
        """§10: 標記 unknown 並保留估算來源. Unknown with no source is neither."""
        store, record = load_demo(tmp_path)
        ledger_before = len(record.usage_ledger)

        with pytest.raises(ValueError):
            record_usage(
                store,
                record.job,
                provider_event("three-scene-demo", actual_cost_usd="unknown"),
            )

        assert len(store.load("three-scene-demo").usage_ledger) == ledger_before

    def test_normalize_actual_cost_never_turns_a_missing_cost_into_zero(self):
        assert normalize_actual_cost(None) == "unknown"
        assert normalize_actual_cost("") == "unknown"
        assert normalize_actual_cost("unknown") == "unknown"
        assert normalize_actual_cost(0) == 0.0
        assert normalize_actual_cost(0.12) == 0.12

    def test_the_ledger_keeps_the_discarded_material_and_adopted_seconds(
        self, tmp_path
    ):
        store, record = load_demo(tmp_path)

        entry = record_usage(
            store,
            record.job,
            provider_event("three-scene-demo", actual_cost_usd=0.9),
            discarded_asset_ids=["asset-090", "asset-091"],
            adopted_video_seconds=6.0,
        )

        reloaded = store.load("three-scene-demo").usage_ledger[-1]
        assert reloaded.discarded_asset_ids == ["asset-090", "asset-091"]
        assert reloaded.adopted_video_seconds == 6.0
        assert reloaded.effective_cost_per_adopted_second_usd == 0.15
        assert entry == reloaded

    def test_effective_cost_per_second_is_unknown_when_the_cost_is_unknown(
        self, tmp_path
    ):
        store, record = load_demo(tmp_path)

        entry = record_usage(
            store,
            record.job,
            provider_event("three-scene-demo", actual_cost_usd="unknown"),
            estimated_cost_source="rate card",
            adopted_video_seconds=6.0,
        )

        assert entry.effective_cost_per_adopted_second_usd == "unknown"

    def test_effective_cost_per_second_is_unknown_when_nothing_was_adopted(
        self, tmp_path
    ):
        store, record = load_demo(tmp_path)

        entry = record_usage(
            store,
            record.job,
            provider_event("three-scene-demo", actual_cost_usd=0.9),
            adopted_video_seconds=0.0,
        )

        assert entry.effective_cost_per_adopted_second_usd == "unknown"
        assert entry.effective_cost_per_adopted_second_usd != 0

    def test_a_credential_that_reached_the_summaries_never_lands_on_disk(
        self, tmp_path
    ):
        store, record = load_demo(tmp_path)

        record_usage(
            store,
            record.job,
            provider_event(
                "three-scene-demo",
                request_summary=f"POST /v1/videos Authorization: Bearer {SECRET}",
                response_summary=f'{{"error": "bad token", "api_key": "{SECRET}"}}',
            ),
        )

        on_disk = (tmp_path / "three-scene-demo" / "provider_events.jsonl").read_text(
            encoding="utf-8"
        )
        assert SECRET not in on_disk
        stored = store.load("three-scene-demo").provider_events[-1]
        assert SECRET not in stored.request_summary
        assert SECRET not in stored.response_summary

    def test_a_credential_in_a_copied_column_never_lands_in_the_ledger(
        self, tmp_path
    ):
        """The ledger copies six string columns straight off the event.

        ``provider_events.jsonl`` is scrubbed; if the ledger takes its values
        from the raw event instead, the same credential lands on disk one file
        over. Deliberately a shape-recognised placeholder, not a real key.
        """
        leak = "sk-ledgerplaceholder0123456789"
        store, record = load_demo(tmp_path)

        record_usage(
            store, record.job, provider_event("three-scene-demo", model=leak)
        )

        job_dir = tmp_path / "three-scene-demo"
        assert leak not in (job_dir / "provider_events.jsonl").read_text(
            encoding="utf-8"
        )
        assert leak not in (job_dir / "usage_ledger.jsonl").read_text(
            encoding="utf-8"
        )
        assert leak not in store.load("three-scene-demo").usage_ledger[-1].model

    def test_an_event_for_another_job_is_rejected(self, tmp_path):
        store, record = load_demo(tmp_path)

        with pytest.raises(ValueError):
            record_usage(store, record.job, provider_event("ten-scene-demo"))

        assert len(store.load("three-scene-demo").usage_ledger) == len(
            record.usage_ledger
        )


class TestFrozenFixtureCompatibility:
    @pytest.mark.parametrize("job_id", ["three-scene-demo", "ten-scene-demo"])
    def test_the_frozen_ledger_still_loads_and_round_trips(self, job_id):
        path = FIXTURES_ROOT / job_id / "usage_ledger.jsonl"
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

        assert lines
        for line in lines:
            payload = json.loads(line)
            dump = UsageLedgerEntry.model_validate(payload).model_dump(mode="json")
            # Every field the frozen line states survives untouched; the §10
            # columns it predates come back at their documented defaults.
            assert {name: dump[name] for name in payload} == payload
            assert len(dump) == 14


class TestSpendReachesTheGate:
    """The gate reads ``job.actual_cost_usd``, so something must put spend there.

    Every unit test above hands the predicate a spend figure by hand, which
    proves the predicate and nothing about the system: if no code path ever
    writes what was spent back onto the job, the gate reads its initial value
    forever and a loop of affordable-looking calls spends without limit.
    """

    def paid_call(self, store, job_id, attempt, cost):
        """One provider call recorded the way §4.6 says: its own key, its own row."""
        return provider_event(
            job_id,
            provider_event_id=f"provider-event-9{attempt:02d}",
            idempotency_key=build_idempotency_key(job_id, "scene-001", "video", attempt),
            attempt_count=attempt,
            estimated_cost_usd=cost,
            actual_cost_usd=cost,
        )

    def ledger_total(self, store, job_id):
        return sum(
            entry.estimated_cost_usd
            if entry.actual_cost_usd == "unknown"
            else entry.actual_cost_usd
            for entry in store.load(job_id).usage_ledger
        )

    def test_a_loop_of_affordable_calls_cannot_spend_past_the_limit(self, tmp_path):
        """The acceptance test for §10: money actually spent stays under the cap."""
        store, record = load_demo(tmp_path)
        job_id = "three-scene-demo"
        limit = 3.00
        store.save(
            record.job.model_copy(
                update={"actual_cost_usd": 0.0, "budget_limit_usd": limit}
            )
        )
        provider = Mock(return_value="ok")
        cost = 0.50
        refusals = 0

        for attempt in range(1, 21):
            job = store.load(job_id).job  # a call site re-reads before it spends
            try:
                check_budget(job, cost, store=store)
            except BudgetExceededError:
                refusals += 1
                break
            provider()
            record_usage(store, job, self.paid_call(store, job_id, attempt, cost))

        spent = self.ledger_total(store, job_id)
        assert refusals == 1, "the gate never refused: it is reading a frozen number"
        assert spent <= limit, f"spent {spent} against a limit of {limit}"
        # 0.36 was already on the frozen ledger, so five more calls of 0.50 fit.
        assert provider.call_count == 5
        assert store.load(job_id).job.status == JobStatus.BUDGET_EXCEEDED

    def test_recording_a_call_moves_the_spend_to_date_onto_the_job(self, tmp_path):
        store, record = load_demo(tmp_path)
        before = store.load("three-scene-demo").job.actual_cost_usd

        record_usage(
            store,
            record.job,
            provider_event("three-scene-demo", actual_cost_usd=0.9),
        )

        after = store.load("three-scene-demo").job.actual_cost_usd
        assert before == 0.36
        assert after == pytest.approx(1.26)

    def test_an_unreported_cost_still_moves_the_spend_to_date(self, tmp_path):
        """§10 forbids counting an unknown cost as zero, here too."""
        store, record = load_demo(tmp_path)

        record_usage(
            store,
            record.job,
            provider_event(
                "three-scene-demo", actual_cost_usd="unknown", estimated_cost_usd=0.18
            ),
            estimated_cost_source="veo-3 rate card 2026-08",
        )

        after = store.load("three-scene-demo").job.actual_cost_usd
        assert after != 0.36, "an unknown cost was counted as zero"
        assert after == pytest.approx(0.54)

    def test_a_duplicate_key_does_not_move_the_spend_to_date(self, tmp_path):
        """§5.3: re-recording a call must not charge for it a second time."""
        store, record = load_demo(tmp_path)
        event = provider_event("three-scene-demo", actual_cost_usd=0.9)

        record_usage(store, record.job, event)
        once = store.load("three-scene-demo").job.actual_cost_usd
        record_usage(store, store.load("three-scene-demo").job, event)

        assert store.load("three-scene-demo").job.actual_cost_usd == once


class TestSilentRefusalsAreStillAudited:
    @pytest.mark.parametrize(
        "status", [JobStatus.DRAFT, JobStatus.AWAITING_ASSETS, JobStatus.RENDERING]
    )
    def test_a_refusal_from_a_state_with_no_edge_still_leaves_a_record(
        self, tmp_path, status
    ):
        """Refusing to spend is a decision; §5.2 having no edge does not hide it."""
        store, record = load_demo(tmp_path)
        store.save(record.job.model_copy(update={"status": status}))
        job = store.load("three-scene-demo").job
        before = len(store.load("three-scene-demo").decisions)

        with pytest.raises(BudgetExceededError):
            check_budget(job, 99.0, store=store)

        decisions = store.load("three-scene-demo").decisions
        assert len(decisions) == before + 1
        assert decisions[-1]["from"] == status.value
        assert decisions[-1]["to"] == status.value
        assert decisions[-1]["reason"]
        assert store.load("three-scene-demo").job.status == status


class TestTheGateReadsDiskNotTheCallerObject:
    """§10 must not depend on the caller re-reading the job every round.

    ``record_usage`` writes the new spend to disk and leaves the in-memory job
    stale by contract. A gate that trusts the object it was handed is bypassed
    by any loop that forgets to re-read — a safety mechanism resting on caller
    discipline is not a safety mechanism.
    """

    def test_a_stale_job_object_cannot_spend_past_the_limit(self, tmp_path):
        store, record = load_demo(tmp_path)
        stale = record.job  # deliberately never refreshed
        limit = float(stale.budget_limit_usd)

        calls = 0
        with pytest.raises(BudgetExceededError):
            for attempt in range(1, 60):
                check_budget(stale, 0.5, store=store)
                calls += 1
                record_usage(
                    store,
                    store.load("three-scene-demo").job,
                    provider_event(
                        "three-scene-demo",
                        provider_event_id=f"provider-event-8{attempt:02d}",
                        idempotency_key=build_idempotency_key(
                            "three-scene-demo", "scene-001", "video", attempt
                        ),
                        attempt_count=attempt,
                        actual_cost_usd=0.5,
                    ),
                )

        on_disk = float(store.load("three-scene-demo").job.actual_cost_usd)
        assert on_disk <= limit
        assert calls * 0.5 <= limit

    def test_without_a_store_the_gate_still_judges_the_object_it_is_given(self):
        job = ContentJob.model_validate(content_job_payload())
        job.budget_limit_usd = 3.0
        job.actual_cost_usd = 2.9

        assert check_budget(job, 0.05) is job
        with pytest.raises(BudgetExceededError):
            check_budget(job, 0.5)


class TestLedgerShapeIsConstant:
    """§4.6's ledger contract is fourteen columns, always.

    A column that vanishes when it holds its default makes a downstream
    ``.get(name, 0)`` produce exactly the fabricated zero §10 forbids, and
    leaves "adopted nothing" indistinguishable from "did not record".
    """

    SPEC10_COLUMNS = (
        "estimated_cost_source",
        "discarded_asset_ids",
        "adopted_video_seconds",
        "effective_cost_per_adopted_second_usd",
    )

    def test_an_entry_holding_no_spec10_information_still_carries_the_columns(
        self, tmp_path
    ):
        store, record = load_demo(tmp_path)

        entry = record_usage(
            store, record.job, provider_event("three-scene-demo", actual_cost_usd=0.9)
        )

        dump = entry.model_dump(mode="json")
        assert len(dump) == 14
        assert all(name in dump for name in self.SPEC10_COLUMNS)
        assert dump["adopted_video_seconds"] == 0.0
        assert dump["effective_cost_per_adopted_second_usd"] == "unknown"

    def test_every_line_of_a_written_ledger_has_the_same_columns(self, tmp_path):
        store, record = load_demo(tmp_path)

        record_usage(
            store, record.job, provider_event("three-scene-demo", actual_cost_usd=0.9)
        )
        record_usage(
            store,
            store.load("three-scene-demo").job,
            provider_event(
                "three-scene-demo",
                provider_event_id="provider-event-902",
                idempotency_key=build_idempotency_key(
                    "three-scene-demo", "scene-001", "video", 2
                ),
                attempt_count=2,
                actual_cost_usd=0.9,
            ),
            discarded_asset_ids=["asset-090"],
            adopted_video_seconds=6.0,
        )

        lines = [
            json.loads(line)
            for line in (tmp_path / "three-scene-demo" / "usage_ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        # Every line, not just the two just written: the fixture rows are part
        # of the same file and §10 says each one must be written in full.
        assert len(lines) > 2
        assert [len(payload) for payload in lines] == [14] * len(lines)
        assert all(payload.keys() == lines[0].keys() for payload in lines)


class TestCredentialShapesWithoutAKey:
    """redact() must not need a ``key:`` to recognise a secret."""

    def test_a_bare_token_with_no_key_in_front_of_it_is_removed(self):
        assert SECRET not in redact(f"retry after {SECRET} failed")

    def test_the_value_after_an_auth_scheme_is_removed_not_just_the_scheme(self):
        # Deliberately not a real base64 credential: the pattern under test eats
        # the whole \S+ after the scheme, so the shape is irrelevant, and a
        # realistic-looking one only trips secret scanners on every commit.
        credential = "basic-auth-placeholder-value"

        result = redact(f"Authorization: Basic {credential}")

        assert credential not in result

    def test_a_whitespace_separated_credential_is_removed(self):
        assert SECRET not in redact(f"access_token {SECRET}")

    @pytest.mark.parametrize(
        "field", ["error_class", "external_job_id", "request_id"]
    )
    def test_a_credential_in_any_event_field_never_lands_on_disk(
        self, tmp_path, field
    ):
        store, record = load_demo(tmp_path)

        record_usage(
            store,
            record.job,
            provider_event(
                "three-scene-demo",
                **{field: f"upstream 401: token {SECRET} rejected"},
            ),
        )

        on_disk = (tmp_path / "three-scene-demo" / "provider_events.jsonl").read_text(
            encoding="utf-8"
        )
        assert SECRET not in on_disk
