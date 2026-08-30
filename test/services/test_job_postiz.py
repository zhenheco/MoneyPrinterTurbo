"""SPEC-001 §6.4 Postiz draft adapter.

Every draft-only guard is asserted the same way: not "it raised", but "the mock
session was never asked to issue a request". A guard that raises *after* the
HTTP call has already created a public post on Postiz is not a guard, and a
test that only checks the exception cannot tell the two apart.

The credential used here is an obvious placeholder rather than a realistic
token shape: the repo's pre-commit gitleaks scan blocks anything that looks
like a live secret, and the tests only need a distinctive string to prove it
never reaches a message, a log line or a job file.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.models.content_job import JobStatus, ProviderEvent
from app.services.jobs.postiz import (
    PostizAPIError,
    PostizConfigurationError,
    PostizDraftOnlyError,
    PostizError,
    PostizPublisher,
    PostizSettings,
    settings_from_config,
)
from app.services.jobs.state_machine import ErrorClass, classify_error, transition
from app.services.jobs.store import JobStore

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "jobs"

#: Not a real credential and not shaped like one on purpose — see the module
#: docstring. Its only job is to be findable if it ever leaks.
POSTIZ_TOKEN = "postiz-placeholder-credential-not-a-real-secret"

#: READY_TO_RENDER (the frozen fixture's state) up to the one state §5.2 allows
#: a draft to be created from.
TO_DRAFTING = (
    JobStatus.RENDERING,
    JobStatus.TECHNICAL_QA,
    JobStatus.CONTENT_QA,
    JobStatus.READY_FOR_REVIEW,
    JobStatus.POSTIZ_DRAFTING,
)


class _Response:
    """Mirrors the slice of ``requests.Response`` the adapter touches."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def settings(**overrides):
    payload = {
        "base_url": "https://postiz.example.test/api",
        "api_token": POSTIZ_TOKEN,
        "platform": "linkedin",
    }
    payload.update(overrides)
    return PostizSettings(**payload)


def mock_session(status_code=200, payload=None):
    session = Mock()
    session.request.return_value = _Response(
        status_code, {"draft_id": "postiz-draft-777"} if payload is None else payload
    )
    return session


def demo_store(tmp_path, job_id="three-scene-demo"):
    """A JobStore over a scratch copy of the frozen slice-1 fixture."""
    shutil.copytree(FIXTURES_ROOT / job_id, tmp_path / job_id)
    return JobStore(tmp_path)


def drafting_job(store, job_id="three-scene-demo"):
    """Walk the fixture job to POSTIZ_DRAFTING through real §5.2 transitions."""
    job = store.load(job_id).job
    for status in TO_DRAFTING:
        job = transition(job, status, reason=f"test setup: -> {status.value}")
        store.save(job)
    return job


def media_file(tmp_path, name="render.mp4"):
    path = tmp_path / name
    path.write_bytes(b"not-really-a-video")
    return str(path)


@pytest.fixture
def drafting(tmp_path):
    """A job parked at POSTIZ_DRAFTING, its store, and a rendered media file."""
    store = demo_store(tmp_path)
    return store, drafting_job(store), media_file(tmp_path)


class TestDraftShape:
    def test_returns_the_spec_6_4_shape(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        result = publisher.create_draft(job, media, "三個錯誤")

        assert result == {
            "provider": "postiz",
            "draft_id": "postiz-draft-777",
            "status": "draft",
            "platform": "linkedin",
            "scheduled_at": None,
        }
        assert session.request.call_count == 1

    def test_request_body_asks_for_a_draft_and_carries_no_publish_switch(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        publisher.create_draft(job, media, "三個錯誤")

        body = session.request.call_args.kwargs["json"]
        assert body["status"] == "draft"
        assert "publish_now" not in body
        assert body["auto_upload"] is False

    def test_authorization_header_is_sent_but_never_stored(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        publisher.create_draft(job, media, "三個錯誤")

        headers = session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {POSTIZ_TOKEN}"
        written = "".join(
            path.read_text(encoding="utf-8")
            for path in (tmp_dir_files(store, job.content_job_id))
        )
        assert POSTIZ_TOKEN not in written


def tmp_dir_files(store, job_id):
    """Every file the job directory holds, so a leak anywhere is caught."""
    return [path for path in (store.root / job_id).rglob("*") if path.is_file()]


class TestDraftOnlyIsEnforcedLocally:
    """Each of these must be refused before a single byte leaves the process."""

    def test_publish_now_is_refused_even_when_false(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, media, "文案", options={"publish_now": False})

        assert session.request.call_count == 0

    def test_auto_upload_true_is_refused(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, media, "文案", options={"auto_upload": True})

        assert session.request.call_count == 0

    def test_auto_upload_false_is_allowed(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        result = publisher.create_draft(
            job, media, "文案", options={"auto_upload": False}
        )

        assert result["status"] == "draft"
        assert session.request.call_count == 1

    def test_non_draft_status_is_refused(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, media, "文案", options={"status": "public"})

        assert session.request.call_count == 0

    def test_job_whose_publish_mode_is_not_draft_is_refused(self, drafting):
        store, job, media = drafting
        public_job = job.model_copy(update={"publish_mode": "postiz_publish"})
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(public_job, media, "文案")

        assert session.request.call_count == 0

    def test_job_not_in_postiz_drafting_is_refused_before_the_call(self, drafting):
        store, job, media = drafting
        early = job.model_copy(update={"status": JobStatus.READY_FOR_REVIEW})
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(early, media, "文案")

        # A draft created here would be an orphan: §5.2 has no edge from
        # READY_FOR_REVIEW to POSTIZ_DRAFTED, so the job could never record it.
        assert session.request.call_count == 0
        # A refusal that left no trace is indistinguishable from a call that
        # never happened, so the state guard must also write its decision line.
        decisions = store.load(job.content_job_id).decisions
        assert decisions[-1]["from"] == decisions[-1]["to"] == "READY_FOR_REVIEW"
        assert "POSTIZ_DRAFTING" in decisions[-1]["reason"]

    def test_missing_media_is_refused_before_the_call(self, drafting):
        store, job, _ = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, str(store.root / "nope.mp4"), "文案")

        assert session.request.call_count == 0

    def test_draft_only_refusal_is_never_retried(self, drafting):
        store, job, media = drafting
        publisher = PostizPublisher(settings(), session=mock_session(), store=store)

        with pytest.raises(PostizDraftOnlyError) as raised:
            publisher.create_draft(job, media, "文案", options={"publish_now": True})

        assert classify_error(raised.value) is ErrorClass.NON_RETRYABLE


class TestFailuresNeverFabricateADraft:
    def test_non_2xx_raises_and_leaves_the_job_at_drafting(self, drafting):
        store, job, media = drafting
        session = mock_session(500, {"error": "postiz exploded"})
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert raised.value.status_code == 500
        assert store.load(job.content_job_id).job.status is JobStatus.POSTIZ_DRAFTING

    def test_2xx_without_a_draft_id_raises(self, drafting):
        store, job, media = drafting
        session = mock_session(200, {"status": "draft"})
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert "draft_id" in str(raised.value)
        assert store.load(job.content_job_id).job.status is JobStatus.POSTIZ_DRAFTING

    def test_2xx_with_a_blank_draft_id_raises(self, drafting):
        store, job, media = drafting
        session = mock_session(200, {"draft_id": "   "})
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError):
            publisher.create_draft(job, media, "文案")

    def test_a_server_that_published_anyway_is_rejected(self, drafting):
        store, job, media = drafting
        session = mock_session(
            200, {"draft_id": "postiz-draft-777", "status": "published"}
        )
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert "published" in str(raised.value)
        assert store.load(job.content_job_id).job.status is JobStatus.POSTIZ_DRAFTING

    def test_invalid_json_raises(self, drafting):
        store, job, media = drafting
        session = mock_session(200, ValueError("no json here"))
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError):
            publisher.create_draft(job, media, "文案")

    def test_transport_failure_is_retryable(self, drafting):
        import requests

        store, job, media = drafting
        session = Mock()
        session.request.side_effect = requests.ConnectionError("connection reset")
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert classify_error(raised.value) is ErrorClass.RETRYABLE

    def test_rate_limit_is_retryable_but_a_bad_request_is_not(self, drafting):
        store, job, media = drafting

        for status_code, expected in ((429, ErrorClass.RETRYABLE), (400, ErrorClass.NON_RETRYABLE)):
            publisher = PostizPublisher(
                settings(), session=mock_session(status_code, {}), store=store
            )
            with pytest.raises(PostizAPIError) as raised:
                publisher.create_draft(job, media, "文案")
            assert classify_error(raised.value) is expected


class TestTokenNeverLeaks:
    def test_token_is_absent_from_repr(self):
        publisher = PostizPublisher(settings(), session=mock_session())

        assert POSTIZ_TOKEN not in repr(publisher)
        assert POSTIZ_TOKEN not in repr(publisher.settings)

    def test_token_echoed_by_the_server_is_redacted_from_the_error(self, drafting):
        store, job, media = drafting
        session = mock_session(
            401, {"error": f"Bearer {POSTIZ_TOKEN} was rejected"}
        )
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert POSTIZ_TOKEN not in str(raised.value)

    def test_token_echoed_by_the_server_is_redacted_from_the_job_files(self, drafting):
        store, job, media = drafting
        session = mock_session(401, {"error": f"Bearer {POSTIZ_TOKEN} was rejected"})
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError):
            publisher.create_draft(job, media, "文案")

        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert POSTIZ_TOKEN not in written


class TestConfiguration:
    def test_session_is_self_created_by_default(self):
        import requests

        publisher = PostizPublisher(settings())

        assert isinstance(publisher.session, requests.Session)

    def test_plain_http_base_url_is_refused(self):
        with pytest.raises(ValueError):
            settings(base_url="http://postiz.example.test/api").validate()

    def test_missing_token_is_refused(self):
        with pytest.raises(ValueError):
            settings(api_token="").validate()


class TestEndToEnd:
    """Real JobStore, real fixture, real transitions — no mocks but the socket."""

    def test_a_draft_id_that_cannot_be_recorded_does_not_leave_a_drafted_status(
        self, tmp_path, monkeypatch
    ):
        """The status and the event that holds the draft id must not diverge.

        POSTIZ_DRAFTED on disk with no provider event is the orphan this module
        exists to prevent: the draft is live on Postiz and nothing local can
        name it.
        """
        store = demo_store(tmp_path)
        job = drafting_job(store)
        media = media_file(tmp_path)
        publisher = PostizPublisher(settings(), session=mock_session(), store=store)
        monkeypatch.setattr(
            PostizPublisher,
            "_event",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no event")),
        )

        with pytest.raises(RuntimeError):
            publisher.create_draft(job, media, "三個錯誤")

        record = store.load(job.content_job_id)
        drafted = record.job.status is JobStatus.POSTIZ_DRAFTED
        recorded = [e for e in record.provider_events if e.provider == "postiz"]
        assert not (drafted and not recorded)

    def test_successful_draft_lands_on_disk_without_losing_the_job(self, tmp_path):
        store = demo_store(tmp_path)
        job = drafting_job(store)
        media = media_file(tmp_path)
        publisher = PostizPublisher(
            settings(), session=mock_session(), store=store
        )

        result = publisher.create_draft(job, media, "三個錯誤")

        record = store.load(job.content_job_id)
        assert record.job.status is JobStatus.POSTIZ_DRAFTED
        # The draft id is persisted where §4.6 keeps a provider's own handle:
        # ContentJob has no field for it and §4.2 forbids extra fields.
        drafts = [
            event for event in record.provider_events if event.provider == "postiz"
        ]
        assert [event.external_job_id for event in drafts] == [result["draft_id"]]
        assert drafts[0].error_class is None
        assert record.decisions[-1]["to"] == JobStatus.POSTIZ_DRAFTED.value
        assert result["draft_id"] in record.decisions[-1]["reason"]

        # Nothing else in the job directory was disturbed.
        assert len(record.scenes) == 3
        assert record.script is not None
        assert record.script.title
        assert record.render_manifest is not None
        assert len(record.assets) == len(
            store.load(job.content_job_id).assets
        )

    def test_failed_draft_leaves_the_job_recoverable(self, tmp_path):
        store = demo_store(tmp_path)
        job = drafting_job(store)
        media = media_file(tmp_path)
        publisher = PostizPublisher(
            settings(), session=mock_session(503, {"error": "upstream down"}), store=store
        )

        with pytest.raises(PostizAPIError):
            publisher.create_draft(job, media, "三個錯誤")

        record = store.load(job.content_job_id)
        assert record.job.status is JobStatus.POSTIZ_DRAFTING
        failures = [
            event
            for event in record.provider_events
            if event.provider == "postiz" and event.error_class
        ]
        assert len(failures) == 1
        assert failures[0].external_job_id == ""
        assert failures[0].retryable is True
        assert record.decisions[-1]["from"] == JobStatus.POSTIZ_DRAFTING.value
        assert record.decisions[-1]["to"] == JobStatus.POSTIZ_DRAFTING.value
        assert len(record.scenes) == 3
        assert record.script is not None
        assert record.render_manifest is not None

    def test_draft_only_refusal_writes_no_http_and_no_state_change(self, tmp_path):
        store = demo_store(tmp_path)
        job = drafting_job(store)
        media = media_file(tmp_path)
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)
        before = store.load(job.content_job_id)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, media, "三個錯誤", options={"publish_now": True})

        after = store.load(job.content_job_id)
        assert session.request.call_count == 0
        assert after.job.status is JobStatus.POSTIZ_DRAFTING
        assert len(after.decisions) == len(before.decisions) + 1
        assert after.decisions[-1]["to"] == JobStatus.POSTIZ_DRAFTING.value
        assert len(after.scenes) == 3

    def test_publisher_works_without_a_store(self, tmp_path):
        """The adapter is usable as a pure client; persistence is opt-in."""
        store = demo_store(tmp_path)
        job = drafting_job(store)
        publisher = PostizPublisher(settings(), session=mock_session())

        result = publisher.create_draft(job, media_file(tmp_path), "三個錯誤")

        assert result["draft_id"] == "postiz-draft-777"
        assert store.load(job.content_job_id).job.status is JobStatus.POSTIZ_DRAFTING


def test_provider_event_written_for_a_draft_is_a_valid_4_6_record(tmp_path):
    store = demo_store(tmp_path)
    job = drafting_job(store)
    publisher = PostizPublisher(settings(), session=mock_session(), store=store)

    publisher.create_draft(job, media_file(tmp_path), "三個錯誤")

    lines = (store.root / job.content_job_id / "provider_events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    payload = json.loads(lines[-1])
    event = ProviderEvent.model_validate(payload)
    assert event.content_job_id == job.content_job_id
    assert event.idempotency_key.startswith(f"{job.content_job_id}:")
    assert "postiz-draft-777" == event.external_job_id


#: An opaque token: no ``Bearer`` prefix, no ``sk-``/``ghp_``/``AKIA`` marker,
#: no adjacent colon, and — unlike :data:`POSTIZ_TOKEN` — not one of the words
#: :func:`~app.services.jobs.budget.redact` treats as self-declaring (``token``,
#: ``secret``, ``credential``…). Nothing in the generic pattern set can spot it,
#: so a test using it proves the publisher redacts its *own* secret rather than
#: proving the redactor is good at well-shaped credentials. Still an obvious
#: placeholder, so the pre-commit gitleaks scan stays quiet.
OPAQUE_TOKEN = "postiz-placeholder-value-0001"


class TestOptionsAreInspectedNotForwarded:
    """``options`` feeds the guards; it never reaches the wire.

    The three guards recognise the literal keys ``publish_now``,
    ``auto_upload`` and ``status``. Anything else a caller puts in ``options``
    used to be copied verbatim into the request body — including ``type``,
    which is what the real Postiz API reads to decide draft/schedule/now. The
    wire format is this module's own invention, so there is nothing a caller
    needs to pass through, and the body is built only from keys this module
    controls.
    """

    @pytest.mark.parametrize(
        "options, leaked_key",
        [
            ({"publishNow": True}, "publishNow"),
            ({"publish": True}, "publish"),
            ({"PUBLISH_NOW": True}, "PUBLISH_NOW"),
            ({"type": "now"}, "type"),
            ({"state": "PUBLISHED"}, "state"),
            ({"settings": {"publish_now": True}}, "settings"),
        ],
    )
    def test_unknown_option_keys_never_reach_the_request_body(
        self, drafting, options, leaked_key
    ):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        publisher.create_draft(job, media, "文案", options=options)

        body = session.request.call_args.kwargs["json"]
        assert leaked_key not in body
        assert set(body) == {
            "status",
            "auto_upload",
            "platform",
            "content",
            "media",
            "content_job_id",
        }

    def test_auto_upload_false_is_allowed_but_still_not_copied_from_options(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        publisher.create_draft(job, media, "文案", options={"auto_upload": False})

        body = session.request.call_args.kwargs["json"]
        assert body["auto_upload"] is False
        assert set(body) == {
            "status",
            "auto_upload",
            "platform",
            "content",
            "media",
            "content_job_id",
        }


class TestStatusGuardIsCaseInsensitive:
    def test_uppercase_public_status_is_refused(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizDraftOnlyError):
            publisher.create_draft(job, media, "文案", options={"status": "PUBLIC"})

        assert session.request.call_count == 0

    def test_mixed_case_draft_status_is_accepted(self, drafting):
        store, job, media = drafting
        session = mock_session()
        publisher = PostizPublisher(settings(), session=session, store=store)

        result = publisher.create_draft(
            job, media, "文案", options={"status": " Draft "}
        )

        assert result["status"] == "draft"
        assert session.request.call_count == 1


class TestOpaqueTokenNeverLeaks:
    """The publisher knows its own secret; it does not rely on a generic pattern."""

    def test_opaque_token_echoed_by_the_server_is_absent_from_the_error(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session(
            401, {"error": f"post rejected: key {OPAQUE_TOKEN} is invalid"}
        )
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert OPAQUE_TOKEN not in str(raised.value)

    def test_a_configured_token_with_whitespace_is_scrubbed_in_both_forms(
        self, drafting
    ):
        """A server may trim the bearer value before echoing it back.

        RFC 7235 lets a server strip the whitespace around the credential, so
        the string quoted back is not the one in the config file. Matching only
        the configured form would miss it and write the secret to disk.
        """
        store, job, media = drafting
        padded = f"  {OPAQUE_TOKEN}  "
        session = mock_session(
            401, {"error": f"post rejected: key {OPAQUE_TOKEN} is invalid"}
        )
        publisher = PostizPublisher(
            settings(api_token=padded), session=session, store=store
        )

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert OPAQUE_TOKEN not in str(raised.value)
        written = "".join(
            path.read_text(encoding="utf-8")
            for path in Path(store.root, job.content_job_id).rglob("*")
            if path.is_file()
        )
        assert OPAQUE_TOKEN not in written

    def test_opaque_token_echoed_by_the_server_never_lands_in_the_job_directory(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session(
            401, {"error": f"post rejected: key {OPAQUE_TOKEN} is invalid"}
        )
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        with pytest.raises(PostizAPIError):
            publisher.create_draft(job, media, "文案")

        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert OPAQUE_TOKEN not in written

    def test_a_credential_that_is_not_ours_is_still_redacted(self, drafting):
        """The generic layer stays: the server may echo somebody else's key.

        Scrubbing our own token cannot cover a credential we have never seen,
        so :func:`~app.services.jobs.budget.redact` still runs behind it.
        """
        store, job, media = drafting
        foreign = "Bearer someone-elses-placeholder-value"
        session = mock_session(400, {"error": f"upstream refused {foreign}"})
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert "someone-elses-placeholder-value" not in str(raised.value)


class TestNonStringDraftIdIsNotAnId:
    """``str()`` on whatever came back would fabricate an id nobody can open."""

    @pytest.mark.parametrize(
        "draft_id", [None, 0, False, ["a"], {"id": "a"}]
    )
    def test_non_string_draft_id_raises_and_leaves_the_job_at_drafting(
        self, drafting, draft_id
    ):
        store, job, media = drafting
        session = mock_session(200, {"draft_id": draft_id})
        publisher = PostizPublisher(settings(), session=session, store=store)

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert "draft_id" in str(raised.value)
        record = store.load(job.content_job_id)
        assert record.job.status is JobStatus.POSTIZ_DRAFTING
        assert [
            event.external_job_id
            for event in record.provider_events
            if event.provider == "postiz"
        ] == [""]



class TestTheServerCanEchoTheTokenIntoAnyFieldWeRead:
    """``status`` and ``draft_id`` are as server-controlled as ``error`` is.

    A server that quotes our own key back in the field we read is either
    confused or hostile; either way the string is untrusted, and the success
    path is not a reason to trust it less carefully than the failure path.
    """

    def test_token_echoed_as_the_status_never_reaches_the_error_or_the_job_files(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session(200, {"draft_id": "D1", "status": OPAQUE_TOKEN})
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        with pytest.raises(PostizAPIError) as raised:
            publisher.create_draft(job, media, "文案")

        assert OPAQUE_TOKEN not in str(raised.value)
        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert OPAQUE_TOKEN not in written

    def test_token_echoed_as_the_draft_id_reaches_neither_the_caller_nor_the_disk(
        self, drafting
    ):
        store, job, media = drafting
        session = mock_session(200, {"draft_id": OPAQUE_TOKEN, "status": "draft"})
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        result = publisher.create_draft(job, media, "文案")

        assert OPAQUE_TOKEN not in result["draft_id"]
        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert OPAQUE_TOKEN not in written


class TestATokenTooShortToScrubIsRefused:
    def test_single_character_token_is_refused(self):
        """``_scrub`` replaces every occurrence, so a 1-char token would eat
        the message it is meant to clean. No real API token is that short."""
        with pytest.raises(ValueError):
            settings(api_token="e").validate()


class TestPostizAPIErrorRedactsItsOwnMessage:
    def test_a_message_built_without_scrub_is_still_redacted(self):
        """The last line of defence, tested on its own contract.

        Every current caller hands :class:`PostizAPIError` an already-scrubbed
        string, so the publisher tests cannot tell this layer apart from
        :meth:`PostizPublisher._scrub`. A future ``raise PostizAPIError(...)``
        that forgets to scrub is exactly what this layer is for.
        """
        error = PostizAPIError("upstream said Bearer someone-elses-placeholder")

        assert "someone-elses-placeholder" not in str(error)


class TestTheFailureRecordIsTheLastGateBeforeDisk:
    def test_an_error_that_was_not_scrubbed_at_its_origin_still_cannot_be_written(
        self, drafting
    ):
        """A boundary contract, not a duplicate of the origin scrubs.

        Today every ``raise`` inside :meth:`create_draft`'s ``try`` scrubs its
        own text, so this layer is a no-op — but that invariant lives in two
        other methods and nothing enforces it. ``_record_failure`` is the last
        code to touch a provider string before it is appended to
        ``decisions.jsonl``, where a leak is permanent, so it is asserted on
        whatever error object it is handed rather than on today's callers.
        """
        store, job, media = drafting
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=mock_session(), store=store
        )

        publisher._record_failure(
            job, {}, PostizError(f"raw upstream text: {OPAQUE_TOKEN}"), "2026-01-01T00:00:00+00:00"
        )

        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert OPAQUE_TOKEN not in written


class TestAForeignCredentialOnTheSuccessPath:
    def test_a_credential_echoed_as_the_draft_id_is_redacted(self, drafting):
        """No exception is raised here, so ``_scrub``'s generic layer is alone.

        The failure path has :class:`PostizAPIError` redacting behind it; the
        success path has nothing else, and a draft id is written to disk and
        handed back to the caller.
        """
        store, job, media = drafting
        session = mock_session(
            200, {"draft_id": "Bearer someone-elses-placeholder", "status": "draft"}
        )
        publisher = PostizPublisher(
            settings(api_token=OPAQUE_TOKEN), session=session, store=store
        )

        result = publisher.create_draft(job, media, "文案")

        assert "someone-elses-placeholder" not in result["draft_id"]
        written = "".join(
            path.read_text(encoding="utf-8")
            for path in tmp_dir_files(store, job.content_job_id)
        )
        assert "someone-elses-placeholder" not in written


# -- the [postiz] config section --------------------------------------------


def config_section(**overrides):
    payload = {
        "base_url": "https://postiz.example.test/api",
        "api_token": POSTIZ_TOKEN,
        "platform": "linkedin",
    }
    payload.update(overrides)
    return payload


class TestSettingsFromConfig:
    """Reading ``[postiz]``: unset is normal, half-set is an error."""

    @pytest.mark.parametrize(
        "section",
        (
            {},
            {"base_url": "", "api_token": "", "platform": ""},
            # A timeout alone still names no endpoint and no credential.
            {"request_timeout_seconds": 30},
        ),
    )
    def test_an_unset_section_is_none_rather_than_an_error(self, section):
        assert settings_from_config(section) is None

    @pytest.mark.parametrize(
        "section,field",
        (
            (config_section(base_url=""), "base_url"),
            (config_section(base_url="http://postiz.example.test"), "base_url"),
            (config_section(base_url="https://u:p@postiz.example.test"), "base_url"),
            (config_section(api_token=""), "api_token"),
            (config_section(api_token="short"), "api_token"),
            (config_section(platform=""), "platform"),
            (config_section(request_timeout_seconds="soon"), "request_timeout_seconds"),
            (config_section(request_timeout_seconds=0), "request_timeout_seconds"),
        ),
    )
    def test_a_malformed_field_is_named_without_its_value(self, section, field):
        with pytest.raises(PostizConfigurationError) as raised:
            settings_from_config(section)

        message = str(raised.value)
        assert field in message
        assert POSTIZ_TOKEN not in message

    def test_a_valid_section_produces_settings_validate_accepts(self):
        result = settings_from_config(
            config_section(request_timeout_seconds="12.5")
        )

        assert result is not None
        result.validate()
        assert result.base_url == "https://postiz.example.test/api"
        assert result.platform == "linkedin"
        # A TOML string survives as a number.
        assert result.request_timeout_seconds == 12.5
        # The token still never reaches a repr.
        assert POSTIZ_TOKEN not in repr(result)

    def test_the_real_config_object_is_the_default_source(self, monkeypatch):
        from app.config import config

        monkeypatch.setattr(config, "postiz", {}, raising=False)
        assert settings_from_config() is None

        monkeypatch.setattr(config, "postiz", config_section(), raising=False)
        result = settings_from_config()

        assert result is not None
        assert result.api_token == POSTIZ_TOKEN


class TestTheRunCommandBindsThePublisherToItsStore:
    """PR #17's property, asserted at the only place that constructs one.

    A publisher bound to another store — or to none — would POST and record
    nothing, and the next run would create a second draft.
    """

    def test_a_configured_section_yields_a_publisher_bound_to_the_run_store(
        self, tmp_path, monkeypatch
    ):
        import cli
        from app.config import config
        from app.services.jobs import runner as runner_module

        monkeypatch.setattr(config, "postiz", config_section(), raising=False)
        store = demo_store(tmp_path)
        seen = {}

        def fake_run_job(job_id, run_store, **kwargs):
            seen["store"] = run_store
            seen["publisher"] = kwargs.get("publisher")
            raise runner_module.RunnerError("stop before doing any work")

        monkeypatch.setattr(runner_module, "run_job", fake_run_job)
        args = cli.parse_args(
            ["run", "--job", "three-scene-demo", "--store", str(store.root)]
        )

        assert cli.run_job_command(args) == 1
        assert isinstance(seen["publisher"], PostizPublisher)
        assert seen["publisher"]._store is seen["store"]

    def test_an_unset_section_passes_no_publisher(self, tmp_path, monkeypatch):
        import cli
        from app.config import config
        from app.services.jobs import runner as runner_module

        monkeypatch.setattr(config, "postiz", {}, raising=False)
        store = demo_store(tmp_path)
        seen = {}

        def fake_run_job(job_id, run_store, **kwargs):
            seen["publisher"] = kwargs.get("publisher")
            raise runner_module.RunnerError("stop before doing any work")

        monkeypatch.setattr(runner_module, "run_job", fake_run_job)
        args = cli.parse_args(
            ["run", "--job", "three-scene-demo", "--store", str(store.root)]
        )

        assert cli.run_job_command(args) == 1
        assert seen["publisher"] is None

    def test_a_misconfigured_section_is_exit_two(self, tmp_path, monkeypatch, capsys):
        import cli
        from app.config import config

        monkeypatch.setattr(
            config, "postiz", config_section(api_token="short"), raising=False
        )
        store = demo_store(tmp_path)
        args = cli.parse_args(
            ["run", "--job", "three-scene-demo", "--store", str(store.root)]
        )

        assert cli.run_job_command(args) == 2
        assert capsys.readouterr().out == ""
