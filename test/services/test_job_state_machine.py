"""SPEC-001 §5 job state machine: legal transitions, guards and error classes.

The §5.2 table is transcribed literally here rather than imported from the
implementation, so a wrong table in ``state_machine`` cannot make its own tests
pass.
"""

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import requests
from pydantic import ValidationError

import app.services.jobs.state_machine
from app.models.content_job import ContentJob, JobStatus
from app.services.jobs.state_machine import (
    TRANSITIONS,
    BudgetExceededError,
    ErrorClass,
    IllegalTransitionError,
    ResumeError,
    UnauthorizedAssetError,
    classify_error,
    decision_record,
    is_legal,
    resume_target,
    transition,
)
from app.services.jobs.store import JobStore, JobStoreError
from test.services.test_content_job_models import content_job_payload

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"

S = JobStatus

#: SPEC-001 §5.2, the eleven explicit rows, transcribed by hand.
LINEAR_TABLE = [
    (S.DRAFT, S.SCRIPTING),
    (S.SCRIPTING, S.SCENE_PLANNING),
    (S.SCENE_PLANNING, S.VOICE_GENERATING),
    (S.VOICE_GENERATING, S.AWAITING_ASSETS),
    (S.AWAITING_ASSETS, S.READY_TO_RENDER),
    (S.READY_TO_RENDER, S.RENDERING),
    (S.RENDERING, S.TECHNICAL_QA),
    (S.TECHNICAL_QA, S.CONTENT_QA),
    (S.CONTENT_QA, S.READY_FOR_REVIEW),
    (S.READY_FOR_REVIEW, S.POSTIZ_DRAFTING),
    (S.POSTIZ_DRAFTING, S.POSTIZ_DRAFTED),
]

#: SPEC-001 §5.2 row "任一可重試階段" — the stages that run work which can fail
#: transiently (a provider call, or the render).
RETRYABLE_SOURCES = [
    S.RESEARCHING,
    S.SCRIPTING,
    S.SCENE_PLANNING,
    S.VOICE_GENERATING,
    S.IMAGE_GENERATING,
    S.VIDEO_GENERATING,
    S.RENDERING,
    S.POSTIZ_DRAFTING,
]

#: SPEC-001 §5.2 row "任一生成階段" — the stages that spend provider budget, plus
#: READY_TO_RENDER, whose §5.2 row is conditioned on "Render Manifest 通過且預算閘門
#: 通過": the gate can refuse there, so that state needs a BUDGET_EXCEEDED edge too.
GENERATING_SOURCES = [
    S.RESEARCHING,
    S.SCRIPTING,
    S.SCENE_PLANNING,
    S.VOICE_GENERATING,
    S.IMAGE_GENERATING,
    S.VIDEO_GENERATING,
    S.READY_TO_RENDER,
]

#: SPEC-001 §5.2 row "RETRYABLE_FAILED → 該次失敗的可恢復階段" — 任一可重試階段
#: minus POSTIZ_DRAFTING, which re-entry would bill twice because the publisher
#: reads no idempotency key, and minus SCENE_PLANNING for the reason below.
RESUMABLE_TARGETS = [
    S.RESEARCHING,
    S.SCRIPTING,
    S.VOICE_GENERATING,
    S.IMAGE_GENERATING,
    S.VIDEO_GENERATING,
    S.RENDERING,
]

#: SPEC-001 §5.2 row "MANUAL_ACTION_REQUIRED → 該次中斷的可恢復階段" — the three
#: stages that actually park there, plus READY_TO_RENDER, the budget gate's
#: refusal landing spot. SCENE_PLANNING is in neither return set: re-entry
#: rebuilds the scene list and discards human-imported assets.
MANUAL_RETURN_TARGETS = [
    S.SCRIPTING,
    S.VOICE_GENERATING,
    S.AWAITING_ASSETS,
    S.READY_TO_RENDER,
]

#: A job in one of these is finished; §5.2 offers no row out of them.
TERMINAL = [S.PUBLISHED, S.FAILED, S.CANCELLED]

ALL_STATES = list(JobStatus)


def build_job(status=S.DRAFT, job_id="job-20260816-001"):
    return ContentJob.model_validate(
        {**content_job_payload(), "content_job_id": job_id, "status": status.value}
    )


class TestTheTwentyThreeStates:
    def test_the_enum_still_carries_every_spec_state(self):
        assert len(ALL_STATES) == 23


class TestLegalTransitions:
    @pytest.mark.parametrize("from_status,to_status", LINEAR_TABLE)
    def test_each_row_of_the_spec_table_is_legal(self, from_status, to_status):
        assert is_legal(from_status, to_status) is True

    @pytest.mark.parametrize("from_status,to_status", LINEAR_TABLE)
    def test_each_row_of_the_spec_table_can_be_applied(self, from_status, to_status):
        job = build_job(from_status)

        updated = transition(job, to_status, reason="spec row")

        assert updated.status == to_status

    def test_transition_returns_a_new_job_and_leaves_the_input_alone(self):
        job = build_job(S.DRAFT)

        updated = transition(job, S.SCRIPTING, reason="input validated")

        assert job.status == S.DRAFT
        assert updated is not job
        assert updated.content_job_id == job.content_job_id

    def test_transition_stamps_updated_at_with_the_supplied_clock(self):
        job = build_job(S.DRAFT)

        updated = transition(
            job, S.SCRIPTING, reason="input validated", now="2026-08-16T10:00:00+00:00"
        )

        assert updated.updated_at == "2026-08-16T10:00:00+00:00"
        assert updated.created_at == job.created_at

    def test_transition_defaults_to_an_iso_utc_timestamp(self):
        job = build_job(S.DRAFT)

        updated = transition(job, S.SCRIPTING, reason="input validated")

        assert updated.updated_at.endswith("+00:00")
        assert updated.updated_at != job.updated_at

    def test_a_transition_to_the_same_state_is_rejected(self):
        assert is_legal(S.SCRIPTING, S.SCRIPTING) is False

    def test_a_blank_reason_is_rejected(self):
        job = build_job(S.DRAFT)

        with pytest.raises(ValueError) as raised:
            transition(job, S.SCRIPTING, reason="   ")

        assert "reason" in str(raised.value)

    @pytest.mark.parametrize("reason", [None, 0, 1, [], object()])
    def test_a_non_string_reason_is_rejected(self, reason):
        job = build_job(S.DRAFT)

        with pytest.raises(ValueError) as raised:
            transition(job, S.SCRIPTING, reason=reason)

        assert "reason" in str(raised.value)

    def test_status_strings_are_accepted_as_well_as_enum_members(self):
        assert is_legal("DRAFT", "SCRIPTING") is True
        assert is_legal("DRAFT", "RENDERING") is False

    def test_an_unknown_status_name_is_rejected(self):
        with pytest.raises(ValueError):
            is_legal("DRAFT", "NOT_A_STATE")


def expected_edges():
    """Every edge SPEC-001 §5.2 allows, rebuilt from the hand-transcribed rows above."""
    edges = set(LINEAR_TABLE)
    edges |= {(s, S.RETRYABLE_FAILED) for s in RETRYABLE_SOURCES}
    edges |= {(s, S.BUDGET_EXCEEDED) for s in GENERATING_SOURCES}
    edges |= {(s, S.CANCELLED) for s in ALL_STATES if s not in TERMINAL}
    edges |= {
        (s, S.MANUAL_ACTION_REQUIRED)
        for s in ALL_STATES
        if s not in TERMINAL and s is not S.MANUAL_ACTION_REQUIRED
    }
    edges |= {(S.RETRYABLE_FAILED, t) for t in RESUMABLE_TARGETS}
    edges.add((S.RETRYABLE_FAILED, S.FAILED))
    edges |= {(S.MANUAL_ACTION_REQUIRED, t) for t in MANUAL_RETURN_TARGETS}
    return edges


class TestTheWholeTableExhaustively:
    """§5.2 must hold edge for edge: an extra edge is as wrong as a missing one."""

    def test_the_spec_allows_exactly_seventy_six_edges(self):
        assert len(expected_edges()) == 76

    @pytest.mark.parametrize("from_status", ALL_STATES)
    def test_every_pair_of_states_matches_the_spec(self, from_status):
        expected = expected_edges()
        for to_status in ALL_STATES:
            assert is_legal(from_status, to_status) is (
                (from_status, to_status) in expected
            ), f"{from_status.value} -> {to_status.value}"

    def test_the_implementation_has_no_edge_the_spec_does_not(self):
        actual = {(a, b) for a in ALL_STATES for b in ALL_STATES if is_legal(a, b)}
        expected = expected_edges()

        assert sorted((a.value, b.value) for a, b in actual - expected) == []
        assert sorted((a.value, b.value) for a, b in expected - actual) == []


class TestTheTableCannotBeRewrittenByCallers:
    def test_transitions_rejects_assignment(self):
        with pytest.raises(TypeError):
            TRANSITIONS[S.DRAFT] = frozenset({S.PUBLISHED})

    def test_transitions_rejects_deletion(self):
        with pytest.raises(TypeError):
            del TRANSITIONS[S.DRAFT]


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (S.DRAFT, S.RENDERING),
            (S.AWAITING_ASSETS, S.TECHNICAL_QA),
            (S.SCRIPTING, S.DRAFT),
            (S.POSTIZ_DRAFTED, S.APPROVED),
            (S.READY_TO_RENDER, S.CONTENT_QA),
        ],
    )
    def test_transitions_outside_the_table_are_rejected(self, from_status, to_status):
        assert is_legal(from_status, to_status) is False

    def test_the_error_names_both_the_source_and_the_target(self):
        job = build_job(S.DRAFT)

        with pytest.raises(IllegalTransitionError) as raised:
            transition(job, S.RENDERING, reason="skip ahead")

        message = str(raised.value)
        assert "DRAFT" in message
        assert "RENDERING" in message

    def test_the_illegal_transition_error_is_a_value_error(self):
        assert issubclass(IllegalTransitionError, ValueError)

    @pytest.mark.parametrize("from_status", TERMINAL)
    @pytest.mark.parametrize("to_status", ALL_STATES)
    def test_terminal_states_have_no_outbound_edge(self, from_status, to_status):
        assert is_legal(from_status, to_status) is False


class TestPublishedIsUnreachable:
    @pytest.mark.parametrize("from_status", ALL_STATES)
    def test_no_state_can_reach_published(self, from_status):
        assert is_legal(from_status, S.PUBLISHED) is False

    @pytest.mark.parametrize("from_status", ALL_STATES)
    def test_transitioning_to_published_always_raises(self, from_status):
        job = build_job(from_status)

        with pytest.raises(IllegalTransitionError):
            transition(job, S.PUBLISHED, reason="v0 must not publish")

    @pytest.mark.parametrize("from_status", ALL_STATES)
    def test_approved_and_scheduled_are_also_unreachable_in_v0(self, from_status):
        assert is_legal(from_status, S.APPROVED) is False
        assert is_legal(from_status, S.SCHEDULED) is False


class TestCrossCuttingTargets:
    @pytest.mark.parametrize("from_status", RETRYABLE_SOURCES)
    def test_every_retryable_stage_reaches_retryable_failed(self, from_status):
        assert is_legal(from_status, S.RETRYABLE_FAILED) is True

    @pytest.mark.parametrize(
        "from_status",
        [S.DRAFT, S.AWAITING_ASSETS, S.READY_TO_RENDER, S.READY_FOR_REVIEW, S.FAILED],
    )
    def test_stages_that_run_no_retryable_work_do_not(self, from_status):
        assert is_legal(from_status, S.RETRYABLE_FAILED) is False

    @pytest.mark.parametrize("from_status", GENERATING_SOURCES)
    def test_every_generating_stage_reaches_budget_exceeded(self, from_status):
        assert is_legal(from_status, S.BUDGET_EXCEEDED) is True

    @pytest.mark.parametrize(
        "from_status",
        [S.DRAFT, S.AWAITING_ASSETS, S.RENDERING, S.TECHNICAL_QA, S.CANCELLED],
    )
    def test_stages_that_spend_no_budget_do_not(self, from_status):
        assert is_legal(from_status, S.BUDGET_EXCEEDED) is False

    @pytest.mark.parametrize(
        "from_status", [s for s in ALL_STATES if s not in TERMINAL + [S.MANUAL_ACTION_REQUIRED]]
    )
    def test_any_unfinished_stage_reaches_manual_action_required(self, from_status):
        assert is_legal(from_status, S.MANUAL_ACTION_REQUIRED) is True

    @pytest.mark.parametrize("from_status", TERMINAL + [S.MANUAL_ACTION_REQUIRED])
    def test_finished_jobs_do_not_reach_manual_action_required(self, from_status):
        assert is_legal(from_status, S.MANUAL_ACTION_REQUIRED) is False

    @pytest.mark.parametrize("from_status", [s for s in ALL_STATES if s not in TERMINAL])
    def test_any_unfinished_stage_can_be_cancelled(self, from_status):
        assert is_legal(from_status, S.CANCELLED) is True

    @pytest.mark.parametrize("from_status", TERMINAL)
    def test_a_finished_job_cannot_be_cancelled(self, from_status):
        assert is_legal(from_status, S.CANCELLED) is False

    def test_the_cross_cutting_targets_can_actually_be_applied(self):
        assert transition(
            build_job(S.VIDEO_GENERATING), S.RETRYABLE_FAILED, reason="429"
        ).status == S.RETRYABLE_FAILED
        assert transition(
            build_job(S.VIDEO_GENERATING), S.BUDGET_EXCEEDED, reason="over budget"
        ).status == S.BUDGET_EXCEEDED
        assert transition(
            build_job(S.AWAITING_ASSETS), S.MANUAL_ACTION_REQUIRED, reason="need assets"
        ).status == S.MANUAL_ACTION_REQUIRED
        assert transition(
            build_job(S.RENDERING), S.CANCELLED, reason="user cancelled"
        ).status == S.CANCELLED


class TestDecisionRecord:
    def test_it_carries_from_to_reason_and_a_timestamp(self):
        job = build_job(S.DRAFT)
        updated = transition(
            job, S.SCRIPTING, reason="input validated", now="2026-08-16T10:00:00+00:00"
        )

        record = decision_record(job.status, updated, "input validated")

        assert record == {
            "from": "DRAFT",
            "to": "SCRIPTING",
            "reason": "input validated",
            "at": "2026-08-16T10:00:00+00:00",
        }

    def test_it_is_json_serialisable(self):
        job = build_job(S.DRAFT)
        updated = transition(job, S.SCRIPTING, reason="input validated")

        assert json.loads(json.dumps(decision_record(job.status, updated, "ok")))


class TestClassifyError:
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("connection reset"),
            TimeoutError("provider timed out"),
            requests.ConnectionError("dns failure"),
            requests.Timeout("read timeout"),
            requests.ConnectTimeout("connect timeout"),
        ],
    )
    def test_network_and_timeout_errors_are_retryable(self, exc):
        assert classify_error(exc) is ErrorClass.RETRYABLE

    def test_a_429_carried_on_the_exception_is_retryable(self):
        exc = RuntimeError("too many requests")
        exc.status_code = 429

        assert classify_error(exc) is ErrorClass.RETRYABLE

    def test_a_429_carried_on_the_response_is_retryable(self):
        response = requests.Response()
        response.status_code = 429
        exc = requests.HTTPError("429 Too Many Requests", response=response)

        assert classify_error(exc) is ErrorClass.RETRYABLE

    def test_a_403_is_not_retryable_even_though_it_is_an_http_error(self):
        response = requests.Response()
        response.status_code = 403
        exc = requests.HTTPError("403 Forbidden", response=response)

        assert classify_error(exc) is ErrorClass.NON_RETRYABLE

    def test_a_provider_error_flagged_retryable_is_retryable(self):
        exc = RuntimeError("upstream hiccup")
        exc.retryable = True

        assert classify_error(exc) is ErrorClass.RETRYABLE

    def test_a_provider_error_flagged_not_retryable_is_not(self):
        exc = RuntimeError("bad request")
        exc.retryable = False

        assert classify_error(exc) is ErrorClass.NON_RETRYABLE

    def test_a_schema_error_is_not_retryable(self):
        with pytest.raises(ValidationError) as raised:
            ContentJob.model_validate({"content_job_id": "x"})

        assert classify_error(raised.value) is ErrorClass.NON_RETRYABLE

    def test_a_file_format_error_is_not_retryable(self):
        assert classify_error(JobStoreError("job.json is not readable UTF-8 JSON")) is (
            ErrorClass.NON_RETRYABLE
        )

    def test_a_permission_error_is_not_retryable(self):
        assert classify_error(PermissionError("denied")) is ErrorClass.NON_RETRYABLE

    def test_a_budget_overrun_is_not_retryable(self):
        assert classify_error(BudgetExceededError("0.9 > 0.5")) is ErrorClass.NON_RETRYABLE

    def test_an_unauthorized_asset_is_not_retryable(self):
        assert classify_error(UnauthorizedAssetError("consent revoked")) is (
            ErrorClass.NON_RETRYABLE
        )

    @pytest.mark.parametrize("flag", [lambda: True, "yes", 1, 0, object()])
    def test_a_non_bool_retryable_attribute_is_not_a_verdict(self, flag):
        exc = RuntimeError("has a retryable-looking attribute")
        exc.retryable = flag

        assert classify_error(exc) is ErrorClass.UNKNOWN

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("ffprobe: Invalid data found when processing input"),
            FileNotFoundError("clip.mp4"),
            IsADirectoryError("assets/"),
        ],
    )
    def test_a_media_file_format_error_is_not_retryable(self, exc):
        assert classify_error(exc) is ErrorClass.NON_RETRYABLE

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionError("connection reset"),
            TimeoutError("provider timed out"),
            requests.ConnectionError("dns failure"),
            requests.Timeout("read timeout"),
        ],
    )
    def test_network_errors_stay_retryable_despite_being_os_errors(self, exc):
        # ConnectionError and TimeoutError are OSError subclasses: the retryable
        # branch must be reached before the OSError one.
        assert isinstance(exc, OSError)
        assert classify_error(exc) is ErrorClass.RETRYABLE

    def test_a_failed_ffmpeg_subprocess_is_not_retried(self):
        exc = subprocess.CalledProcessError(1, ["ffprobe", "clip.mp4"])

        assert classify_error(exc).is_retryable is False

    @pytest.mark.parametrize("cls", [BudgetExceededError, UnauthorizedAssetError])
    def test_the_pipeline_errors_follow_the_repo_value_error_convention(self, cls):
        assert issubclass(cls, ValueError)

    def test_an_unrecognised_error_is_neither_retried_nor_declared_fatal(self):
        assert classify_error(RuntimeError("who knows")) is ErrorClass.UNKNOWN

    def test_only_the_retryable_class_may_be_retried(self):
        assert ErrorClass.RETRYABLE.is_retryable is True
        assert ErrorClass.NON_RETRYABLE.is_retryable is False
        assert ErrorClass.UNKNOWN.is_retryable is False


class TestPurity:
    """The module decides; the store persists. Checked on the parsed module, not
    on its prose, so a docstring mentioning a path cannot fail this."""

    def _module_ast(self):
        source = (
            Path(app.services.jobs.state_machine.__file__)
        ).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_it_imports_no_filesystem_or_store_module(self):
        imported = set()
        for node in ast.walk(self._module_ast()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not imported & {
            "os",
            "io",
            "pathlib",
            "shutil",
            "tempfile",
            "app.services.jobs.store",
        }

    def test_it_never_calls_open(self):
        called = {
            node.func.id
            for node in ast.walk(self._module_ast())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "open" not in called


class TestTransitionWithTheStore:
    def test_a_transition_persists_one_decision_next_to_the_frozen_fixture_job(
        self, tmp_path
    ):
        shutil.copytree(FIXTURES_ROOT / "three-scene-demo", tmp_path / "three-scene-demo")
        store = JobStore(tmp_path)
        record = store.load("three-scene-demo")
        before = len(record.decisions)

        updated = transition(
            record.job, S.RENDERING, reason="manifest and budget gate passed"
        )
        store.save(updated)
        store.append_decision(
            "three-scene-demo", decision_record(record.job.status, updated, "manifest and budget gate passed")
        )

        reloaded = store.load("three-scene-demo")
        assert reloaded.job.status == S.RENDERING
        assert len(reloaded.decisions) == before + 1
        assert reloaded.decisions[-1] == {
            "from": "READY_TO_RENDER",
            "to": "RENDERING",
            "reason": "manifest and budget gate passed",
            "at": updated.updated_at,
        }
        # save() only rewrites job.json: the rest of the job must survive.
        assert len(reloaded.scenes) == 3
        assert reloaded.script is not None
        assert reloaded.render_manifest is not None


def line(from_status, to_status, reason="test"):
    """One ``decisions.jsonl`` record, shaped exactly like ``decision_record``."""
    return {
        "from": from_status.value,
        "to": to_status.value,
        "reason": reason,
        "at": "2026-08-16T09:00:00+00:00",
    }


class TestResumeTarget:
    """Which stage a parked job goes back to, derived from its decision log."""

    def test_a_retryable_failure_returns_to_the_stage_that_failed(self):
        decisions = [
            line(S.DRAFT, S.SCRIPTING),
            line(S.SCRIPTING, S.SCENE_PLANNING),
            line(S.SCENE_PLANNING, S.VOICE_GENERATING),
            line(S.VOICE_GENERATING, S.RETRYABLE_FAILED),
        ]

        assert resume_target(S.RETRYABLE_FAILED, decisions) == S.VOICE_GENERATING

    def test_a_manual_park_returns_to_the_stage_that_was_interrupted(self):
        decisions = [
            line(S.VOICE_GENERATING, S.AWAITING_ASSETS),
            line(S.AWAITING_ASSETS, S.MANUAL_ACTION_REQUIRED),
        ]

        assert resume_target(S.MANUAL_ACTION_REQUIRED, decisions) == S.AWAITING_ASSETS

    def test_a_status_string_is_accepted_like_everywhere_else(self):
        decisions = [line(S.RENDERING, S.RETRYABLE_FAILED)]

        assert resume_target("RETRYABLE_FAILED", decisions) == S.RENDERING

    def test_the_park_lines_from_wins_when_its_own_advance_was_never_logged(self):
        # store.save() runs before store.append_decision(); a crash between the
        # two leaves job.json advanced with no line for it. Reading the previous
        # line's "to" here would answer SCENE_PLANNING, which is not a return
        # target at all - so this shape is caught, but only by the return-set
        # check. The next test is the one that pins the rule itself.
        decisions = [
            line(S.DRAFT, S.SCRIPTING),
            line(S.SCRIPTING, S.SCENE_PLANNING),
            line(S.VOICE_GENERATING, S.RETRYABLE_FAILED),
        ]

        assert resume_target(S.RETRYABLE_FAILED, decisions) == S.VOICE_GENERATING

    def test_the_unlogged_advance_is_caught_even_when_the_wrong_answer_is_legal(self):
        # The same crash window on the master-voice advance: AWAITING_ASSETS was
        # saved but never logged, and the captions stage then parked from it.
        # Reading the previous line's "to" answers VOICE_GENERATING, which *is*
        # a legal manual return target - a wrong answer no table check can catch.
        decisions = [
            line(S.SCENE_PLANNING, S.VOICE_GENERATING),
            line(S.AWAITING_ASSETS, S.MANUAL_ACTION_REQUIRED, reason="no timeline"),
        ]

        assert resume_target(S.MANUAL_ACTION_REQUIRED, decisions) == S.AWAITING_ASSETS

    def test_a_no_op_refusal_trace_is_not_a_movement(self):
        # budget.check_budget and the Postiz refusal paths log from == to when a
        # call was blocked without the job moving. Reading that line as a
        # movement here would answer SCENE_PLANNING, which is not a return target.
        decisions = [
            line(S.VOICE_GENERATING, S.AWAITING_ASSETS),
            line(S.AWAITING_ASSETS, S.MANUAL_ACTION_REQUIRED),
            line(S.SCENE_PLANNING, S.SCENE_PLANNING, reason="budget guard refused"),
        ]

        assert resume_target(S.MANUAL_ACTION_REQUIRED, decisions) == S.AWAITING_ASSETS

    def test_chained_parks_are_walked_past_one_after_another(self):
        decisions = [
            line(S.SCENE_PLANNING, S.VOICE_GENERATING),
            line(S.VOICE_GENERATING, S.BUDGET_EXCEEDED),
            line(S.BUDGET_EXCEEDED, S.MANUAL_ACTION_REQUIRED),
        ]

        assert resume_target(S.MANUAL_ACTION_REQUIRED, decisions) == S.VOICE_GENERATING

    def test_budget_exceeded_is_refused_and_points_at_the_two_hop_path(self):
        decisions = [line(S.VOICE_GENERATING, S.BUDGET_EXCEEDED)]

        with pytest.raises(ResumeError) as raised:
            resume_target(S.BUDGET_EXCEEDED, decisions)

        message = str(raised.value)
        assert "BUDGET_EXCEEDED" in message
        assert "MANUAL_ACTION_REQUIRED" in message

    @pytest.mark.parametrize(
        "status", [s for s in ALL_STATES if s not in [S.RETRYABLE_FAILED, S.MANUAL_ACTION_REQUIRED]]
    )
    def test_a_job_that_is_not_parked_has_nothing_to_resume(self, status):
        decisions = [line(S.SCENE_PLANNING, S.VOICE_GENERATING)]

        with pytest.raises(ResumeError):
            resume_target(status, decisions)

    def test_an_empty_decision_log_is_refused(self):
        # DRAFT -> MANUAL_ACTION_REQUIRED is legal but only start_scripting
        # writes the first line, so a parked job with no log is reachable.
        with pytest.raises(ResumeError) as raised:
            resume_target(S.MANUAL_ACTION_REQUIRED, [])

        assert "empty" in str(raised.value)

    @pytest.mark.parametrize(
        "bad",
        [
            {"to": "RETRYABLE_FAILED", "reason": "no from"},
            {"from": "VOICE_GENERATING", "reason": "no to"},
            {"from": "VOICE_GENERATING", "to": "NOT_A_STATE"},
            {"from": "NOT_A_STATE", "to": "RETRYABLE_FAILED"},
        ],
    )
    def test_a_malformed_record_is_refused_rather_than_skipped(self, bad):
        # The line below it would answer SCRIPTING if malformed records were skipped.
        decisions = [line(S.DRAFT, S.SCRIPTING), bad]

        with pytest.raises(ResumeError):
            resume_target(S.RETRYABLE_FAILED, decisions)

    def test_a_log_with_no_stage_in_it_is_refused(self):
        decisions = [line(S.BUDGET_EXCEEDED, S.MANUAL_ACTION_REQUIRED)]

        with pytest.raises(ResumeError):
            resume_target(S.MANUAL_ACTION_REQUIRED, decisions)

    def test_a_stage_the_table_does_not_return_to_is_refused(self):
        # SCENE_PLANNING parks like any other stage but is deliberately not a
        # MANUAL_ACTION_REQUIRED return target.
        decisions = [
            line(S.SCRIPTING, S.SCENE_PLANNING),
            line(S.SCENE_PLANNING, S.MANUAL_ACTION_REQUIRED),
        ]

        with pytest.raises(ResumeError) as raised:
            resume_target(S.MANUAL_ACTION_REQUIRED, decisions)

        message = str(raised.value)
        assert "MANUAL_ACTION_REQUIRED" in message
        assert "SCENE_PLANNING" in message

    def test_postiz_drafting_is_refused_as_a_retryable_return_target(self):
        decisions = [
            line(S.READY_FOR_REVIEW, S.POSTIZ_DRAFTING),
            line(S.POSTIZ_DRAFTING, S.RETRYABLE_FAILED),
        ]

        with pytest.raises(ResumeError):
            resume_target(S.RETRYABLE_FAILED, decisions)

    def test_the_log_is_walked_in_file_order_not_in_at_order(self):
        # "at" is job.updated_at, and transition() lets a caller stamp it, so it
        # is not guaranteed monotonic. Sorting by it walks back into the DRAFT
        # line and answers SCRIPTING - a legal return target, and the wrong one.
        decisions = [
            {**line(S.DRAFT, S.SCRIPTING), "at": "2026-08-16T11:00:00+00:00"},
            {**line(S.SCRIPTING, S.VOICE_GENERATING), "at": "2026-08-16T09:00:00+00:00"},
            {**line(S.VOICE_GENERATING, S.RETRYABLE_FAILED), "at": "2026-08-16T10:00:00+00:00"},
        ]

        assert resume_target(S.RETRYABLE_FAILED, decisions) == S.VOICE_GENERATING

    @pytest.mark.parametrize(
        "status, terminal",
        [
            (S.MANUAL_ACTION_REQUIRED, S.CANCELLED),
            (S.RETRYABLE_FAILED, S.CANCELLED),
            (S.RETRYABLE_FAILED, S.FAILED),
        ],
    )
    def test_a_terminal_state_is_never_handed_back_as_a_return_stage(
        self, status, terminal
    ):
        # CANCELLED and FAILED are legal moves out of a parked job, so a check
        # against the whole §5.2 row would accept them and let a caller asked to
        # restart the job silently kill it instead.
        decisions = [line(S.RENDERING, terminal)]

        with pytest.raises(ResumeError) as raised:
            resume_target(status, decisions)

        assert terminal.value in str(raised.value)

    @pytest.mark.parametrize("job_id", ["three-scene-demo", "ten-scene-demo"])
    def test_the_frozen_fixtures_are_not_parked_so_they_are_refused(self, job_id):
        store = JobStore(FIXTURES_ROOT)
        record = store.load(job_id)

        assert record.job.status == S.READY_TO_RENDER
        with pytest.raises(ResumeError):
            resume_target(record.job.status, record.decisions)
