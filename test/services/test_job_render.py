"""PLAN-001 issue #9: render manifest, renderer and technical QA.

Mock boundary, deliberately low, for the reason issue #8's suite states: issue
#4 shipped 100% broken behind 57 green tests because every one of them mocked
the thing that was broken. These drive the real :class:`JobStore` on real files,
the real state machine, real PNG/WAV/MP4 bytes and the real ``app.services.video``
renderer.

``app.services.video`` *is* stubbed in three tests, and only there: those tests'
subject is the isolation layer, and what they stub is the exact silent-success
behaviour ``test_combine_videos_really_does_return_the_path_it_was_given``
measures from the real function first. Stubbing the upstream lie is how the
refusal of that lie gets exercised at all.

**The frozen fixtures carry no media.** ``find test/fixtures -type f ! -name
"*.json" ! -name "*.jsonl"`` returns nothing, every ``storage_key`` points at a
file that does not exist, and every ``sha256`` is a hand-written placeholder. So
each test copies ``three-scene-demo`` into ``tmp_path`` and synthesises real
media at the storage keys the fixture itself names, recomputing ``bytes`` and
``sha256`` in the copy. Nothing under ``test/fixtures`` is mutated and no binary
is committed — ``test_job_store.py`` reads every fixture file as UTF-8 text.

The end-to-end render runs against a **timeline scaled down to 3 seconds** in
the copy (the fixture's own is 50 s). 1080x1920 is the render target and is not
scaled; only the durations are, so the suite stays minutes rather than tens of
minutes. Everything else about that render is the real thing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.models.content_job import JobStatus, RenderManifest
from app.services.jobs import media_probe, render_adapter, render_manifest, renderer
from app.services.jobs.render_adapter import RenderError
from app.services.jobs.render_manifest import (
    RenderManifestError,
    build_render_manifest,
    validate_render_manifest,
)
from app.services.jobs.renderer import render_job, start_rendering, technical_qa
from app.services.jobs.state_machine import BudgetExceededError, resume_target
from app.services.jobs.store import JobStore
from app.utils import utils

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"
JOB_ID = "three-scene-demo"
NOW = "2026-08-30T10:00:00+00:00"

requires_decoder = pytest.mark.skipif(
    not media_probe.decoder_available(),
    reason="no ffmpeg on this host, so no media can be produced or verified",
)

#: The fixture's own 50 s timeline, and the 3 s one the render tests use.
FULL_SLOTS = ((0, 6000), (6000, 28000), (28000, 50000))
SHORT_SLOTS = ((0, 1000), (1000, 2000), (2000, 3000))
SCENE_IDS = ("scene-001", "scene-002", "scene-003")
SAMPLE_RATE = 48000


# -- media helpers ----------------------------------------------------------


def _ffmpeg(*args):
    subprocess.run(
        [utils.get_ffmpeg_binary(), "-y", "-v", "error", *args],
        check=True,
        capture_output=True,
    )


def make_png(path, width=1080, height=1920):
    Image.new("RGB", (width, height), (32, 64, 128)).save(path, "PNG")
    return Path(path)


def make_wav(path, ms, rate=SAMPLE_RATE):
    """Silence, written by the standard library — no decoder needed to produce it.

    Deliberately 48 kHz, the rate SPEC-001 §8's example and both frozen
    fixtures name, so the tests exercise the resample the renderer really does.
    """
    frames = int(rate * ms / 1000)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return Path(path)


def make_mp4(
    path,
    width=1080,
    height=1920,
    ms=1000,
    audio=False,
    *,
    pix_fmt="yuv420p",
    fps=30,
    audio_rate=44100,
    video_codec=None,
    faststart=False,
):
    """``faststart`` moves the moov atom to the front, which is what makes a
    truncated file *openable* — see ``test_qa_catches_a_truncated_render``."""
    still = Path(path).with_name(f"{Path(path).stem}.source.png")
    make_png(still, width, height)
    args = ["-loop", "1", "-i", str(still), "-t", f"{ms / 1000:.3f}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={audio_rate}"]
        args += ["-ar", str(audio_rate)]
    args += ["-r", str(fps), "-pix_fmt", pix_fmt, "-shortest"]
    if video_codec:
        args += ["-c:v", video_codec]
    if faststart:
        args += ["-movflags", "+faststart"]
    args += [str(path)]
    _ffmpeg(*args)
    still.unlink()
    return Path(path)


def truncate(path, fraction=0.6):
    data = Path(path).read_bytes()
    Path(path).write_bytes(data[: int(len(data) * fraction)])
    return Path(path)


# -- fixture staging --------------------------------------------------------


def _assets(store):
    path = store.root / JOB_ID / "assets" / "assets.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_assets(store, rows):
    path = store.root / JOB_ID / "assets" / "assets.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _patch_job(store, **fields):
    path = store.root / JOB_ID / "job.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(fields)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _patch_scene(store, scene_id, **fields):
    index = SCENE_IDS.index(scene_id) + 1
    path = store.root / JOB_ID / "scenes" / f"scene-{index:03d}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(fields)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _srt(slots, texts):
    def stamp(ms):
        hours, rest = divmod(ms, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, milliseconds = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    blocks = [
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for index, ((start, end), text) in enumerate(zip(slots, texts), start=1)
    ]
    return "\n".join(blocks) + "\n"


def staged(tmp_path, *, slots=FULL_SLOTS, media=True, video_audio=False, rate=SAMPLE_RATE):
    """A copy of ``three-scene-demo`` with real media behind its storage keys."""
    shutil.copytree(FIXTURES_ROOT / JOB_ID, tmp_path / JOB_ID)
    store = JobStore(tmp_path)
    record = store.load(JOB_ID)
    total_ms = slots[-1][1]

    rows = _assets(store)
    by_id = {row["asset_id"]: row for row in rows}
    if media:
        make_png(store.asset_path(JOB_ID, by_id["asset-001"]["storage_key"]))
        make_mp4(
            store.asset_path(JOB_ID, by_id["asset-002"]["storage_key"]),
            ms=slots[1][1] - slots[1][0],
            audio=video_audio,
        )
        make_png(store.asset_path(JOB_ID, by_id["asset-003"]["storage_key"]))
        make_wav(
            store.asset_path(JOB_ID, by_id["asset-voice-001"]["storage_key"]),
            total_ms,
            rate=rate,
        )

    texts = [scene.narration for scene in sorted(record.scenes, key=lambda s: s.scene_index)]
    subtitle_path = store.asset_path(JOB_ID, by_id["asset-subtitle-001"]["storage_key"])
    subtitle_path.write_text(_srt(slots, texts), encoding="utf-8")

    for row in rows:
        path = store.asset_path(JOB_ID, row["storage_key"])
        if path.is_file():
            row["bytes"] = path.stat().st_size
            row["sha256"] = media_probe.file_sha256(path)
    by_id["asset-voice-001"]["duration_ms"] = total_ms
    by_id["asset-subtitle-001"]["duration_ms"] = total_ms
    by_id["asset-002"]["duration_ms"] = slots[1][1] - slots[1][0]
    _write_assets(store, rows)

    store.write_captions_document(
        JOB_ID,
        {
            "content_job_id": JOB_ID,
            "master_voice_asset_id": "asset-voice-001",
            "subtitle_asset_id": "asset-subtitle-001",
            "voice_total_duration_ms": total_ms,
            "voice_duration_source": "decoded",
            "captions": [
                {
                    "caption_ref": f"caption-{index:03d}",
                    "scene_id": scene_id,
                    "scene_index": index,
                    "srt_index": index,
                    "start_ms": start,
                    "end_ms": end,
                    "text": text,
                }
                for index, (scene_id, (start, end), text) in enumerate(
                    zip(SCENE_IDS, slots, texts), start=1
                )
            ],
        },
    )
    return store, store.load(JOB_ID).job


# -- the manifest builder ---------------------------------------------------


def test_the_built_manifest_reproduces_the_frozen_fixture(tmp_path):
    """The acceptance shape: every §8 field derived from documents on disk.

    One field deliberately differs from the frozen fixture, and it is the only
    one: ``audio.sample_rate``. The fixture and SPEC-001 §8's example both say
    48000; the renderer produces 44100 for any source, because moviepy's
    ``AudioFileClip`` resamples to its own 44100 default before
    ``generate_video`` ever reads ``clip.fps`` (measured 2026-08-30 on a 48 kHz
    WAV). A manifest declaring 48000 would be a number technical QA could only
    ever fail against, so the builder declares what comes out.
    """
    store, job = staged(tmp_path, media=False)
    make_wav(store.asset_path(JOB_ID, "assets/asset-voice-001.wav"), 50_000)
    built = build_render_manifest(job, store).model_dump(mode="json")
    frozen = json.loads(
        (FIXTURES_ROOT / JOB_ID / "render_manifest.json").read_text(encoding="utf-8")
    )
    assert built["audio"]["sample_rate"] == render_manifest.AUDIO_SAMPLE_RATE == 44100
    assert frozen["audio"]["sample_rate"] == 48000
    frozen["audio"]["sample_rate"] = render_manifest.AUDIO_SAMPLE_RATE
    assert built == frozen


def test_the_manifest_needs_the_captions_document(tmp_path):
    store, job = staged(tmp_path, media=False)
    (store.root / JOB_ID / "subtitles" / "captions.json").unlink()
    with pytest.raises(RenderManifestError, match="captions.json"):
        build_render_manifest(job, store)


def test_a_caption_naming_an_unknown_scene_is_refused(tmp_path):
    store, job = staged(tmp_path, media=False)
    document = store.read_captions_document(JOB_ID)
    document["captions"][1]["scene_id"] = "scene-999"
    store.write_captions_document(JOB_ID, document)
    with pytest.raises(RenderManifestError, match="scene-999"):
        build_render_manifest(job, store)


def test_a_scene_with_no_imported_media_is_refused(tmp_path):
    store, job = staged(tmp_path, media=False)
    _write_assets(
        store, [row for row in _assets(store) if row["asset_id"] != "asset-002"]
    )
    with pytest.raises(RenderManifestError, match="scene-002"):
        build_render_manifest(job, store)


# -- SPEC-001:625: the schema's rejection cases -----------------------------


def _manifest(store):
    return RenderManifest.model_validate(
        json.loads(
            (store.root / JOB_ID / "render_manifest.json").read_text(encoding="utf-8")
        )
    )


def _mutate(manifest: RenderManifest, path, value) -> RenderManifest:
    payload = manifest.model_dump(mode="json")
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return RenderManifest.model_validate(payload)


REJECTIONS = (
    ("a 7x3 canvas", ("canvas", "width"), 7, "render target"),
    ("zero fps", ("canvas", "fps"), 0, "fps must be positive"),
    ("a made-up pixel format", ("canvas", "pixel_format"), "nonsense", "pixel format"),
    ("a made-up container", ("output", "container"), "webm", "V0 renders"),
    ("a made-up video codec", ("output", "video_codec"), "zzz", "V0 renders"),
    ("native_speech_avatar", ("audio", "mode"), "native_speech_avatar", "not supported"),
    ("a negative sample rate", ("audio", "sample_rate"), -1, "sample_rate must be positive"),
    ("an unknown voice asset", ("audio", "master_voice_asset_id"), "asset-nope", "master_voice_asset_id"),
    ("an unknown subtitle asset", ("subtitle_asset_id",), "asset-nope", "subtitle_asset_id"),
    ("an unknown scene", ("scenes", 0, "scene_id"), "scene-999", "not a scene"),
    ("an unknown scene asset", ("scenes", 0, "asset_id"), "asset-nope", "does not resolve"),
    ("a backwards slot", ("scenes", 0, "end_ms"), 0, "ends before it starts"),
    ("a negative slot", ("scenes", 0, "start_ms"), -1, "negative slot"),
    ("a gap in the timeline", ("scenes", 1, "start_ms"), 7000, "tile the timeline"),
    ("an unimplemented motion", ("scenes", 0, "motion", "type"), "totally_made_up", "does not implement"),
    ("ken_burns without a scale", ("scenes", 0, "motion", "scale_start"), None, "positive scale_start"),
)


@pytest.mark.parametrize(
    "label,path,value,message",
    [pytest.param(*case, id=case[0].replace(" ", "-")) for case in REJECTIONS],
)
def test_the_validator_rejects(tmp_path, label, path, value, message):
    """The pydantic model accepts every one of these; §8 does not."""
    store, _ = staged(tmp_path, media=False)
    record = store.load(JOB_ID)
    broken = _mutate(_manifest(store), path, value)
    # Proof the model itself is not what refuses these: it already validated.
    assert isinstance(broken, RenderManifest)
    with pytest.raises(RenderManifestError, match=message):
        validate_render_manifest(broken, record)


def test_the_frozen_manifest_passes_the_validator(tmp_path):
    """SPEC-001:625's 通過案例, against the frozen fixture itself."""
    store, _ = staged(tmp_path, media=False)
    record = store.load(JOB_ID)
    assert validate_render_manifest(_manifest(store), record) is not None


def test_a_manifest_for_another_job_is_refused(tmp_path):
    store, _ = staged(tmp_path, media=False)
    record = store.load(JOB_ID)
    with pytest.raises(RenderManifestError, match="belongs to"):
        validate_render_manifest(
            _mutate(_manifest(store), ("content_job_id",), "other-job"), record
        )


def test_a_manifest_naming_one_scene_twice_is_refused(tmp_path):
    store, _ = staged(tmp_path, media=False)
    record = store.load(JOB_ID)
    payload = _manifest(store).model_dump(mode="json")
    payload["scenes"][1]["scene_id"] = "scene-001"
    with pytest.raises(RenderManifestError, match="more than once"):
        validate_render_manifest(RenderManifest.model_validate(payload), record)


# -- SPEC-001:405 native_speech_avatar --------------------------------------


@requires_decoder
def test_an_avatar_scene_carrying_its_own_speech_is_refused(tmp_path):
    """SPEC-001:405 forbids the renderer overwriting that track, so V0 refuses."""
    store, job = staged(tmp_path, slots=SHORT_SLOTS, video_audio=True)
    _patch_scene(store, "scene-002", visual_type="avatar")
    with pytest.raises(RenderManifestError, match="native_speech_avatar"):
        build_render_manifest(store.load(JOB_ID).job, store)


@requires_decoder
def test_an_avatar_scene_with_visual_only_material_still_builds(tmp_path):
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    _patch_scene(store, "scene-002", visual_type="avatar")
    manifest = build_render_manifest(store.load(JOB_ID).job, store)
    assert manifest.audio.mode == render_manifest.AUDIO_MODE_MASTER_VOICE


# -- the budget gate at READY_TO_RENDER -> RENDERING ------------------------


def test_the_gate_opens_rendering(tmp_path):
    store, job = staged(tmp_path, media=False)
    opened = start_rendering(job, store, now=NOW)
    assert opened.status is JobStatus.RENDERING
    assert store.load(JOB_ID).job.status is JobStatus.RENDERING
    assert store.load(JOB_ID).decisions[-1]["to"] == "RENDERING"


def test_an_unknown_spend_parks_at_budget_exceeded(tmp_path):
    """§10: an unprovable spend is refused even at a 0.0 estimate."""
    store, _ = staged(tmp_path, media=False)
    _patch_job(store, actual_cost_usd="unknown")
    job = store.load(JOB_ID).job
    with pytest.raises(BudgetExceededError):
        start_rendering(job, store, now=NOW)
    assert store.load(JOB_ID).job.status is JobStatus.BUDGET_EXCEEDED
    assert not (store.root / JOB_ID / "renders").exists()


def test_budget_exceeded_recovers_in_two_hops(tmp_path):
    """PR #13's recovery, for the state this gate actually sends jobs to."""
    from app.services.jobs.state_machine import decision_record, transition

    store, _ = staged(tmp_path, media=False)
    _patch_job(store, actual_cost_usd="unknown")
    with pytest.raises(BudgetExceededError):
        start_rendering(store.load(JOB_ID).job, store, now=NOW)

    parked = store.load(JOB_ID).job
    manual = transition(parked, JobStatus.MANUAL_ACTION_REQUIRED, reason="human cleared spend")
    store.save(manual)
    store.append_decision(JOB_ID, decision_record(parked.status, manual, "human cleared spend"))
    assert resume_target(manual.status, store.load(JOB_ID).decisions) is JobStatus.READY_TO_RENDER


def test_rendering_is_refused_from_the_wrong_state(tmp_path):
    store, _ = staged(tmp_path, media=False)
    _patch_job(store, status="DRAFT")
    with pytest.raises(RenderManifestError, match="READY_TO_RENDER"):
        start_rendering(store.load(JOB_ID).job, store)


# -- the isolation layer ----------------------------------------------------


@requires_decoder
def test_combine_videos_really_does_return_the_path_it_was_given(tmp_path):
    """Characterisation, so the stubs below are known to be faithful.

    ``combine_videos`` with no clips writes nothing and hands back the path it
    was given. Everything in ``render_adapter`` follows from this being true.
    """
    from app.services import video
    from app.models.schema import VideoAspect, VideoConcatMode

    voice = make_wav(tmp_path / "voice.wav", 1000)
    target = tmp_path / "combined.mp4"
    returned = video.combine_videos(
        combined_video_path=str(target),
        video_paths=[],
        audio_file=str(voice),
        video_aspect=VideoAspect.portrait,
        video_concat_mode=VideoConcatMode.sequential,
        max_clip_duration=1,
        threads=1,
    )
    assert returned == str(target)
    assert not target.exists()


@requires_decoder
def test_the_adapter_refuses_a_combine_that_wrote_nothing(tmp_path, monkeypatch):
    from app.services import video

    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    monkeypatch.setattr(
        video, "combine_videos", lambda **kwargs: kwargs["combined_video_path"]
    )
    with pytest.raises(RenderError, match="wrote no video"):
        _render(store, manifest)


@requires_decoder
def test_the_adapter_refuses_a_generate_that_wrote_nothing(tmp_path, monkeypatch):
    """``generate_video``'s bool is BGM mixing, not the render (video.py:979)."""
    from app.services import video

    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    monkeypatch.setattr(video, "generate_video", lambda **kwargs: True)
    with pytest.raises(RenderError, match="wrote no output file"):
        _render(store, manifest)


def _render(store, manifest, output=None):
    record = store.load(JOB_ID)
    assets = {asset.asset_id: asset for asset in record.assets}
    return render_adapter.render(
        manifest,
        scene_sources=render_manifest.scene_source_paths(manifest, store, record),
        voice_path=store.asset_path(
            JOB_ID, assets[manifest.audio.master_voice_asset_id].storage_key
        ),
        subtitle_path=render_manifest.subtitle_source_path(manifest, store, record),
        output_path=output or store.render_output_path(JOB_ID, ".mp4"),
    )


# -- technical QA reads the encoded file ------------------------------------


@requires_decoder
def test_qa_catches_a_wrong_sized_render(tmp_path):
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    wrong = make_mp4(tmp_path / "wrong.mp4", width=1080, height=1080, ms=3000, audio=True)
    result = technical_qa(wrong, manifest)
    assert not result.passed
    assert any("1080x1080" in failure for failure in result.failures)


@requires_decoder
def test_qa_catches_a_render_with_no_audio(tmp_path):
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    silent = make_mp4(tmp_path / "silent.mp4", ms=3000, audio=False)
    result = technical_qa(silent, manifest)
    assert not result.passed
    assert any("no audio stream" in failure for failure in result.failures)


@requires_decoder
def test_qa_catches_a_short_render(tmp_path):
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    short = make_mp4(tmp_path / "short.mp4", ms=500, audio=True)
    result = technical_qa(short, manifest)
    assert not result.passed
    assert any("manifest timeline" in failure for failure in result.failures)


@requires_decoder
def test_qa_catches_a_long_render(tmp_path):
    """The duration check is symmetric, and that half was missing.

    ``combine_videos`` loops clips to the Master Voice's length, so a voice
    longer than the caption timeline yields seconds of content that no scene
    entry and no subtitle cue covers. Measured 2026-08-30: an 8000 ms render
    against a 3000 ms manifest passed QA and reached ``TECHNICAL_QA``.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    long_render = make_mp4(tmp_path / "long.mp4", ms=8000, audio=True)
    result = technical_qa(long_render, manifest)
    assert not result.passed
    assert any("ends at 3000 ms" in failure for failure in result.failures)


@requires_decoder
@pytest.mark.parametrize(
    "label, kwargs, message",
    [
        ("pixel-format", {"pix_fmt": "yuv444p"}, "pixel format"),
        ("frame-rate", {"fps": 24}, "frame rate"),
        ("audio-sample-rate", {"audio_rate": 48000}, "audio sample rate"),
        ("video-codec", {"video_codec": "libx265"}, "video codec"),
    ],
)
def test_qa_catches_a_metadata_mismatch(tmp_path, label, kwargs, message):
    """FR-008's 「ffprobe metadata」 half: each comparison has to be able to fail.

    Without these four, deleting any of the codec / pixel-format / fps /
    sample-rate branches in :func:`technical_qa` left the suite green. The
    sample-rate one matters most: 44100 is the value the builder deliberately
    departs from the frozen fixture's 48000 to declare, and nothing was
    enforcing it.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    odd = make_mp4(tmp_path / f"{label}.mp4", ms=3000, audio=True, **kwargs)
    result = technical_qa(odd, manifest)
    assert not result.passed, result.facts
    assert any(message in failure for failure in result.failures), result.failures


@requires_decoder
def test_qa_catches_a_truncated_render(tmp_path):
    """Both container layouts, because they fail at different points.

    A default mp4 puts moov last, so truncation breaks the *open* and ffmpeg
    fails with or without ``-xerror``. A ``+faststart`` file opens fine and only
    the decode fails — measured 2026-08-30, exit 183 with ``-xerror`` and exit 0
    without it. ``inspect`` runs on any pre-existing ``final.mp4`` on the reuse
    path, so that case is reachable and ``-xerror`` is load-bearing.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    for name, faststart in (("broken", False), ("broken-faststart", True)):
        broken = truncate(
            make_mp4(tmp_path / f"{name}.mp4", ms=3000, audio=True, faststart=faststart)
        )
        with pytest.raises(RenderError, match="truncated"):
            technical_qa(broken, manifest)


@requires_decoder
def test_qa_catches_subtitle_cues_past_the_render(tmp_path):
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    manifest = build_render_manifest(job, store)
    clip = make_mp4(tmp_path / "clip.mp4", ms=3000, audio=True)
    result = technical_qa(
        clip, manifest, subtitle_text="1\n00:00:00,000 --> 00:09:00,000\nlate\n\n"
    )
    assert not result.passed
    assert any("subtitle cue" in failure for failure in result.failures)


def test_qa_needs_a_file_that_exists(tmp_path):
    store, _ = staged(tmp_path, media=False)
    with pytest.raises(RenderError):
        render_adapter.inspect(tmp_path / "nothing.mp4")


# -- the stage, end to end --------------------------------------------------


@requires_decoder
def test_the_fixture_renders_end_to_end_and_reaches_technical_qa(tmp_path):
    """PLAN-001 row 9's acceptance: the fixture's documents drive a real render."""
    store, job = staged(tmp_path, slots=SHORT_SLOTS)

    rendered = render_job(job, store, now=NOW)

    assert rendered.status is JobStatus.TECHNICAL_QA
    final = store.root / JOB_ID / "renders" / "final.mp4"
    assert final.is_file() and final.stat().st_size > 0
    facts = render_adapter.inspect(final)
    assert (facts.width, facts.height) == (1080, 1920)
    assert facts.video_codec == "h264"
    assert facts.audio_codec == "aac"
    assert facts.pixel_format == "yuv420p"

    record = store.load(JOB_ID)
    assert record.render_manifest is not None
    asset = [item for item in record.assets if item.asset_id == renderer.RENDER_ASSET_ID]
    assert len(asset) == 1
    assert asset[0].storage_key == "renders/final.mp4"
    assert asset[0].sha256 == media_probe.file_sha256(final)
    assert asset[0].bytes == final.stat().st_size
    # video.py writes temp-clip-*.mp4, ffmpeg-concat-list.txt and
    # *TEMP_MPY_wvf_snd.mp4 into the output directory. None of it survives.
    assert sorted(path.name for path in final.parent.iterdir()) == ["final.mp4"]

    # Idempotent: a second run re-verifies rather than re-rendering, and appends
    # no second AssetRecord.
    again = render_job(store.load(JOB_ID).job, store, now=NOW)
    assert again.status is JobStatus.TECHNICAL_QA
    assert (
        len([item for item in store.load(JOB_ID).assets if item.asset_id == renderer.RENDER_ASSET_ID])
        == 1
    )


@requires_decoder
def test_a_stale_render_is_redone_rather_than_shipped(tmp_path):
    """"final.mp4 exists" is not "this job is rendered"."""
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    render_job(job, store, now=NOW)
    final = store.root / JOB_ID / "renders" / "final.mp4"
    first = media_probe.file_sha256(final)

    # The voice got longer, so the manifest timeline the render must cover did.
    longer = ((0, 1000), (1000, 2000), (2000, 5000))
    make_wav(store.asset_path(JOB_ID, "assets/asset-voice-001.wav"), 5000)
    document = store.read_captions_document(JOB_ID)
    for cue, (start, end) in zip(document["captions"], longer):
        cue["start_ms"], cue["end_ms"] = start, end
    document["voice_total_duration_ms"] = 5000
    store.write_captions_document(JOB_ID, document)

    again = render_job(store.load(JOB_ID).job, store, now=NOW)
    assert again.status is JobStatus.TECHNICAL_QA
    assert media_probe.file_sha256(final) != first
    records = [
        item for item in store.load(JOB_ID).assets if item.asset_id == renderer.RENDER_ASSET_ID
    ]
    # Append-only: the re-render leaves the superseded record behind it, and the
    # last line is the current one.
    assert len(records) == 2
    assert records[-1].sha256 == media_probe.file_sha256(final)


@requires_decoder
def test_a_swapped_render_is_not_reused_on_a_resume(tmp_path):
    """The sha re-verify is what keeps the Asset Record honest across a crash.

    Measured 2026-08-30 with that check removed: a ``final.mp4`` replaced
    between the render and the crash is reused as-is, and the recorded sha then
    describes a file that is no longer there.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    render_job(job, store, now=NOW)
    final = store.root / JOB_ID / "renders" / "final.mp4"

    # A different file that would pass QA on its own merits.
    swapped = make_mp4(tmp_path / "swapped.mp4", ms=3000, audio=True)
    shutil.copyfile(swapped, final)
    swapped_sha = media_probe.file_sha256(final)
    _patch_job(store, status="RENDERING")

    again = render_job(store.load(JOB_ID).job, store, now=NOW)
    assert again.status is JobStatus.TECHNICAL_QA
    assert media_probe.file_sha256(final) != swapped_sha  # re-rendered, not reused
    records = [
        item for item in store.load(JOB_ID).assets if item.asset_id == renderer.RENDER_ASSET_ID
    ]
    assert records[-1].sha256 == media_probe.file_sha256(final)


@requires_decoder
def test_any_failure_parks_the_job_not_just_the_expected_types(tmp_path, monkeypatch):
    """``render_job`` catches ``Exception``, and it has to.

    Measured 2026-08-30 against the earlier four-type tuple: a corrupt scene
    image raises ``av.error.InvalidDataError`` and a bad ``storage_key`` raises
    ``JobStoreError`` — both plain ``ValueError`` subclasses. Neither was
    caught, so the job stayed in ``RENDERING``: not parked, ``resume_target``
    raising ``ResumeError``, and no ``RETRYABLE_FAILED`` line for the §5.3 retry
    limit to count.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)

    def boom(*args, **kwargs):
        raise ValueError("a plain ValueError, the shape that used to leak")

    monkeypatch.setattr(render_adapter, "render", boom)
    with pytest.raises(ValueError, match="used to leak"):
        render_job(job, store, now=NOW)
    record = store.load(JOB_ID)
    assert record.job.status is JobStatus.RETRYABLE_FAILED
    assert resume_target(record.job.status, record.decisions) is JobStatus.RENDERING


@requires_decoder
def test_a_failure_reason_does_not_leak_the_host_filesystem(tmp_path, monkeypatch):
    """ffmpeg messages quote the absolute job path; decisions.jsonl must not."""
    store, job = staged(tmp_path, slots=SHORT_SLOTS)

    def leaky(*args, **kwargs):
        raise RenderError(f"could not open {store.root / JOB_ID / 'x.mp4'} " + "y" * 500)

    monkeypatch.setattr(render_adapter, "render", leaky)
    with pytest.raises(RenderError):
        render_job(job, store, now=NOW)
    reason = store.load(JOB_ID).decisions[-1]["reason"]
    assert str(store.root) not in reason
    assert "<store>" in reason
    assert len(reason) < 520


@requires_decoder
def test_a_qa_failure_lands_in_retryable_failed_and_resumes_into_rendering(
    tmp_path, monkeypatch
):
    """The whole reason QA runs inside RENDERING: this path has to converge."""
    store, job = staged(tmp_path, slots=SHORT_SLOTS)

    def wrong_size(manifest, *, output_path, **kwargs):
        make_mp4(output_path, width=1080, height=1080, ms=3000, audio=True)
        return output_path

    monkeypatch.setattr(render_adapter, "render", wrong_size)
    with pytest.raises(RenderError, match="technical QA refused"):
        render_job(job, store, now=NOW)

    record = store.load(JOB_ID)
    assert record.job.status is JobStatus.RETRYABLE_FAILED
    assert resume_target(record.job.status, record.decisions) is JobStatus.RENDERING
    assert not [item for item in record.assets if item.asset_id == renderer.RENDER_ASSET_ID]


def test_a_manifest_failure_lands_in_retryable_failed(tmp_path):
    store, job = staged(tmp_path, media=False)
    (store.root / JOB_ID / "subtitles" / "captions.json").unlink()
    with pytest.raises(RenderManifestError):
        render_job(job, store, now=NOW)
    record = store.load(JOB_ID)
    assert record.job.status is JobStatus.RETRYABLE_FAILED
    assert resume_target(record.job.status, record.decisions) is JobStatus.RENDERING


def test_render_job_refuses_a_job_that_is_not_ready(tmp_path):
    store, _ = staged(tmp_path, media=False)
    _patch_job(store, status="DRAFT")
    with pytest.raises(RenderManifestError, match="READY_TO_RENDER"):
        render_job(store.load(JOB_ID).job, store)


# -- the store's render paths ----------------------------------------------


def test_the_render_directory_survives_a_replace(tmp_path):
    """``renders/`` is stage-owned, like ``audio/``: ``replace`` cannot delete it."""
    store, _ = staged(tmp_path, media=False)
    path = store.render_output_path(JOB_ID, ".mp4")
    path.write_bytes(b"not really a video")
    record = store.load(JOB_ID)
    store.replace(record)
    assert path.is_file()


def test_a_storage_key_cannot_escape_the_job(tmp_path):
    store, _ = staged(tmp_path, media=False)
    for key in ("../../etc/passwd", "/etc/passwd", "assets\\win.png", ""):
        with pytest.raises(ValueError):
            store.asset_path(JOB_ID, key)


def test_the_render_extension_is_validated(tmp_path):
    store, _ = staged(tmp_path, media=False)
    for extension in ("mp4", "../mp4", ".", ".m p4"):
        with pytest.raises(ValueError):
            store.render_output_path(JOB_ID, extension)
        with pytest.raises(ValueError):
            store.render_output_relative_path(extension)


@requires_decoder
def test_the_last_asset_record_wins_not_the_first(tmp_path, monkeypatch):
    """``assets.jsonl`` is an append-only log, so a re-render supersedes.

    Measured 2026-08-30 by mutation: reading the *first* ``RENDER_ASSET_ID``
    line instead of the last left the whole suite green, because no test until
    this one put two of them in the file. A stale first record makes the sha
    re-verify miss and the render is redone for nothing.
    """
    store, job = staged(tmp_path, slots=SHORT_SLOTS)
    render_job(job, store, now=NOW)

    rows = _assets(store)
    others = [row for row in rows if row["asset_id"] != renderer.RENDER_ASSET_ID]
    current = [row for row in rows if row["asset_id"] == renderer.RENDER_ASSET_ID][-1]
    superseded = dict(current, sha256="0" * 64)
    _write_assets(store, others + [superseded, current])
    _patch_job(store, status="RENDERING")

    def boom(*args, **kwargs):
        raise AssertionError("the superseded record was read; the render was redone")

    monkeypatch.setattr(render_adapter, "render", boom)

    again = render_job(store.load(JOB_ID).job, store, now=NOW)
    assert again.status is JobStatus.TECHNICAL_QA
