"""PLAN-001 issue #11: the orchestrator, the human gates, and the whole arc.

**Mock boundary, deliberately low.** The centrepiece here is one test that
really runs ``create_job`` to a Postiz draft with exactly two things stubbed:
``app.services.llm.OpenAI`` (the same boundary ``test_job_pipeline`` uses, so no
paid call is ever made) and the Postiz HTTP session. Everything else is the real
thing — the real ``JobStore`` on real files, the real state machine, real Edge
TTS over the network, real PNG bytes, the real ffmpeg render and the real
technical QA. Measured before this file existed: **no test in this repository
imported more than two stage modules**, and SPEC-001 §12:639 asks for
本機渲染完整流程.

**Which tests run offline.** Everything except the ones marked
``needs_the_full_stack``: the QA-gate tests, both new fixtures, the CLI tests
and the caption rule all drive the frozen fixtures through real transitions and
need neither ffmpeg nor a network. ``needs_the_full_stack`` gates on a real
one-word Edge TTS call and a real decoder, so a host with no ffmpeg or no
network skips rather than fails.

**Cost.** The full-stack job is built **once** per module and copied per test:
one ~5 s narration, three 1080x1920 stills, one render. The crash-resume tests
replay every stage over that copy and assert nothing was rebuilt, which is both
the cheap way to run them and the actual claim being made.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletion

import cli
from app.models.content_job import JobStatus
from app.services.jobs import master_voice, pipeline, voice_adapter
from app.services.jobs.postiz import PostizPublisher, PostizSettings
from app.services.jobs import renderer as job_renderer
from app.services.jobs import runner as job_runner
from app.services.jobs.runner import (
    JobBusyError,
    RunnerError,
    _job_lock,
    postiz_caption,
    run_job,
)
from app.services.jobs.state_machine import (
    ResumeError,
    decision_record,
    resume_target,
    transition,
)
from app.services.jobs.store import JobStore
from PIL import Image

from test.services.test_job_render import make_mp4

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"

#: Not a real credential, and not shaped like one — same reasoning as
#: ``test_job_postiz``: it exists only to be findable if it ever leaks.
POSTIZ_TOKEN = "postiz-placeholder-credential-not-a-real-secret"


# -- gates ------------------------------------------------------------------


@lru_cache(maxsize=1)
def full_stack_available() -> bool:
    """Can this host decode media *and* reach Edge TTS?

    A real synthesis, not a socket poke: what the run depends on is
    ``voice.tts`` returning a usable take, and nothing weaker proves that. Edge
    TTS needs no credential (measured), but it does need the network.

    The probe sentence is a full sentence on purpose. Measured 2026-08-30: a
    one-word take ("測試") comes back with a 775 ms timeline over 1780 ms of
    audio and ``voice_adapter`` rightly refuses it, which would have skipped
    this whole file on a host that is perfectly capable of running it.
    """
    if not voice_adapter.decoder_available():
        return False
    probe = Path(tempfile.mkdtemp()) / "probe.mp3"
    try:
        voice_adapter.synthesize(
            text="這是一段用來確認語音服務可用的測試語句。",
            voice_name=master_voice.DEFAULT_VOICE_NAME,
            voice_file=str(probe),
        )
    except Exception:
        return False
    return True


needs_the_full_stack = pytest.mark.skipif(
    not full_stack_available(),
    reason="this host has no decoder or cannot reach Edge TTS",
)


# -- the stubbed LLM --------------------------------------------------------

#: As short as the planner allows. ``scene_planner.MIN_SCENES`` is 8, so this is
#: eight one-sentence units (hook + five body paragraphs + conclusion + cta) and
#: not a word more: the narration is what Edge TTS speaks and what the render is
#: as long as, so every character here is CI wall-clock.
SCRIPT_PAYLOAD = {
    "title": "AI 導入的三個陷阱",
    "target_audience": "中小企業主管",
    "core_message": "先定義問題再選工具。",
    "hook": "多數專案第一週就失敗。",
    "body": [
        "先問要解決什麼問題。",
        "再挑最小可驗證流程。",
        "接著設定成功指標。",
        "然後找真實使用者試。",
        "最後才選工具。",
    ],
    "conclusion": "問題先行工具其次。",
    "cta": "追蹤看下一集。",
    "claims": ["多數導入失敗源於問題定義不清。"],
    "sources": ["https://example.test/ai-adoption"],
    "risk_flags": [],
}


def fake_llm_client():
    completion = ChatCompletion.model_validate(
        {
            "id": "completion-runner",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": json.dumps(SCRIPT_PAYLOAD, ensure_ascii=False),
                        "role": "assistant",
                    },
                }
            ],
            "created": 0,
            "model": "deepseek-v4-pro",
            "object": "chat.completion",
        }
    )
    client = Mock()
    client.chat.completions.create.return_value = completion
    return client


def llm_config():
    return patch.dict(
        pipeline.config.app,
        {
            "llm_provider": "deepseek",
            "deepseek_model_name": "deepseek-v4-pro",
            "deepseek_api_key": "demo-value",
            # Empty so the voice is chosen from the job's language rather than
            # from whatever this developer machine has in config.toml.
            "voice_name": "",
        },
        clear=False,
    )


def demo_request(**overrides):
    request = {
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "topic": "企業導入AI最常犯的三個錯誤",
        "target_duration_sec": 20,
        "language": "zh-TW",
        "image_mode": "assisted_qwen",
        "video_mode": "manual_google_flow",
        # Zero, so every scene is a still image: no MP4 has to be synthesised
        # and the render stays cheap. The video path is covered by
        # ``test_job_render``.
        "max_generated_video_scenes": 0,
        "publish_mode": "postiz_draft",
        "budget_limit_usd": 3,
    }
    request.update(overrides)
    return request


# -- Postiz doubles ---------------------------------------------------------


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"draft_id": "postiz-draft-777"}

    def json(self):
        return self._payload


def mock_publisher(store):
    session = Mock()
    session.request.return_value = _Response()
    publisher = PostizPublisher(
        PostizSettings(
            base_url="https://postiz.example.test/api",
            api_token=POSTIZ_TOKEN,
            platform="linkedin",
        ),
        session=session,
        store=store,
    )
    return publisher, session


# -- fixture staging --------------------------------------------------------


def place_imported_media(store: JobStore, job_id: str) -> None:
    """Do what a human does at ``AWAITING_ASSETS``: put the files where the
    §6.1 generation manifest says they belong."""
    manifest = store.read_generation_manifest(job_id)
    assert manifest is not None
    for index, entry in enumerate(manifest.entries):
        kind = PurePosixPath(entry.import_dir).parts[2]
        path = store.scene_media_dir(job_id, entry.scene_id, kind) / entry.expected_filename
        if entry.expected_filename.lower().endswith(".png"):
            # A different colour per scene on purpose: ``import_assets`` refuses
            # two scenes whose bytes hash the same, and a solid 1080x1920 fill
            # is byte-identical for every scene otherwise.
            Image.new("RGB", (1080, 1920), (32 + index * 7, 64, 128)).save(path, "PNG")
        else:
            make_mp4(path, ms=2000, width=1080 - index * 2)


def fixture_store(tmp_path, job_id):
    shutil.copytree(FIXTURES_ROOT / job_id, tmp_path / job_id)
    return JobStore(tmp_path)


def walk_to(store, job_id, *statuses):
    """Move a fixture job through real §5.2 transitions, decision lines and all."""
    job = store.load(job_id).job
    for status in statuses:
        reason = f"test setup: -> {status.value}"
        moved = transition(job, status, reason=reason)
        store.save(moved)
        store.append_decision(job_id, decision_record(job.status, moved, reason))
        job = moved
    return job


def hops(record):
    return [(entry["from"], entry["to"]) for entry in record.decisions]


def digests(root: Path):
    """sha of every file under ``root``, so "was this rebuilt" is answerable."""
    import hashlib

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# -- the whole arc ----------------------------------------------------------


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    """``create_job`` all the way to a mock Postiz draft. Built once, copied per test.

    Three ``run_job`` calls, because the arc really has three human moments:

    1. the run parks at ``MANUAL_ACTION_REQUIRED`` because nobody has imported
       the scene media yet (SPEC-001 §12:640, 缺素材時安全暫停);
    2. after a human drops the files in, the run resumes and stops at
       ``TECHNICAL_QA`` — content QA is a person's job (PRD-001 FR-008);
    3. with the human's approval and a publisher, it reaches ``POSTIZ_DRAFTED``.
    """
    root = tmp_path_factory.mktemp("demo-run")
    store = JobStore(root)
    client = fake_llm_client()

    with llm_config(), patch("app.services.llm.OpenAI", return_value=client):
        job = pipeline.create_job(demo_request(), store)
        job_id = job.content_job_id

        parked = run_job(job_id, store)
        place_imported_media(store, job_id)
        rendered = run_job(job_id, store)

        publisher, session = mock_publisher(store)
        drafted = run_job(
            job_id, store, content_qa_approved=True, publisher=publisher
        )

    return {
        "root": root,
        "job_id": job_id,
        "parked": parked,
        "rendered": rendered,
        "drafted": drafted,
        "llm_calls": client.chat.completions.create.call_count,
        "postiz_calls": session.request.call_args_list,
    }


@needs_the_full_stack
def test_the_whole_arc_reaches_a_postiz_draft(demo_run):
    """SPEC-001 §12:639 本機渲染完整流程, end to end, in one test."""
    store = JobStore(demo_run["root"])
    job_id = demo_run["job_id"]
    record = store.load(job_id)

    assert demo_run["drafted"].status is JobStatus.POSTIZ_DRAFTED
    assert demo_run["drafted"].draft == {
        "provider": "postiz",
        "draft_id": "postiz-draft-777",
        "status": "draft",
        "platform": "linkedin",
        "scheduled_at": None,
    }

    # Every artifact the arc is supposed to leave behind.
    assert record.script is not None and record.script.title == SCRIPT_PAYLOAD["title"]
    assert record.scenes
    assert record.render_manifest is not None
    assert store.master_voice_path(job_id, ".mp3").is_file()
    assert store.captions_srt_path(job_id).is_file()
    assert store.render_output_path(job_id, ".mp4").stat().st_size > 0
    assert store.read_technical_qa(job_id) is not None

    # Exactly one LLM call for the whole arc, across three run_job invocations.
    assert demo_run["llm_calls"] == 1


@needs_the_full_stack
def test_the_arc_walks_every_status_in_spec_5_2_order(demo_run):
    record = JobStore(demo_run["root"]).load(demo_run["job_id"])
    walked = hops(record)

    assert walked[:5] == [
        ("DRAFT", "SCRIPTING"),
        ("SCRIPTING", "SCENE_PLANNING"),
        ("SCENE_PLANNING", "VOICE_GENERATING"),
        ("VOICE_GENERATING", "AWAITING_ASSETS"),
        ("AWAITING_ASSETS", "MANUAL_ACTION_REQUIRED"),
    ]
    assert walked[-6:] == [
        ("READY_TO_RENDER", "RENDERING"),
        ("RENDERING", "TECHNICAL_QA"),
        ("TECHNICAL_QA", "CONTENT_QA"),
        ("CONTENT_QA", "READY_FOR_REVIEW"),
        ("READY_FOR_REVIEW", "POSTIZ_DRAFTING"),
        ("POSTIZ_DRAFTING", "POSTIZ_DRAFTED"),
    ]


@needs_the_full_stack
def test_the_missing_asset_pause_is_safe_and_resumable(demo_run):
    """SPEC-001 §12:640. The first run parked; the second one recovered."""
    assert demo_run["parked"].status is JobStatus.MANUAL_ACTION_REQUIRED
    assert demo_run["parked"].needs_a_human
    assert isinstance(demo_run["parked"].error, Exception)
    assert "no progress" in demo_run["parked"].stopped_because
    assert demo_run["rendered"].status is JobStatus.TECHNICAL_QA


@needs_the_full_stack
def test_the_run_stops_at_technical_qa_without_a_human_verdict(demo_run):
    assert demo_run["rendered"].status is JobStatus.TECHNICAL_QA
    assert "content QA is a human gate" in demo_run["rendered"].stopped_because
    assert demo_run["rendered"].draft is None


@needs_the_full_stack
def test_the_draft_request_is_draft_only_and_carries_the_script_caption(demo_run):
    """SPEC-001 §12:643, reached through the runner rather than a forced status."""
    (call,) = demo_run["postiz_calls"]
    body = call.kwargs.get("json") or call.args[-1]

    assert body["status"] == "draft"
    assert body.get("auto_upload") is False
    assert SCRIPT_PAYLOAD["hook"] in json.dumps(body, ensure_ascii=False)


@needs_the_full_stack
def test_the_technical_qa_report_says_what_was_checked_and_what_was_found(demo_run):
    """SPEC-001 §12:644. Before this slice these facts were computed and dropped."""
    store = JobStore(demo_run["root"])
    report = store.read_technical_qa(demo_run["job_id"])
    manifest = store.load(demo_run["job_id"]).render_manifest

    assert report["passed"] is True
    assert report["failures"] == []
    assert report["measured_with"] == "ffmpeg"
    assert "ffprobe" in report["measured_with_note"]
    for field in ("pixel_format", "fps", "audio_sample_rate"):
        assert report["measured"][field] is not None
    assert (report["measured"]["width"], report["measured"]["height"]) == (
        manifest.canvas.width,
        manifest.canvas.height,
    )
    assert report["expected"]["video_codec"] == manifest.output.video_codec
    assert store.technical_qa_path(demo_run["job_id"]).parent.name == "qa"


# -- crash between store.save and store.append_decision ---------------------

#: The three stages the crash was actually measured at. ``resume_target``
#: answers for none of them — that is the whole point.
CRASH_POINTS = (
    (JobStatus.SCRIPTING, 0),
    (JobStatus.SCENE_PLANNING, 1),
    (JobStatus.AWAITING_ASSETS, 3),
)


def rewind(store: JobStore, job_id: str, status: JobStatus, keep_lines: int) -> None:
    """Reproduce a kill between ``store.save`` and ``store.append_decision``.

    ``job.json`` is advanced to ``status`` and the decision line that would have
    followed is missing — measured 2026-08-30 by actually killing the process
    at these three points. The artifacts *downstream* of the crash are left in
    place on purpose: a real crash would not have them, and leaving them makes
    the assertion stronger, not weaker — the runner replays every stage and must
    rebuild none of them.
    """
    job_file = store.root / job_id / "job.json"
    payload = json.loads(job_file.read_text(encoding="utf-8"))
    payload["status"] = status.value
    job_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    decisions = store.root / job_id / "decisions.jsonl"
    lines = [line for line in decisions.read_text(encoding="utf-8").splitlines() if line]
    decisions.write_text(
        "".join(line + "\n" for line in lines[:keep_lines]), encoding="utf-8"
    )

    # The Postiz attempt is rewound too. A crash at one of these three stages
    # happened long before any draft existed, and the runner now refuses to POST
    # a second time for a job that already has an attempt on record — so a
    # fixture keeping that event would be modelling a state the crash cannot
    # produce, not a stricter one.
    events = store.root / job_id / "provider_events.jsonl"
    kept = [
        line
        for line in events.read_text(encoding="utf-8").splitlines()
        if line and json.loads(line).get("provider") != "postiz"
    ]
    events.write_text("".join(line + "\n" for line in kept), encoding="utf-8")


@needs_the_full_stack
@pytest.mark.parametrize(("status", "keep_lines"), CRASH_POINTS)
def test_resume_target_cannot_answer_for_a_crashed_stage(
    tmp_path, demo_run, status, keep_lines
):
    """The measurement the runner's dispatch rule is built on."""
    shutil.copytree(demo_run["root"], tmp_path / "store")
    store = JobStore(tmp_path / "store")
    rewind(store, demo_run["job_id"], status, keep_lines)
    record = store.load(demo_run["job_id"])

    with pytest.raises(ResumeError, match="not a parked status"):
        resume_target(record.job.status, record.decisions)


@needs_the_full_stack
@pytest.mark.parametrize(("status", "keep_lines"), CRASH_POINTS)
def test_a_crashed_job_resumes_to_the_same_end_state_without_redoing_work(
    tmp_path, demo_run, status, keep_lines
):
    shutil.copytree(demo_run["root"], tmp_path / "store")
    root = tmp_path / "store"
    store = JobStore(root)
    job_id = demo_run["job_id"]
    rewind(store, job_id, status, keep_lines)

    expensive = {
        name: digest
        for name, digest in digests(root).items()
        if name.endswith((".mp3", ".mp4", ".srt")) or name.endswith("script.json")
    }
    assert expensive, "the staged job should already hold the expensive artifacts"

    client = fake_llm_client()
    publisher, session = mock_publisher(store)
    with llm_config(), patch("app.services.llm.OpenAI", return_value=client):
        result = run_job(job_id, store, content_qa_approved=True, publisher=publisher)

    assert result.status is JobStatus.POSTIZ_DRAFTED
    assert client.chat.completions.create.call_count == 0
    assert session.request.call_count == 1
    after = digests(root)
    assert {name: after[name] for name in expensive} == expensive


# -- the human content-QA gate ----------------------------------------------


@pytest.fixture
def at_technical_qa(tmp_path):
    """The frozen fixture walked to ``TECHNICAL_QA`` through real transitions.

    No render is needed to exercise the three edges this slice added; the render
    itself is covered by ``test_job_render`` and by the full-arc test above.
    """
    store = fixture_store(tmp_path, "three-scene-demo")
    walk_to(
        store,
        "three-scene-demo",
        JobStatus.RENDERING,
        JobStatus.TECHNICAL_QA,
    )
    # The publisher refuses to draft a media file that does not exist, and the
    # frozen fixtures carry no bytes. Same placeholder ``test_job_postiz`` uses.
    store.render_output_path("three-scene-demo", ".mp4").write_bytes(
        b"not-really-a-video"
    )
    return store, "three-scene-demo"


def test_without_a_verdict_the_runner_refuses_to_pass_content_qa(at_technical_qa):
    store, job_id = at_technical_qa
    before = len(store.load(job_id).decisions)

    result = run_job(job_id, store)

    assert result.status is JobStatus.TECHNICAL_QA
    assert "human gate" in result.stopped_because
    assert len(store.load(job_id).decisions) == before
    assert result.rounds == 1


def test_an_approval_walks_the_three_edges_nothing_else_implements(at_technical_qa):
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)

    result = run_job(job_id, store, content_qa_approved=True, publisher=publisher)

    assert result.status is JobStatus.POSTIZ_DRAFTED
    assert hops(store.load(job_id))[-4:] == [
        ("TECHNICAL_QA", "CONTENT_QA"),
        ("CONTENT_QA", "READY_FOR_REVIEW"),
        ("READY_FOR_REVIEW", "POSTIZ_DRAFTING"),
        ("POSTIZ_DRAFTING", "POSTIZ_DRAFTED"),
    ]
    assert session.request.call_count == 1


def test_a_human_refusal_parks_the_job_and_is_recorded(at_technical_qa):
    """PRD-001 FR-008 人工否決能力: the gate must be able to say no."""
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)

    result = run_job(job_id, store, content_qa_approved=False, publisher=publisher)

    assert result.status is JobStatus.MANUAL_ACTION_REQUIRED
    assert result.stopped_because.startswith("a human refused content QA")
    assert session.request.call_count == 0
    last = store.load(job_id).decisions[-1]
    assert (last["from"], last["to"]) == ("CONTENT_QA", "MANUAL_ACTION_REQUIRED")
    assert "人工否決" in last["reason"]


def test_without_a_publisher_the_runner_stops_rather_than_inventing_settings(
    at_technical_qa,
):
    store, job_id = at_technical_qa

    result = run_job(job_id, store, content_qa_approved=True)

    assert result.status is JobStatus.POSTIZ_DRAFTING
    assert "no publisher was supplied" in result.stopped_because
    # The message must point at the config surface a shell user can actually
    # fill in, not at "a Python caller constructs it".
    assert "[postiz]" in result.stopped_because


# -- the caption rule -------------------------------------------------------


def test_the_caption_is_the_script_hook_and_cta(tmp_path):
    store = fixture_store(tmp_path, "three-scene-demo")
    script = store.load("three-scene-demo").script

    assert postiz_caption(script) == f"{script.hook}\n\n{script.cta}"


def test_a_job_with_no_script_is_refused_a_fabricated_caption():
    with pytest.raises(RunnerError, match="no script"):
        postiz_caption(None)


def test_an_explicit_caption_overrides_the_derived_one(at_technical_qa):
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)

    run_job(
        job_id,
        store,
        content_qa_approved=True,
        publisher=publisher,
        caption="人工撰寫的貼文文案",
    )

    body = json.dumps(session.request.call_args.kwargs.get("json"), ensure_ascii=False)
    assert "人工撰寫的貼文文案" in body


# -- the two new fixtures ---------------------------------------------------


def test_budget_exceeded_never_reaches_the_renderer(tmp_path):
    """SPEC-001 §12:642, at the orchestrator level."""
    store = fixture_store(tmp_path, "budget-exceeded")
    before = len(store.load("budget-exceeded").decisions)

    result = run_job("budget-exceeded", store)

    assert result.status is JobStatus.BUDGET_EXCEEDED
    # Deliberately pinned to the runner's own wording, not to "two hops":
    # ``resume_target``'s refusal message contains that phrase too, so the
    # looser assertion stayed green with this branch deleted (measured by
    # mutation 2026-08-30).
    assert result.stopped_because.startswith("the budget gate refused this job")
    assert len(store.load("budget-exceeded").decisions) == before
    assert not store.render_output_path("budget-exceeded", ".mp4").is_file()


def test_budget_exceeded_recovers_only_through_the_two_hop_path(tmp_path):
    store = fixture_store(tmp_path, "budget-exceeded")
    job_id = "budget-exceeded"

    # §5.2 gives BUDGET_EXCEEDED no return row; the human hop is the first of two.
    walk_to(store, job_id, JobStatus.MANUAL_ACTION_REQUIRED)
    # ...and the human clears the spend, or the gate simply refuses again.
    job_file = store.root / job_id / "job.json"
    payload = json.loads(job_file.read_text(encoding="utf-8"))
    payload["budget_limit_usd"] = 5.0
    job_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_job(job_id, store)

    walked = hops(store.load(job_id))
    assert ("MANUAL_ACTION_REQUIRED", "READY_TO_RENDER") in walked
    assert ("READY_TO_RENDER", "RENDERING") in walked
    # No second budget refusal: the gate passed this time. The job still cannot
    # render — this fixture carries no media bytes — so it parks on the render.
    assert walked.count(("READY_TO_RENDER", "BUDGET_EXCEEDED")) == 1
    assert result.status is JobStatus.RETRYABLE_FAILED


def test_the_render_failure_fixture_resumes_into_rendering(tmp_path):
    store = fixture_store(tmp_path, "render-failure")
    record = store.load("render-failure")
    assert record.job.status is JobStatus.RETRYABLE_FAILED
    assert resume_target(record.job.status, record.decisions) is JobStatus.RENDERING

    result = run_job("render-failure", store)

    assert ("RETRYABLE_FAILED", "RENDERING") in hops(store.load("render-failure"))
    assert result.status is JobStatus.RETRYABLE_FAILED
    assert result.error is not None


def test_a_job_that_reparks_identically_stops_instead_of_spinning(tmp_path):
    """The loop's termination condition, pinned against the naive one.

    ``resume_target``'s own docstring warns that a job which re-parks
    identically is handed the same stage forever, and that "did the log grow" is
    not a stop condition. Measured before this slice: 7 rounds, +302 bytes each,
    zero progress. This asserts both halves — the runner stops after one round,
    **and** the log did grow during it, so a naive check would still be running.
    """
    store = fixture_store(tmp_path, "render-failure")
    before = len(store.load("render-failure").decisions)

    result = run_job("render-failure", store)

    after = store.load("render-failure").decisions
    assert result.rounds == 1
    assert "no progress" in result.stopped_because
    assert len(after) == before + 2, "the log grew — the naive condition would loop"
    assert hops(store.load("render-failure"))[-2:] == [
        ("RETRYABLE_FAILED", "RENDERING"),
        ("RENDERING", "RETRYABLE_FAILED"),
    ]


def test_resume_can_be_switched_off(tmp_path):
    store = fixture_store(tmp_path, "render-failure")
    before = len(store.load("render-failure").decisions)

    result = run_job("render-failure", store, resume=False)

    assert result.status is JobStatus.RETRYABLE_FAILED
    assert "resume is disabled" in result.stopped_because
    assert len(store.load("render-failure").decisions) == before


def test_a_terminal_job_is_left_alone(tmp_path):
    store = fixture_store(tmp_path, "three-scene-demo")
    walk_to(store, "three-scene-demo", JobStatus.CANCELLED)

    result = run_job("three-scene-demo", store)

    assert result.status is JobStatus.CANCELLED
    assert result.rounds == 0
    assert "terminal" in result.stopped_because


# -- the CLI surface --------------------------------------------------------


def test_the_run_subcommand_does_not_disturb_the_flat_pipeline():
    """Measured: a non-required subparser coexists with the 40 flat options."""
    flat = cli.parse_args(["--video-subject", "x"])
    sub = cli.parse_args(["run", "--job", "job-1"])

    assert flat.command is None and flat.video_subject == "x"
    assert sub.command == "run" and sub.job == "job-1"
    assert sub.content_qa_approved is None and sub.resume is True


def test_the_flat_validation_block_no_longer_fires_for_a_subcommand():
    """It used to run unconditionally and would reject every ``run`` call."""
    with pytest.raises(SystemExit) as raised:
        cli.parse_args([])
    assert raised.value.code == 2

    assert cli.parse_args(["run", "--job", "job-1"]).video_subject == ""


def test_content_qa_can_be_refused_from_the_command_line():
    assert cli.parse_args(
        ["run", "--job", "j", "--no-content-qa-approved"]
    ).content_qa_approved is False


def test_the_run_command_prints_one_json_object_and_exits_zero(tmp_path, capsys):
    store = fixture_store(tmp_path, "three-scene-demo")
    walk_to(store, "three-scene-demo", JobStatus.RENDERING, JobStatus.TECHNICAL_QA)
    args = cli.parse_args(
        ["run", "--job", "three-scene-demo", "--store", str(store.root)]
    )

    code = cli.run_job_command(args)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert payload["status"] == "TECHNICAL_QA"
    assert payload["draft"] is None
    assert "human gate" in payload["stopped_because"]


def test_a_parked_job_exits_one(tmp_path, capsys):
    store = fixture_store(tmp_path, "render-failure")
    args = cli.parse_args(["run", "--job", "render-failure", "--store", str(store.root)])

    code = cli.run_job_command(args)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 1
    assert payload["status"] == "RETRYABLE_FAILED"
    assert payload["error"].startswith("RenderError")


def test_an_unknown_job_exits_two(tmp_path, capsys):
    args = cli.parse_args(["run", "--job", "nope", "--store", str(tmp_path)])

    assert cli.run_job_command(args) == 2
    assert capsys.readouterr().out == ""


# -- one runner per job ------------------------------------------------------


def test_a_second_runner_is_refused_while_the_first_holds_the_job(at_technical_qa):
    """Measured 2026-08-30: two threads on one job made two Postiz drafts and
    two paid LLM calls, each believing it had succeeded. ``cli.py run --job`` is
    a repeatable automated entry point, so an overlap is the ordinary case."""
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)

    with _job_lock(store._job_dir(job_id)):
        with pytest.raises(JobBusyError, match="already holds"):
            run_job(job_id, store, content_qa_approved=True, publisher=publisher)

    assert session.request.call_count == 0
    # The lock is released on the way out, so the next run is not blocked.
    assert run_job(
        job_id, store, content_qa_approved=True, publisher=publisher
    ).status is JobStatus.POSTIZ_DRAFTED
    assert not (store._job_dir(job_id) / ".runner.lock").exists()


# -- the draft is created at most once ---------------------------------------


def test_a_rerun_does_not_post_a_second_draft(at_technical_qa):
    """``RESUMABLE_STAGES`` excludes POSTIZ_DRAFTING so a resume cannot draft
    twice; the runner arrives by the front door and needs the same guard.

    The trigger is real: a POST the server accepted whose reply we could not
    read leaves the job at POSTIZ_DRAFTING with a provider event carrying the
    draft id, and Postiz sends no idempotency key on the wire."""
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)
    # A 200 the publisher cannot read a draft id out of: the POST happened, the
    # job stays at POSTIZ_DRAFTING, and the event records an empty draft id.
    session.request.return_value = _Response(payload={"ok": True})
    first = run_job(job_id, store, content_qa_approved=True, publisher=publisher)
    assert session.request.call_count == 1
    assert first.status is JobStatus.POSTIZ_DRAFTING

    session.request.return_value = _Response()
    result = run_job(job_id, store, content_qa_approved=True, publisher=publisher)

    assert session.request.call_count == 1
    assert "may already exist" in result.stopped_because


def test_a_storeless_publisher_never_reaches_the_socket(at_technical_qa):
    """A publisher persists only when a store was injected at construction, and
    the caller builds it. Measured 2026-08-30 in the real local demo: with a
    store-less publisher the POST went out, the job stayed at POSTIZ_DRAFTING
    with zero postiz provider events, and the duplicate-draft guard therefore
    saw nothing and would have posted a second time."""
    store, job_id = at_technical_qa
    session = Mock()
    session.request.return_value = _Response()
    storeless = PostizPublisher(
        PostizSettings(
            base_url="https://postiz.example.test/api",
            api_token=POSTIZ_TOKEN,
            platform="linkedin",
        ),
        session=session,
    )

    result = run_job(job_id, store, content_qa_approved=True, publisher=storeless)

    assert session.request.call_count == 0
    assert result.status is JobStatus.POSTIZ_DRAFTING
    assert "not bound to this job store" in result.stopped_because
    assert not [
        event
        for event in store.load(job_id).provider_events
        if event.provider == "postiz"
    ]


def test_a_publisher_bound_to_another_store_never_reaches_the_socket(
    at_technical_qa, tmp_path
):
    """Bound, but elsewhere: the draft would land in a different job tree — the
    same unrecorded-call failure wearing a disguise."""
    store, job_id = at_technical_qa
    elsewhere, session = mock_publisher(JobStore(str(tmp_path / "other")))

    result = run_job(job_id, store, content_qa_approved=True, publisher=elsewhere)

    assert session.request.call_count == 0
    assert "not bound to this job store" in result.stopped_because


def test_an_empty_render_is_never_drafted(at_technical_qa):
    """Nothing between the render and the draft re-checks the file, and
    ``postiz`` only asks whether it exists. A truncated ``final.mp4`` reached
    POSTIZ_DRAFTED (measured 2026-08-30)."""
    store, job_id = at_technical_qa
    publisher, session = mock_publisher(store)
    store.render_output_path(job_id, ".mp4").write_bytes(b"")

    result = run_job(job_id, store, content_qa_approved=True, publisher=publisher)

    assert session.request.call_count == 0
    assert result.status is JobStatus.POSTIZ_DRAFTING
    assert "missing or empty" in result.stopped_because


# -- the CLI exit contract ---------------------------------------------------


def test_a_failure_that_does_not_park_still_exits_one(tmp_path, capsys, monkeypatch):
    """SPEC-001 §5.3 lets a retryable failure *stay put* rather than park, so
    ``needs_a_human`` alone cannot be the exit condition: a job that failed at a
    plain stage status told cron it had succeeded (measured 2026-08-30)."""
    store = fixture_store(tmp_path, "three-scene-demo")
    walk_to(store, "three-scene-demo", JobStatus.RENDERING)

    def boom(*args, **kwargs):
        raise RuntimeError("the renderer died without parking the job")

    monkeypatch.setattr(job_renderer, "render_job", boom)
    args = cli.parse_args(
        ["run", "--job", "three-scene-demo", "--store", str(store.root)]
    )

    code = cli.run_job_command(args)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "RENDERING"
    assert payload["error"].startswith("RuntimeError")
    assert code == 1


def test_a_job_that_will_not_converge_exits_one_not_two(tmp_path, monkeypatch):
    """``RunnerError`` is a task failure; the epilog reserves 2 for bad input."""
    store = fixture_store(tmp_path, "three-scene-demo")
    args = cli.parse_args(
        ["run", "--job", "three-scene-demo", "--store", str(store.root)]
    )

    def boom(*a, **kw):
        raise RunnerError("not converging")

    monkeypatch.setattr(job_runner, "run_job", boom)
    assert cli.run_job_command(args) == 1
