"""Product-level provider selection policy.

The policy deliberately contains provider metadata only. It does not load
credentials or turn an interactive CLI session into an API client.
"""

from dataclasses import dataclass, replace
from typing import Iterable


ASSISTED_PRIORITY = (
    "gemini_cli",
    "manual_google_flow",
    "grok_build",
    "qwen_code_plan",
    "manual_import",
)

AUTOMATED_PRIORITY = (
    "gemini_api",
    "vertex_ai",
    "xai_api",
    "modelstudio_api",
    "modelstudio_token_plan",
)

PROVIDER_ALIASES = {
    "gemini_veo_api": "gemini_api",
    "qwen_assisted": "qwen_code_plan",
}

CANONICAL_AUTH_MODES = {
    "gemini_cli": "oauth_cli",
    "manual_google_flow": "manual_import",
    "grok_build": "oauth_cli",
    "qwen_code_plan": "interactive_subscription",
    "gemini_api": "api_key",
    "vertex_ai": "vertex",
    "xai_api": "api_key",
    "modelstudio_api": "api_key",
    "modelstudio_token_plan": "modelstudio_token_plan",
    "manual_import": "manual_import",
    "qwen_oauth": "qwen_oauth",
}

READY_STATUSES = frozenset({"ready", "assisted_ready", "automated_ready"})
MANUAL_STATUSES = frozenset({"manual_action_required", "manual_reauth_required"})
ASSISTED_ONLY_AUTH_MODES = frozenset(
    {"oauth_cli", "interactive_subscription", "agent_session", "manual_import"}
)

AUTOMATED_READY = "AUTOMATED_READY"
ASSISTED_READY = "ASSISTED_READY"
ASSISTED_ONLY = "ASSISTED_ONLY"
MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """Non-secret capability metadata used by the product selector."""

    provider: str
    auth_mode: str
    execution_mode: str
    capability_status: str
    fallback_policy: str
    model: str = ""


@dataclass(frozen=True, slots=True)
class ProviderDecision:
    """The result of selecting a provider without exposing credentials."""

    status: str
    candidate: ProviderCandidate | None = None


def select_provider(
    candidates: Iterable[ProviderCandidate], execution_mode: str
) -> ProviderDecision:
    """Select the highest-priority ready candidate for one execution mode.

    Fallback is intentionally scoped to the requested mode. Interactive OAuth
    and subscription sessions can never satisfy an automated selection, and
    the retired Qwen OAuth mode is never selected in either mode.
    """

    if execution_mode not in {"assisted", "automated"}:
        raise ValueError(f"unsupported execution mode: {execution_mode}")

    candidates = tuple(_canonical_candidate(candidate) for candidate in candidates)
    priority = ASSISTED_PRIORITY if execution_mode == "assisted" else AUTOMATED_PRIORITY

    for provider in priority:
        for canonical_candidate in candidates:
            if canonical_candidate.provider != provider:
                continue
            if canonical_candidate.execution_mode != execution_mode:
                continue
            if not _is_allowed(canonical_candidate, execution_mode):
                continue
            if canonical_candidate.capability_status in READY_STATUSES:
                status = (
                    ASSISTED_READY if execution_mode == "assisted" else AUTOMATED_READY
                )
                return ProviderDecision(status=status, candidate=canonical_candidate)

    if _has_assisted_only_candidate(candidates, execution_mode):
        return ProviderDecision(status=ASSISTED_ONLY)

    if any(
        candidate.execution_mode == execution_mode
        and candidate.auth_mode != "qwen_oauth"
        and candidate.capability_status in MANUAL_STATUSES
        for candidate in candidates
    ):
        return ProviderDecision(status=MANUAL_ACTION_REQUIRED)

    return ProviderDecision(status=PROVIDER_UNAVAILABLE)


def _is_allowed(candidate: ProviderCandidate, execution_mode: str) -> bool:
    if candidate.fallback_policy != "no_silent_token_fallback":
        return False
    if candidate.auth_mode == "qwen_oauth":
        return False
    expected_auth_mode = CANONICAL_AUTH_MODES.get(candidate.provider)
    if expected_auth_mode and candidate.auth_mode != expected_auth_mode:
        return False
    return not (
        execution_mode == "automated"
        and candidate.auth_mode in ASSISTED_ONLY_AUTH_MODES
    )


def _has_assisted_only_candidate(
    candidates: tuple[ProviderCandidate, ...], execution_mode: str
) -> bool:
    if execution_mode != "automated":
        return False

    return any(
        candidate.capability_status in READY_STATUSES
        and candidate.fallback_policy == "no_silent_token_fallback"
        and candidate.provider in ASSISTED_PRIORITY
        and candidate.auth_mode in ASSISTED_ONLY_AUTH_MODES
        for candidate in candidates
    )


def _canonical_candidate(candidate: ProviderCandidate) -> ProviderCandidate:
    provider = PROVIDER_ALIASES.get(candidate.provider, candidate.provider)
    if provider == candidate.provider:
        return candidate
    return replace(candidate, provider=provider)
