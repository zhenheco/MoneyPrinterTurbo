"""Issue #7: derive the subtitle track from the Master Voice timeline.

Mock boundary: ``edge_tts.Communicate`` / ``edge_tts.SubMaker`` only, reusing
issue #6's helpers so the voice really is synthesised before the captions are
derived from it. Nothing under ``app/services/jobs/`` is stubbed — every
assertion below reads bytes that the code under test actually wrote.
"""

import hashlib
import json
import re
from unittest.mock import patch

import pytest

from app.models.content_job import JobStatus
from app.services.jobs import voice_adapter
from app.services.jobs.captions import (
    CaptionCue,
    CaptionsError,
    caption_ref,
    generate_captions,
    render_srt,
    scene_cues,
    srt_timestamp,
)
from app.services.jobs.master_voice import generate_master_voice, start_voice_generating
from app.services.jobs.store import JobStoreError

from test.services.test_job_master_voice import (  # noqa: E402
    FIXTURES_ROOT,
    FakeCommunicate,
    edge,
    seeded,
)

TIMING = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


def _ms(hours, minutes, seconds, millis):
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def parse_srt(text: str):
    """Parse the timing lines ourselves.

    ``subtitle.file_to_subtitles`` returns its own running counter as the cue
    number, so it cannot detect a wrong or duplicated index, and its timestamp
    regex even matches ``::,``.
    """
    blocks = [b for b in text.split("\n\n") if b.strip()]
    parsed = []
    for block in blocks:
        lines = block.split("\n")
        match = TIMING.match(lines[1])
        assert match, f"malformed timing line: {lines[1]!r}"
        numbers = [int(g) for g in match.groups()]
        parsed.append(
            {
                "index": int(lines[0]),
                "start_ms": _ms(*numbers[:4]),
                "end_ms": _ms(*numbers[4:]),
                # A block ends with a newline before the blank separator, so
                # the text is everything after the timing line minus that.
                "text": "\n".join(lines[2:]).rstrip("\n"),
            }
        )
    return parsed


def awaiting(tmp_path, job_id="three-scene-demo", **edge_kwargs):
    """A job carrying a real Master Voice, sitting in AWAITING_ASSETS."""
    store, job = seeded(tmp_path, job_id)
    voicing = start_voice_generating(job, store)
    with edge(**edge_kwargs):
        generate_master_voice(voicing, store)
    return store, job.content_job_id


def captioned(tmp_path, job_id="three-scene-demo", **edge_kwargs):
    store, jid = awaiting(tmp_path, job_id, **edge_kwargs)
    asset = generate_captions(store.load(jid).job, store)
    return store, jid, asset


def subtitle_assets(store, job_id):
    return [a for a in store.load(job_id).assets if a.asset_type == "subtitle"]


# -- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "ms,expected",
    [
        (0, "00:00:00,000"),
        (8123, "00:00:08,123"),  # utils.time_convert_seconds_to_hmsm gives ,122
        (61_001, "00:01:01,001"),
        (3_661_010, "01:01:01,010"),
    ],
)
def test_srt_timestamp_is_exact_integer_arithmetic(ms, expected):
    assert srt_timestamp(ms) == expected


@pytest.mark.parametrize("bad", [-1, 1.5, True, "0"])
def test_srt_timestamp_refuses_anything_that_is_not_a_non_negative_int(bad):
    with pytest.raises(CaptionsError):
        srt_timestamp(bad)


def test_render_srt_has_no_trailing_whitespace():
    """utils.text_to_srt appends eight spaces to every block; this does not."""
    cues = [
        CaptionCue(1, "caption-001", "scene-001", 1, "第一句。", 0, 1500),
        CaptionCue(2, "caption-002", "scene-002", 2, "第二句。", 1500, 3000),
    ]

    text = render_srt(cues)

    assert text == (
        "1\n00:00:00,000 --> 00:00:01,500\n第一句。\n"
        "\n"
        "2\n00:00:01,500 --> 00:00:03,000\n第二句。\n"
    )
    for line in text.split("\n"):
        assert line == line.rstrip(), f"trailing whitespace: {line!r}"


def test_caption_ref_matches_the_frozen_render_manifests():
    manifest = json.loads(
        (FIXTURES_ROOT / "three-scene-demo" / "render_manifest.json").read_text()
    )
    for position, entry in enumerate(manifest["scenes"], start=1):
        assert entry["caption_ref"] == caption_ref(position)


# -- timeline validation ---------------------------------------------------


def _timeline(total=3000, segments=None, source="measured"):
    if segments is None:
        segments = [
            {"index": 1, "text": "a", "start_ms": 0, "end_ms": 1500},
            {"index": 2, "text": "b", "start_ms": 1500, "end_ms": 3000},
        ]
    return {
        "content_job_id": "x",
        "master_voice_asset_id": "asset-1",
        "total_duration_ms": total,
        "duration_source": source,
        "segments": segments,
    }


@pytest.mark.parametrize(
    "timeline",
    [
        None,
        {},
        _timeline(total=0),
        _timeline(total=-1),
        _timeline(source=""),
        _timeline(segments=[]),
        _timeline(segments=[{"start_ms": 5, "end_ms": 1}]),
        _timeline(segments=[{"start_ms": "0", "end_ms": 10}]),
        _timeline(segments=[{"start_ms": 1000, "end_ms": 2000}, {"start_ms": 500, "end_ms": 3000}]),
    ],
)
def test_a_malformed_timeline_is_refused(tmp_path, timeline):
    store, job = seeded(tmp_path)
    scenes = store.load(job.content_job_id).scenes

    with pytest.raises(CaptionsError):
        scene_cues(scenes=scenes, timeline=timeline)


def test_a_scene_with_no_narration_is_refused(tmp_path):
    store, job = seeded(tmp_path)
    scenes = store.load(job.content_job_id).scenes
    scenes[1] = scenes[1].model_copy(update={"narration": "   "})

    with pytest.raises(CaptionsError, match="narration"):
        scene_cues(scenes=scenes, timeline=_timeline())


def test_no_scenes_is_refused():
    with pytest.raises(CaptionsError, match="scenes"):
        scene_cues(scenes=[], timeline=_timeline())


# -- the headline path -----------------------------------------------------


def test_generate_captions_writes_both_artifacts(tmp_path):
    store, job_id, asset = captioned(tmp_path)
    job_dir = tmp_path / job_id

    srt = job_dir / "subtitles" / "captions.srt"
    document = job_dir / "subtitles" / "captions.json"
    assert srt.is_file() and srt.stat().st_size > 0
    assert json.loads(document.read_text(encoding="utf-8"))["content_job_id"] == job_id
    assert srt.read_text(encoding="utf-8")


def test_the_job_does_not_change_status_or_append_a_decision(tmp_path):
    store, job_id = awaiting(tmp_path)
    before = len(store.load(job_id).decisions)

    generate_captions(store.load(job_id).job, store)

    reloaded = store.load(job_id)
    assert reloaded.job.status is JobStatus.AWAITING_ASSETS
    assert len(reloaded.decisions) == before


def test_no_provider_event_or_ledger_row_is_written(tmp_path):
    store, job_id = awaiting(tmp_path)
    before = store.load(job_id)

    generate_captions(store.load(job_id).job, store)

    after = store.load(job_id)
    assert len(after.provider_events) == len(before.provider_events)
    assert len(after.usage_ledger) == len(before.usage_ledger)


def test_cue_text_is_each_scene_narration_verbatim(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    scenes = sorted(store.load(job_id).scenes, key=lambda s: s.scene_index)
    srt = (tmp_path / job_id / "subtitles" / "captions.srt").read_text(encoding="utf-8")

    cues = parse_srt(srt)

    assert [c["text"] for c in cues] == [s.narration for s in scenes]
    assert all("<redacted>" not in c["text"] for c in cues)


def test_cue_indexes_are_one_based_and_contiguous(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    srt = (tmp_path / job_id / "subtitles" / "captions.srt").read_text(encoding="utf-8")

    cues = parse_srt(srt)

    assert [c["index"] for c in cues] == list(range(1, len(cues) + 1))


# -- the timing ceiling ----------------------------------------------------


@pytest.mark.parametrize("job_id", ["three-scene-demo", "ten-scene-demo"])
def test_no_cue_runs_past_the_voice(tmp_path, job_id):
    store, jid, _ = captioned(tmp_path, job_id)
    timeline = store.read_master_voice_timestamps(jid)
    srt = (tmp_path / jid / "subtitles" / "captions.srt").read_text(encoding="utf-8")

    cues = parse_srt(srt)

    ceiling = timeline["total_duration_ms"]
    assert cues
    for cue in cues:
        assert 0 <= cue["start_ms"] < cue["end_ms"] <= ceiling
    for earlier, later in zip(cues, cues[1:]):
        assert earlier["end_ms"] <= later["start_ms"]
    assert max(c["end_ms"] for c in cues) <= ceiling


def test_a_legal_overrunning_timeline_is_clamped(tmp_path):
    """voice_adapter allows 25% drift and then reports the MEASURED duration,
    so the last segment can legitimately end past total_duration_ms."""
    store, job = seeded(tmp_path)
    scenes = store.load(job.content_job_id).scenes
    timeline = _timeline(
        total=3000,
        segments=[
            {"index": 1, "text": "a", "start_ms": 0, "end_ms": 1800},
            {"index": 2, "text": "b", "start_ms": 1800, "end_ms": 3600},
        ],
    )

    cues = scene_cues(scenes=scenes, timeline=timeline)

    assert cues[-1].end_ms == 3000
    assert all(cue.end_ms <= 3000 for cue in cues)


def test_a_voice_too_short_for_the_scenes_is_refused(tmp_path):
    store, job = seeded(tmp_path, "ten-scene-demo")
    scenes = store.load(job.content_job_id).scenes
    timeline = _timeline(
        total=3, segments=[{"index": 1, "text": "a", "start_ms": 0, "end_ms": 3}]
    )

    with pytest.raises(CaptionsError, match="too short"):
        scene_cues(scenes=scenes, timeline=timeline)


# -- caption_ref mapping ---------------------------------------------------


@pytest.mark.parametrize("job_id", ["three-scene-demo", "ten-scene-demo"])
def test_every_scene_gets_exactly_one_caption(tmp_path, job_id):
    store, jid, asset = captioned(tmp_path, job_id)
    scenes = sorted(store.load(jid).scenes, key=lambda s: s.scene_index)
    document = store.read_captions_document(jid)
    srt_cues = parse_srt(
        (tmp_path / jid / "subtitles" / "captions.srt").read_text(encoding="utf-8")
    )

    entries = document["captions"]
    assert len(entries) == len(scenes)
    assert [e["scene_id"] for e in entries] == [s.scene_id for s in scenes]
    assert [e["caption_ref"] for e in entries] == [
        f"caption-{s.scene_index:03d}" for s in scenes
    ]
    assert len({e["caption_ref"] for e in entries}) == len(entries)
    assert [e["srt_index"] for e in entries] == [c["index"] for c in srt_cues]
    for entry, cue in zip(entries, srt_cues):
        assert entry["start_ms"] == cue["start_ms"]
        assert entry["end_ms"] == cue["end_ms"]
        assert entry["text"] == cue["text"]
    assert document["subtitle_asset_id"] == asset.asset_id


def test_the_document_carries_the_master_voice_asset_id(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    voice = [a for a in store.load(job_id).assets if a.asset_type == "audio"][0]

    assert store.read_captions_document(job_id)["master_voice_asset_id"] == voice.asset_id


# -- duration_source is carried, not asserted away -------------------------


def test_a_measured_timeline_is_recorded_as_measured(tmp_path):
    store, job_id, _ = captioned(tmp_path)

    timeline = store.read_master_voice_timestamps(job_id)
    document = store.read_captions_document(job_id)
    assert document["voice_duration_source"] == timeline["duration_source"]
    assert document["voice_total_duration_ms"] == timeline["total_duration_ms"]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_an_unproven_timeline_still_produces_captions_and_says_so(tmp_path):
    """``duration_source == "timeline"`` means no decoder was available, so the
    ceiling is the provider's own claim. #6 records rather than refuses; #7
    does the same and passes the fact on to #9.

    ``decoder_available`` is patched because that state is a property of the
    machine, not of the pipeline — undecodable bytes alone will not reach this
    branch any more, since #6 now refuses them outright when a decoder exists.
    """
    with patch.object(voice_adapter, "decoder_available", return_value=False):
        store, job_id, _ = captioned(
            tmp_path, audio=[b"opaque-bytes-no-decoder-can-read"]
        )

    document = store.read_captions_document(job_id)
    assert document["voice_duration_source"] == "timeline"
    cues = parse_srt(
        (tmp_path / job_id / "subtitles" / "captions.srt").read_text(encoding="utf-8")
    )
    assert cues
    assert max(c["end_ms"] for c in cues) <= document["voice_total_duration_ms"]


# -- the AssetRecord -------------------------------------------------------


def test_the_subtitle_asset_describes_the_file_on_disk(tmp_path):
    store, job_id, asset = captioned(tmp_path)
    srt = tmp_path / job_id / "subtitles" / "captions.srt"
    timeline = store.read_master_voice_timestamps(job_id)

    assert subtitle_assets(store, job_id) == [asset]
    assert asset.asset_type == "subtitle"
    assert asset.scene_id is None
    assert asset.storage_key == "subtitles/captions.srt"
    assert asset.original_filename == "captions.srt"
    assert asset.mime_type == "application/x-subrip"
    assert asset.bytes == srt.stat().st_size
    assert asset.sha256 == hashlib.sha256(srt.read_bytes()).hexdigest()
    assert asset.duration_ms == timeline["total_duration_ms"]
    assert asset.consent_status == "not_applicable"
    assert asset.provider == "local_render"


def test_the_caption_asset_does_not_break_the_single_master_voice_rule(tmp_path):
    """#6 keys its uniqueness on asset_type == "audio"."""
    store, job_id, _ = captioned(tmp_path)

    with edge():
        FakeCommunicate.constructed = 0
        generate_master_voice(store.load(job_id).job, store)

    assert FakeCommunicate.constructed == 0
    assert len([a for a in store.load(job_id).assets if a.asset_type == "audio"]) == 1


# -- guards, idempotency, partial artifacts --------------------------------


@pytest.mark.parametrize(
    "status", [JobStatus.VOICE_GENERATING, JobStatus.READY_TO_RENDER, JobStatus.SCRIPTING]
)
def test_any_other_status_is_refused_and_writes_nothing(tmp_path, status):
    store, job_id = awaiting(tmp_path)
    moved = store.load(job_id).job.model_copy(update={"status": status})
    store.save(moved)
    before = len(store.load(job_id).decisions)

    with pytest.raises(CaptionsError, match="AWAITING_ASSETS"):
        generate_captions(moved, store)

    assert not (tmp_path / job_id / "subtitles").exists()
    assert subtitle_assets(store, job_id) == []
    assert len(store.load(job_id).decisions) == before
    assert store.load(job_id).job.status is status


def test_captions_without_a_master_voice_park_the_job(tmp_path):
    store, job = seeded(tmp_path, status=JobStatus.AWAITING_ASSETS)

    with pytest.raises(CaptionsError, match="Master Voice"):
        generate_captions(store.load(job.content_job_id).job, store)

    reloaded = store.load(job.content_job_id)
    assert reloaded.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert reloaded.decisions[-1]["to"] == JobStatus.MANUAL_ACTION_REQUIRED.value
    assert not (tmp_path / job.content_job_id / "subtitles").exists()


def test_rerun_is_idempotent(tmp_path):
    store, job_id, asset = captioned(tmp_path)
    srt = tmp_path / job_id / "subtitles" / "captions.srt"
    document = tmp_path / job_id / "subtitles" / "captions.json"
    before = (srt.read_bytes(), document.read_bytes())

    again = generate_captions(store.load(job_id).job, store)

    assert again == asset
    assert subtitle_assets(store, job_id) == [asset]
    assert (srt.read_bytes(), document.read_bytes()) == before


def test_a_recorded_asset_with_a_missing_srt_is_not_silently_regenerated(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    (tmp_path / job_id / "subtitles" / "captions.srt").unlink()

    with pytest.raises(CaptionsError, match="missing or empty"):
        generate_captions(store.load(job_id).job, store)

    assert len(subtitle_assets(store, job_id)) == 1


def test_a_recorded_asset_with_a_missing_document_is_not_silently_regenerated(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    (tmp_path / job_id / "subtitles" / "captions.json").unlink()

    with pytest.raises(CaptionsError, match="captions.json"):
        generate_captions(store.load(job_id).job, store)

    assert len(subtitle_assets(store, job_id)) == 1


def test_a_tampered_srt_is_detected_by_its_checksum(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    srt = tmp_path / job_id / "subtitles" / "captions.srt"
    srt.write_text(srt.read_text(encoding="utf-8") + "\n99\n", encoding="utf-8")

    with pytest.raises(CaptionsError, match="checksum"):
        generate_captions(store.load(job_id).job, store)

    assert len(subtitle_assets(store, job_id)) == 1


# -- store boundary --------------------------------------------------------


@pytest.mark.parametrize("bad", ["srt", ".s/rt", "..", ".tooooolong", ""])
def test_the_store_refuses_a_bogus_captions_extension(tmp_path, bad):
    store, _ = seeded(tmp_path)

    with pytest.raises(JobStoreError):
        store.captions_relative_path(bad)


def test_the_store_refuses_a_document_for_another_job(tmp_path):
    store, job = seeded(tmp_path)

    with pytest.raises(JobStoreError):
        store.write_captions_document(
            job.content_job_id, {"content_job_id": "someone-else"}
        )


def test_replace_does_not_delete_the_subtitle_artifacts(tmp_path):
    store, job_id, _ = captioned(tmp_path)
    record = store.load(job_id)

    store.replace(record)

    assert (tmp_path / job_id / "subtitles" / "captions.srt").is_file()
    assert (tmp_path / job_id / "subtitles" / "captions.json").is_file()


# -- scope -----------------------------------------------------------------


def test_the_frozen_fixtures_are_not_mutated(tmp_path):
    before = {
        path: path.read_bytes()
        for path in sorted(FIXTURES_ROOT.rglob("*"))
        if path.is_file()
    }
    captioned(tmp_path)
    after = {
        path: path.read_bytes()
        for path in sorted(FIXTURES_ROOT.rglob("*"))
        if path.is_file()
    }
    assert before == after


def test_the_srt_round_trips_through_the_repository_parser(tmp_path):
    """A sanity check that the file is really SubRip, using the repo's own
    reader — but not for the index or the ceiling: its cue number is the
    parser's own counter and its timestamp regex even matches '::,'."""
    from app.services.subtitle import file_to_subtitles

    store, job_id, _ = captioned(tmp_path)
    scenes = sorted(store.load(job_id).scenes, key=lambda s: s.scene_index)

    parsed = file_to_subtitles(str(tmp_path / job_id / "subtitles" / "captions.srt"))

    assert len(parsed) == len(scenes)
    assert [item[2] for item in parsed] == [s.narration for s in scenes]
