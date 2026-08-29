"""PLAN-001 issue #8: asset import + creator profile preflight.

Mock boundary, deliberately low: these drive the real :class:`JobStore` on real
files, the real state machine, and real PNG/MP4 bytes produced by the real
decoder. Only ``media_probe.decoder_available`` is ever patched, and only in
the one test whose subject *is* a host without a decoder. Issue #4 shipped
100% broken behind 57 green tests because every one of them mocked the thing
that was broken; the acceptance criteria here are file-level facts, so they are
checked against files.

Tests that need media a decoder must produce are gated with
``requires_decoder``, in the same shape as
``test_job_master_voice._decoder_available``. On the Windows CI leg the
``imageio-ffmpeg`` wheel supplies ffmpeg, so they run there too. What does *not*
run anywhere without a decoder: every happy path, every §7 rule 2/3/5 rejection
and the checksum case — an asset cannot be validated on a host that cannot read
it, which is itself asserted by ``test_a_decoderless_host_refuses...``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from app.models.content_job import AssetRecord, JobStatus
from app.services.jobs import asset_import, media_probe
from app.services.jobs.asset_import import (
    AssetImportError,
    asset_id_for,
    import_assets,
    normalized_profile,
    preflight,
)
from app.services.jobs.state_machine import (
    UnauthorizedAssetError,
    decision_record,
    resume_target,
    transition,
)
from app.services.jobs.store import JobStore
from app.utils import utils

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"
JOB_ID = "missing-asset"
NOW = "2026-08-20T10:00:00+00:00"
MOMENT = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

requires_decoder = pytest.mark.skipif(
    not media_probe.decoder_available(),
    reason="no ffmpeg on this host, so no media can be produced or verified",
)

#: ``os.chmod`` on Windows toggles only the read-only flag, so mode 0 leaves a
#: file perfectly readable and there is no portable way to make one unreadable.
#: The rule under test is not platform-specific; the way to provoke it is.
requires_posix_permissions = pytest.mark.skipif(
    os.name == "nt",
    reason="chmod(0) does not remove read access on Windows",
)


# -- helpers ----------------------------------------------------------------


def staged(tmp_path):
    """A copy of the missing-asset fixture, media-less, at AWAITING_ASSETS."""
    shutil.copytree(FIXTURES_ROOT / JOB_ID, tmp_path / JOB_ID)
    store = JobStore(tmp_path)
    return store, store.load(JOB_ID).job


def _ffmpeg(*args):
    subprocess.run(
        [utils.get_ffmpeg_binary(), "-y", "-v", "error", *args],
        check=True,
        capture_output=True,
    )


def make_png(path, width=1080, height=1920):
    """A real PNG, written by Pillow rather than by ffmpeg.

    Nothing here needs a filter graph, and the CI decoder is the single binary
    in the ``imageio-ffmpeg`` wheel — so the fewer ffmpeg subsystems these
    fixtures depend on, the fewer ways they can fail on a runner rather than on
    the code under test.
    """
    Image.new("RGB", (width, height), (32, 64, 128)).save(path, "PNG")
    return Path(path)


def make_mp4(path, width=1080, height=1920, seconds="2"):
    """``seconds`` of that still, encoded at 25 fps — the moviepy code path."""
    still = Path(path).with_name(f"{Path(path).stem}.source.png")
    make_png(still, width, height)
    _ffmpeg(
        "-loop", "1", "-i", str(still), "-t", str(seconds),
        "-r", "25", "-pix_fmt", "yuv420p", str(path),
    )
    still.unlink()
    return Path(path)


def image_path(store, scene_id="scene-001"):
    return store.scene_media_dir(JOB_ID, scene_id, "images") / f"{scene_id}.png"


def video_path(store, scene_id="scene-002"):
    return store.scene_media_dir(JOB_ID, scene_id, "videos") / f"{scene_id}.mp4"


def supply_all(store):
    make_png(image_path(store))
    make_mp4(video_path(store))


def consented_asset(**overrides):
    """An AssetRecord carrying a complete, valid real-person consent."""
    fields = dict(
        asset_id="asset-voice-001",
        content_job_id=JOB_ID,
        scene_id=None,
        asset_type="audio",
        storage_key="audio/master-voice.mp3",
        original_filename="master-voice.mp3",
        mime_type="audio/mpeg",
        bytes=1024,
        width=0,
        height=0,
        duration_ms=20000,
        sha256="a" * 64,
        source_mode="creator_recording",
        provider="elevenlabs",
        model="voice-clone",
        license_or_consent="creator_profile:creator-001",
        consent_status="explicit_granted",
        usage_scope="zhenhe-ai V0 short videos",
        consent_source="signed_consent_form",
        consent_expires_at="2030-01-01T00:00:00+00:00",
        consent_revoked_at=None,
        manual_review_status="approved",
        created_at=NOW,
    )
    fields.update(overrides)
    return AssetRecord(**fields)


def profile_payload(**overrides):
    payload = {
        "creator_profile_id": "creator-001",
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "voice": {
            "asset_ref": "asset-voice-001",
            "consent_status": "explicit_granted",
            "usage_scope": "zhenhe-ai V0 short videos",
            "source": "signed_consent_form",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "revoked_at": None,
            "manual_review_status": "approved",
        },
        "avatar": {
            "asset_ref": asset_id_for("scene-002"),
            "consent_status": "explicit_granted",
            "usage_scope": "zhenhe-ai V0 short videos",
            "source": "user_provided_still",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "revoked_at": None,
            "manual_review_status": "approved",
        },
    }
    for section, changes in overrides.items():
        payload[section].update(changes)
    return payload


def resume(store):
    """What a human does to a parked job: send it back to AWAITING_ASSETS."""
    record = store.load(JOB_ID)
    target = resume_target(record.job.status, record.decisions)
    reason = "human supplied the outstanding scene media"
    back = transition(record.job, target, reason=reason)
    store.save(back)
    store.append_decision(JOB_ID, decision_record(record.job.status, back, reason))
    return target


# -- media_probe ------------------------------------------------------------


def test_sniffing_reads_the_bytes_not_the_name(tmp_path):
    png = tmp_path / "actually.mp4"
    png.write_bytes(media_probe.PNG_MAGIC + b"\x00" * 32)
    mp4 = tmp_path / "actually.png"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    other = tmp_path / "note.png"
    other.write_bytes(b"GIF89a" + b"\x00" * 32)

    assert media_probe.sniffed_mime(png) == media_probe.IMAGE_PNG
    assert media_probe.sniffed_mime(mp4) == media_probe.VIDEO_MP4
    assert media_probe.sniffed_mime(other) is None


def test_probe_refuses_a_missing_or_empty_file(tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")

    with pytest.raises(media_probe.MediaProbeError, match="does not exist"):
        media_probe.probe(tmp_path / "nope.png")
    with pytest.raises(media_probe.MediaProbeError, match="empty"):
        media_probe.probe(empty)


def test_probe_refuses_an_unrecognised_container(tmp_path):
    path = tmp_path / "scene-001.png"
    path.write_bytes(b"GIF89a" + b"\x00" * 64)

    with pytest.raises(media_probe.MediaProbeError, match="neither PNG nor MP4"):
        media_probe.probe(path)


@requires_decoder
def test_probe_measures_real_media(tmp_path):
    facts = media_probe.probe(make_mp4(tmp_path / "clip.mp4", 640, 480, seconds="1"))

    assert (facts.mime, facts.width, facts.height) == ("video/mp4", 640, 480)
    assert facts.duration_ms == 1000
    assert facts.decoded is True
    assert len(facts.sha256) == 64


@requires_decoder
def test_a_present_decoder_that_cannot_read_the_file_refuses(tmp_path):
    """The issue #6 lesson: never fall back to trusting the file."""
    good = make_mp4(tmp_path / "clip.mp4")
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 3])

    with pytest.raises(media_probe.MediaProbeError, match="cannot read this file"):
        media_probe.probe(truncated)


def test_a_decoderless_host_reports_it_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(media_probe, "decoder_available", lambda: False)
    path = tmp_path / "scene-001.png"
    path.write_bytes(media_probe.PNG_MAGIC + b"\x00" * 64)

    facts = media_probe.probe(path)

    assert facts.decoded is False
    assert (facts.width, facts.height, facts.duration_ms) == (0, 0, None)


# -- the acceptance criteria ------------------------------------------------


def test_missing_files_park_the_job(tmp_path):
    store, job = staged(tmp_path)

    with pytest.raises(AssetImportError) as caught:
        import_assets(job, store, now=NOW)

    assert caught.value.missing == 2
    assert caught.value.retryable is False
    record = store.load(JOB_ID)
    assert record.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert record.assets == []
    assert record.decisions[-1]["from"] == "AWAITING_ASSETS"
    assert record.decisions[-1]["to"] == "MANUAL_ACTION_REQUIRED"


def test_the_park_reason_names_scenes_and_not_filenames(tmp_path):
    store, job = staged(tmp_path)

    with pytest.raises(AssetImportError):
        import_assets(job, store, now=NOW)

    reason = store.load(JOB_ID).decisions[-1]["reason"]
    assert "scene-001" in reason and "scene-002" in reason
    assert ".png" not in reason and ".mp4" not in reason


@requires_decoder
def test_a_parked_job_resumes_and_then_opens_the_render_gate(tmp_path):
    store, job = staged(tmp_path)
    with pytest.raises(AssetImportError):
        import_assets(job, store, now=NOW)

    assert resume(store) is JobStatus.AWAITING_ASSETS
    supply_all(store)
    updated = import_assets(store.load(JOB_ID).job, store, now=NOW)

    assert updated.status is JobStatus.READY_TO_RENDER
    assert store.load(JOB_ID).job.status is JobStatus.READY_TO_RENDER
    assert [
        (line["from"], line["to"]) for line in store.load(JOB_ID).decisions[-3:]
    ] == [
        ("AWAITING_ASSETS", "MANUAL_ACTION_REQUIRED"),
        ("MANUAL_ACTION_REQUIRED", "AWAITING_ASSETS"),
        ("AWAITING_ASSETS", "READY_TO_RENDER"),
    ]


@requires_decoder
def test_a_partial_import_keeps_what_it_validated(tmp_path):
    store, job = staged(tmp_path)
    make_png(image_path(store))

    with pytest.raises(AssetImportError) as caught:
        import_assets(job, store, now=NOW)

    assert caught.value.missing == 1
    assert [asset.asset_id for asset in store.load(JOB_ID).assets] == [
        asset_id_for("scene-001")
    ]

    resume(store)
    make_mp4(video_path(store))
    assert (
        import_assets(store.load(JOB_ID).job, store, now=NOW).status
        is JobStatus.READY_TO_RENDER
    )
    assert len(store.load(JOB_ID).assets) == 2


@requires_decoder
def test_the_imported_record_carries_the_manifest_path_and_real_provenance(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)
    png = image_path(store)

    import_assets(job, store, now=NOW)

    assets = {asset.asset_id: asset for asset in store.load(JOB_ID).assets}
    still = assets[asset_id_for("scene-001")]
    assert still.storage_key == "scenes/scene-001/images/scene-001.png"
    assert still.sha256 == media_probe.file_sha256(png)
    assert still.bytes == png.stat().st_size
    assert (still.width, still.height) == (1080, 1920)
    assert still.duration_ms is None
    assert still.asset_type == "image" and still.mime_type == "image/png"
    assert still.source_mode == "human_import"
    assert still.provider == "qwen_code_plan"
    assert still.scene_id == "scene-001"

    clip = assets[asset_id_for("scene-002")]
    assert clip.asset_type == "video" and clip.duration_ms == 2000
    assert clip.storage_key == "scenes/scene-002/videos/scene-002.mp4"


@requires_decoder
def test_rerunning_appends_no_duplicate_and_still_advances(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)

    first = import_assets(job, store, now=NOW)
    ids = [asset.asset_id for asset in store.load(JOB_ID).assets]
    second = import_assets(first, store, now=NOW)

    assert second.status is JobStatus.READY_TO_RENDER
    assert [asset.asset_id for asset in store.load(JOB_ID).assets] == ids
    assert len(ids) == 2


@requires_decoder
def test_the_gate_opens_even_when_the_crash_landed_before_the_status_write(tmp_path):
    """The idempotent short circuit must advance a job it finds already imported."""
    store, job = staged(tmp_path)
    supply_all(store)
    import_assets(job, store, now=NOW)
    # Rewind only job.json, exactly what a crash between the last append and
    # the save leaves behind.
    ready = store.load(JOB_ID).job
    store.save(ready.model_copy(update={"status": JobStatus.AWAITING_ASSETS}))

    assert import_assets(store.load(JOB_ID).job, store, now=NOW).status is (
        JobStatus.READY_TO_RENDER
    )


@requires_decoder
def test_a_replaced_file_is_caught_on_the_next_run(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)
    import_assets(job, store, now=NOW)
    make_png(image_path(store), 720, 1280)

    with pytest.raises(AssetImportError, match="no longer matches the checksum"):
        import_assets(store.load(JOB_ID).job, store, now=NOW)


# -- §7 rejections ----------------------------------------------------------


@requires_decoder
def test_video_bytes_at_an_image_entry_are_refused(tmp_path):
    store, job = staged(tmp_path)
    make_mp4(tmp_path / "clip.mp4")
    image_path(store).write_bytes((tmp_path / "clip.mp4").read_bytes())
    make_mp4(video_path(store))

    with pytest.raises(AssetImportError, match="video/mp4 but the manifest accepts"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED


@requires_decoder
def test_image_bytes_at_a_video_entry_are_refused(tmp_path):
    store, job = staged(tmp_path)
    make_png(image_path(store))
    make_png(tmp_path / "still.png")
    video_path(store).write_bytes((tmp_path / "still.png").read_bytes())

    with pytest.raises(AssetImportError, match="image/png but the manifest accepts"):
        import_assets(job, store, now=NOW)


@requires_decoder
def test_the_extension_must_agree_with_the_sniff(tmp_path):
    """The other half of §7 rule 2, reachable only entry-side today.

    Every ``_MEDIA_SHAPE`` row pairs one extension with one MIME type, so a
    manifest cannot currently accept a type its filename contradicts. The check
    exists so that widening that table cannot silently drop half the rule.
    """
    store, _ = staged(tmp_path)
    entry = store.read_generation_manifest(JOB_ID).entries[0]
    mislabelled = entry.model_copy(update={"expected_filename": "scene-001.mp4"})

    with pytest.raises(AssetImportError, match="is named '.mp4'"):
        asset_import._validated_facts(make_png(tmp_path / "a.png"), mislabelled)


@requires_decoder
@pytest.mark.parametrize("size", [(200, 200), (7681, 7681)])
def test_out_of_range_dimensions_are_refused(tmp_path, size):
    store, job = staged(tmp_path)
    make_png(image_path(store), *size)
    make_mp4(video_path(store))

    with pytest.raises(AssetImportError, match="is outside"):
        import_assets(job, store, now=NOW)


@requires_decoder
def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    """The byte ceiling, hit at its boundary rather than with a 200 MB file.

    Refused by ``probe`` off the ``stat`` alone, before the file is hashed or
    decoded — a 5 GB drop should not cost a full read plus the decoder wall to
    be told it is too big.
    """
    store, job = staged(tmp_path)
    supply_all(store)
    monkeypatch.setattr(
        media_probe, "MAX_ASSET_BYTES", image_path(store).stat().st_size - 1
    )
    monkeypatch.setattr(
        media_probe, "file_sha256", lambda path: pytest.fail("hashed an oversized file")
    )

    with pytest.raises(media_probe.MediaProbeError, match="exceeds the"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED


@requires_decoder
@pytest.mark.parametrize("seconds", ["0.2", "121"])
def test_out_of_range_video_duration_is_refused(tmp_path, seconds):
    store, job = staged(tmp_path)
    make_png(image_path(store))
    make_mp4(video_path(store), 640, 480, seconds=seconds)

    with pytest.raises(AssetImportError, match="duration"):
        import_assets(job, store, now=NOW)


@requires_decoder
def test_the_same_bytes_are_never_recorded_twice(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)
    store.append_event(
        JOB_ID,
        consented_asset(
            asset_id="asset-earlier",
            consent_status="not_applicable",
            usage_scope="",
            consent_source="",
            consent_expires_at="",
            manual_review_status="not_required",
            sha256=media_probe.file_sha256(image_path(store)),
        ),
    )

    with pytest.raises(AssetImportError, match="already recorded as asset-earlier"):
        import_assets(job, store, now=NOW)


def test_a_manifest_entry_for_another_jobs_scene_is_refused(tmp_path):
    store, job = staged(tmp_path)
    manifest = store.read_generation_manifest(JOB_ID)
    stray = manifest.entries[0].model_copy(
        update={
            "scene_id": "scene-404",
            "import_dir": "scenes/scene-404/images",
            "expected_filename": "scene-404.png",
        }
    )
    store.write_generation_manifest(
        JOB_ID, manifest.model_copy(update={"entries": [stray]})
    )
    # The file has to be *there*. Without it the entry falls into the 缺件
    # branch and the test passes on its error string alone while the defect the
    # guard exists to prevent — a foreign scene's asset opening the render gate
    # — goes unexercised.
    stray_path = store.scene_media_dir(JOB_ID, "scene-404", "images") / "scene-404.png"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    make_png(stray_path)

    with pytest.raises(AssetImportError, match="not a scene of job"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert store.load(JOB_ID).assets == []


def test_import_without_a_generation_manifest_is_refused(tmp_path):
    store, job = staged(tmp_path)
    (tmp_path / JOB_ID / "generation_manifest.json").unlink()

    with pytest.raises(AssetImportError, match="needs the §6.1 generation manifest"):
        import_assets(job, store, now=NOW)


def test_import_outside_awaiting_assets_is_refused(tmp_path):
    store, job = staged(tmp_path)
    parked = transition(job, JobStatus.MANUAL_ACTION_REQUIRED, reason="by hand")
    store.save(parked)

    with pytest.raises(AssetImportError, match="requires AWAITING_ASSETS"):
        import_assets(parked, store, now=NOW)


def test_a_decoderless_host_refuses_rather_than_trusting_the_file(
    tmp_path, monkeypatch
):
    store, job = staged(tmp_path)
    monkeypatch.setattr(media_probe, "decoder_available", lambda: False)
    image_path(store).write_bytes(media_probe.PNG_MAGIC + b"\x00" * 4096)
    video_path(store).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4096)

    with pytest.raises(AssetImportError, match="no decoder is available"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED


# -- creator profile preflight ----------------------------------------------


def test_a_profile_without_an_expiry_is_refused(tmp_path):
    """Measured: upstream ``validate_creator_profile`` accepts this and stores
    ``""``. SPEC-001 §7 rule 11 requires an expiry, so it is enforced here."""
    payload = profile_payload()
    del payload["voice"]["expires_at"]

    with pytest.raises(UnauthorizedAssetError, match="no expiry"):
        normalized_profile(payload, now=MOMENT)


def test_an_expired_profile_is_refused():
    with pytest.raises(UnauthorizedAssetError, match="not usable"):
        normalized_profile(
            profile_payload(voice={"expires_at": "2026-01-01T00:00:00+00:00"}),
            now=MOMENT,
        )


def test_a_profile_reference_that_resolves_to_nothing_is_refused():
    profile = normalized_profile(profile_payload(), now=MOMENT)

    with pytest.raises(UnauthorizedAssetError, match="voice.asset_ref resolves to no"):
        preflight([], profile, now=MOMENT)


def test_a_voice_reference_that_resolves_twice_is_refused():
    profile = normalized_profile(profile_payload(), now=MOMENT)
    twice = [consented_asset(), consented_asset()]

    with pytest.raises(UnauthorizedAssetError, match="resolves to 2 assets"):
        preflight(twice, profile, now=MOMENT)


def test_the_voice_reference_must_be_the_master_voice():
    profile = normalized_profile(profile_payload(), now=MOMENT)
    assets = [
        consented_asset(asset_type="image", scene_id="scene-001"),
        consented_asset(asset_id=asset_id_for("scene-002"), scene_id="scene-002"),
    ]

    with pytest.raises(UnauthorizedAssetError, match="must resolve to the Master Voice"):
        preflight(assets, profile, now=MOMENT)


def test_the_avatar_reference_must_be_a_scene_asset():
    profile = normalized_profile(profile_payload(), now=MOMENT)
    assets = [
        consented_asset(),
        consented_asset(asset_id=asset_id_for("scene-002"), scene_id=None),
    ]

    with pytest.raises(UnauthorizedAssetError, match="must resolve to a scene asset"):
        preflight(assets, profile, now=MOMENT)


def test_a_complete_consent_passes_preflight():
    profile = normalized_profile(profile_payload(), now=MOMENT)
    assets = [
        consented_asset(),
        consented_asset(
            asset_id=asset_id_for("scene-002"),
            scene_id="scene-002",
            asset_type="video",
        ),
    ]

    preflight(assets, profile, now=MOMENT)  # does not raise


@pytest.mark.parametrize(
    "defect, message",
    [
        ({"consent_status": "implied"}, "consent_status"),
        ({"manual_review_status": "pending"}, "manual review"),
        ({"usage_scope": "   "}, "usage scope"),
        ({"consent_source": ""}, "consent source"),
        ({"consent_revoked_at": "2026-08-01T00:00:00+00:00"}, "has been revoked"),
        ({"consent_expires_at": ""}, "no expiry"),
        ({"consent_expires_at": "2026-08-19T00:00:00+00:00"}, "has expired"),
    ],
)
def test_each_consent_defect_refuses_the_render(defect, message):
    profile = normalized_profile(profile_payload(), now=MOMENT)
    assets = [
        consented_asset(**defect),
        consented_asset(
            asset_id=asset_id_for("scene-002"),
            scene_id="scene-002",
            asset_type="video",
        ),
    ]

    with pytest.raises(UnauthorizedAssetError, match=message):
        preflight(assets, profile, now=MOMENT)


def test_an_unreferenced_asset_that_claims_consent_is_still_checked():
    """The scope rule of Decision 3, pinned: ``not_applicable`` opts out,
    anything else opts in, profile or no profile."""
    with pytest.raises(UnauthorizedAssetError, match="has expired"):
        preflight(
            [consented_asset(consent_expires_at="2026-01-01T00:00:00+00:00")],
            None,
            now=MOMENT,
        )


@requires_decoder
def test_synthetic_tts_material_passes_preflight_untouched(tmp_path):
    """PRD-001 FR-005 scopes preflight to real people. A V0 job whose voice is
    synthesised and whose stills are machine-generated must not be parked."""
    store, job = staged(tmp_path)
    supply_all(store)
    store.append_event(
        JOB_ID,
        consented_asset(
            asset_type="audio",
            license_or_consent="synthetic_tts_no_creator_reference",
            consent_status="not_applicable",
            usage_scope="",
            consent_source="",
            consent_expires_at="",
            manual_review_status="not_required",
        ),
    )

    assert import_assets(job, store, now=NOW).status is JobStatus.READY_TO_RENDER
    assert all(
        asset.consent_status == "not_applicable"
        for asset in store.load(JOB_ID).assets
    )


@requires_decoder
def test_a_profile_stamps_its_consent_onto_the_avatar_it_authorises(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)
    store.append_event(JOB_ID, consented_asset())

    updated = import_assets(
        job, store, creator_profile=profile_payload(), now=NOW
    )

    assert updated.status is JobStatus.READY_TO_RENDER
    assets = {asset.asset_id: asset for asset in store.load(JOB_ID).assets}
    avatar = assets[asset_id_for("scene-002")]
    assert avatar.consent_status == "explicit_granted"
    assert avatar.manual_review_status == "approved"
    assert avatar.usage_scope == "zhenhe-ai V0 short videos"
    assert avatar.consent_source == "user_provided_still"
    assert avatar.consent_expires_at == "2030-01-01T00:00:00+00:00"
    assert avatar.license_or_consent == "creator_profile:creator-001"
    # The unreferenced still stays outside the consent regime entirely.
    assert assets[asset_id_for("scene-001")].consent_status == "not_applicable"


@requires_decoder
def test_a_defective_profile_asset_never_reaches_ready_to_render(tmp_path):
    store, job = staged(tmp_path)
    supply_all(store)
    store.append_event(JOB_ID, consented_asset(manual_review_status="pending"))

    with pytest.raises(UnauthorizedAssetError, match="manual review"):
        import_assets(job, store, creator_profile=profile_payload(), now=NOW)

    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED


# -- scope ------------------------------------------------------------------


def test_the_frozen_fixtures_are_not_mutated(tmp_path):
    before = {
        path: path.read_bytes() for path in sorted(FIXTURES_ROOT.rglob("*")) if path.is_file()
    }
    store, job = staged(tmp_path)
    with pytest.raises(AssetImportError):
        import_assets(job, store, now=NOW)
    after = {
        path: path.read_bytes() for path in sorted(FIXTURES_ROOT.rglob("*")) if path.is_file()
    }

    assert before == after


def test_the_missing_asset_fixture_ships_no_media_bytes():
    """Metadata-only on purpose: ``test_job_store`` reads every file under a
    fixture as UTF-8 text, and a real PNG would raise UnicodeDecodeError."""
    for path in sorted((FIXTURES_ROOT / JOB_ID).rglob("*")):
        if path.is_file():
            path.read_text(encoding="utf-8")


# -- repairs: each of these failed before the fix it guards -------------------


@requires_decoder
def test_a_truncated_video_is_refused_rather_than_half_believed(tmp_path):
    """§7 rule 6, 不完整下載 — the case ``-f null -`` alone gets wrong.

    Measured 2026-08-29: without ``-xerror`` ffmpeg exits 0 on a *faststart*
    mp4 cut to 60% of its bytes. It decodes the frames it can reach, and
    ``Duration`` still reports the header's full 4000 ms, so the job reached
    READY_TO_RENDER carrying a ``duration_ms`` no bytes support. A non-faststart
    mp4 hides this: its moov atom is at the end, so truncation fails outright.
    """
    store, job = staged(tmp_path)
    make_png(image_path(store))
    whole = tmp_path / "whole.mp4"
    still = make_png(tmp_path / "still.png")
    _ffmpeg(
        "-loop", "1", "-i", str(still), "-t", "4", "-r", "25",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(whole),
    )
    data = whole.read_bytes()
    video_path(store).write_bytes(data[: len(data) * 60 // 100])

    with pytest.raises(media_probe.MediaProbeError, match="truncated"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED
    # scene-001's PNG is valid and is kept, as any partial import is; what must
    # not exist is a record for the truncated video.
    assert [asset.scene_id for asset in store.load(JOB_ID).assets] == ["scene-001"]


@requires_decoder
def test_a_symlink_at_the_import_path_is_refused(tmp_path):
    """The store proves the *directory* is under its root; ``open`` follows links.

    Measured: the bytes came from outside the job tree while ``storage_key``
    still claimed the manifest's path.
    """
    store, job = staged(tmp_path)
    make_png(image_path(store))
    outside = make_mp4(tmp_path / "elsewhere.mp4")
    video_path(store).parent.mkdir(parents=True, exist_ok=True)
    video_path(store).symlink_to(outside)

    with pytest.raises(AssetImportError, match="symlink"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED


def test_a_quicktime_container_is_not_recorded_as_mp4(tmp_path):
    """``ftyp`` marks any ISOBMFF file. The major brand is what says mp4."""
    mov = tmp_path / "scene.mp4"
    mov.write_bytes(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32)

    assert media_probe.sniffed_mime(mov) is None
    for brand in (b"isom", b"mp42", b"avc1"):
        real = tmp_path / f"{brand.decode().strip()}.mp4"
        real.write_bytes(b"\x00\x00\x00\x14ftyp" + brand + b"\x00" * 32)
        assert media_probe.sniffed_mime(real) == media_probe.VIDEO_MP4


@requires_posix_permissions
def test_an_unreadable_file_keeps_its_path_out_of_the_audit_trail(tmp_path):
    """§7 rule 12. The OSError's ``str()`` carries the absolute path; the park
    reason is what lands in ``decisions.jsonl``, so it must not."""
    store, job = staged(tmp_path)
    blocked = make_png(image_path(store))
    blocked.chmod(0)
    try:
        with pytest.raises(media_probe.MediaProbeError):
            import_assets(job, store, now=NOW)
    finally:
        blocked.chmod(0o644)

    reason = store.load(JOB_ID).decisions[-1]["reason"]
    assert "not readable" in reason
    assert str(tmp_path) not in reason and "/" not in reason


def test_a_manifest_naming_one_scene_twice_is_refused(tmp_path):
    """``asset_id`` is derived from the scene id, so a repeated scene minted two
    records under one id, opened the gate, and then failed every later run's
    checksum re-verification against the wrong record — permanently."""
    store, job = staged(tmp_path)
    manifest = store.read_generation_manifest(JOB_ID)
    twin = manifest.entries[0].model_copy(
        update={"expected_filename": "scene-001-alt.png"}
    )
    store.write_generation_manifest(
        JOB_ID, manifest.model_copy(update={"entries": [*manifest.entries, twin]})
    )

    with pytest.raises(AssetImportError, match="names a scene more than once"):
        import_assets(job, store, now=NOW)
    assert store.load(JOB_ID).assets == []


@requires_decoder
def test_a_profile_arriving_after_the_import_parks_the_open_gate(tmp_path):
    """Consent is written when the asset is recorded, and the store is
    append-only, so a profile cannot authorise an asset after the fact.

    The load-bearing assertion is the *status*: refusing while leaving the job
    in READY_TO_RENDER would leave an unauthorised real-person asset sitting in
    an open render gate.
    """
    store, job = staged(tmp_path)
    supply_all(store)
    store.append_event(JOB_ID, consented_asset())

    assert import_assets(job, store, now=NOW).status is JobStatus.READY_TO_RENDER

    with pytest.raises(UnauthorizedAssetError, match="imported without this creator"):
        import_assets(store.load(JOB_ID).job, store, creator_profile=profile_payload(), now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.MANUAL_ACTION_REQUIRED
