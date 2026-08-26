"""SPEC-001 §6.4: create a Postiz *draft*, and refuse anything that isn't one.

What the spec fixes and what this module chose
----------------------------------------------
§6.4 fixes exactly two things: the return shape

    {"provider": "postiz", "draft_id": "", "status": "draft",
     "platform": "", "scheduled_at": null}

and the rule that「任何 publish_now、auto_upload=true 或公開狀態都必須在 V0 被
拒絕」. It says nothing about Postiz's HTTP surface, and V0 never calls the real
endpoint, so **everything below about the wire format is this module's design
choice, not the spec**: the ``POST {base_url}/posts`` path, the JSON body keys
(``status`` / ``auto_upload`` / ``platform`` / ``content`` / ``media`` /
``content_job_id``), the ``Authorization: Bearer`` scheme, and the
``draft_id``/``status`` keys read back off the response. When the real API is
wired up, only :meth:`PostizPublisher._send` and
:meth:`PostizPublisher._read_draft_id` should
need to change; the guards and the returned contract are spec-level.

Two further design choices worth naming:

* ``scheduled_at`` is always ``None``. Scheduling is a publish decision, and V0
  is draft-only; the key exists because §6.4 lists it.
* the ``draft_id`` is persisted as the provider event's ``external_job_id``
  (§4.6 「外部 job id」). :class:`~app.models.content_job.ContentJob` is
  ``extra="forbid"`` and §4.2 gives it no draft column, so the alternative
  would be inventing a field the data contract does not have.

Why the guards run before the socket
------------------------------------
A refusal that happens after the request has gone out is not a refusal — by
then Postiz may already hold a public post. So every draft-only check is made
against the request this module is *about* to build, and the caller-supplied
``options`` are read only to be checked: they feed the guards and are never
copied into the request body, which is built solely from keys this module
chose. Reading §6.4 literally:
``publish_now`` is refused by its mere presence (「任何 publish_now」), while
``auto_upload`` is refused only when true (「auto_upload=true」), so a caller
explicitly passing ``auto_upload=False`` is agreeing with us and is let through.

Two guards are not in §6.4 and are here because the state machine makes them
necessary. The job must already be in ``POSTIZ_DRAFTING`` (§5.2 has no edge
from anywhere else to ``POSTIZ_DRAFTED``, so a draft created from another state
could never be recorded and would be an orphan on Postiz), and its
``publish_mode`` must be the draft mode (§11 「Postiz 預設拒絕公開發布」).

Credentials
-----------
The token lives in a ``field(repr=False)`` setting, is never formatted into a
message, and every provider-controlled string — the server's own error text,
the ``status`` it reports and the ``draft_id`` it assigns, each of which can
echo the key it was just sent — goes through :meth:`PostizPublisher._scrub`
before it reaches an exception, a job file or the caller. ``_scrub`` strips
this publisher's own token — in both its configured and whitespace-stripped
forms, since a server may trim the bearer value it echoes — before applying
the generic :func:`app.services.jobs.budget.redact`.

Scrubbing happens at the source, where the untrusted string is read, and
again in :meth:`PostizPublisher._record_failure` immediately before anything
reaches ``decisions.jsonl``. That second pass is defence in depth, not a
redundant copy: a leak that reaches disk is permanent, so the last code to
touch a provider string before it is persisted keeps its own guard. Request
and response bodies are
described by :func:`~app.services.jobs.budget.summarize`, which reproduces
neither values nor key names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import requests

from app.models.content_job import ContentJob, JobStatus, ProviderEvent
from app.services.jobs.budget import (
    REDACTED,
    build_idempotency_key,
    redact,
    summarize,
)
from app.services.jobs.state_machine import (
    classify_error,
    decision_record,
    transition,
)

PROVIDER = "postiz"
DRAFT_STATUS = "draft"
DRAFT_PUBLISH_MODE = "postiz_draft"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DRAFT_PATH = "/posts"

#: ``_scrub`` replaces every occurrence of the token, so a token of one or
#: two characters would blank out most of the message it is cleaning. No
#: real API token is that short, so this rejects a typo rather than
#: shipping an unreadable error.
_MIN_TOKEN_LENGTH = 8

#: §5.3: transport trouble and back-pressure earn another attempt; a rejected
#: request does not.
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


class PostizError(RuntimeError):
    """Base error for the Postiz integration.

    ``retryable`` is a class attribute rather than an afterthought so
    :func:`~app.services.jobs.state_machine.classify_error` never has to guess:
    a Postiz failure is non-retryable unless the adapter says otherwise.
    """

    retryable = False


class PostizConfigurationError(ValueError):
    """The settings cannot produce a safe request. Never retryable (§5.3)."""


class PostizDraftOnlyError(PostizError):
    """The request is not a draft, so it was refused before any HTTP call."""


class PostizAPIError(PostizError):
    """Postiz rejected the request, or answered with something unusable."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ):
        super().__init__(redact(message))
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class PostizSettings:
    """Where to talk to Postiz, and with what. The token never reaches ``repr``."""

    base_url: str
    api_token: str = field(repr=False)
    platform: str = ""
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def validate(self) -> None:
        if not str(self.base_url).strip():
            raise PostizConfigurationError("postiz base_url is required")
        token = str(self.api_token).strip()
        if not token:
            raise PostizConfigurationError("postiz api_token is required")
        if len(token) < _MIN_TOKEN_LENGTH:
            raise PostizConfigurationError(
                f"postiz api_token must be at least {_MIN_TOKEN_LENGTH} characters"
            )
        if not str(self.platform).strip():
            raise PostizConfigurationError("postiz platform is required")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise PostizConfigurationError(
                "postiz base_url must be an absolute HTTPS URL"
            )
        if parsed.username or parsed.password:
            raise PostizConfigurationError(
                "postiz base_url must not contain embedded credentials"
            )
        if self.request_timeout_seconds <= 0:
            raise PostizConfigurationError(
                "postiz request_timeout_seconds must be greater than zero"
            )


class PostizPublisher:
    """Create Postiz drafts for a job. Draft-only, enforced locally."""

    def __init__(self, settings: PostizSettings, session=None, store=None):
        settings.validate()
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self._store = store

    def create_draft(
        self,
        job: ContentJob,
        media_path: str,
        caption: str,
        options: Optional[Mapping[str, Any]] = None,
        *,
        now: str = "",
        attempt: int = 1,
    ) -> dict:
        """Create one draft and return the §6.4 shape. Never publishes.

        On success the job moves ``POSTIZ_DRAFTING`` → ``POSTIZ_DRAFTED`` and,
        when a store was injected, that status, the decision line and a §4.6
        provider event carrying the ``draft_id`` are persisted. On failure the
        job stays at ``POSTIZ_DRAFTING`` — the state §5.2 allows a retry from —
        and the failure is recorded instead.
        """
        timestamp = now or _utc_now()
        body = self._draft_request(job, media_path, caption, options, timestamp)

        try:
            payload = self._send(body)
            draft_id = self._read_draft_id(payload)
        except PostizError as exc:
            self._record_failure(job, body, exc, timestamp, attempt)
            raise

        self._record_success(job, body, payload, draft_id, timestamp, attempt)
        return {
            "provider": PROVIDER,
            "draft_id": draft_id,
            "status": DRAFT_STATUS,
            "platform": self.settings.platform,
            # §6.4 lists the key; V0 never schedules, so it is always null.
            "scheduled_at": None,
        }

    # -- request construction: this is where draft-only is enforced --------

    def _draft_request(
        self,
        job: ContentJob,
        media_path: str,
        caption: str,
        options: Optional[Mapping[str, Any]],
        timestamp: str,
    ) -> dict:
        """Build the request body, refusing anything that is not a draft.

        Nothing in here touches the network, and every ``_refuse`` below is
        reached before :meth:`_send` exists as a possibility. ``options`` is an
        input to the guards only — nothing in it is forwarded.
        """
        extra = dict(options or {})

        if "publish_now" in extra:
            self._refuse(
                job,
                "publish_now is not accepted in V0: Postiz is draft-only (§6.4)",
                timestamp,
            )
        if extra.get("auto_upload"):
            self._refuse(
                job,
                "auto_upload=true is not accepted in V0: Postiz is draft-only (§6.4)",
                timestamp,
            )
        requested_status = str(extra.get("status", DRAFT_STATUS)).strip().lower()
        if requested_status != DRAFT_STATUS:
            self._refuse(
                job,
                f"requested post status {requested_status!r} is not "
                f"{DRAFT_STATUS!r}: Postiz is draft-only (§6.4)",
                timestamp,
            )
        if job.publish_mode != DRAFT_PUBLISH_MODE:
            self._refuse(
                job,
                f"job publish_mode is {job.publish_mode!r}, "
                f"only {DRAFT_PUBLISH_MODE!r} may reach Postiz in V0 (§11)",
                timestamp,
            )
        if job.status is not JobStatus.POSTIZ_DRAFTING:
            self._refuse(
                job,
                f"job is in {job.status.value}, not POSTIZ_DRAFTING: a draft "
                "created now could never be recorded (§5.2)",
                timestamp,
            )

        resolved = os.path.realpath(str(media_path))
        if not os.path.isfile(resolved):
            self._refuse(
                job, "media file does not exist, refusing to draft (§11)", timestamp
            )

        # Every key is one this module chose (see the module docstring); none
        # is copied from ``options``. The three guards above only recognise
        # three literal key names, so forwarding the rest would let
        # ``{"type": "now"}`` — the field the real Postiz API reads to pick
        # draft/schedule/now — walk straight past them.
        return {
            "status": DRAFT_STATUS,
            "auto_upload": False,
            "platform": self.settings.platform,
            "content": str(caption),
            "media": [{"path": resolved}],
            "content_job_id": job.content_job_id,
        }

    def _scrub(self, text: str) -> str:
        """Remove *our* token, then anything else credential-shaped.

        :func:`~app.services.jobs.budget.redact` only recognises credentials
        that announce themselves — a scheme word, an adjacent ``key:``, or a
        ``sk-``/``ghp_``/``AKIA`` prefix. An opaque token quoted back by the
        server matches none of those, and this publisher is the one place that
        knows the exact string to look for. Both layers run: ours for our own
        secret, the generic one for whatever else the server echoed.
        """
        result = str(text)
        token = str(self.settings.api_token)
        # Both forms: a server that trims the bearer value (RFC 7235 allows it)
        # echoes back the stripped token, which would not match the configured
        # one if that carried surrounding whitespace.
        for candidate in (token, token.strip()):
            if candidate:
                result = result.replace(candidate, REDACTED)
        return redact(result)

    def _refuse(self, job: ContentJob, reason: str, timestamp: str) -> None:
        message = f"postiz draft-only guard refused the request: {reason}"
        if self._store is not None:
            # from == to: nothing moved, and the line says why the call was
            # never made. A refusal that left no trace is indistinguishable
            # from a call that simply never happened.
            self._store.append_decision(
                job.content_job_id, decision_record(job.status, job, message)
            )
        raise PostizDraftOnlyError(message)

    # -- transport ---------------------------------------------------------

    def _send(self, body: Mapping[str, Any]) -> dict:
        url = f"{self.settings.base_url.rstrip('/')}{DRAFT_PATH}"
        try:
            response = self.session.request(
                "POST",
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.settings.api_token}",
                    "Content-Type": "application/json",
                },
                json=dict(body),
                timeout=(5.0, self.settings.request_timeout_seconds),
            )
        except requests.RequestException as exc:
            raise PostizAPIError(
                f"postiz request failed: {type(exc).__name__}", retryable=True
            ) from exc

        if not 200 <= response.status_code < 300:
            raise PostizAPIError(
                f"postiz returned HTTP {response.status_code}: "
                f"{self._scrub(_server_message(response))}",
                status_code=response.status_code,
                retryable=(
                    response.status_code in _RETRYABLE_STATUS_CODES
                    or response.status_code >= 500
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PostizAPIError("postiz returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PostizAPIError("postiz response must be a JSON object")
        return payload

    # -- response reading --------------------------------------------------

    def _read_draft_id(self, payload: Mapping[str, Any]) -> str:
        """The draft id Postiz assigned, or a loud failure. Never a stand-in.

        A fabricated id would be worse than an error: the job would record a
        draft that nobody can open, and the failure would surface days later at
        review.

        ``status`` and ``draft_id`` come off the same response as ``error`` and
        deserve the same distrust, so both go through :meth:`_scrub` — which is
        why this is a method and not the module-level function it used to be.
        Scrubbing the id at the single point it is read is what makes the
        returned contract, the decision line and the provider event clean; a
        server echoing our key back as an id is broken either way, and a
        ``<redacted>`` id is a visible fault rather than a leak.
        """
        reported = self._scrub(str(payload.get("status", DRAFT_STATUS)).strip().lower())
        if reported != DRAFT_STATUS:
            raise PostizAPIError(
                f"postiz reported status {reported!r}, not {DRAFT_STATUS!r}; "
                "V0 refuses to record anything but a draft"
            )
        # Read the raw value before coercing: ``str(None)`` is ``"None"`` and
        # ``str(0)`` is ``"0"`` — ids nobody can open, recorded as if real.
        raw = payload.get("draft_id")
        if not isinstance(raw, str) or not raw.strip():
            raise PostizAPIError(
                f"postiz response carries no usable draft_id "
                f"(got {type(raw).__name__})"
            )
        return self._scrub(raw.strip())

    # -- persistence -------------------------------------------------------

    def _record_success(
        self,
        job: ContentJob,
        body: Mapping[str, Any],
        payload: Mapping[str, Any],
        draft_id: str,
        timestamp: str,
        attempt: int,
    ) -> None:
        if self._store is None:
            return
        reason = f"postiz draft created: {draft_id}"
        drafted = transition(job, JobStatus.POSTIZ_DRAFTED, reason=reason, now=timestamp)
        # Build the event that holds the draft id *before* the status is
        # persisted: POSTIZ_DRAFTED on disk with no event naming the draft is
        # the orphan this adapter exists to prevent.
        event = self._event(
            job, body, payload, timestamp, attempt, external_job_id=draft_id
        )
        self._store.save(drafted)
        self._store.append_event(job.content_job_id, event)
        self._store.append_decision(
            job.content_job_id, decision_record(job.status, drafted, reason)
        )

    def _record_failure(
        self,
        job: ContentJob,
        body: Mapping[str, Any],
        exc: PostizError,
        timestamp: str,
        attempt: int = 1,
    ) -> None:
        if self._store is None:
            return
        error_class = classify_error(exc)
        reason = self._scrub(f"postiz draft failed ({error_class.value}): {exc}")
        # No transition: POSTIZ_DRAFTING is the state §5.2 allows a retry from,
        # and moving to RETRYABLE_FAILED here would be the caller's decision.
        self._store.append_event(
            job.content_job_id,
            self._event(
                job,
                body,
                None,
                timestamp,
                attempt,
                error_class=error_class.value,
                retryable=error_class.is_retryable,
            ),
        )
        self._store.append_decision(
            job.content_job_id, decision_record(job.status, job, reason)
        )

    def _event(
        self,
        job: ContentJob,
        body: Mapping[str, Any],
        payload: Optional[Mapping[str, Any]],
        timestamp: str,
        attempt: int,
        *,
        external_job_id: str = "",
        error_class: Optional[str] = None,
        retryable: bool = False,
    ) -> ProviderEvent:
        """One §4.6 row for this call. Costs are a real zero, not an unknown:
        creating a draft spends no provider budget."""
        return ProviderEvent(
            provider_event_id=f"postiz-{job.content_job_id}-attempt-{attempt}",
            content_job_id=job.content_job_id,
            scene_id=None,
            provider=PROVIDER,
            model="",
            request_id="",
            # Either "" or an id ``_read_draft_id`` already scrubbed.
            external_job_id=str(external_job_id),
            idempotency_key=build_idempotency_key(
                job.content_job_id, "job", "postiz_draft", attempt
            ),
            attempt_count=attempt,
            estimated_cost_usd=0.0,
            actual_cost_usd=0.0,
            request_summary=summarize("postiz draft request", dict(body)),
            response_summary=(
                summarize("postiz draft response", dict(payload))
                if payload is not None
                else ""
            ),
            error_class=error_class,
            retryable=retryable,
            created_at=timestamp,
            completed_at=timestamp,
        )


# -- response reading ------------------------------------------------------


def _server_message(response) -> str:
    """The server's own error text, redacted — it may quote the rejected key."""
    try:
        payload = response.json()
    except ValueError:
        return "request rejected"
    if isinstance(payload, dict):
        message = str(payload.get("error", "")).strip()
        if message:
            return message
    return "request rejected"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
