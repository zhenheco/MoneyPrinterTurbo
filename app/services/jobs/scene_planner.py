"""Turn one persisted Script into 8-10 Scenes plus the §6.1 generation manifest.

PLAN-001 issue #5. Two entry points, in order::

    planning = start_scene_planning(job, store)   # SCRIPTING -> SCENE_PLANNING
    scenes = plan_scenes(planning, store)         # writes scenes + manifest

``start_scene_planning`` exists because nothing else closed that edge: issue #4
leaves a job sitting in ``SCRIPTING`` after ``scripts/script.json`` lands, and
§5.2 allows exactly one step from there.

No provider is called and no budget is spent: everything here is a
deterministic function of the persisted Script and the job's own limits, so the
same script always plans the same way. That is a requirement, not a
coincidence — a replan that shuffled scenes would invalidate material a human
had already generated against the previous manifest.

Both functions read the job back from the store rather than trusting the
argument, matching ``budget.check_budget``: a caller holding a stale
:class:`~app.models.content_job.ContentJob` must not be able to re-run a stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.models.content_job import (
    ContentJob,
    GenerationManifest,
    GenerationManifestEntry,
    JobStatus,
    Scene,
    Script,
    VisualType,
)
from app.services.jobs.state_machine import decision_record, transition, utc_now
from app.services.jobs.store import JobStore

#: SPEC-001 §4.4 / PLAN-001 issue #5: a V0 job is always 8 to 10 scenes.
MIN_SCENES = 8
MAX_SCENES = 10

#: §4.4 hard ceiling. The job's own ``max_generated_video_scenes`` may be lower
#: but never higher — ``create_job`` already refuses anything above 3, and this
#: is the second, independent guard on the expensive path.
MAX_GENERATED_VIDEO_SCENES = 3

#: How many body scenes must exist before spending one AI-video slot. A video
#: scene is manual work in Google Flow, so the default stays sparse: the job's
#: ceiling caps this, it does not become a quota to fill.
_BODY_SCENES_PER_GENERATED_VIDEO = 3

#: Where a sentence ends, and where a sentence may be cut in half when a script
#: is too thin to reach ``MIN_SCENES`` on sentence boundaries alone.
_SENTENCE_END = "。！？!?"
_SOFT_BREAK = "，、；;,"

#: Neither half of a cut may be shorter than this, so "split it further" never
#: degrades into single-word scenes.
_MIN_UNIT_CHARS = 6

_CAPTION_MAX_CHARS = 20

_PURPOSE_LABELS = {
    "hook": "開場鉤子",
    "body": "內文",
    "conclusion": "結論",
    "cta": "行動呼籲",
}

#: visual_type -> (provider, fallback_type, generation_required). The provider
#: names are the ones already used by the frozen job fixtures.
_VISUAL_PLAN: Dict[str, Tuple[str, str, bool]] = {
    "generated_image": ("qwen_code_plan", "image_motion", True),
    "generated_video": ("manual_google_flow", "image_motion", True),
    "title_card": ("local_title_card", "none", False),
}

#: visual_type -> (expected file extension, accepted MIME types). Every §4.4
#: visual type is listed even though V0 only plans three of them: an unlisted
#: type must fail loudly rather than default to "image".
_MEDIA_SHAPE: Dict[str, Tuple[str, List[str]]] = {
    "generated_image": (".png", ["image/png", "image/jpeg"]),
    "motion_graphic": (".png", ["image/png", "image/jpeg"]),
    "title_card": (".png", ["image/png", "image/jpeg"]),
    "generated_video": (".mp4", ["video/mp4"]),
    "avatar": (".mp4", ["video/mp4"]),
    "screen_recording": (".mp4", ["video/mp4"]),
}

_MEDIA_LABELS = {
    "generated_image": "一張靜態圖",
    "generated_video": "一段 AI 影片",
    "title_card": "一張標題卡",
}


class ScenePlanError(ValueError):
    """The job or its script cannot be planned into 8-10 scenes."""


@dataclass
class _Unit:
    """One candidate scene: a slice of the script plus where it came from."""

    purpose: str
    text: str


# -- narrative segmentation ------------------------------------------------


def _sentences(text: str) -> List[str]:
    out: List[str] = []
    current = ""
    for character in text:
        current += character
        if character in _SENTENCE_END:
            if current.strip():
                out.append(current.strip())
            current = ""
    if current.strip():
        out.append(current.strip())
    return out


def _initial_units(script: Script) -> List[_Unit]:
    """The script in narrative order, one unit per sentence."""
    segments: List[Tuple[str, str]] = [("hook", script.hook)]
    segments += [("body", item) for item in script.body]
    segments += [("conclusion", script.conclusion), ("cta", script.cta)]
    units: List[_Unit] = []
    for purpose, text in segments:
        units += [_Unit(purpose, sentence) for sentence in _sentences(text)]
    return units


def _split_text(text: str) -> Optional[Tuple[str, str]]:
    """Cut ``text`` at the soft break closest to its midpoint, or give up.

    Ties go to the earlier break so the choice never depends on iteration order.
    """
    midpoint = len(text) / 2
    best: Optional[Tuple[float, str, str]] = None
    for index, character in enumerate(text):
        if character not in _SOFT_BREAK:
            continue
        head, tail = text[: index + 1], text[index + 1 :]
        if len(head) < _MIN_UNIT_CHARS or len(tail) < _MIN_UNIT_CHARS:
            continue
        distance = abs(len(head) - midpoint)
        if best is None or distance < best[0]:
            best = (distance, head, tail)
    return None if best is None else (best[1], best[2])


def _merge_shortest_pair(units: List[_Unit]) -> bool:
    """Join the cheapest adjacent same-purpose pair. False when none is left."""
    best: Optional[Tuple[int, int]] = None
    for index in range(len(units) - 1):
        left, right = units[index], units[index + 1]
        if left.purpose != right.purpose:
            continue
        combined = len(left.text) + len(right.text)
        if best is None or combined < best[0]:
            best = (combined, index)
    if best is None:
        return False
    index = best[1]
    units[index : index + 2] = [
        _Unit(units[index].purpose, units[index].text + units[index + 1].text)
    ]
    return True


def _split_longest(units: List[_Unit]) -> bool:
    """Cut the longest splittable unit in two. False when none can be cut."""
    for position in sorted(range(len(units)), key=lambda i: (-len(units[i].text), i)):
        halves = _split_text(units[position].text)
        if halves is None:
            continue
        purpose = units[position].purpose
        units[position : position + 1] = [
            _Unit(purpose, halves[0]),
            _Unit(purpose, halves[1]),
        ]
        return True
    return False


def _segment(script: Script) -> List[_Unit]:
    units = _initial_units(script)
    if not units:
        raise ScenePlanError("script has no narration to plan scenes from")
    while len(units) > MAX_SCENES:
        if not _merge_shortest_pair(units):
            raise ScenePlanError(
                f"script yields {len(units)} scenes and none of them can be merged "
                f"down to {MAX_SCENES}"
            )
    while len(units) < MIN_SCENES:
        if not _split_longest(units):
            raise ScenePlanError(
                f"script yields only {len(units)} scenes and cannot be split into "
                f"the required {MIN_SCENES}"
            )
    return units


# -- per-scene decisions ---------------------------------------------------


def _video_positions(units: Sequence[_Unit], job: ContentJob) -> frozenset:
    """Which units become ``generated_video``, longest body scenes first."""
    ceiling = min(MAX_GENERATED_VIDEO_SCENES, job.max_generated_video_scenes)
    if ceiling <= 0:
        return frozenset()
    body = [index for index, unit in enumerate(units) if unit.purpose == "body"]
    slots = min(ceiling, len(body) // _BODY_SCENES_PER_GENERATED_VIDEO)
    ranked = sorted(body, key=lambda index: (-len(units[index].text), index))
    return frozenset(ranked[:slots])


def _visual_type(unit: _Unit, position: int, videos: frozenset) -> VisualType:
    if unit.purpose in {"conclusion", "cta"}:
        return "title_card"
    if position in videos:
        return "generated_video"
    return "generated_image"


def _durations(units: Sequence[_Unit], target_duration_sec: int) -> List[int]:
    """Split the requested runtime by narration length, summing to it exactly."""
    total_ms = target_duration_sec * 1000
    weights = [len(unit.text) for unit in units]
    total_weight = sum(weights)
    durations = [total_ms * weight // total_weight for weight in weights]
    remainder = total_ms - sum(durations)
    ranked = sorted(range(len(units)), key=lambda index: (-weights[index], index))
    for step in range(remainder):
        durations[ranked[step % len(ranked)]] += 1
    return durations


def _caption(narration: str) -> str:
    text = narration.rstrip("".join(_SENTENCE_END) + "".join(_SOFT_BREAK))
    if len(text) <= _CAPTION_MAX_CHARS:
        return text
    window = text[:_CAPTION_MAX_CHARS]
    for index in range(len(window) - 1, _MIN_UNIT_CHARS - 1, -1):
        if window[index] in _SOFT_BREAK:
            return window[:index]
    return window


def _phrase(text: str) -> str:
    """Trim a script field's own terminal punctuation before quoting it.

    Without this the prompt reads ``...再決定要不要買模型。。本段旁白`` whenever
    the script's field already ended in a full stop, which most do.
    """
    return text.rstrip(_SENTENCE_END + _SOFT_BREAK + " ")


def _visual_prompt(
    script: Script, scene_id: str, unit: _Unit, visual_type: str, caption: str
) -> str:
    purpose = _PURPOSE_LABELS.get(unit.purpose, unit.purpose)
    media = _MEDIA_LABELS.get(visual_type, visual_type)
    if visual_type == "title_card":
        requirement = (
            "畫面需求：直式 1080x1920 標題卡，深色背景、置中白字，"
            f"主文字為「{caption}」。"
        )
    else:
        requirement = (
            "畫面需求：直式 1080x1920、構圖與旁白語意一致、"
            "畫面內不要出現任何文字或浮水印。"
        )
    return (
        f"為短影音場景 {scene_id}（{purpose}）產生{media}。"
        f"影片主題：{_phrase(script.title)}。"
        f"核心訊息：{_phrase(script.core_message)}。"
        f"本段旁白：「{_phrase(unit.text)}」。{requirement}"
    )


def _build_scenes(job: ContentJob, script: Script) -> List[Scene]:
    units = _segment(script)
    videos = _video_positions(units, job)
    durations = _durations(units, job.target_duration_sec)
    scenes: List[Scene] = []
    for position, unit in enumerate(units):
        scene_id = f"scene-{position + 1:03d}"
        visual_type = _visual_type(unit, position, videos)
        provider, fallback_type, generation_required = _VISUAL_PLAN[visual_type]
        caption = _caption(unit.text)
        scenes.append(
            Scene(
                scene_id=scene_id,
                content_job_id=job.content_job_id,
                scene_index=position + 1,
                semantic_purpose=unit.purpose,
                narration=unit.text,
                caption=caption,
                duration_target_ms=durations[position],
                visual_type=visual_type,
                visual_prompt=_visual_prompt(
                    script, scene_id, unit, visual_type, caption
                ),
                reference_assets=[],
                generation_required=generation_required,
                provider=provider,
                provider_model="",
                fallback_type=fallback_type,
                attempt_count=0,
                status=JobStatus.AWAITING_ASSETS.value,
            )
        )
    return scenes


def _build_manifest(
    job: ContentJob, scenes: Sequence[Scene], store: JobStore
) -> GenerationManifest:
    entries = []
    for scene in scenes:
        extension, mime_types = _MEDIA_SHAPE[scene.visual_type]
        entries.append(
            GenerationManifestEntry(
                scene_id=scene.scene_id,
                scene_index=scene.scene_index,
                semantic_purpose=scene.semantic_purpose,
                visual_type=scene.visual_type,
                fallback_type=scene.fallback_type,
                generation_required=scene.generation_required,
                provider=scene.provider,
                prompt=scene.visual_prompt,
                narration=scene.narration,
                duration_target_ms=scene.duration_target_ms,
                import_dir=store.scene_asset_relative_dir(scene.scene_id),
                expected_filename=f"{scene.scene_id}{extension}",
                accepted_mime_types=list(mime_types),
            )
        )
    return GenerationManifest(
        content_job_id=job.content_job_id,
        image_mode=job.image_mode,
        video_mode=job.video_mode,
        max_generated_video_scenes=job.max_generated_video_scenes,
        generated_video_scene_count=len(
            [scene for scene in scenes if scene.visual_type == "generated_video"]
        ),
        entries=entries,
        created_at=utc_now(),
    )


# -- public API ------------------------------------------------------------


def start_scene_planning(job: ContentJob, store: JobStore) -> ContentJob:
    """Move a scripted job from ``SCRIPTING`` to ``SCENE_PLANNING``."""
    record = store.load(job.content_job_id)
    persisted = record.job
    if record.script is None:
        raise ScenePlanError(
            "scene planning needs a persisted script; run generate_script first"
        )
    if persisted.status is not JobStatus.SCRIPTING:
        raise ScenePlanError(
            f"scene planning starts from SCRIPTING, got {persisted.status.value}"
        )
    reason = "script passed schema validation and was persisted"
    planning = transition(persisted, JobStatus.SCENE_PLANNING, reason=reason)
    store.save(planning)
    store.append_decision(
        job.content_job_id, decision_record(persisted.status, planning, reason)
    )
    return planning


def plan_scenes(job: ContentJob, store: JobStore) -> List[Scene]:
    """Plan, persist and publish the scenes and generation manifest for ``job``.

    Idempotent: a job that already has scenes keeps them, so a replan never
    rewrites the manifest a human is working against and never touches what
    they already imported.

    Publication is three separate writes — scene documents, import directories,
    manifest — so a crash can land between them. Re-running finishes whichever
    ones are missing instead of short-circuiting on the scenes alone and
    leaving the job permanently without its manifest.
    """
    job_id = job.content_job_id
    record = store.load(job_id)
    scenes = record.scenes
    if not scenes:
        if record.job.status is not JobStatus.SCENE_PLANNING:
            raise ScenePlanError(
                f"plan_scenes requires SCENE_PLANNING, got {record.job.status.value}"
            )
        if record.script is None:
            raise ScenePlanError("scene planning needs a persisted script")
        scenes = _build_scenes(record.job, record.script)
        record.scenes = scenes
        store.replace(record)

    # Directories before the manifest: it must never name an import path that
    # is not there yet for whoever reads it. Both steps are create-only.
    for scene in scenes:
        store.scene_asset_dir(job_id, scene.scene_id)
    if store.read_generation_manifest(job_id) is None:
        store.write_generation_manifest(
            job_id, _build_manifest(record.job, scenes, store)
        )
    return scenes
