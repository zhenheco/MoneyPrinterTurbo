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
from app.services.jobs.budget import redact
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

#: The stages a job reaches *after* scene planning succeeded. A plan persisted
#: while the job sits in one of these is frozen — later stages already built on
#: those exact scenes — so it is checked structurally instead of recomputed.
#:
#: Enumerated rather than subtracted, because the interesting exclusions are
#: not the pre-planning stages but the failure ones. A job parked in
#: ``MANUAL_ACTION_REQUIRED`` still has its previous scenes on disk; adopting
#: them would hand the caller a plan and republish a manifest as if nothing
#: were wrong, hiding the very state that is asking for a human.
_FROZEN_PLAN_STATUSES = frozenset(
    {
        JobStatus.VOICE_GENERATING,
        JobStatus.AWAITING_ASSETS,
        JobStatus.IMAGE_GENERATING,
        JobStatus.VIDEO_GENERATING,
        JobStatus.READY_TO_RENDER,
        JobStatus.RENDERING,
        JobStatus.TECHNICAL_QA,
        JobStatus.CONTENT_QA,
        JobStatus.READY_FOR_REVIEW,
        JobStatus.POSTIZ_DRAFTING,
        JobStatus.POSTIZ_DRAFTED,
        JobStatus.APPROVED,
        JobStatus.SCHEDULED,
        JobStatus.PUBLISHED,
    }
)

_CAPTION_MAX_CHARS = 20

#: What a prompt says instead of quoting narration whose source field tripped
#: the credential filter. Deliberately actionable: over-redaction here is
#: usually a false positive (``budget.redact`` treats the bare word ``token``
#: as credential-shaped), and the operator needs to know which it is.
_WITHHELD = "（本段旁白被憑證過濾器標記，已從 prompt 略去；請確認腳本是否誤含憑證）"

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

#: visual_type -> (SPEC-001 §3.2 per-scene directory, file extension, accepted
#: MIME types). Every §4.4 visual type is listed even though V0 only plans
#: three of them: an unlisted type must fail loudly rather than default to
#: "image".
#:
#: One MIME type per extension, on purpose. §7 validates the file by MIME
#: sniffing *and* extension, so advertising ``image/jpeg`` next to a required
#: ``.png`` filename would tell the operator to produce something the import
#: step then rejects. Widening this means widening the filename rule with it.
_MEDIA_SHAPE: Dict[str, Tuple[str, str, List[str]]] = {
    "generated_image": ("images", ".png", ["image/png"]),
    "motion_graphic": ("images", ".png", ["image/png"]),
    "title_card": ("images", ".png", ["image/png"]),
    "generated_video": ("videos", ".mp4", ["video/mp4"]),
    "avatar": ("videos", ".mp4", ["video/mp4"]),
    "screen_recording": ("videos", ".mp4", ["video/mp4"]),
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
    #: The Script field this slice came from matched the credential filter.
    #:
    #: The slice itself is kept raw — it is what the voice stage speaks — but
    #: the prompt must not quote it. Redacting the slices instead of the field
    #: does not work: measured, ``api_key=sk-x,<token>`` splits at the ASCII
    #: comma into ``<redacted>`` and a bare token that no pattern matches any
    #: more. Deciding per source field is what makes the prompt safe whatever
    #: way the split happened to fall.
    suspect: bool = False


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
    """The script in narrative order, one unit per sentence.

    Each field is *tested* against the credential filter whole, before it is
    cut up, and the verdict rides along on every unit it produces. The text
    itself is never rewritten here.

    Both halves of that matter. Testing the whole field is what closes the
    split hole — measured, ``api_key=sk-x,<token>`` cut at the ASCII comma
    leaves a bare token no pattern matches. Not rewriting is what keeps
    narration usable: ``budget.redact`` is a deliberately greedy *summary*
    filter, and measured, it turns the ordinary sentence ``token economy 正在
    改變創作者的收入結構`` into ``<redacted> 正在改變創作者的收入結構``. Running
    narration through it would corrupt legitimate scripts, which is the same
    mistake that once ate a job's idempotency key.
    """
    segments: List[Tuple[str, str]] = [("hook", script.hook)]
    segments += [("body", item) for item in script.body]
    segments += [("conclusion", script.conclusion), ("cta", script.cta)]
    units: List[_Unit] = []
    for purpose, text in segments:
        suspect = redact(text) != text
        units += [_Unit(purpose, sentence, suspect) for sentence in _sentences(text)]
    return units


def _usable_cut(text: str, cut: int) -> bool:
    """A cut is usable only if both halves are long enough and carry content.

    The length floor alone is not enough: a run of whitespace is long, so a
    purely positional cut can carve out a scene whose narration is blank.
    """
    head, tail = text[:cut], text[cut:]
    return (
        len(head) >= _MIN_UNIT_CHARS
        and len(tail) >= _MIN_UNIT_CHARS
        and bool(head.strip())
        and bool(tail.strip())
    )


def _split_text(text: str) -> Optional[Tuple[str, str]]:
    """Cut ``text`` in two: at the soft break nearest its midpoint if there is
    a usable one, otherwise at the usable position nearest the midpoint.

    Ties go to the earlier cut so the choice never depends on iteration order.

    The positional fallback is deliberate. PLAN-001 row 5 asks for 8–10 scenes
    to hold for every script, and a script whose sentences carry no commas
    would otherwise be refused even though it has plenty of content. A slightly
    awkward cut is recoverable — a human edits the script and replans. A script
    genuinely too short to make ``MIN_SCENES`` readable units still returns
    ``None`` here, and :func:`plan_scenes` turns that into
    ``MANUAL_ACTION_REQUIRED`` rather than leaving the job silent.
    """
    midpoint = len(text) / 2
    soft = [
        index + 1
        for index, character in enumerate(text)
        if character in _SOFT_BREAK and _usable_cut(text, index + 1)
    ]
    candidates = soft or [cut for cut in range(1, len(text)) if _usable_cut(text, cut)]
    if not candidates:
        return None
    best = min(candidates, key=lambda cut: (abs(cut - midpoint), cut))
    return text[:best], text[best:]


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
        _Unit(
            units[index].purpose,
            units[index].text + units[index + 1].text,
            units[index].suspect or units[index + 1].suspect,
        )
    ]
    return True


def _split_longest(units: List[_Unit]) -> bool:
    """Cut the longest splittable unit in two. False when none can be cut."""
    for position in sorted(range(len(units)), key=lambda i: (-len(units[i].text), i)):
        halves = _split_text(units[position].text)
        if halves is None:
            continue
        purpose = units[position].purpose
        suspect = units[position].suspect
        units[position : position + 1] = [
            _Unit(purpose, halves[0], suspect),
            _Unit(purpose, halves[1], suspect),
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
                f"script yields only {len(units)} scenes and is too short to split "
                f"into the required {MIN_SCENES}: every remaining unit is under "
                f"{_MIN_UNIT_CHARS * 2} characters"
            )
    return units


def _is_structurally_complete(job: ContentJob, scenes: Sequence[Scene]) -> bool:
    """The invariants a whole plan satisfies, without recomputing it."""
    return (
        MIN_SCENES <= len(scenes) <= MAX_SCENES
        and all(scene.content_job_id == job.content_job_id for scene in scenes)
        and [scene.scene_index for scene in scenes] == list(range(1, len(scenes) + 1))
        and len({scene.scene_id for scene in scenes}) == len(scenes)
    )


def _is_current_plan(record) -> bool:
    """Is what is on disk this script's whole plan, or the debris of a crash?

    ``JobStore.replace`` writes one ``scene-NNN.json`` per scene, so a crash
    part-way through leaves a prefix. Counting the files is not enough — an
    8-file prefix of a planned 10-scene job is a perfectly plausible count —
    so while the job is still in ``SCENE_PLANNING`` the plan is recomputed and
    compared outright. That is only affordable because planning is a pure
    function of the job and its script.

    Past ``SCENE_PLANNING`` the plan is frozen and must not be recomputed:
    later stages own those scenes, and a script edited afterwards must not
    silently reshuffle them. The structural invariants are still checked
    though, so a damaged set is refused rather than published as a manifest
    with one entry in it.

    Before ``SCENE_PLANNING`` nothing is accepted at all. Scenes sitting on
    disk while the job is still in ``DRAFT`` or ``SCRIPTING`` are not a plan
    this stage may publish, and letting them through here would walk straight
    past :func:`plan_scenes`'s status guard.
    """
    scenes = record.scenes
    if not scenes:
        return False
    if record.job.status is JobStatus.SCENE_PLANNING:
        if record.script is None:
            return False
        try:
            return list(scenes) == _build_scenes(record.job, record.script)
        except ScenePlanError:
            return False
    if record.job.status not in _FROZEN_PLAN_STATUSES:
        return False
    return _is_structurally_complete(record.job, scenes)


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
    """On-screen text for one scene: never blank, never longer than the cap.

    Internal whitespace is collapsed first. Without that, narration carrying a
    long run of spaces truncates to a caption made entirely of them, which is
    not a caption at all.
    """
    collapsed = " ".join(narration.split())
    text = collapsed.rstrip(_SENTENCE_END + _SOFT_BREAK).strip() or collapsed
    if len(text) <= _CAPTION_MAX_CHARS:
        return text
    window = text[:_CAPTION_MAX_CHARS]
    for index in range(len(window) - 1, _MIN_UNIT_CHARS - 1, -1):
        if window[index] in _SOFT_BREAK:
            return window[:index]
    return window


def _prompt_fragment(text: str) -> str:
    """A whole Script field, or the withheld marker if the filter reacted.

    Never the filter's *output*: see :func:`_visual_prompt` for why a partially
    redacted string is not a safe substitute.
    """
    return _WITHHELD if redact(text) != text else _phrase(text)


def _phrase(text: str) -> str:
    """Trim a script field's own terminal punctuation before quoting it.

    Without this the prompt reads ``...再決定要不要買模型。。本段旁白`` whenever
    the script's field already ended in a full stop, which most do.
    """
    return text.rstrip(_SENTENCE_END + _SOFT_BREAK + " ")


def _visual_prompt(
    script: Script, scene_id: str, unit: _Unit, visual_type: str, caption: str
) -> str:
    """Build one scene's generation prompt from the script.

    PRD-001 FR-004A and SPEC-001 §12 / §14 all say the same thing in the same
    words: ``secret、credential 不得寫入 log、audit 摘要或 prompt``. A ``Script``
    is model output built from a user-supplied topic, so it is exactly the kind
    of text that can carry one by accident.

    One rule for every script-derived fragment: if the credential filter reacts
    to the source field *at all*, that field does not appear in the prompt.

    Substituting the filter's own output is not good enough, and that is the
    whole point. Measured, ``redact("api_key=sk-x,abcdefghijklmnopqrst")``
    returns ``<redacted>,abcdefghijklmnopqrst`` — the pattern stops at the
    ASCII comma and the bare token rides along. Withholding the field is what
    makes the prompt safe regardless of how well the filter matched.

    What is never filtered: ``scene_id`` and the fixed labels (locally built
    identifiers — running those through a credential filter is what once turned
    an idempotency key into ``<redacted>`` and double-billed), and
    ``Scene.narration`` itself, which the voice stage has to speak.
    """
    purpose = _PURPOSE_LABELS.get(unit.purpose, unit.purpose)
    media = _MEDIA_LABELS.get(visual_type, visual_type)
    if visual_type == "title_card":
        card_text = _WITHHELD if unit.suspect else caption
        requirement = (
            "畫面需求：直式 1080x1920 標題卡，深色背景、置中白字，"
            f"主文字為「{card_text}」。"
        )
    else:
        requirement = (
            "畫面需求：直式 1080x1920、構圖與旁白語意一致、"
            "畫面內不要出現任何文字或浮水印。"
        )
    line = _WITHHELD if unit.suspect else f"「{_phrase(unit.text)}」"
    return (
        f"為短影音場景 {scene_id}（{purpose}）產生{media}。"
        f"影片主題：{_prompt_fragment(script.title)}。"
        f"核心訊息：{_prompt_fragment(script.core_message)}。"
        f"本段旁白：{line}。{requirement}"
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
        kind, extension, mime_types = _MEDIA_SHAPE[scene.visual_type]
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
                import_dir=store.scene_media_relative_dir(scene.scene_id, kind),
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


def _persist_unplannable(
    job: ContentJob, store: JobStore, error: ScenePlanError
) -> None:
    """Park a job whose script cannot be planned, instead of leaving it silent.

    §5.2 does allow ``SCENE_PLANNING -> MANUAL_ACTION_REQUIRED``, and
    ``classify_error`` puts a ``ValueError`` in the non-retryable class, so
    this is the same shape ``pipeline._persist_failed_status`` uses. Without
    it the job would sit in ``SCENE_PLANNING`` looking healthy while every
    replan raised.
    """
    current = store.load(job.content_job_id).job
    if current.status is not JobStatus.SCENE_PLANNING:
        return
    reason = f"scene planning failed (non_retryable): {error}"
    parked = transition(current, JobStatus.MANUAL_ACTION_REQUIRED, reason=reason)
    store.save(parked)
    store.append_decision(
        job.content_job_id, decision_record(current.status, parked, reason)
    )


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

    Idempotent: a job that already holds a *complete* plan keeps it, so a
    replan never rewrites the manifest a human is working against and never
    touches what they already imported.

    Publication is several separate writes — one file per scene, then the
    import directories, then the manifest — so a crash can land between any of
    them. Re-running finishes whichever are missing, and rebuilds a scene set
    that is only a partial prefix. "Already did some of it" is not the same as
    "done", and short-circuiting on the first of those would wedge the job.
    """
    job_id = job.content_job_id
    record = store.load(job_id)
    scenes = record.scenes
    if not _is_current_plan(record):
        if record.job.status is not JobStatus.SCENE_PLANNING:
            raise ScenePlanError(
                f"plan_scenes requires SCENE_PLANNING, got {record.job.status.value}"
            )
        if record.script is None:
            raise ScenePlanError("scene planning needs a persisted script")
        try:
            scenes = _build_scenes(record.job, record.script)
        except ScenePlanError as error:
            _persist_unplannable(record.job, store, error)
            raise
        record.scenes = scenes
        store.replace(record)

    # Directories before the manifest: it must never name an import path that
    # is not there yet for whoever reads it. Both steps are create-only.
    for scene in scenes:
        store.scene_media_dir(job_id, scene.scene_id, _MEDIA_SHAPE[scene.visual_type][0])
    # The manifest is compared against the scenes rather than against "did I
    # rebuild on this call". A crash between the scene write and the manifest
    # write leaves a manifest that describes the *previous* plan, and the next
    # call sees scenes that now match the script and would think there was
    # nothing to do. Only the timestamp is exempt: it records when the manifest
    # was written, not what it says.
    expected = _build_manifest(record.job, scenes, store)
    published = store.read_generation_manifest(job_id)
    if published is None or published.model_dump(
        exclude={"created_at"}
    ) != expected.model_dump(exclude={"created_at"}):
        store.write_generation_manifest(job_id, expected)
    return scenes
