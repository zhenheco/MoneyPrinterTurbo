"""Shape validation for the SPEC-001 §4 job data contracts."""

import pytest
from pydantic import ValidationError

from app.models.content_job import (
    AssetRecord,
    ContentJob,
    JobStatus,
    ProviderEvent,
    RenderManifest,
    Scene,
    Script,
    UsageLedgerEntry,
)

SPEC_STATUSES = (
    "DRAFT",
    "RESEARCHING",
    "SCRIPTING",
    "SCENE_PLANNING",
    "VOICE_GENERATING",
    "AWAITING_ASSETS",
    "IMAGE_GENERATING",
    "VIDEO_GENERATING",
    "READY_TO_RENDER",
    "RENDERING",
    "TECHNICAL_QA",
    "CONTENT_QA",
    "READY_FOR_REVIEW",
    "POSTIZ_DRAFTING",
    "POSTIZ_DRAFTED",
    "APPROVED",
    "SCHEDULED",
    "PUBLISHED",
    "RETRYABLE_FAILED",
    "MANUAL_ACTION_REQUIRED",
    "BUDGET_EXCEEDED",
    "FAILED",
    "CANCELLED",
)


def content_job_payload():
    """SPEC-001 §4.2 example, field for field."""
    return {
        "content_job_id": "job-20260816-001",
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "creator_profile_id": "creator-001",
        "topic": "企業導入AI最常犯的三個錯誤",
        "language": "zh-TW",
        "target_duration_sec": 50,
        "image_mode": "assisted_qwen",
        "video_mode": "manual_google_flow",
        "max_generated_video_scenes": 3,
        "publish_mode": "postiz_draft",
        "budget_limit_usd": 3,
        "estimated_cost_usd": 0,
        "actual_cost_usd": 0,
        "status": "DRAFT",
        "created_at": "",
        "updated_at": "",
    }


def script_payload():
    """SPEC-001 §4.3 example."""
    return {
        "title": "企業導入AI最常犯的三個錯誤",
        "target_audience": "中小企業決策者",
        "core_message": "先定義流程再談模型",
        "hook": "九成企業第一步就走錯",
        "body": ["錯誤一：先買工具", "錯誤二：沒有資料治理", "錯誤三：沒有驗收標準"],
        "conclusion": "從一個可量測的流程開始",
        "cta": "追蹤看下一集",
        "claims": ["導入失敗率高"],
        "sources": ["https://example.com/report"],
        "risk_flags": [],
    }


def scene_payload():
    """SPEC-001 §4.4 example."""
    return {
        "scene_id": "scene-001",
        "content_job_id": "job-20260816-001",
        "scene_index": 1,
        "semantic_purpose": "hook",
        "narration": "九成企業第一步就走錯",
        "caption": "九成企業第一步就走錯",
        "duration_target_ms": 5000,
        "visual_type": "generated_image",
        "visual_prompt": "office desk, dramatic lighting",
        "reference_assets": [],
        "generation_required": True,
        "provider": "qwen_code_plan",
        "provider_model": "",
        "fallback_type": "image_motion",
        "attempt_count": 0,
        "status": "AWAITING_ASSETS",
    }


def asset_record_payload():
    """SPEC-001 §4.5 example."""
    return {
        "asset_id": "asset-001",
        "content_job_id": "job-20260816-001",
        "scene_id": "scene-001",
        "asset_type": "image",
        "storage_key": "assets/asset-001.png",
        "original_filename": "scene-001.png",
        "mime_type": "image/png",
        "bytes": 240118,
        "width": 1080,
        "height": 1920,
        "duration_ms": None,
        "sha256": "a" * 64,
        "source_mode": "assisted_qwen",
        "provider": "qwen_code_plan",
        "model": "",
        "license_or_consent": "owner_generated",
        "consent_status": "not_applicable",
        "usage_scope": "",
        "consent_source": "",
        "consent_expires_at": "",
        "consent_revoked_at": None,
        "manual_review_status": "pending",
        "created_at": "2026-08-16T09:00:00+00:00",
    }


def provider_event_payload():
    """SPEC-001 §4.6 example."""
    return {
        "provider_event_id": "provider-event-001",
        "content_job_id": "job-20260816-001",
        "scene_id": "scene-001",
        "provider": "manual_google_flow",
        "model": "",
        "request_id": "",
        "external_job_id": "",
        "idempotency_key": "job-20260816-001:scene-001:video:attempt-1",
        "attempt_count": 1,
        "estimated_cost_usd": 0,
        "actual_cost_usd": 0,
        "request_summary": "",
        "response_summary": "",
        "error_class": None,
        "retryable": False,
        "created_at": "2026-08-16T09:00:00+00:00",
        "completed_at": "",
    }


def usage_ledger_payload():
    """Billing-relevant subset of SPEC-001 §4.6."""
    return {
        "provider_event_id": "provider-event-001",
        "content_job_id": "job-20260816-001",
        "scene_id": "scene-001",
        "provider": "manual_google_flow",
        "model": "",
        "idempotency_key": "job-20260816-001:scene-001:video:attempt-1",
        "attempt_count": 1,
        "estimated_cost_usd": 0,
        "actual_cost_usd": 0,
        "created_at": "2026-08-16T09:00:00+00:00",
    }


def render_manifest_payload():
    """SPEC-001 §8 example."""
    return {
        "content_job_id": "job-20260816-001",
        "canvas": {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "pixel_format": "yuv420p",
        },
        "audio": {
            "mode": "master_voice",
            "master_voice_asset_id": "asset-voice-001",
            "sample_rate": 48000,
            "codec": "aac",
        },
        "scenes": [
            {
                "scene_id": "scene-001",
                "asset_id": "asset-001",
                "start_ms": 0,
                "end_ms": 5000,
                "motion": {
                    "type": "ken_burns",
                    "scale_start": 1.0,
                    "scale_end": 1.08,
                },
                "caption_ref": "caption-001",
            }
        ],
        "subtitle_asset_id": "asset-subtitle-001",
        "output": {
            "container": "mp4",
            "video_codec": "h264",
            "audio_codec": "aac",
        },
    }


ACCEPTED_CASES = (
    (ContentJob, content_job_payload),
    (Script, script_payload),
    (Scene, scene_payload),
    (AssetRecord, asset_record_payload),
    (ProviderEvent, provider_event_payload),
    (UsageLedgerEntry, usage_ledger_payload),
    (RenderManifest, render_manifest_payload),
)

#: Every top-level key of every accepted payload, so all required fields are
#: covered rather than just whichever one happens to come first.
MISSING_FIELD_CASES = tuple(
    pytest.param(model, payload_factory, field, id=f"{model.__name__}-{field}")
    for model, payload_factory in ACCEPTED_CASES
    for field in payload_factory()
)

REJECTION_CASES = (
    (ContentJob, content_job_payload, "budget_limit_usd", None),
    (Script, script_payload, "body", "not-a-list"),
    (Scene, scene_payload, "duration_target_ms", "five-seconds"),
    (AssetRecord, asset_record_payload, "bytes", "big"),
    (ProviderEvent, provider_event_payload, "retryable", "maybe"),
    (UsageLedgerEntry, usage_ledger_payload, "attempt_count", "once"),
    (RenderManifest, render_manifest_payload, "canvas", None),
)


class TestContentJobModels:
    @pytest.mark.parametrize("model, payload_factory", ACCEPTED_CASES)
    def test_valid_payload_round_trips_every_field(self, model, payload_factory):
        """Every field the payload states comes back holding the same value.

        A subset comparison, because a model may carry columns the §4.x example
        predates: the contract under test is that nothing the payload said is
        lost or altered, not that the model has no other fields.
        """
        payload = payload_factory()

        dump = model.model_validate(payload).model_dump(mode="json")

        assert {name: dump[name] for name in payload} == payload

    @pytest.mark.parametrize("model, payload_factory, removed_field", MISSING_FIELD_CASES)
    def test_missing_required_field_names_the_field(
        self, model, payload_factory, removed_field
    ):
        payload = payload_factory()
        payload.pop(removed_field)

        with pytest.raises(ValidationError) as raised:
            model.model_validate(payload)

        assert removed_field in str(raised.value)

    @pytest.mark.parametrize("model, payload_factory, field, bad_value", REJECTION_CASES)
    def test_wrong_type_is_rejected_and_names_the_field(
        self, model, payload_factory, field, bad_value
    ):
        payload = payload_factory()
        payload[field] = bad_value

        with pytest.raises(ValidationError) as raised:
            model.model_validate(payload)

        assert field in str(raised.value)

    @pytest.mark.parametrize("status", SPEC_STATUSES)
    def test_every_spec_status_is_accepted(self, status):
        payload = content_job_payload()
        payload["status"] = status

        parsed = ContentJob.model_validate(payload)

        assert parsed.status == JobStatus(status)
        assert parsed.model_dump(mode="json")["status"] == status

    def test_status_enum_covers_exactly_the_spec_states(self):
        assert {member.value for member in JobStatus} == set(SPEC_STATUSES)

    def test_unknown_status_is_rejected(self):
        payload = content_job_payload()
        payload["status"] = "NOT_A_STATE"

        with pytest.raises(ValidationError) as raised:
            ContentJob.model_validate(payload)

        assert "status" in str(raised.value)

    def test_actual_cost_usd_accepts_unknown_marker(self):
        payload = content_job_payload()
        payload["actual_cost_usd"] = "unknown"

        parsed = ContentJob.model_validate(payload)

        assert parsed.actual_cost_usd == "unknown"
        assert parsed.model_dump(mode="json")["actual_cost_usd"] == "unknown"

    def test_actual_cost_usd_rejects_other_strings(self):
        payload = content_job_payload()
        payload["actual_cost_usd"] = "free"

        with pytest.raises(ValidationError) as raised:
            ContentJob.model_validate(payload)

        assert "actual_cost_usd" in str(raised.value)

    def test_scene_rejects_visual_type_outside_the_allowed_set(self):
        payload = scene_payload()
        payload["visual_type"] = "hologram"

        with pytest.raises(ValidationError) as raised:
            Scene.model_validate(payload)

        assert "visual_type" in str(raised.value)

    @pytest.mark.parametrize(
        "visual_type",
        (
            "avatar",
            "generated_video",
            "generated_image",
            "screen_recording",
            "motion_graphic",
            "title_card",
        ),
    )
    def test_scene_accepts_every_allowed_visual_type(self, visual_type):
        payload = scene_payload()
        payload["visual_type"] = visual_type

        parsed = Scene.model_validate(payload)

        assert parsed.visual_type == visual_type

    def test_extra_fields_are_rejected(self):
        payload = content_job_payload()
        payload["secret_api_key"] = "sk-live-should-never-land-here"

        with pytest.raises(ValidationError) as raised:
            ContentJob.model_validate(payload)

        assert "secret_api_key" in str(raised.value)
