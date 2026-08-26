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
    UnauthorizedAssetError,
    classify_error,
    decision_record,
    is_legal,
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
    return edges


class TestTheWholeTableExhaustively:
    """§5.2 must hold edge for edge: an extra edge is as wrong as a missing one."""

    def test_the_spec_allows_exactly_sixty_five_edges(self):
        assert len(expected_edges()) == 65

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
