"""Issue #6: synthesise the one Master Voice and lay out its timeline.

Mock boundary: ``edge_tts.Communicate`` and ``edge_tts.SubMaker`` only — the
two module attributes ``test/services/test_voice.py`` already patches. Nothing
under ``app/services/jobs/`` and nothing in ``app/services/voice.py`` is
stubbed, so the real ``voice.tts`` dispatcher and the real ``azure_tts_v1``
body execute and write real bytes. Issue #4 shipped a completely broken
feature behind 57 green tests because every one of them mocked the function
that destroyed the data; the handoff's lesson 1 is that the mock boundary
declares what you are not testing.
"""

import hashlib
import io
import json
import shutil
import tempfile
import wave
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.content_job import JobStatus
from app.services import voice as vs
from app.services.jobs import master_voice, voice_adapter
from app.services.jobs.master_voice import (
    MAX_VOICE_GENERATION_ATTEMPTS,
    VOICE_TTS_CALL_COST_CEILING_USD,
    MasterVoiceError,
    generate_master_voice,
    narration_text,
    resolve_voice_name,
    start_voice_generating,
)
from app.services.jobs.state_machine import BudgetExceededError
from app.services.jobs.store import JobStore

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"

TICKS_PER_SECOND = 10_000_000


# -- audio and edge_tts fakes ---------------------------------------------


def wav_bytes(seconds: float = 3.0, rate: int = 8000) -> bytes:
    """A real, decodable mono WAV. Measured: ffmpeg reads it even from a
    ``.mp3`` path, because it sniffs content rather than trusting the name."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def _decoder_available() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mp3"
        probe.write_bytes(wav_bytes(1.0))
        return vs.get_audio_duration(str(probe)) > 0


DECODER = _decoder_available()
needs_decoder = pytest.mark.skipif(
    not DECODER, reason="no audio decoder on this host, so duration cannot be measured"
)


class _Cue:
    def __init__(self, start_s: float, end_s: float, content: str):
        self.start = timedelta(seconds=start_s)
        self.end = timedelta(seconds=end_s)
        self.content = content


class FakeSubMaker:
    """Stands in for ``edge_tts.SubMaker``: boundary events in, cues out."""

    def __init__(self):
        self.events = []

    def feed(self, chunk):
        self.events.append(chunk)

    @property
    def cues(self):
        return [
            _Cue(
                event["offset"] / TICKS_PER_SECOND,
                (event["offset"] + event["duration"]) / TICKS_PER_SECOND,
                event["text"],
            )
            for event in self.events
        ]

    def get_srt(self):
        # azure_tts_v1's only success gate. Empty means "retry".
        return "1\n00:00:00,000 --> 00:00:01,000\nx\n" if self.events else ""


class FakeCommunicate:
    """Stands in for ``edge_tts.Communicate``. Counts its own construction so
    a test can prove the provider was never reached."""

    constructed = 0
    audio = None
    boundaries = None

    def __init__(self, text, voice, rate="+0%"):
        type(self).constructed += 1
        self.text = text
        self.voice = voice
        self.rate = rate

    async def stream(self):
        for chunk in type(self).audio or []:
            yield {"type": "audio", "data": chunk}
        for boundary in type(self).boundaries or []:
            yield {"type": "WordBoundary", **boundary}


def edge(audio=None, boundaries=None, seconds: float = 3.0):
    """Patch the two edge_tts attributes for one synthesis outcome."""
    if audio is None:
        audio = [wav_bytes(seconds)]
    if boundaries is None:
        half = int(seconds * TICKS_PER_SECOND / 2)
        boundaries = [
            {"offset": 0, "duration": half, "text": "first"},
            {"offset": half, "duration": half, "text": "second"},
        ]
    FakeCommunicate.constructed = 0
    FakeCommunicate.audio = audio
    FakeCommunicate.boundaries = boundaries
    return patch.multiple(
        vs.edge_tts, Communicate=FakeCommunicate, SubMaker=FakeSubMaker
    )


# -- job helpers -----------------------------------------------------------


def seeded(tmp_path, job_id="three-scene-demo", status=JobStatus.SCENE_PLANNING):
    """A writable copy of a frozen fixture, rewound to ``status``.

    Both fixtures are frozen at READY_TO_RENDER, so they already carry the
    image, video, audio and subtitle assets of a finished job. Rewinding only
    the status would leave a SCENE_PLANNING job holding a Master Voice it
    cannot yet have had — an inconsistent state the test invented, not one the
    pipeline can produce. So the imported assets are rewound with it.
    ``assets.jsonl`` is append-only through the store, which is exactly why the
    file is rewritten directly here rather than through it.
    """
    shutil.copytree(FIXTURES_ROOT / job_id, tmp_path / job_id)
    (tmp_path / job_id / "assets" / "assets.jsonl").write_text("", encoding="utf-8")
    store = JobStore(tmp_path)
    record = store.load(job_id)
    store.save(record.job.model_copy(update={"status": status}))
    return store, store.load(job_id).job


def voiced(tmp_path, job_id="three-scene-demo", **edge_kwargs):
    store, job = seeded(tmp_path, job_id)
    voicing = start_voice_generating(job, store)
    with edge(**edge_kwargs):
        asset = generate_master_voice(voicing, store)
    return store, job.content_job_id, asset


def audio_assets(store, job_id):
    return [a for a in store.load(job_id).assets if a.asset_type == "audio"]


# -- the SCENE_PLANNING -> VOICE_GENERATING edge ---------------------------


def test_start_voice_generating_closes_the_scene_planning_edge(tmp_path):
    store, job = seeded(tmp_path)
    scenes = store.load(job.content_job_id).scenes
    total_ms = sum(scene.duration_target_ms for scene in scenes)

    voicing = start_voice_generating(job, store)

    assert voicing.status is JobStatus.VOICE_GENERATING
    reloaded = store.load(job.content_job_id)
    assert reloaded.job.status is JobStatus.VOICE_GENERATING
    assert reloaded.decisions[-1]["from"] == JobStatus.SCENE_PLANNING.value
    assert reloaded.decisions[-1]["to"] == JobStatus.VOICE_GENERATING.value
    assert reloaded.decisions[-1]["reason"] == (
        f"{len(scenes)} scenes, duration total {total_ms} ms"
    )


@pytest.mark.parametrize(
    "status", [JobStatus.SCRIPTING, JobStatus.VOICE_GENERATING, JobStatus.AWAITING_ASSETS]
)
def test_start_voice_generating_refuses_any_other_status(tmp_path, status):
    store, job = seeded(tmp_path, status=status)
    before = len(store.load(job.content_job_id).decisions)

    with pytest.raises(MasterVoiceError, match="SCENE_PLANNING"):
        start_voice_generating(job, store)

    assert store.load(job.content_job_id).job.status is status
    assert len(store.load(job.content_job_id).decisions) == before


def test_start_voice_generating_uses_the_persisted_status_not_the_argument(tmp_path):
    store, job = seeded(tmp_path)
    start_voice_generating(job, store)

    # ``job`` is stale: on disk the job already left SCENE_PLANNING.
    with pytest.raises(MasterVoiceError):
        start_voice_generating(job, store)


def test_start_voice_generating_refuses_a_job_with_no_scenes(tmp_path):
    store, job = seeded(tmp_path)
    record = store.load(job.content_job_id)
    record.scenes = []
    store.replace(record)

    with pytest.raises(MasterVoiceError, match="scenes"):
        start_voice_generating(store.load(job.content_job_id).job, store)


# -- the headline end-to-end path ------------------------------------------


def test_master_voice_end_to_end_writes_both_artifacts(tmp_path):
    store, job_id, asset = voiced(tmp_path)
    job_dir = tmp_path / job_id

    audio = job_dir / "audio" / "master-voice.mp3"
    assert audio.is_file() and audio.stat().st_size > 0
    timeline = json.loads((job_dir / "audio" / "master-voice-timestamps.json").read_text())
    assert timeline["content_job_id"] == job_id
    assert timeline["master_voice_asset_id"] == asset.asset_id

    reloaded = store.load(job_id)
    assert reloaded.job.status is JobStatus.AWAITING_ASSETS
    assert reloaded.decisions[-1]["from"] == JobStatus.VOICE_GENERATING.value
    assert reloaded.decisions[-1]["to"] == JobStatus.AWAITING_ASSETS.value
    assert reloaded.decisions[-1]["reason"] == "master voice and timeline created"
    # The provider really was reached: the fake was constructed.
    assert FakeCommunicate.constructed == 1


def test_the_asset_record_describes_the_file_on_disk(tmp_path):
    store, job_id, asset = voiced(tmp_path)
    audio = tmp_path / job_id / "audio" / "master-voice.mp3"

    assert audio_assets(store, job_id) == [asset]
    assert asset.scene_id is None
    assert asset.asset_type == "audio"
    assert asset.storage_key == "audio/master-voice.mp3"
    assert asset.mime_type == "audio/mpeg"
    assert asset.bytes == audio.stat().st_size
    assert asset.sha256 == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert asset.duration_ms > 0
    assert asset.provider == "edge_tts"
    assert asset.consent_status == "not_applicable"


def test_narration_is_the_whole_script_in_scene_order_unmodified(tmp_path):
    store, job = seeded(tmp_path)
    scenes = sorted(store.load(job.content_job_id).scenes, key=lambda s: s.scene_index)

    assert narration_text(scenes) == "".join(scene.narration for scene in scenes)
    assert "<redacted>" not in narration_text(scenes)


# -- the timeline contract issue #7 will consume ---------------------------


def test_timeline_is_integer_milliseconds_not_ticks(tmp_path):
    store, job_id, _ = voiced(tmp_path, seconds=3.0)
    timeline = store.read_master_voice_timestamps(job_id)

    segments = timeline["segments"]
    assert segments
    assert [s["start_ms"] for s in segments] == [0, 1500]
    assert [s["end_ms"] for s in segments] == [1500, 3000]
    for segment in segments:
        assert isinstance(segment["start_ms"], int)
        assert isinstance(segment["end_ms"], int)
        assert segment["start_ms"] <= segment["end_ms"]
    assert segments[-1]["end_ms"] <= timeline["total_duration_ms"]
    # 3 seconds is 3000 ms, never the 30000000 ticks voice.py speaks in.
    assert timeline["total_duration_ms"] < 10_000


def test_legacy_tick_timeline_is_normalised_the_same_way():
    """Every non-Edge provider fills subs/offset in 100-nanosecond ticks."""

    class LegacyMaker:
        cues = []
        subs = ["Hello world", "Goodbye now"]
        offset = [(0, 15000000), (15000000, 30000000)]

    segments = voice_adapter.timeline_segments(LegacyMaker())

    assert [(s.start_ms, s.end_ms) for s in segments] == [(0, 1500), (1500, 3000)]
    assert [s.text for s in segments] == ["Hello world", "Goodbye now"]


@needs_decoder
def test_duration_is_measured_from_the_audio_when_a_decoder_exists(tmp_path):
    """The fixture is asymmetric on purpose.

    With a 3.0 s timeline over 3.0 s of audio the two candidate values are
    identical, so the test would grade only the literal string "measured" and
    would stay green if the code returned the provider's timeline instead.
    2.7 s of boundaries over 3.0 s of audio is 10% drift — inside tolerance,
    and far enough apart to tell the two answers apart.
    """
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)
    half = 27 * TICKS_PER_SECOND // 20  # 1.35 s, so the timeline claims 2.7 s

    with edge(
        audio=[wav_bytes(3.0)],
        boundaries=[
            {"offset": 0, "duration": half, "text": "first"},
            {"offset": half, "duration": half, "text": "second"},
        ],
    ):
        asset = generate_master_voice(voicing, store)

    timeline = store.read_master_voice_timestamps(job.content_job_id)
    assert timeline["duration_source"] == "measured"
    assert abs(timeline["total_duration_ms"] - 3000) <= 100
    assert timeline["total_duration_ms"] > 2800  # not the 2700 ms timeline
    assert asset.duration_ms == timeline["total_duration_ms"]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_duration_falls_back_to_the_timeline_only_on_a_decoderless_host(tmp_path):
    """A host with no decoder must still produce a usable job, and must say so.

    ``decoder_available`` is patched because it reports a property of the
    machine, not of the pipeline — this is the only way to stand on a host
    without ffmpeg. Nothing under ``app/services/jobs/`` is otherwise stubbed:
    the real ``voice.tts`` still runs and writes the bytes.
    """
    with patch.object(voice_adapter, "decoder_available", return_value=False):
        store, job_id, asset = voiced(
            tmp_path, audio=[b"opaque-bytes-no-decoder-can-read"]
        )
    timeline = store.read_master_voice_timestamps(job_id)

    assert timeline["duration_source"] == "timeline"
    assert timeline["total_duration_ms"] == 3000
    assert asset.duration_ms == 3000


@needs_decoder
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_undecodable_audio_is_refused_when_a_decoder_exists(tmp_path):
    """The drift check cannot see this failure, so it needs its own guard.

    ``_measure`` uses the very decoder that failed inside the provider, so a
    truncated download measures as 0.0 and would slip into the decoder-less
    branch — believing the fabricated timeline that came with it. SiliconFlow
    ships exactly this shape: undecodable bytes plus a flat one-second
    timeline, reported as success.

    The filter is for an upstream moviepy bug: when ffmpeg refuses the input,
    ``FFMPEG_AudioReader.__init__`` raises before setting ``self.proc`` and its
    ``__del__`` then trips over the missing attribute.
    """
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    with edge(
        audio=[b"truncated-download-that-is-not-audio" * 100],
        boundaries=[{"offset": 0, "duration": TICKS_PER_SECOND, "text": "one"}],
    ):
        with pytest.raises(MasterVoiceError, match="decoder cannot read"):
            generate_master_voice(voicing, store)

    assert audio_assets(store, job.content_job_id) == []
    assert store.read_master_voice_timestamps(job.content_job_id) is None
    assert not (tmp_path / job.content_job_id / "audio" / "master-voice.mp3").exists()


@needs_decoder
def test_segments_never_end_after_the_duration_the_take_reports(tmp_path):
    """An in-tolerance take can still decode shorter than its own timeline."""
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    # 3.0 s of audio, a timeline claiming 3.6 s: 17% drift, inside tolerance.
    with edge(
        audio=[wav_bytes(3.0)],
        boundaries=[
            {"offset": 0, "duration": 18 * TICKS_PER_SECOND // 10, "text": "first"},
            {
                "offset": 18 * TICKS_PER_SECOND // 10,
                "duration": 18 * TICKS_PER_SECOND // 10,
                "text": "second",
            },
        ],
    ):
        generate_master_voice(voicing, store)

    timeline = store.read_master_voice_timestamps(job.content_job_id)
    assert timeline["duration_source"] == "measured"
    total = timeline["total_duration_ms"]
    assert total < 3600
    assert timeline["segments"]
    assert max(s["end_ms"] for s in timeline["segments"]) <= total


@needs_decoder
def test_clamping_never_loses_a_spoken_word(tmp_path):
    """Trimming the bounds is not the same as dropping the segment.

    A word the provider timed just past the decoded end is only a few percent
    of drift — well inside tolerance — and deleting it would silently remove
    text from the document the captions stage reads, with nothing to show it
    was ever there.
    """
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)
    # 3.0 s of audio; the second boundary starts at 3.02 s, just past the end.
    with edge(
        audio=[wav_bytes(3.0)],
        boundaries=[
            {"offset": 0, "duration": 29 * TICKS_PER_SECOND // 10, "text": "first"},
            {
                "offset": 302 * TICKS_PER_SECOND // 100,
                "duration": 8 * TICKS_PER_SECOND // 100,
                "text": "LAST-WORD",
            },
        ],
    ):
        generate_master_voice(voicing, store)

    timeline = store.read_master_voice_timestamps(job.content_job_id)
    total = timeline["total_duration_ms"]
    texts = [segment["text"] for segment in timeline["segments"]]

    assert timeline["duration_source"] == "measured"
    assert texts == ["first", "LAST-WORD"]
    for segment in timeline["segments"]:
        assert segment["start_ms"] <= segment["end_ms"] <= total


def test_decoder_available_agrees_with_the_resolver(tmp_path):
    """It fails closed, but a wrong answer either way changes which branch of
    synthesize runs, so pin it to the resolver it is derived from."""
    with patch.object(voice_adapter.utils, "get_ffmpeg_binary", return_value=""):
        assert voice_adapter.decoder_available() is False
    with patch.object(
        voice_adapter.utils, "get_ffmpeg_binary", return_value="/nonexistent/ffmpeg"
    ):
        assert voice_adapter.decoder_available() is False
    with patch.object(
        voice_adapter.utils, "get_ffmpeg_binary", return_value="definitely-not-a-binary"
    ):
        assert voice_adapter.decoder_available() is False


def test_a_crash_before_the_status_write_is_finished_on_rerun(tmp_path):
    """The asset write and the status write are two steps.

    A crash between them leaves every artifact on disk with the job still in
    VOICE_GENERATING — and ``start_voice_generating`` refuses to re-enter that
    status, so without this the job would be wedged for good.
    """
    store, job_id, asset = voiced(tmp_path)
    rewound = store.load(job_id).job.model_copy(
        update={"status": JobStatus.VOICE_GENERATING}
    )
    store.save(rewound)

    with edge():
        FakeCommunicate.constructed = 0
        again = generate_master_voice(store.load(job_id).job, store)

    assert again == asset
    assert FakeCommunicate.constructed == 0
    assert store.load(job_id).job.status is JobStatus.AWAITING_ASSETS
    assert audio_assets(store, job_id) == [asset]


# -- refusing to advance on a bad take -------------------------------------


def test_a_provider_failure_does_not_advance_the_job(tmp_path):
    """voice.tts reports every failure by returning None and never raising."""
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    # No boundary events -> azure_tts_v1's get_srt() gate stays empty -> None.
    with edge(boundaries=[]):
        with pytest.raises(MasterVoiceError):
            generate_master_voice(voicing, store)

    reloaded = store.load(job.content_job_id)
    assert reloaded.job.status is JobStatus.VOICE_GENERATING
    assert audio_assets(store, job.content_job_id) == []
    assert not (tmp_path / job.content_job_id / "audio" / "master-voice.mp3").exists()
    assert store.read_master_voice_timestamps(job.content_job_id) is None
    # The attempt is still audited, and marked retryable. Filtered on the voice
    # slot: the fixtures ship with their own provider events.
    voice_events = [
        e for e in reloaded.provider_events if ":voice:generate:" in e.idempotency_key
    ]
    assert len(voice_events) == 1
    assert voice_events[0].retryable is True


def test_a_zero_byte_take_is_refused(tmp_path):
    """azure_tts_v1 gates success on a non-empty subtitle stream and never
    checks that an audio chunk arrived, so this take looks valid to it."""
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    with edge(audio=[]):
        with pytest.raises(MasterVoiceError):
            generate_master_voice(voicing, store)

    assert audio_assets(store, job.content_job_id) == []
    assert store.load(job.content_job_id).job.status is JobStatus.VOICE_GENERATING


def test_a_zero_byte_take_is_refused_on_a_decoderless_host_too(tmp_path):
    """The only path where the size check is load-bearing.

    With a decoder present an empty file is caught downstream by "the decoder
    cannot read this" — so without this test the size check can be deleted and
    the suite stays green, while a host with no ffmpeg silently accepts a
    zero-byte Master Voice.
    """
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    with patch.object(voice_adapter, "decoder_available", return_value=False):
        with edge(audio=[]):
            with pytest.raises(MasterVoiceError, match="zero-byte"):
                generate_master_voice(voicing, store)

    assert audio_assets(store, job.content_job_id) == []
    assert store.load(job.content_job_id).job.status is JobStatus.VOICE_GENERATING


@needs_decoder
def test_a_timeline_that_does_not_describe_its_audio_is_refused(tmp_path):
    """SiliconFlow fabricates a flat one-second timeline when it cannot decode
    what it downloaded; a take whose timeline disagrees with its audio is that
    failure, not a rounding difference."""
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    with edge(
        audio=[wav_bytes(10.0)],
        boundaries=[{"offset": 0, "duration": TICKS_PER_SECOND, "text": "one"}],
    ):
        with pytest.raises(MasterVoiceError, match="does not describe"):
            generate_master_voice(voicing, store)

    assert audio_assets(store, job.content_job_id) == []


def test_empty_narration_parks_the_job(tmp_path):
    store, job = seeded(tmp_path)
    record = store.load(job.content_job_id)
    record.scenes = [
        scene.model_copy(update={"narration": "  "}) for scene in record.scenes
    ]
    store.replace(record)
    voicing = start_voice_generating(store.load(job.content_job_id).job, store)

    with edge():
        with pytest.raises(MasterVoiceError, match="narration"):
            generate_master_voice(voicing, store)

    reloaded = store.load(job.content_job_id)
    assert reloaded.job.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert reloaded.decisions[-1]["to"] == JobStatus.MANUAL_ACTION_REQUIRED.value
    assert FakeCommunicate.constructed == 0


# -- budget ----------------------------------------------------------------


def test_the_budget_gate_blocks_the_provider(tmp_path):
    store, job = seeded(tmp_path)
    priced = store.load(job.content_job_id).job.model_copy(
        update={"budget_limit_usd": 0.01, "actual_cost_usd": 0.0}
    )
    store.save(priced)
    voicing = start_voice_generating(store.load(job.content_job_id).job, store)
    # A paid provider, so the ceiling is non-zero and the gate has something
    # to refuse.
    with patch.dict(master_voice.config.app, {"voice_name": "elevenlabs:v1:Fake"}):
        with edge():
            with pytest.raises(BudgetExceededError):
                generate_master_voice(voicing, store)

    reloaded = store.load(job.content_job_id)
    assert FakeCommunicate.constructed == 0
    assert reloaded.job.status is JobStatus.BUDGET_EXCEEDED
    assert audio_assets(store, job.content_job_id) == []
    assert not (tmp_path / job.content_job_id / "audio").exists()


def test_a_free_provider_records_a_known_zero_not_an_unknown(tmp_path):
    store, job_id, _ = voiced(tmp_path)
    reloaded = store.load(job_id)

    event = reloaded.provider_events[-1]
    assert event.provider == "edge_tts"
    assert event.estimated_cost_usd == 0.0
    assert event.actual_cost_usd == 0.0
    ledger = reloaded.usage_ledger[-1]
    assert ledger.actual_cost_usd == 0.0
    # Graded on its content, not its truthiness: swapping the free and paid
    # provenance strings has to fail something.
    assert "unmetered" in ledger.estimated_cost_source
    assert "ceiling" not in ledger.estimated_cost_source


def test_a_free_provider_is_not_charged_against_the_budget(tmp_path):
    """The gate's estimate must be the free 0.0, not the ceiling.

    Headroom here is 0.01 — under the 0.05 paid ceiling — so a stage that
    forgot the free branch would be refused before the provider was reached.
    """
    store, job = seeded(tmp_path)
    priced = store.load(job.content_job_id).job.model_copy(
        update={"budget_limit_usd": 0.01, "actual_cost_usd": 0.0}
    )
    store.save(priced)
    voicing = start_voice_generating(store.load(job.content_job_id).job, store)

    with edge():
        generate_master_voice(voicing, store)

    assert store.load(job.content_job_id).job.status is JobStatus.AWAITING_ASSETS
    assert FakeCommunicate.constructed == 1


def test_a_paid_provider_records_a_conservative_ceiling_and_unknown(tmp_path):
    """A paid provider cannot be driven to success here without a real key, so
    this grades the audit row — which is written on the failure path too, and
    is the row §10 cares about."""
    store, job = seeded(tmp_path)
    voicing = start_voice_generating(job, store)

    with patch.dict(master_voice.config.app, {"voice_name": "elevenlabs:v1:Fake"}):
        with edge():
            with pytest.raises(MasterVoiceError):
                generate_master_voice(voicing, store)

    reloaded = store.load(job.content_job_id)
    event = [
        e for e in reloaded.provider_events if ":voice:generate:" in e.idempotency_key
    ][-1]
    assert event.provider == "elevenlabs"
    assert event.estimated_cost_usd == VOICE_TTS_CALL_COST_CEILING_USD
    assert event.actual_cost_usd == "unknown"
    ledger = [
        e for e in reloaded.usage_ledger if ":voice:generate:" in e.idempotency_key
    ][-1]
    assert ledger.estimated_cost_source
    # edge_tts was never constructed: the dispatcher went to another provider.
    assert FakeCommunicate.constructed == 0


def test_one_audit_row_per_call_keyed_by_the_voice_slot(tmp_path):
    store, job_id, _ = voiced(tmp_path)
    reloaded = store.load(job_id)

    voice_events = [
        e for e in reloaded.provider_events if ":voice:generate:" in e.idempotency_key
    ]
    assert len(voice_events) == 1
    assert voice_events[0].idempotency_key == f"{job_id}:voice:generate:attempt-1"
    assert voice_events[0].scene_id is None
    voice_ledger = [
        e for e in reloaded.usage_ledger if ":voice:generate:" in e.idempotency_key
    ]
    assert len(voice_ledger) == 1


def test_voice_attempts_do_not_consume_the_script_attempt_budget(tmp_path):
    """pipeline filters on the ``script`` slot; reusing it would collide."""
    store, job_id, _ = voiced(tmp_path)
    keys = [e.idempotency_key for e in store.load(job_id).provider_events]

    assert f"{job_id}:voice:generate:attempt-1" in keys
    assert not any(":script:generate:" in key for key in keys if ":voice:" in key)


# -- idempotency and partial artifacts -------------------------------------


def test_rerun_makes_no_second_provider_call_and_no_second_asset(tmp_path):
    store, job_id, asset = voiced(tmp_path)

    FakeCommunicate.constructed = 0
    with edge():
        FakeCommunicate.constructed = 0
        again = generate_master_voice(store.load(job_id).job, store)

    assert again == asset
    assert audio_assets(store, job_id) == [asset]
    assert FakeCommunicate.constructed == 0
    voice_events = [
        e for e in store.load(job_id).provider_events if ":voice:generate:" in e.idempotency_key
    ]
    assert len(voice_events) == 1


def test_a_recorded_asset_with_a_missing_timeline_is_not_silently_resynthesised(tmp_path):
    store, job_id, _ = voiced(tmp_path)
    (tmp_path / job_id / "audio" / "master-voice-timestamps.json").unlink()

    with edge():
        with pytest.raises(MasterVoiceError, match="timeline"):
            generate_master_voice(store.load(job_id).job, store)

    assert FakeCommunicate.constructed == 0
    assert len(audio_assets(store, job_id)) == 1


def test_a_recorded_asset_with_missing_audio_is_not_silently_resynthesised(tmp_path):
    store, job_id, _ = voiced(tmp_path)
    (tmp_path / job_id / "audio" / "master-voice.mp3").unlink()

    with edge():
        with pytest.raises(MasterVoiceError, match="audio"):
            generate_master_voice(store.load(job_id).job, store)

    assert FakeCommunicate.constructed == 0
    assert len(audio_assets(store, job_id)) == 1


def test_the_attempt_cap_stops_calling_the_provider(tmp_path):
    store, job = seeded(tmp_path)
    start_voice_generating(job, store)
    for _ in range(MAX_VOICE_GENERATION_ATTEMPTS):
        with edge(boundaries=[]):
            with pytest.raises(MasterVoiceError):
                generate_master_voice(store.load(job.content_job_id).job, store)
        # A retryable failure leaves the job where it was, so it can retry.
        assert store.load(job.content_job_id).job.status is JobStatus.VOICE_GENERATING

    with edge():
        with pytest.raises(MasterVoiceError, match="attempt limit"):
            generate_master_voice(store.load(job.content_job_id).job, store)

    assert FakeCommunicate.constructed == 0
    assert store.load(job.content_job_id).job.status is JobStatus.MANUAL_ACTION_REQUIRED


# -- voice selection -------------------------------------------------------


@pytest.mark.parametrize(
    "language,expected_prefix",
    [("zh-TW", "zh-TW-"), ("zh-CN", "zh-CN-"), ("en-US", "en-US-"), ("xx-YY", "zh-CN-")],
)
def test_the_voice_follows_the_job_language(tmp_path, language, expected_prefix):
    store, job = seeded(tmp_path)
    localised = store.load(job.content_job_id).job.model_copy(
        update={"language": language}
    )

    with patch.dict(master_voice.config.app, {}, clear=False):
        master_voice.config.app.pop("voice_name", None)
        assert resolve_voice_name(localised).startswith(expected_prefix)


def test_a_configured_voice_wins_over_the_language_default(tmp_path):
    store, job = seeded(tmp_path)
    with patch.dict(master_voice.config.app, {"voice_name": "en-GB-SoniaNeural-Female"}):
        assert resolve_voice_name(store.load(job.content_job_id).job) == (
            "en-GB-SoniaNeural-Female"
        )


@pytest.mark.parametrize(
    "voice_name,provider",
    [
        ("zh-TW-HsiaoChenNeural-Female", "edge_tts"),
        ("elevenlabs:abc:Fake", "elevenlabs"),
        ("siliconflow:model:alex-Male", "siliconflow"),
        ("no-voice", "no_voice"),
    ],
)
def test_provider_identity_is_derived_from_the_voice_name(voice_name, provider):
    assert voice_adapter.resolve_identity(voice_name)[0] == provider


# -- scope -----------------------------------------------------------------


def test_the_frozen_fixtures_are_not_mutated(tmp_path):
    before = {
        path: path.read_bytes()
        for path in sorted(FIXTURES_ROOT.rglob("*"))
        if path.is_file()
    }
    voiced(tmp_path)
    after = {
        path: path.read_bytes()
        for path in sorted(FIXTURES_ROOT.rglob("*"))
        if path.is_file()
    }
    assert before == after


def test_ten_scene_fixture_also_voices(tmp_path):
    store, job_id, asset = voiced(tmp_path, job_id="ten-scene-demo")

    assert store.load(job_id).job.status is JobStatus.AWAITING_ASSETS
    assert asset.duration_ms > 0
