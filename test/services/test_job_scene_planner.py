"""Issue #5: split a Script into 8-10 Scenes and emit the generation manifest."""

import json
from pathlib import Path

import pytest

from app.models.content_job import (
    ContentJob,
    GenerationManifest,
    JobStatus,
    Script,
)
from app.services.jobs.scene_planner import (
    MAX_SCENES,
    MIN_SCENES,
    ScenePlanError,
    plan_scenes,
    start_scene_planning,
)
from app.services.jobs.store import JobRecord, JobStore

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"


def load_fixture_script(name: str) -> Script:
    payload = json.loads(
        (FIXTURES_ROOT / name / "scripts" / "script.json").read_text(encoding="utf-8")
    )
    return Script.model_validate(payload)


def make_job(**overrides) -> ContentJob:
    fields = {
        "content_job_id": "job-scene-planner",
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "creator_profile_id": "",
        "topic": "企業導入AI最常犯的三個錯誤",
        "language": "zh-TW",
        "target_duration_sec": 50,
        "image_mode": "assisted_qwen",
        "video_mode": "manual_google_flow",
        "max_generated_video_scenes": 3,
        "publish_mode": "postiz_draft",
        "budget_limit_usd": 3.0,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "status": JobStatus.SCRIPTING,
        "created_at": "2026-08-27T00:00:00+00:00",
        "updated_at": "2026-08-27T00:00:00+00:00",
    }
    fields.update(overrides)
    return ContentJob(**fields)


def script_with_body(count: int) -> Script:
    return Script(
        title="短影音沒人看完的五個原因",
        target_audience="自媒體經營者",
        core_message="完播率是可以逐項拆解修好的。",
        hook="你的短影音沒有人看完，問題不在演算法。",
        body=[f"第 {index + 1} 個問題是節奏太平，觀眾撐不到轉折。" for index in range(count)],
        conclusion="從下一支片的第一秒開始改，這週就看得到差別。",
        cta="留言告訴我你卡在哪一項。",
        claims=[],
        sources=[],
        risk_flags=[],
    )


def seeded_store(tmp_path, script: Script, **job_overrides):
    """A store holding one job in SCRIPTING with ``script`` already persisted."""
    store = JobStore(tmp_path)
    job = make_job(**job_overrides)
    store.create(JobRecord(job=job, script=script))
    return store, job


def planned(tmp_path, script: Script, **job_overrides):
    store, job = seeded_store(tmp_path, script, **job_overrides)
    planning = start_scene_planning(job, store)
    scenes = plan_scenes(planning, store)
    return store, planning, scenes


# -- the SCRIPTING -> SCENE_PLANNING bridge --------------------------------


def test_start_scene_planning_moves_a_scripted_job_and_records_the_decision(tmp_path):
    store, job = seeded_store(tmp_path, load_fixture_script("three-scene-demo"))

    planning = start_scene_planning(job, store)

    assert planning.status is JobStatus.SCENE_PLANNING
    reloaded = store.load(job.content_job_id)
    assert reloaded.job.status is JobStatus.SCENE_PLANNING
    assert reloaded.decisions[-1]["from"] == JobStatus.SCRIPTING.value
    assert reloaded.decisions[-1]["to"] == JobStatus.SCENE_PLANNING.value
    assert reloaded.decisions[-1]["reason"]
    assert reloaded.script is not None


def test_start_scene_planning_refuses_a_job_without_a_script(tmp_path):
    store = JobStore(tmp_path)
    job = make_job()
    store.create(job)

    with pytest.raises(ScenePlanError, match="script"):
        start_scene_planning(job, store)

    assert store.load(job.content_job_id).job.status is JobStatus.SCRIPTING


def test_start_scene_planning_uses_the_persisted_status_not_the_argument(tmp_path):
    store, job = seeded_store(tmp_path, load_fixture_script("three-scene-demo"))
    start_scene_planning(job, store)

    # ``job`` is now stale: on disk the job already left SCRIPTING.
    with pytest.raises(ScenePlanError):
        start_scene_planning(job, store)


@pytest.mark.parametrize(
    "status", [JobStatus.SCRIPTING, JobStatus.DRAFT, JobStatus.AWAITING_ASSETS]
)
def test_plan_scenes_requires_scene_planning_status(tmp_path, status):
    store, job = seeded_store(
        tmp_path, load_fixture_script("three-scene-demo"), status=status
    )

    with pytest.raises(ScenePlanError, match="SCENE_PLANNING"):
        plan_scenes(job, store)

    assert store.load(job.content_job_id).scenes == []


# -- 8-10 scenes always hold ----------------------------------------------


@pytest.mark.parametrize("fixture", ["three-scene-demo", "ten-scene-demo"])
def test_frozen_fixtures_plan_into_the_allowed_scene_range(tmp_path, fixture):
    _, _, scenes = planned(tmp_path, load_fixture_script(fixture))

    assert MIN_SCENES <= len(scenes) <= MAX_SCENES


@pytest.mark.parametrize("body_count", list(range(1, 13)))
def test_any_body_length_still_plans_into_the_allowed_range(tmp_path, body_count):
    _, _, scenes = planned(tmp_path, script_with_body(body_count))

    assert MIN_SCENES <= len(scenes) <= MAX_SCENES


def test_a_punctuation_poor_script_is_still_cut_into_the_minimum(tmp_path):
    """No commas to cut on must not wedge the job.

    §5.2 has no edge out of SCENE_PLANNING for a planning failure, so refusing
    here would strand the job with no operator signal. A midpoint cut is the
    lesser evil and stays inside the 8-10 contract.
    """
    unpunctuated = Script(
        title="完播率拆解",
        target_audience="自媒體經營者",
        core_message="完播率可以逐項拆解修好",
        hook="你的短影音沒有人看完問題其實不在演算法而在前三秒",
        body=["第一個原因是資訊密度太低前十秒只講了一句話沒有給觀眾理由留下"],
        conclusion="從下一支片的第一秒開始改這週就看得到差別不需要換設備",
        cta="留言告訴我你卡在哪一項我會逐題回覆",
        claims=[],
        sources=[],
        risk_flags=[],
    )

    _, _, scenes = planned(tmp_path, unpunctuated)

    assert MIN_SCENES <= len(scenes) <= MAX_SCENES
    assert all(scene.narration.strip() for scene in scenes)


@pytest.mark.parametrize(
    "body_count,expected", [(5, 8), (6, 9), (7, 10), (8, 10), (11, 10)]
)
def test_scene_count_follows_the_script_until_the_ceiling(
    tmp_path, body_count, expected
):
    _, _, scenes = planned(tmp_path, script_with_body(body_count))

    assert len(scenes) == expected


def test_indivisible_script_that_cannot_reach_the_minimum_is_rejected(tmp_path):
    tiny = Script(
        title="短",
        target_audience="",
        core_message="",
        hook="一。",
        body=["二。"],
        conclusion="三。",
        cta="四。",
        claims=[],
        sources=[],
        risk_flags=[],
    )
    store, job = seeded_store(tmp_path, tiny)
    planning = start_scene_planning(job, store)

    with pytest.raises(ScenePlanError, match="8"):
        plan_scenes(planning, store)

    assert store.load(job.content_job_id).scenes == []


# -- scene shape -----------------------------------------------------------


def test_scenes_are_contiguous_ordered_and_uniquely_identified(tmp_path):
    _, job, scenes = planned(tmp_path, script_with_body(5))

    assert [scene.scene_index for scene in scenes] == list(range(1, len(scenes) + 1))
    assert [scene.scene_id for scene in scenes] == [
        f"scene-{index:03d}" for index in range(1, len(scenes) + 1)
    ]
    assert all(scene.content_job_id == job.content_job_id for scene in scenes)


def test_every_scene_carries_a_usable_prompt_and_fallback(tmp_path):
    _, _, scenes = planned(tmp_path, script_with_body(5))

    for scene in scenes:
        assert scene.narration.strip()
        assert scene.caption.strip()
        assert scene.visual_prompt.strip()
        assert scene.fallback_type.strip()
        assert scene.semantic_purpose in {"hook", "body", "conclusion", "cta"}
        assert scene.status == JobStatus.AWAITING_ASSETS.value
        assert scene.attempt_count == 0


def test_durations_add_up_to_the_requested_target(tmp_path):
    _, _, scenes = planned(tmp_path, script_with_body(5), target_duration_sec=50)

    assert sum(scene.duration_target_ms for scene in scenes) == 50_000
    assert all(scene.duration_target_ms > 0 for scene in scenes)


@pytest.mark.parametrize("body_count", [1, 5, 11])
def test_narration_reassembles_into_the_original_script_in_order(
    tmp_path, body_count
):
    """Splitting and merging must move text around, never lose or reorder it.

    Body 1 exercises splitting, 5 exercises neither, 11 exercises merging.
    """
    script = script_with_body(body_count)
    _, _, scenes = planned(tmp_path, script)

    assert "".join(scene.narration for scene in scenes) == "".join(
        [script.hook, *script.body, script.conclusion, script.cta]
    )


# -- the generated_video ceiling ------------------------------------------


@pytest.mark.parametrize("ceiling", [0, 1, 2, 3])
def test_every_ceiling_is_respected_for_a_video_heavy_script(tmp_path, ceiling):
    _, _, scenes = planned(
        tmp_path, script_with_body(11), max_generated_video_scenes=ceiling
    )

    videos = [scene for scene in scenes if scene.visual_type == "generated_video"]
    assert len(videos) <= ceiling
    assert len(videos) <= 3


def test_a_zero_video_job_plans_no_generated_video_scene(tmp_path):
    _, _, scenes = planned(tmp_path, script_with_body(11), max_generated_video_scenes=0)

    assert not [scene for scene in scenes if scene.visual_type == "generated_video"]


def test_generated_video_scenes_are_chosen_deterministically(tmp_path):
    first = planned(tmp_path / "a", script_with_body(9))[2]
    second = planned(tmp_path / "b", script_with_body(9))[2]

    assert [scene.visual_type for scene in first] == [
        scene.visual_type for scene in second
    ]
    assert [scene.narration for scene in first] == [scene.narration for scene in second]


# -- per-scene directories and the manifest --------------------------------


def test_each_scene_gets_its_own_import_directory_inside_the_job(tmp_path):
    store, job, scenes = planned(tmp_path, script_with_body(5))
    job_dir = tmp_path / job.content_job_id

    manifest = store.read_generation_manifest(job.content_job_id)
    assert manifest is not None
    assert len(manifest.entries) == len(scenes)

    seen = set()
    root = job_dir.resolve()
    for entry in manifest.entries:
        assert not Path(entry.import_dir).is_absolute()
        assert ".." not in Path(entry.import_dir).parts
        resolved = (job_dir / entry.import_dir).resolve()
        assert resolved.is_dir()
        # is_relative_to, not a string prefix: a "/" suffix check is wrong on
        # Windows, which this repo's CI does run.
        assert resolved.is_relative_to(root)
        assert resolved != root
        assert entry.import_dir not in seen
        seen.add(entry.import_dir)


def test_manifest_lists_every_scene_with_a_prompt_and_an_import_path(tmp_path):
    _, job, scenes = planned(tmp_path, script_with_body(5))

    manifest_path = tmp_path / job.content_job_id / "generation_manifest.json"
    manifest = GenerationManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )

    assert manifest.content_job_id == job.content_job_id
    assert [entry.scene_id for entry in manifest.entries] == [
        scene.scene_id for scene in scenes
    ]
    for entry, scene in zip(manifest.entries, scenes):
        assert entry.prompt.strip()
        assert entry.import_dir.strip()
        assert entry.expected_filename.strip()
        assert entry.accepted_mime_types
        assert entry.visual_type == scene.visual_type
        assert entry.fallback_type == scene.fallback_type
        assert entry.generation_required == scene.generation_required


def test_manifest_video_count_matches_the_planned_scenes(tmp_path):
    store, job, scenes = planned(tmp_path, script_with_body(9))

    manifest = store.read_generation_manifest(job.content_job_id)
    assert manifest.generated_video_scene_count == len(
        [scene for scene in scenes if scene.visual_type == "generated_video"]
    )
    assert manifest.generated_video_scene_count <= manifest.max_generated_video_scenes


def test_scenes_and_manifest_survive_a_store_round_trip(tmp_path):
    store, job, scenes = planned(tmp_path, script_with_body(5))

    reloaded = store.load(job.content_job_id)
    assert reloaded.scenes == scenes
    assert reloaded.job.status is JobStatus.SCENE_PLANNING
    assert store.read_generation_manifest(job.content_job_id) is not None


# -- re-running is safe ----------------------------------------------------


def test_replanning_is_idempotent_and_keeps_imported_material(tmp_path):
    store, job, scenes = planned(tmp_path, script_with_body(5))
    job_dir = tmp_path / job.content_job_id
    manifest_before = store.read_generation_manifest(job.content_job_id)

    imported = job_dir / manifest_before.entries[0].import_dir / "hand-made.png"
    imported.write_bytes(b"human asset")

    again = plan_scenes(store.load(job.content_job_id).job, store)

    assert again == scenes
    assert store.read_generation_manifest(job.content_job_id) == manifest_before
    assert imported.read_bytes() == b"human asset"
    scene_dirs = sorted(path.name for path in (job_dir / "assets" / "scenes").iterdir())
    assert scene_dirs == sorted(scene.scene_id for scene in scenes)


def test_rerun_restores_a_manifest_lost_between_the_two_writes(tmp_path):
    """Scenes and manifest are separate writes; a crash can land between them."""
    store, job, scenes = planned(tmp_path, script_with_body(5))
    manifest_path = tmp_path / job.content_job_id / "generation_manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.unlink()

    again = plan_scenes(store.load(job.content_job_id).job, store)

    assert again == scenes
    restored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert restored["entries"] == before["entries"]
    assert restored["generated_video_scene_count"] == before["generated_video_scene_count"]


def test_rerun_rebuilds_a_scene_set_left_partial_by_a_crash(tmp_path):
    """``JobStore.replace`` writes one file per scene; a crash leaves a prefix.

    Treating that prefix as "already planned" would publish a manifest with
    fewer than MIN_SCENES entries and wedge the job there for good.
    """
    store, job, scenes = planned(tmp_path, script_with_body(5))
    job_dir = tmp_path / job.content_job_id
    for scene in scenes[3:]:
        (job_dir / "scenes" / f"scene-{scene.scene_index:03d}.json").unlink()
    (job_dir / "generation_manifest.json").unlink()
    assert len(store.load(job.content_job_id).scenes) == 3

    again = plan_scenes(store.load(job.content_job_id).job, store)

    assert again == scenes
    assert store.load(job.content_job_id).scenes == scenes
    manifest = store.read_generation_manifest(job.content_job_id)
    assert len(manifest.entries) == len(scenes)


def test_rerun_recreates_an_import_directory_that_was_removed(tmp_path):
    store, job, scenes = planned(tmp_path, script_with_body(5))
    scene_dirs = tmp_path / job.content_job_id / "assets" / "scenes"
    (scene_dirs / scenes[0].scene_id).rmdir()

    plan_scenes(store.load(job.content_job_id).job, store)

    assert (scene_dirs / scenes[0].scene_id).is_dir()


def test_replanning_does_not_append_a_second_transition(tmp_path):
    store, job, _ = planned(tmp_path, script_with_body(5))
    before = len(store.load(job.content_job_id).decisions)

    plan_scenes(store.load(job.content_job_id).job, store)

    assert len(store.load(job.content_job_id).decisions) == before
