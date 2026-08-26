"""File-backed job store behaviour: round-trip, append order and path safety."""

import json
from pathlib import Path

import pytest

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
from app.services.jobs.store import JobRecord, JobStore, JobStoreError
from test.services.test_content_job_models import (
    asset_record_payload,
    content_job_payload,
    provider_event_payload,
    render_manifest_payload,
    scene_payload,
    script_payload,
    usage_ledger_payload,
)

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"


def build_record(job_id="job-20260816-001", scene_count=1):
    job = ContentJob.model_validate({**content_job_payload(), "content_job_id": job_id})
    scenes = []
    for index in range(1, scene_count + 1):
        payload = scene_payload()
        payload["scene_id"] = f"scene-{index:03d}"
        payload["content_job_id"] = job_id
        payload["scene_index"] = index
        scenes.append(Scene.model_validate(payload))
    manifest_payload = render_manifest_payload()
    manifest_payload["content_job_id"] = job_id
    manifest_payload["scenes"] = [
        {
            "scene_id": scene.scene_id,
            "asset_id": f"asset-{scene.scene_index:03d}",
            "start_ms": (scene.scene_index - 1) * 5000,
            "end_ms": scene.scene_index * 5000,
            "motion": {"type": "ken_burns", "scale_start": 1.0, "scale_end": 1.08},
            "caption_ref": f"caption-{scene.scene_index:03d}",
        }
        for scene in scenes
    ]
    return JobRecord(
        job=job,
        script=Script.model_validate(script_payload()),
        scenes=scenes,
        render_manifest=RenderManifest.model_validate(manifest_payload),
    )


class TestJobStoreRoundTrip:
    def test_create_then_load_returns_every_field(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record()

        store.create(record)
        loaded = store.load("job-20260816-001")

        assert loaded.job == record.job
        assert loaded.script == record.script
        assert loaded.scenes == record.scenes
        assert loaded.render_manifest == record.render_manifest

    def test_replace_then_load_preserves_updates(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record()
        store.create(record)

        loaded = store.load("job-20260816-001")
        loaded.job.status = JobStatus.RENDERING
        loaded.job.actual_cost_usd = "unknown"
        loaded.script.title = "updated title"
        store.replace(loaded)
        reloaded = store.load("job-20260816-001")

        assert reloaded.job.status is JobStatus.RENDERING
        assert reloaded.job.actual_cost_usd == "unknown"
        assert reloaded.script.title == "updated title"
        assert reloaded.scenes == record.scenes

    def test_create_writes_the_documented_directory_layout(self, tmp_path):
        store = JobStore(tmp_path)

        store.create(build_record(scene_count=2))

        job_dir = tmp_path / "job-20260816-001"
        for relative in (
            "job.json",
            "scripts/script.json",
            "scenes/scene-001.json",
            "scenes/scene-002.json",
            "assets/assets.jsonl",
            "provider_events.jsonl",
            "usage_ledger.jsonl",
            "decisions.jsonl",
            "render_manifest.json",
        ):
            assert (job_dir / relative).exists(), relative

    def test_create_accepts_a_bare_content_job(self, tmp_path):
        store = JobStore(tmp_path)
        job = ContentJob.model_validate(content_job_payload())

        store.create(job)
        loaded = store.load(job.content_job_id)

        assert loaded.job == job
        assert loaded.script is None
        assert loaded.scenes == []

    def test_create_twice_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())

        with pytest.raises(JobStoreError) as raised:
            store.create(build_record())

        assert "job-20260816-001" in str(raised.value)

    def test_load_of_a_missing_job_raises_a_clear_error(self, tmp_path):
        store = JobStore(tmp_path)

        with pytest.raises(JobStoreError) as raised:
            store.load("job-does-not-exist")

        assert "job-does-not-exist" in str(raised.value)

    def test_replace_of_a_missing_job_raises_a_clear_error(self, tmp_path):
        store = JobStore(tmp_path)

        with pytest.raises(JobStoreError):
            store.replace(build_record(job_id="never-created"))

    def test_a_colon_in_a_job_id_is_rejected_before_anything_is_written(
        self, tmp_path
    ):
        """``build_idempotency_key`` splits on ``:``, so a job id carrying one
        can never produce a key. Accepting it here only defers the ValueError
        to the point where a provider call has already been made."""
        store = JobStore(tmp_path)
        job = ContentJob.model_validate(content_job_payload())
        job.content_job_id = "tenant:job-1"

        with pytest.raises(JobStoreError) as raised:
            store.create(job)

        assert "opaque token" in str(raised.value)
        assert list(tmp_path.rglob("job.json")) == []


class TestJobStoreAppend:
    def test_provider_events_read_back_in_write_order(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        first = ProviderEvent.model_validate(
            {**provider_event_payload(), "provider_event_id": "provider-event-001"}
        )
        second = ProviderEvent.model_validate(
            {**provider_event_payload(), "provider_event_id": "provider-event-002"}
        )

        store.append_event("job-20260816-001", first)
        store.append_event("job-20260816-001", second)

        loaded = store.load("job-20260816-001")
        assert [event.provider_event_id for event in loaded.provider_events] == [
            "provider-event-001",
            "provider-event-002",
        ]

    def test_append_event_routes_each_record_to_its_own_file(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        asset = AssetRecord.model_validate(asset_record_payload())
        event = ProviderEvent.model_validate(provider_event_payload())
        ledger = UsageLedgerEntry.model_validate(usage_ledger_payload())

        store.append_event("job-20260816-001", asset)
        store.append_event("job-20260816-001", event)
        store.append_event("job-20260816-001", ledger)

        loaded = store.load("job-20260816-001")
        assert [record.asset_id for record in loaded.assets] == ["asset-001"]
        assert [record.provider_event_id for record in loaded.provider_events] == [
            "provider-event-001"
        ]
        assert [record.idempotency_key for record in loaded.usage_ledger] == [
            "job-20260816-001:scene-001:video:attempt-1"
        ]

    def test_decisions_read_back_in_write_order(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())

        store.append_decision(
            "job-20260816-001", {"from": "DRAFT", "to": "SCRIPTING", "reason": "input ok"}
        )
        store.append_decision(
            "job-20260816-001",
            {"from": "SCRIPTING", "to": "SCENE_PLANNING", "reason": "schema ok"},
        )

        loaded = store.load("job-20260816-001")
        assert [record["to"] for record in loaded.decisions] == [
            "SCRIPTING",
            "SCENE_PLANNING",
        ]

    def test_append_does_not_rewrite_earlier_lines(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        events_file = tmp_path / "job-20260816-001" / "provider_events.jsonl"

        store.append_event(
            "job-20260816-001", ProviderEvent.model_validate(provider_event_payload())
        )
        after_first = events_file.read_bytes()
        store.append_event(
            "job-20260816-001",
            ProviderEvent.model_validate(
                {**provider_event_payload(), "provider_event_id": "provider-event-002"}
            ),
        )

        assert events_file.read_bytes().startswith(after_first)

    def test_replace_does_not_touch_append_only_files(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        store.append_event(
            "job-20260816-001", ProviderEvent.model_validate(provider_event_payload())
        )
        events_file = tmp_path / "job-20260816-001" / "provider_events.jsonl"
        before = events_file.read_bytes()

        record = store.load("job-20260816-001")
        record.job.status = JobStatus.RENDERING
        store.replace(record)

        assert events_file.read_bytes() == before

    def test_append_to_a_missing_job_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)

        with pytest.raises(JobStoreError):
            store.append_event(
                "job-does-not-exist",
                ProviderEvent.model_validate(provider_event_payload()),
            )

    def test_unknown_record_type_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())

        with pytest.raises(JobStoreError):
            store.append_event("job-20260816-001", {"not": "a model"})


class TestJobStorePathSafety:
    ESCAPING_IDS = (
        "../evil",
        "/etc/passwd",
        "a/b",
        "a\\b",
        "..",
        ".",
        "",
        "   ",
        "job\x00id",
        "~/evil",
        "a..b",
    )
    #: The token guard rejects the id itself; the realpath guard rejects a
    #: well-formed id that resolves outside the root. Asserting the message
    #: keeps each guard independently covered.
    TOKEN_GUARD = "opaque token"
    JOB_DIR_GUARD = "job directory escapes the store root"
    FILE_GUARD = "path escapes the store root"

    @pytest.mark.parametrize("job_id", ESCAPING_IDS)
    def test_escaping_ids_are_rejected_on_load(self, tmp_path, job_id):
        store = JobStore(tmp_path / "root")

        with pytest.raises(JobStoreError) as raised:
            store.load(job_id)

        assert self.TOKEN_GUARD in str(raised.value)

    @pytest.mark.parametrize("job_id", ESCAPING_IDS)
    def test_escaping_ids_create_nothing_outside_the_root(self, tmp_path, job_id):
        root = tmp_path / "root"
        store = JobStore(root)
        job = ContentJob.model_validate(content_job_payload())
        job.content_job_id = job_id

        with pytest.raises(JobStoreError) as raised:
            store.create(job)

        assert self.TOKEN_GUARD in str(raised.value)
        assert list(tmp_path.rglob("job.json")) == []

    @pytest.mark.parametrize("job_id", ESCAPING_IDS)
    def test_escaping_ids_are_rejected_on_append(self, tmp_path, job_id):
        store = JobStore(tmp_path / "root")

        with pytest.raises(JobStoreError) as raised:
            store.append_decision(job_id, {"reason": "should never be written"})

        assert self.TOKEN_GUARD in str(raised.value)

    def test_symlinked_job_directory_outside_the_root_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        # Sibling name sharing the root's prefix: the guard must compare with a
        # separator so that "<root>evil" is not read as living under "<root>".
        outside = tmp_path / "rootevil"
        outside.mkdir()
        (root / "escaped").symlink_to(outside, target_is_directory=True)
        store = JobStore(root)

        with pytest.raises(JobStoreError) as raised:
            store.load("escaped")

        assert self.JOB_DIR_GUARD in str(raised.value)

    def _job_with_symlinked_subdir(self, tmp_path, subdir):
        """Create a real job whose ``subdir`` points at a sibling of the root."""
        root = tmp_path / "root"
        store = JobStore(root)
        store.create(build_record(scene_count=1))
        outside = tmp_path / "rootevil"
        outside.mkdir()
        target = root / "job-20260816-001" / subdir
        if target.is_dir():
            for child in target.iterdir():
                child.unlink()
            target.rmdir()
        target.symlink_to(outside, target_is_directory=True)
        return store, outside

    @pytest.mark.parametrize("subdir", ("scripts", "scenes"))
    def test_symlinked_subdirectory_cannot_be_written_through(self, tmp_path, subdir):
        store, outside = self._job_with_symlinked_subdir(tmp_path, subdir)

        with pytest.raises(JobStoreError) as raised:
            store.replace(build_record(scene_count=1))

        assert self.FILE_GUARD in str(raised.value)
        assert list(outside.iterdir()) == []

    def test_symlinked_append_directory_cannot_be_written_through(self, tmp_path):
        store, outside = self._job_with_symlinked_subdir(tmp_path, "assets")

        with pytest.raises(JobStoreError) as raised:
            store.append_event(
                "job-20260816-001", AssetRecord.model_validate(asset_record_payload())
            )

        assert self.FILE_GUARD in str(raised.value)
        assert list(outside.iterdir()) == []

    @pytest.mark.parametrize("subdir", ("scripts", "scenes", "assets"))
    def test_symlinked_subdirectory_is_rejected_on_load(self, tmp_path, subdir):
        store, outside = self._job_with_symlinked_subdir(tmp_path, subdir)
        (outside / "script.json").write_text("{}", encoding="utf-8")

        with pytest.raises(JobStoreError) as raised:
            store.load("job-20260816-001")

        assert self.FILE_GUARD in str(raised.value)

    def _symlink_file_outside(self, tmp_path, relative):
        """Replace one job file with a symlink to a sibling of the root."""
        root = tmp_path / "root"
        store = JobStore(root)
        store.create(build_record(scene_count=1))
        outside = tmp_path / "rootevil"
        outside.mkdir()
        victim = outside / Path(relative).name
        victim.write_text("{}", encoding="utf-8")
        target = root / "job-20260816-001" / relative
        if target.exists():
            target.unlink()
        target.symlink_to(victim)
        return store, victim

    def test_symlinked_append_only_file_is_rejected_on_load(self, tmp_path):
        store, _ = self._symlink_file_outside(tmp_path, "provider_events.jsonl")

        with pytest.raises(JobStoreError) as raised:
            store.load("job-20260816-001")

        assert self.FILE_GUARD in str(raised.value)

    # -- deletion guards: replace() unlinks documents, so every removal target
    # must be proven inside the root before it is unlinked.

    def test_symlinked_script_directory_cannot_be_deleted_through(self, tmp_path):
        store, outside = self._job_with_symlinked_subdir(tmp_path, "scripts")
        victim = outside / "script.json"
        victim.write_text("{}", encoding="utf-8")
        record = build_record(scene_count=1)
        record.script = None

        with pytest.raises(JobStoreError) as raised:
            store.replace(record)

        assert self.FILE_GUARD in str(raised.value)
        assert victim.exists()

    def test_symlinked_scenes_directory_cannot_be_pruned_through(self, tmp_path):
        store, outside = self._job_with_symlinked_subdir(tmp_path, "scenes")
        victim = outside / "scene-001.json"
        victim.write_text("{}", encoding="utf-8")
        record = build_record(scene_count=1)
        record.scenes = []

        with pytest.raises(JobStoreError) as raised:
            store.replace(record)

        assert self.FILE_GUARD in str(raised.value)
        assert victim.exists()

    def test_symlinked_render_manifest_cannot_be_deleted_through(self, tmp_path):
        store, victim = self._symlink_file_outside(tmp_path, "render_manifest.json")
        record = build_record(scene_count=1)
        record.render_manifest = None

        with pytest.raises(JobStoreError) as raised:
            store.replace(record)

        assert self.FILE_GUARD in str(raised.value)
        assert victim.exists()

    def test_a_job_id_aliasing_the_root_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "self").symlink_to(root, target_is_directory=True)
        store = JobStore(root)
        job = ContentJob.model_validate(content_job_payload())
        job.content_job_id = "self"

        with pytest.raises(JobStoreError) as raised:
            store.save(job)

        assert self.JOB_DIR_GUARD in str(raised.value)
        assert not (root / "job.json").exists()


class TestJobStoreReplaceDocuments:
    def test_replace_with_fewer_scenes_deletes_the_stale_scene_files(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record(scene_count=3))
        scenes_dir = tmp_path / "job-20260816-001" / "scenes"
        assert len(list(scenes_dir.glob("scene-*.json"))) == 3

        store.replace(build_record(scene_count=1))

        assert [path.name for path in sorted(scenes_dir.glob("scene-*.json"))] == [
            "scene-001.json"
        ]
        assert len(store.load("job-20260816-001").scenes) == 1

    def test_replace_with_no_script_removes_the_script_file(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        record = store.load("job-20260816-001")
        record.script = None

        store.replace(record)

        assert not (tmp_path / "job-20260816-001" / "scripts" / "script.json").exists()
        assert store.load("job-20260816-001").script is None

    def test_replace_with_no_render_manifest_removes_the_manifest_file(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        record = store.load("job-20260816-001")
        record.render_manifest = None

        store.replace(record)

        assert not (tmp_path / "job-20260816-001" / "render_manifest.json").exists()
        assert store.load("job-20260816-001").render_manifest is None

    def test_save_of_a_bare_content_job_only_rewrites_job_json(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record(scene_count=3)
        store.create(record)
        job = store.load("job-20260816-001").job
        job.status = JobStatus.RENDERING

        store.save(job)

        reloaded = store.load("job-20260816-001")
        assert reloaded.job.status is JobStatus.RENDERING
        assert reloaded.scenes == record.scenes
        assert reloaded.script == record.script
        assert reloaded.render_manifest == record.render_manifest


    def test_replace_with_a_bare_content_job_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record(scene_count=3)
        store.create(record)

        with pytest.raises(JobStoreError) as raised:
            store.replace(record.job)

        assert "save()" in str(raised.value)
        assert len(store.load("job-20260816-001").scenes) == 3

    def test_save_of_a_missing_job_raises_a_clear_error(self, tmp_path):
        store = JobStore(tmp_path)

        with pytest.raises(JobStoreError) as raised:
            store.save(build_record(job_id="never-created").job)

        assert "never-created" in str(raised.value)

    def test_save_with_a_job_record_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record(scene_count=3)
        store.create(record)

        with pytest.raises(JobStoreError) as raised:
            store.save(record)

        assert "replace()" in str(raised.value)
        assert len(store.load("job-20260816-001").scenes) == 3

    def test_replace_with_a_default_record_wipes_documents(self, tmp_path):
        """The destructive semantics of ``replace`` are the explicit contract."""
        store = JobStore(tmp_path)
        record = build_record(scene_count=3)
        store.create(record)

        store.replace(JobRecord(job=record.job))

        reloaded = store.load("job-20260816-001")
        assert reloaded.scenes == []
        assert reloaded.script is None
        assert reloaded.render_manifest is None
        assert reloaded.job == record.job


class TestJobStoreSceneIndexUniqueness:
    @staticmethod
    def duplicated_record():
        record = build_record(scene_count=2)
        record.scenes[1].scene_index = record.scenes[0].scene_index
        return record

    def test_create_rejects_a_duplicate_scene_index(self, tmp_path):
        store = JobStore(tmp_path)

        with pytest.raises(JobStoreError) as raised:
            store.create(self.duplicated_record())

        assert "scene_index" in str(raised.value)
        assert "1" in str(raised.value)

    def test_replace_rejects_a_duplicate_scene_index(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record(scene_count=2))

        with pytest.raises(JobStoreError) as raised:
            store.replace(self.duplicated_record())

        assert "scene_index" in str(raised.value)
        assert len(store.load("job-20260816-001").scenes) == 2


class TestJobStoreChildJobIdConsistency:
    def test_a_scene_from_another_job_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        record = store.load("job-20260816-001")
        record.scenes[0].content_job_id = "some-other-job"

        with pytest.raises(JobStoreError) as raised:
            store.replace(record)

        assert "some-other-job" in str(raised.value)

    def test_a_render_manifest_from_another_job_is_rejected(self, tmp_path):
        store = JobStore(tmp_path)
        record = build_record()
        record.render_manifest.content_job_id = "a-third-job"

        with pytest.raises(JobStoreError) as raised:
            store.create(record)

        assert "a-third-job" in str(raised.value)


class TestJobStoreDecisionValidation:
    @pytest.mark.parametrize("line", ("123", '"a string"', "[1, 2]", "null"))
    def test_a_non_object_decision_line_is_rejected_on_load(self, tmp_path, line):
        store = JobStore(tmp_path)
        store.create(build_record())
        (tmp_path / "job-20260816-001" / "decisions.jsonl").write_text(
            line + "\n", encoding="utf-8"
        )

        with pytest.raises(JobStoreError) as raised:
            store.load("job-20260816-001")

        assert "decisions.jsonl" in str(raised.value)

    def test_object_decision_lines_load_as_dicts(self, tmp_path):
        store = JobStore(tmp_path)
        store.create(build_record())
        store.append_decision("job-20260816-001", {"from": "DRAFT", "to": "SCRIPTING"})

        decisions = store.load("job-20260816-001").decisions

        assert all(isinstance(entry, dict) for entry in decisions)
        assert decisions == [{"from": "DRAFT", "to": "SCRIPTING"}]


#: Every fixture walks the same transition chain, so an emptied or rewritten
#: ``decisions.jsonl`` is caught rather than merely "truthy".
EXPECTED_DECISION_CHAIN = (
    ("DRAFT", "SCRIPTING"),
    ("SCRIPTING", "SCENE_PLANNING"),
    ("SCENE_PLANNING", "VOICE_GENERATING"),
    ("VOICE_GENERATING", "AWAITING_ASSETS"),
    ("AWAITING_ASSETS", "READY_TO_RENDER"),
)

EXPECTED_FIXTURES = {
    "three-scene-demo": {
        "scene_count": 3,
        "script_title": "企業導入AI最常犯的三個錯誤",
        "script_body_len": 3,
        "script_risk_flags": [],
    },
    "ten-scene-demo": {
        "scene_count": 10,
        "script_title": "短影音沒人看完的五個原因",
        "script_body_len": 5,
        "script_risk_flags": ["claim_needs_source_check"],
    },
}


class TestFrozenFixtures:
    @pytest.mark.parametrize("job_id", tuple(EXPECTED_FIXTURES))
    def test_fixture_loads_and_validates(self, job_id):
        expected = EXPECTED_FIXTURES[job_id]
        expected_scene_count = expected["scene_count"]
        store = JobStore(FIXTURES_ROOT)

        record = store.load(job_id)

        assert record.job.content_job_id == job_id
        assert len(record.scenes) == expected_scene_count
        assert [scene.scene_index for scene in record.scenes] == list(
            range(1, expected_scene_count + 1)
        )
        assert record.render_manifest is not None
        assert len(record.render_manifest.scenes) == expected_scene_count
        assert {entry.scene_id for entry in record.render_manifest.scenes} == {
            scene.scene_id for scene in record.scenes
        }
        assert len(record.assets) >= expected_scene_count

        assert record.script is not None
        assert record.script.title == expected["script_title"]
        assert len(record.script.body) == expected["script_body_len"]
        assert all(paragraph.strip() for paragraph in record.script.body)
        assert record.script.hook.strip()
        assert record.script.cta.strip()
        assert record.script.claims and all(claim.strip() for claim in record.script.claims)
        assert record.script.sources and all(
            source.startswith("https://") for source in record.script.sources
        )
        assert record.script.risk_flags == expected["script_risk_flags"]

        assert [event.provider_event_id for event in record.provider_events] == [
            f"provider-event-{index:03d}" for index in range(1, expected_scene_count + 1)
        ]
        assert {event.content_job_id for event in record.provider_events} == {job_id}
        assert all(
            event.idempotency_key.startswith(f"{job_id}:")
            for event in record.provider_events
        )

        assert [entry.idempotency_key for entry in record.usage_ledger] == [
            event.idempotency_key for event in record.provider_events
        ]
        assert {entry.content_job_id for entry in record.usage_ledger} == {job_id}
        assert all(entry.created_at for entry in record.usage_ledger)

        assert [
            (decision["from"], decision["to"]) for decision in record.decisions
        ] == list(EXPECTED_DECISION_CHAIN)
        assert all(
            decision["reason"].strip() and decision["at"].startswith("2026-")
            for decision in record.decisions
        )
        assert {decision["to"] for decision in record.decisions} <= {
            status.value for status in JobStatus
        }

    @pytest.mark.parametrize("job_id", ("three-scene-demo", "ten-scene-demo"))
    def test_fixture_carries_no_credentials(self, job_id):
        job_dir = FIXTURES_ROOT / job_id
        forbidden = ("authorization", "bearer ", "api_key", "sk-", "secret", "token")

        for path in sorted(job_dir.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").lower()
            for needle in forbidden:
                assert needle not in text, f"{path}: {needle}"

    @pytest.mark.parametrize("job_id", ("three-scene-demo", "ten-scene-demo"))
    def test_fixture_round_trips_through_a_copy(self, tmp_path, job_id):
        source = JobStore(FIXTURES_ROOT)
        record = source.load(job_id)
        target = JobStore(tmp_path)

        target.create(record)
        reloaded = target.load(job_id)

        assert reloaded.job == record.job
        assert reloaded.scenes == record.scenes
        assert reloaded.render_manifest == record.render_manifest
        assert reloaded.assets == record.assets
        assert reloaded.provider_events == record.provider_events
        assert reloaded.usage_ledger == record.usage_ledger
        assert reloaded.decisions == record.decisions

    def test_fixture_job_json_is_readable_as_plain_json(self):
        payload = json.loads(
            (FIXTURES_ROOT / "three-scene-demo" / "job.json").read_text(encoding="utf-8")
        )

        assert payload["content_job_id"] == "three-scene-demo"
        assert payload["status"] in {status.value for status in JobStatus}
