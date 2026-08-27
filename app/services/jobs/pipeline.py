"""Create V0 content jobs and run their script-generation stage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from app.config import config
from app.models.content_job import ContentJob, JobStatus, ProviderEvent, Script
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID, get_llm_provider
from app.services.jobs.budget import (
    build_idempotency_key,
    check_budget,
    record_usage,
    summarize,
)
from app.services.jobs import llm_adapter
from app.services.jobs.state_machine import (
    IllegalTransitionError,
    classify_error,
    decision_record,
    transition,
)
from app.services.jobs.store import JobStore

_REQUIRED_FIELDS = (
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
)
_ALLOWED_IMAGE_MODES = frozenset({"assisted_qwen"})
_ALLOWED_VIDEO_MODES = frozenset({"manual_google_flow"})
_ALLOWED_PUBLISH_MODES = frozenset({"postiz_draft"})

# Conservative ceiling for one structured-script call: one-to-two orders of
# magnitude above the measured call cost, leaving about 60 equivalent calls in
# the default $3 budget. Replace it with provider/model pricing plus measured
# token usage later; never replace it with zero or an unknown sentinel.
SCRIPT_LLM_CALL_COST_CEILING_USD = 0.05
_MAX_SCRIPT_GENERATION_ATTEMPTS = 2
_REPAIR_CONTEXT_MAX_CHARS = 1_600


class JobInputError(ValueError):
    """A create-job request does not satisfy the V0 domain contract."""


class ScriptGenerationError(ValueError):
    """The initial and repair LLM responses both failed the Script schema."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_request(request: Mapping[str, Any]) -> None:
    for field in _REQUIRED_FIELDS:
        if field not in request:
            raise JobInputError(f"missing required field: {field}")
    if (
        isinstance(request["target_duration_sec"], bool)
        or not isinstance(request["target_duration_sec"], int)
        or request["target_duration_sec"] <= 0
    ):
        raise JobInputError("target_duration_sec must be a positive integer")
    if (
        isinstance(request["max_generated_video_scenes"], bool)
        or not isinstance(request["max_generated_video_scenes"], int)
        or request["max_generated_video_scenes"] < 0
        or request["max_generated_video_scenes"] > 3
    ):
        raise JobInputError("max_generated_video_scenes must be between 0 and 3")
    for field, allowed in (
        ("image_mode", _ALLOWED_IMAGE_MODES),
        ("video_mode", _ALLOWED_VIDEO_MODES),
        ("publish_mode", _ALLOWED_PUBLISH_MODES),
    ):
        if request[field] not in allowed:
            raise JobInputError(f"unknown {field}: {request[field]!r}")
    budget = request["budget_limit_usd"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise JobInputError("budget_limit_usd must be positive")


def create_job(request: Mapping[str, Any], store: JobStore) -> ContentJob:
    """Create and persist a DRAFT job from the SPEC-001 §3.1 request."""
    _validate_request(request)
    now = _utc_now()
    job = ContentJob(
        content_job_id=f"job-{uuid4().hex}",
        tenant_id=request["tenant_id"],
        brand_id=request["brand_id"],
        creator_profile_id="",
        topic=request["topic"],
        language=request["language"],
        target_duration_sec=request["target_duration_sec"],
        image_mode=request["image_mode"],
        video_mode=request["video_mode"],
        max_generated_video_scenes=request["max_generated_video_scenes"],
        publish_mode=request["publish_mode"],
        budget_limit_usd=request["budget_limit_usd"],
        estimated_cost_usd=0,
        actual_cost_usd=0,
        status=JobStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    store.create(job)
    return job


def start_scripting(job: ContentJob, store: JobStore) -> ContentJob:
    """Move a DRAFT job to SCRIPTING and persist its decision record."""
    reason = "create-job input passed validation"
    scripting = transition(job, JobStatus.SCRIPTING, reason=reason)
    store.save(scripting)
    store.append_decision(
        job.content_job_id, decision_record(job.status, scripting, reason)
    )
    return scripting


def _resolve_llm_identity(
    runtime_app_config: Mapping[str, Any],
) -> tuple[str, str]:
    provider_id = str(
        runtime_app_config.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
    ).lower()
    provider = get_llm_provider(provider_id)
    if provider is None:
        raise ValueError(f"{provider_id}: unsupported llm provider")
    configured_model = runtime_app_config.get(provider.config_key("model_name"), "")
    model = provider.resolve_model_name(configured_model)
    if not model:
        raise ValueError(f"{provider_id}: model_name is not set")
    return provider_id, model


def _repair_prompt(error: ValueError, response: str) -> str:
    context = f"Validation error:\n{error}\nPrevious invalid response:\n{response}"
    marker = "\n[truncated]"
    if len(context) > _REPAIR_CONTEXT_MAX_CHARS:
        context = context[: _REPAIR_CONTEXT_MAX_CHARS - len(marker)] + marker
    return (
        "The previous response failed the required JSON schema. Repair it and "
        "return a complete replacement JSON object only.\n"
        f"{context}"
    )


def _persist_failed_status(
    job: ContentJob, store: JobStore, exc: BaseException
) -> None:
    classification = classify_error(exc)
    if classification.is_retryable:
        # SPEC §5.2 lacks a RETRYABLE_FAILED->generation edge; product decision keeps SCRIPTING.
        return
    current = store.load(job.content_job_id).job
    reason = (
        f"script generation failed ({classification.value}): "
        f"{type(exc).__name__}"
    )
    failed = transition(current, JobStatus.MANUAL_ACTION_REQUIRED, reason=reason)
    store.save(failed)
    store.append_decision(
        job.content_job_id, decision_record(current.status, failed, reason)
    )


def _script_generation_attempts(record) -> set[int]:
    """Return attempts already recorded for this job's script generation."""
    attempts: set[int] = set()
    keys = [
        event.idempotency_key for event in record.provider_events
    ] + [entry.idempotency_key for entry in record.usage_ledger]
    for key in keys:
        parts = key.split(":")
        if len(parts) != 4:
            continue
        content_job_id, scene_id, operation, attempt_part = parts
        if (
            content_job_id != record.job.content_job_id
            or scene_id != "script"
            or operation != "generate"
            or not attempt_part.startswith("attempt-")
        ):
            continue
        attempt_text = attempt_part.removeprefix("attempt-")
        if attempt_text.isdecimal() and int(attempt_text) >= 1:
            attempts.add(int(attempt_text))
    return attempts


def generate_script(job: ContentJob, store: JobStore) -> Script:
    """Generate one structured Script document and persist it for ``job``."""
    persisted_record = store.load(job.content_job_id)
    if persisted_record.script is not None:
        return persisted_record.script
    persisted_job = persisted_record.job
    if persisted_job.status is not JobStatus.SCRIPTING:
        raise IllegalTransitionError(
            f"generate_script requires SCRIPTING, got {persisted_job.status.value}"
        )
    used_attempts = _script_generation_attempts(persisted_record)
    first_attempt = max(used_attempts, default=0) + 1
    if first_attempt > _MAX_SCRIPT_GENERATION_ATTEMPTS:
        error = ScriptGenerationError("script generation attempt limit reached")
        _persist_failed_status(persisted_job, store, error)
        raise error

    runtime_app_config = dict(config.app)
    provider_id, model = _resolve_llm_identity(runtime_app_config)
    script = None
    repair_prompt = ""
    for attempt in range(first_attempt, _MAX_SCRIPT_GENERATION_ATTEMPTS + 1):
        current_job = check_budget(
            persisted_job, SCRIPT_LLM_CALL_COST_CEILING_USD, store=store
        )
        started_at = _utc_now()
        response = ""
        call_error = None
        try:
            response = llm_adapter.generate_script(
                topic=current_job.topic,
                language=current_job.language,
                repair_prompt=repair_prompt,
                app_config=runtime_app_config,
            )
        except Exception as exc:
            call_error = exc
        validation_error = None
        if call_error is None:
            try:
                script = Script.model_validate(json.loads(response))
            except ValueError as exc:
                validation_error = exc
        event_error = call_error or validation_error
        classification = classify_error(event_error) if event_error else None
        event = ProviderEvent(
            provider_event_id=f"provider-event-{uuid4().hex}",
            content_job_id=job.content_job_id,
            scene_id=None,
            provider=provider_id,
            model=model,
            request_id="",
            external_job_id="",
            idempotency_key=build_idempotency_key(
                job.content_job_id, "script", "generate", attempt
            ),
            attempt_count=attempt,
            estimated_cost_usd=SCRIPT_LLM_CALL_COST_CEILING_USD,
            actual_cost_usd="unknown",
            request_summary=summarize(
                "script request",
                {"topic": current_job.topic, "language": current_job.language},
            ),
            response_summary=summarize("script response", response),
            error_class=(
                "script_schema_invalid"
                if validation_error is not None
                else type(call_error).__name__ if call_error is not None else None
            ),
            retryable=classification.is_retryable if classification else False,
            created_at=started_at,
            completed_at=_utc_now(),
        )
        record_usage(
            store,
            current_job,
            event,
            estimated_cost_source=(
                "conservative structured-script ceiling; app.services.llm exposes "
                "no provider pricing or measured token usage"
            ),
        )
        if call_error is not None:
            _persist_failed_status(current_job, store, call_error)
            raise call_error
        if validation_error is None:
            break
        if attempt == _MAX_SCRIPT_GENERATION_ATTEMPTS:
            _persist_failed_status(current_job, store, validation_error)
            raise ScriptGenerationError(
                "LLM returned invalid Script JSON on two attempts"
            ) from validation_error
        repair_prompt = _repair_prompt(validation_error, response)
    assert script is not None
    record = store.load(job.content_job_id)
    record.script = script
    store.replace(record)
    return script
