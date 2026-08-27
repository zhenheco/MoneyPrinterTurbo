"""Issue #4: create jobs and generate structured Script JSON."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletion

from app.models.content_job import JobStatus
from app.services.jobs import pipeline
from app.services.jobs.budget import check_budget as real_check_budget
from app.services.jobs.state_machine import BudgetExceededError, IllegalTransitionError
from app.services.jobs.pipeline import (
    JobInputError,
    ScriptGenerationError,
    create_job,
    generate_script,
    start_scripting,
)
from app.services.jobs.store import JobStore

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"


@pytest.fixture(autouse=True)
def configured_llm():
    with patch.dict(
        pipeline.config.app,
        {"llm_provider": "deepseek", "deepseek_model_name": "deepseek-v4-pro"},
        clear=True,
    ):
        yield


def valid_request(**overrides):
    request = {
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "topic": "企業導入AI最常犯的三個錯誤",
        "target_duration_sec": 50,
        "language": "zh-TW",
        "image_mode": "assisted_qwen",
        "video_mode": "manual_google_flow",
        "max_generated_video_scenes": 3,
        "publish_mode": "postiz_draft",
        "budget_limit_usd": 3,
    }
    request.update(overrides)
    return request


def test_create_job_persists_a_draft_that_loads_back(tmp_path):
    store = JobStore(tmp_path)
    request = valid_request()

    job = create_job(request, store)

    assert job.status is JobStatus.DRAFT
    for field in request:
        assert getattr(job, field) == request[field]
    assert (tmp_path / job.content_job_id / "job.json").is_file()
    assert store.load(job.content_job_id).job == job


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "brand_id",
        "topic",
        "target_duration_sec",
        "language",
        "image_mode",
        "video_mode",
        "max_generated_video_scenes",
        "publish_mode",
        "budget_limit_usd",
    ],
)
def test_create_job_rejects_each_missing_required_field(tmp_path, field):
    store = JobStore(tmp_path)
    request = valid_request()
    request.pop(field)

    with pytest.raises(JobInputError, match=field):
        create_job(request, store)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", None),
        ("target_duration_sec", 0),
        ("max_generated_video_scenes", 4),
        ("image_mode", "unknown-image"),
        ("video_mode", "unknown-video"),
        ("publish_mode", "unknown-publish"),
        ("budget_limit_usd", 0),
    ],
)
def test_create_job_rejects_invalid_input_before_llm_call(tmp_path, field, value):
    store = JobStore(tmp_path)
    request = valid_request()
    if value is None:
        request.pop(field)
    else:
        request[field] = value

    with pytest.raises(JobInputError, match=field):
        create_job(request, store)

    assert list(tmp_path.iterdir()) == []


def test_start_scripting_persists_only_a_draft_transition(tmp_path):
    store = JobStore(tmp_path)
    draft = create_job(valid_request(), store)

    scripting = start_scripting(draft, store)

    loaded = store.load(draft.content_job_id)
    assert scripting.status is JobStatus.SCRIPTING
    assert loaded.job == scripting
    assert loaded.decisions[-1]["from"] == "DRAFT"
    assert loaded.decisions[-1]["to"] == "SCRIPTING"
    assert loaded.decisions[-1]["reason"]
    assert loaded.decisions[-1]["at"] == scripting.updated_at

    job_file = tmp_path / draft.content_job_id / "job.json"
    before = job_file.read_bytes()
    with pytest.raises(IllegalTransitionError):
        start_scripting(scripting, store)

    assert job_file.read_bytes() == before
    assert store.load(draft.content_job_id).job == scripting


def test_generate_script_accepts_first_valid_llm_json_and_persists_it(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    response = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script", return_value=response
    ) as llm_call:
        script = generate_script(job, store)

    assert store.load(job.content_job_id).script == script
    assert (tmp_path / job.content_job_id / "scripts/script.json").is_file()
    llm_call.assert_called_once()
    assert llm_call.call_args.kwargs["app_config"] == {
        "llm_provider": "deepseek",
        "deepseek_model_name": "deepseek-v4-pro",
    }


def test_generate_script_preserves_structured_json_through_provider_client(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    expected = json.loads(
        (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
            encoding="utf-8"
        )
    )
    completion = ChatCompletion.model_validate(
        {
            "id": "completion-regression",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": json.dumps(expected, ensure_ascii=False),
                        "role": "assistant",
                    },
                }
            ],
            "created": 0,
            "model": "deepseek-v4-pro",
            "object": "chat.completion",
        }
    )
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = completion

    with (
        patch.dict(
            pipeline.config.app,
            {"deepseek_api_key": "demo-value"},
            clear=False,
        ),
        patch("app.services.llm.OpenAI", return_value=fake_client),
        patch("app.services.llm.generate_script") as legacy_generate,
    ):
        script = generate_script(job, store)

    fake_client.chat.completions.create.assert_called_once()
    legacy_generate.assert_not_called()
    assert script.model_dump(mode="json") == expected
    assert store.load(job.content_job_id).script.model_dump(mode="json") == expected
    assert json.loads(
        (tmp_path / job.content_job_id / "scripts/script.json").read_text(
            encoding="utf-8"
        )
    ) == expected
    assert script.body == expected["body"]
    assert script.claims == expected["claims"]
    assert script.sources == expected["sources"]
    assert script.risk_flags == expected["risk_flags"]


def test_generate_script_classifies_provider_client_failure_as_retryable(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    fake_client = Mock()
    fake_client.chat.completions.create.side_effect = TimeoutError(
        "provider timed out"
    )

    with (
        patch.dict(
            pipeline.config.app,
            {"deepseek_api_key": "demo-value"},
            clear=False,
        ),
        patch("app.services.llm.OpenAI", return_value=fake_client),
    ):
        with pytest.raises(
            pipeline.llm_adapter.LlmTransportError, match="timed out"
        ) as exc_info:
            generate_script(job, store)

    record = store.load(job.content_job_id)
    assert exc_info.value.retryable is True
    assert fake_client.chat.completions.create.call_count == 1
    assert record.job.status is JobStatus.SCRIPTING
    assert len(record.provider_events) == 1
    assert record.provider_events[0].retryable is True
    assert record.provider_events[0].error_class == "LlmTransportError"
    assert record.provider_events[0].error_class != "script_schema_invalid"


def test_generate_script_classifies_missing_api_key_as_non_retryable(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    with pytest.raises(
        pipeline.llm_adapter.LlmTransportError, match="api_key is not set"
    ) as exc_info:
        generate_script(job, store)

    record = store.load(job.content_job_id)
    assert exc_info.value.retryable is False
    assert record.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert len(record.provider_events) == 1
    assert record.provider_events[0].retryable is False
    assert record.provider_events[0].error_class == "LlmTransportError"


def test_generate_script_is_idempotent_after_success(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script", return_value=valid
    ) as first_call:
        script = generate_script(job, store)

    script_path = tmp_path / job.content_job_id / "scripts/script.json"
    before_bytes = script_path.read_bytes()
    before_record = store.load(job.content_job_id)
    with patch("app.services.jobs.pipeline.llm_adapter.generate_script") as second_call:
        reinvoked = generate_script(job, store)

    after_record = store.load(job.content_job_id)
    assert reinvoked == script
    assert after_record.script == before_record.script
    assert len(after_record.provider_events) == len(before_record.provider_events)
    assert len(after_record.usage_ledger) == len(before_record.usage_ledger)
    assert script_path.read_bytes() == before_bytes
    assert after_record.job.status is JobStatus.SCRIPTING
    first_call.assert_called_once()
    second_call.assert_not_called()


def test_generate_script_repairs_one_invalid_response_then_persists(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    invalid = '{"title": broken}'
    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=[invalid, valid],
    ) as llm_call:
        script = generate_script(job, store)

    assert store.load(job.content_job_id).script == script
    assert llm_call.call_count == 2
    repair_prompt = llm_call.call_args_list[1].kwargs["repair_prompt"]
    assert "Expecting value" in repair_prompt
    assert invalid in repair_prompt
    record = store.load(job.content_job_id)
    assert pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD == pytest.approx(0.05)
    assert len(record.provider_events) == 2
    assert len(record.usage_ledger) == 2
    assert record.job.actual_cost_usd == pytest.approx(0.1)


def test_generate_script_sends_repair_context_through_provider_client(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )
    invalid = '{"title": broken}'

    def completion(response_id, content):
        return ChatCompletion.model_validate(
            {
                "id": response_id,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {"content": content, "role": "assistant"},
                    }
                ],
                "created": 0,
                "model": "deepseek-v4-pro",
                "object": "chat.completion",
            }
        )

    fake_client = Mock()
    fake_client.chat.completions.create.side_effect = [
        completion("completion-invalid", invalid),
        completion("completion-valid", valid),
    ]

    with (
        patch.dict(
            pipeline.config.app,
            {"deepseek_api_key": "demo-value"},
            clear=False,
        ),
        patch("app.services.llm.OpenAI", return_value=fake_client),
    ):
        script = generate_script(job, store)

    assert store.load(job.content_job_id).script == script
    assert fake_client.chat.completions.create.call_count == 2
    second_messages = fake_client.chat.completions.create.call_args_list[1].kwargs[
        "messages"
    ]
    assert "Expecting value" in second_messages[0]["content"]
    assert invalid in second_messages[0]["content"]


def test_default_budget_allows_initial_and_repair_calls_at_conservative_ceiling(
    tmp_path,
):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=['{"title": broken}', valid],
    ):
        generate_script(job, store)

    # $3 / $0.05 = 60 equivalent calls; pricing and measured tokens are later inputs.
    assert pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD == pytest.approx(0.05)
    assert store.load(job.content_job_id).job.actual_cost_usd == pytest.approx(0.1)


def test_generate_script_marks_truncated_repair_context(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )
    invalid = '{"title": "' + ("x" * 2_500)

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=[invalid, valid],
    ) as llm_call:
        generate_script(job, store)

    repair_prompt = llm_call.call_args_list[1].kwargs["repair_prompt"]
    assert repair_prompt.endswith("[truncated]")
    assert len(repair_prompt) <= pipeline._REPAIR_CONTEXT_MAX_CHARS + 200
    assert invalid not in repair_prompt


def test_generate_script_fails_before_call_when_provider_identity_is_unknown(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    with (
        patch.dict(pipeline.config.app, {"llm_provider": "missing"}, clear=True),
        patch("app.services.jobs.pipeline.llm_adapter.generate_script") as llm_call,
    ):
        with pytest.raises(ValueError, match="unsupported llm provider"):
            generate_script(job, store)

    llm_call.assert_not_called()
    assert store.load(job.content_job_id).provider_events == []


def test_generate_script_rejects_two_invalid_responses_without_a_third_call_or_file(
    tmp_path,
):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=['{"title": "incomplete"}', "still-not-json"],
    ) as llm_call:
        with pytest.raises(ScriptGenerationError, match="two attempts"):
            generate_script(job, store)

    assert llm_call.call_count == 2
    assert not (tmp_path / job.content_job_id / "scripts/script.json").exists()
    record = store.load(job.content_job_id)
    assert record.script is None
    assert record.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert record.decisions[-1]["from"] == "SCRIPTING"
    assert record.decisions[-1]["to"] == "MANUAL_ACTION_REQUIRED"
    assert len(record.provider_events) == 2
    assert len(record.usage_ledger) == 2
    for event, entry in zip(record.provider_events, record.usage_ledger):
        assert event.provider == "deepseek"
        assert event.model == "deepseek-v4-pro"
        assert event.estimated_cost_usd == pytest.approx(
            pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD
        )
        assert entry.provider == "deepseek"
        assert entry.model == "deepseek-v4-pro"
        assert entry.estimated_cost_usd == pytest.approx(
            pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD
        )


@pytest.mark.parametrize(
    "status",
    [s for s in JobStatus if s is not JobStatus.SCRIPTING],
)
def test_generate_script_requires_scripting_status(tmp_path, status):
    store = JobStore(tmp_path)
    draft = create_job(valid_request(), store)
    guarded = draft.model_copy(update={"status": status})
    store.save(guarded)

    with patch("app.services.jobs.pipeline.llm_adapter.generate_script") as llm_call:
        with pytest.raises(IllegalTransitionError, match="SCRIPTING"):
            generate_script(guarded, store)

    llm_call.assert_not_called()
    assert store.load(guarded.content_job_id).job.status is status


def test_generate_script_retries_across_calls_with_new_attempt_and_cost(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=TimeoutError("provider timed out"),
    ) as first_call:
        with pytest.raises(TimeoutError, match="provider timed out"):
            generate_script(job, store)

    first_record = store.load(job.content_job_id)
    assert first_record.job.status is JobStatus.SCRIPTING
    assert len(first_record.provider_events) == 1
    assert len(first_record.usage_ledger) == 1
    assert first_record.job.actual_cost_usd == pytest.approx(0.05)
    first_call.assert_called_once()

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script", return_value=valid
    ) as second_call:
        script = generate_script(job, store)

    record = store.load(job.content_job_id)
    assert record.script == script
    assert len(record.provider_events) == 2
    assert len(record.usage_ledger) == 2
    assert record.job.actual_cost_usd == pytest.approx(0.1)
    assert record.provider_events[0].idempotency_key == (
        f"{job.content_job_id}:script:generate:attempt-1"
    )
    assert record.provider_events[1].idempotency_key == (
        f"{job.content_job_id}:script:generate:attempt-2"
    )
    assert record.usage_ledger[1].idempotency_key == (
        f"{job.content_job_id}:script:generate:attempt-2"
    )
    second_call.assert_called_once()


def test_generate_script_is_idempotent_after_timeout_then_success(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=TimeoutError("provider timed out"),
    ):
        with pytest.raises(TimeoutError):
            generate_script(job, store)

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script", return_value=valid
    ):
        script = generate_script(job, store)

    before_record = store.load(job.content_job_id)
    with patch("app.services.jobs.pipeline.llm_adapter.generate_script") as reinvoke:
        assert generate_script(job, store) == script

    after_record = store.load(job.content_job_id)
    assert reinvoke.call_count == 0
    assert after_record.script == before_record.script
    assert len(after_record.provider_events) == 2
    assert len(after_record.usage_ledger) == 2
    assert after_record.job.status is JobStatus.SCRIPTING


def test_generate_script_exhausts_cross_call_attempt_budget_without_provider_call(
    tmp_path,
):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    for message in ("first timeout", "second timeout"):
        with patch(
            "app.services.jobs.pipeline.llm_adapter.generate_script",
            side_effect=TimeoutError(message),
        ):
            with pytest.raises(TimeoutError, match=message):
                generate_script(job, store)

    with patch("app.services.jobs.pipeline.llm_adapter.generate_script") as third_call:
        with pytest.raises(ScriptGenerationError, match="attempt limit"):
            generate_script(job, store)

    record = store.load(job.content_job_id)
    assert record.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert len(record.provider_events) == 2
    assert len(record.usage_ledger) == 2
    assert record.job.actual_cost_usd == pytest.approx(0.1)
    third_call.assert_not_called()


def test_generate_script_uses_persisted_status_guard_for_stale_job(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    persisted_manual = job.model_copy(update={"status": JobStatus.MANUAL_ACTION_REQUIRED})
    store.save(persisted_manual)

    with patch("app.services.jobs.pipeline.llm_adapter.generate_script") as llm_call:
        with pytest.raises(IllegalTransitionError, match="SCRIPTING"):
            generate_script(job, store)

    llm_call.assert_not_called()
    assert store.load(job.content_job_id).job.status is JobStatus.MANUAL_ACTION_REQUIRED


def test_generate_script_blocks_call_when_conservative_estimate_exceeds_budget(
    tmp_path,
):
    store = JobStore(tmp_path)
    job = start_scripting(
        create_job(valid_request(budget_limit_usd=0.01), store), store
    )

    with (
        patch(
            "app.services.jobs.pipeline.check_budget", wraps=real_check_budget
        ) as budget_gate,
        patch("app.services.jobs.pipeline.llm_adapter.generate_script") as llm_call,
    ):
        with pytest.raises(BudgetExceededError):
            generate_script(job, store)

    llm_call.assert_not_called()
    budget_gate.assert_called_once()
    assert budget_gate.call_args.kwargs["store"] is store
    record = store.load(job.content_job_id)
    assert not (tmp_path / job.content_job_id / "scripts/script.json").exists()
    assert record.usage_ledger == []
    assert record.job.status is JobStatus.BUDGET_EXCEEDED
    assert record.decisions[-1]["from"] == "SCRIPTING"
    assert record.decisions[-1]["to"] == "BUDGET_EXCEEDED"


def test_generate_script_audits_llm_exception_and_classifies_retryability(tmp_path):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=TimeoutError("provider timed out"),
    ):
        with pytest.raises(TimeoutError, match="provider timed out"):
            generate_script(job, store)

    record = store.load(job.content_job_id)
    assert record.job.status is JobStatus.SCRIPTING
    assert len(record.provider_events) == 1
    assert record.provider_events[0].retryable is True
    assert record.provider_events[0].error_class == "TimeoutError"
    assert record.decisions == [
        {
            "from": "DRAFT",
            "to": "SCRIPTING",
            "reason": "create-job input passed validation",
            "at": record.decisions[0]["at"],
        }
    ]


def test_generate_script_audits_non_retryable_provider_exception_as_manual_action(
    tmp_path,
):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)

    with patch(
        "app.services.jobs.pipeline.llm_adapter.generate_script",
        side_effect=PermissionError("provider access denied"),
    ):
        with pytest.raises(PermissionError, match="provider access denied"):
            generate_script(job, store)

    record = store.load(job.content_job_id)
    assert record.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert len(record.provider_events) == 1
    assert record.provider_events[0].retryable is False
    assert record.decisions[-1]["from"] == "SCRIPTING"
    assert record.decisions[-1]["to"] == "MANUAL_ACTION_REQUIRED"


@pytest.mark.parametrize("responses", [("valid",), ("invalid", "valid")])
def test_generate_script_gates_and_audits_every_llm_call(tmp_path, responses):
    store = JobStore(tmp_path)
    job = start_scripting(create_job(valid_request(), store), store)
    valid = (FIXTURES_ROOT / "three-scene-demo/scripts/script.json").read_text(
        encoding="utf-8"
    )
    response_values = [valid if value == "valid" else "not-json" for value in responses]
    order = Mock()

    with (
        patch(
            "app.services.jobs.pipeline.check_budget", wraps=real_check_budget
        ) as budget_gate,
        patch(
            "app.services.jobs.pipeline.llm_adapter.generate_script",
            side_effect=response_values,
        ) as llm_call,
    ):
        order.attach_mock(budget_gate, "budget")
        order.attach_mock(llm_call, "llm")
        generate_script(job, store)

    expected_calls = len(responses)
    assert [call[0] for call in order.mock_calls] == ["budget", "llm"] * expected_calls
    record = store.load(job.content_job_id)
    assert len(record.provider_events) == expected_calls
    assert len(record.usage_ledger) == expected_calls
    for attempt, (event, entry) in enumerate(
        zip(record.provider_events, record.usage_ledger), start=1
    ):
        assert event.provider == "deepseek"
        assert event.model == "deepseek-v4-pro"
        assert f"attempt-{attempt}" in event.idempotency_key
        assert isinstance(event.estimated_cost_usd, float)
        assert event.estimated_cost_usd == pytest.approx(
            pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD
        )
        assert event.actual_cost_usd == "unknown"
        assert entry.provider == "deepseek"
        assert entry.model == "deepseek-v4-pro"
        assert f"attempt-{attempt}" in entry.idempotency_key
        assert isinstance(entry.estimated_cost_usd, float)
        assert entry.estimated_cost_usd == pytest.approx(
            pipeline.SCRIPT_LLM_CALL_COST_CEILING_USD
        )
        assert entry.actual_cost_usd == "unknown"
    assert record.job.actual_cost_usd == pytest.approx(
        expected_calls * record.usage_ledger[0].estimated_cost_usd
    )
