"""SPEC-001 §4 job data contracts.

These models only validate shape. Persistence lives in ``app.services.jobs.store``
and transition rules live in ``app.services.jobs.state_machine``.

Every field mirrors the JSON examples in ``docs/specs/SPEC-001-v0-local-pipeline.md``
§4.2–§4.6 and §8 field for field. Extra fields are rejected so that credentials or
provider payloads cannot ride along into a job file.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict

# ponytail: one shared config instead of repeating it on seven classes.
_STRICT = ConfigDict(extra="forbid")

#: ``actual_cost_usd`` is a number, or the explicit ``"unknown"`` marker when the
#: provider did not report a cost. SPEC-001 §4.6 / issue 01: a missing cost is
#: recorded as ``unknown``, never as ``0``.
CostUsd = Union[float, Literal["unknown"]]

VisualType = Literal[
    "avatar",
    "generated_video",
    "generated_image",
    "screen_recording",
    "motion_graphic",
    "title_card",
]


class JobStatus(str, Enum):
    """The 23 states listed in SPEC-001 §5.1."""

    DRAFT = "DRAFT"
    RESEARCHING = "RESEARCHING"
    SCRIPTING = "SCRIPTING"
    SCENE_PLANNING = "SCENE_PLANNING"
    VOICE_GENERATING = "VOICE_GENERATING"
    AWAITING_ASSETS = "AWAITING_ASSETS"
    IMAGE_GENERATING = "IMAGE_GENERATING"
    VIDEO_GENERATING = "VIDEO_GENERATING"
    READY_TO_RENDER = "READY_TO_RENDER"
    RENDERING = "RENDERING"
    TECHNICAL_QA = "TECHNICAL_QA"
    CONTENT_QA = "CONTENT_QA"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    POSTIZ_DRAFTING = "POSTIZ_DRAFTING"
    POSTIZ_DRAFTED = "POSTIZ_DRAFTED"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ContentJob(BaseModel):
    """SPEC-001 §4.2."""

    model_config = _STRICT

    content_job_id: str
    tenant_id: str
    brand_id: str
    creator_profile_id: str
    topic: str
    language: str
    target_duration_sec: int
    image_mode: str
    video_mode: str
    max_generated_video_scenes: int
    publish_mode: str
    budget_limit_usd: float
    estimated_cost_usd: float
    actual_cost_usd: CostUsd
    status: JobStatus
    created_at: str
    updated_at: str


class Script(BaseModel):
    """SPEC-001 §4.3."""

    model_config = _STRICT

    title: str
    target_audience: str
    core_message: str
    hook: str
    body: List[str]
    conclusion: str
    cta: str
    claims: List[str]
    sources: List[str]
    risk_flags: List[str]


class Scene(BaseModel):
    """SPEC-001 §4.4."""

    model_config = _STRICT

    scene_id: str
    content_job_id: str
    scene_index: int
    semantic_purpose: str
    narration: str
    caption: str
    duration_target_ms: int
    visual_type: VisualType
    visual_prompt: str
    reference_assets: List[str]
    generation_required: bool
    provider: str
    provider_model: str
    fallback_type: str
    attempt_count: int
    status: str


class AssetRecord(BaseModel):
    """SPEC-001 §4.5."""

    model_config = _STRICT

    asset_id: str
    content_job_id: str
    scene_id: Optional[str]
    asset_type: str
    storage_key: str
    original_filename: str
    mime_type: str
    bytes: int
    width: int
    height: int
    duration_ms: Optional[int]
    sha256: str
    source_mode: str
    provider: str
    model: str
    license_or_consent: str
    consent_status: str
    usage_scope: str
    consent_source: str
    consent_expires_at: str
    consent_revoked_at: Optional[str]
    manual_review_status: str
    created_at: str


class ProviderEvent(BaseModel):
    """SPEC-001 §4.6."""

    model_config = _STRICT

    provider_event_id: str
    content_job_id: str
    scene_id: Optional[str]
    provider: str
    model: str
    request_id: str
    external_job_id: str
    idempotency_key: str
    attempt_count: int
    estimated_cost_usd: float
    actual_cost_usd: CostUsd
    request_summary: str
    response_summary: str
    error_class: Optional[str]
    retryable: bool
    created_at: str
    completed_at: str


class UsageLedgerEntry(BaseModel):
    """The billing-relevant subset of SPEC-001 §4.6, plus the §10 columns.

    The first ten fields are the §4.6 example minus the request/response and
    error columns, keyed by ``idempotency_key`` so a resumed job cannot be
    billed twice. The remaining four are what §10 additionally requires kept:
    the source behind an estimate, the images/videos that were thrown away, the
    seconds of video actually adopted, and the effective cost of each of those
    seconds. They default to "no such information", never to zero — §10 forbids
    presenting an unknown cost as ``0``.

    All fourteen columns are always serialized, including the four whose value
    is the default. A column that disappeared when it held nothing would make a
    reader's ``payload.get(name, 0)`` produce exactly the fabricated zero §10
    forbids, and would leave "adopted no seconds" indistinguishable from "this
    call recorded no seconds".
    """

    model_config = _STRICT

    provider_event_id: str
    content_job_id: str
    scene_id: Optional[str]
    provider: str
    model: str
    idempotency_key: str
    attempt_count: int
    estimated_cost_usd: float
    actual_cost_usd: CostUsd
    created_at: str

    #: §10 「保留估算來源」 — mandatory once ``actual_cost_usd`` is ``"unknown"``.
    estimated_cost_source: str = ""
    #: §10 「被放棄的圖片／影片」 — asset ids generated for this call and dropped.
    discarded_asset_ids: List[str] = []
    #: §10 「最終採用影片秒數」 — seconds of this call's output that shipped.
    adopted_video_seconds: float = 0.0
    #: §10 「每個採用影片秒數的有效成本」 — ``"unknown"`` whenever the actual cost
    #: or the adopted duration is unknown, so it is never a fabricated zero.
    effective_cost_per_adopted_second_usd: CostUsd = "unknown"


class RenderCanvas(BaseModel):
    model_config = _STRICT

    width: int
    height: int
    fps: int
    pixel_format: str


class RenderAudio(BaseModel):
    model_config = _STRICT

    mode: str
    master_voice_asset_id: str
    sample_rate: int
    codec: str


class RenderMotion(BaseModel):
    model_config = _STRICT

    type: str
    # Only meaningful for scaling motions such as ken_burns.
    scale_start: Optional[float] = None
    scale_end: Optional[float] = None


class RenderSceneEntry(BaseModel):
    model_config = _STRICT

    scene_id: str
    asset_id: str
    start_ms: int
    end_ms: int
    motion: RenderMotion
    caption_ref: Optional[str]


class RenderOutput(BaseModel):
    model_config = _STRICT

    container: str
    video_codec: str
    audio_codec: str


class RenderManifest(BaseModel):
    """SPEC-001 §8."""

    model_config = _STRICT

    content_job_id: str
    canvas: RenderCanvas
    audio: RenderAudio
    scenes: List[RenderSceneEntry]
    subtitle_asset_id: Optional[str]
    output: RenderOutput


class GenerationManifestEntry(BaseModel):
    """One row of the SPEC-001 §6.1 generation manifest.

    The manifest is the hand-off to a human: everything needed to produce this
    scene's visual and drop it in the right place, without reading the job's
    other files. ``import_dir`` is relative to the job directory — an absolute
    path would not survive the job tree being moved between machines.
    """

    model_config = _STRICT

    scene_id: str
    scene_index: int
    semantic_purpose: str
    visual_type: VisualType
    fallback_type: str
    generation_required: bool
    provider: str
    prompt: str
    narration: str
    duration_target_ms: int
    import_dir: str
    expected_filename: str
    accepted_mime_types: List[str]


class GenerationManifest(BaseModel):
    """SPEC-001 §6.1: the assisted-generation work list for one job.

    ``generated_video_scene_count`` sits next to the ceiling it must respect so
    the §4.4 「generated_video 最多 3 個」 rule is checkable from the manifest
    alone, without reloading every scene document.
    """

    model_config = _STRICT

    content_job_id: str
    image_mode: str
    video_mode: str
    max_generated_video_scenes: int
    generated_video_scene_count: int
    entries: List[GenerationManifestEntry]
    created_at: str
