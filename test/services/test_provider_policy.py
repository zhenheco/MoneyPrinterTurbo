from app.services.provider_policy import ProviderCandidate, select_provider


def _candidate(**values):
    return ProviderCandidate(
        fallback_policy="no_silent_token_fallback",
        **values,
    )


def test_oauth_cli_is_assisted_only_for_automated_jobs():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_cli",
                auth_mode="oauth_cli",
                execution_mode="automated",
                capability_status="ready",
            )
        ],
        execution_mode="automated",
    )

    assert decision.candidate is None
    assert decision.status == "ASSISTED_ONLY"


def test_priority_is_applied_within_the_requested_execution_mode():
    decision = select_provider(
        [
            _candidate(
                provider="xai_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="ready",
            ),
            _candidate(
                provider="gemini_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="ready",
            ),
        ],
        execution_mode="automated",
    )

    assert decision.status == "AUTOMATED_READY"
    assert decision.candidate is not None
    assert decision.candidate.provider == "gemini_api"


def test_fallback_stays_within_automated_mode():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="manual_reauth_required",
            ),
            _candidate(
                provider="grok_build",
                auth_mode="oauth_cli",
                execution_mode="assisted",
                capability_status="ready",
            ),
            _candidate(
                provider="xai_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="ready",
            ),
        ],
        execution_mode="automated",
    )

    assert decision.status == "AUTOMATED_READY"
    assert decision.candidate is not None
    assert decision.candidate.provider == "xai_api"


def test_assisted_candidate_does_not_become_automated_fallback():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_cli",
                auth_mode="oauth_cli",
                execution_mode="assisted",
                capability_status="ready",
            )
        ],
        execution_mode="automated",
    )

    assert decision.candidate is None
    assert decision.status == "ASSISTED_ONLY"


def test_assisted_success_has_an_explicit_status():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_cli",
                auth_mode="oauth_cli",
                execution_mode="assisted",
                capability_status="ready",
            )
        ],
        execution_mode="assisted",
    )

    assert decision.status == "ASSISTED_READY"
    assert decision.candidate is not None
    assert decision.candidate.provider == "gemini_cli"


def test_qwen_code_interactive_token_plan_is_assisted():
    decision = select_provider(
        [
            _candidate(
                provider="qwen_code_plan",
                auth_mode="interactive_subscription",
                execution_mode="assisted",
                capability_status="ready",
            )
        ],
        execution_mode="assisted",
    )

    assert decision.status == "ASSISTED_READY"
    assert decision.candidate is not None
    assert decision.candidate.provider == "qwen_code_plan"


def test_qwen_oauth_is_not_selectable():
    decision = select_provider(
        [
            _candidate(
                provider="qwen_code_plan",
                auth_mode="qwen_oauth",
                execution_mode="assisted",
                capability_status="ready",
            )
        ],
        execution_mode="assisted",
    )

    assert decision.candidate is None
    assert decision.status == "PROVIDER_UNAVAILABLE"


def test_qwen_oauth_manual_reauth_is_not_a_manual_action():
    decision = select_provider(
        [
            _candidate(
                provider="qwen_code_plan",
                auth_mode="qwen_oauth",
                execution_mode="assisted",
                capability_status="manual_reauth_required",
            )
        ],
        execution_mode="assisted",
    )

    assert decision.candidate is None
    assert decision.status == "PROVIDER_UNAVAILABLE"


def test_gemini_veo_alias_is_recorded_as_gemini_api():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_veo_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="ready",
            )
        ],
        execution_mode="automated",
    )

    assert decision.status == "AUTOMATED_READY"
    assert decision.candidate is not None
    assert decision.candidate.provider == "gemini_api"


def test_unknown_auth_mode_fails_closed():
    decision = select_provider(
        [
            _candidate(
                provider="gemini_api",
                auth_mode="oauth_unknown",
                execution_mode="automated",
                capability_status="ready",
            )
        ],
        execution_mode="automated",
    )

    assert decision.candidate is None
    assert decision.status == "PROVIDER_UNAVAILABLE"


def test_invalid_fallback_policy_fails_closed():
    decision = select_provider(
        [
            ProviderCandidate(
                provider="gemini_api",
                auth_mode="api_key",
                execution_mode="automated",
                capability_status="ready",
                fallback_policy="allow_silent_token_fallback",
            )
        ],
        execution_mode="automated",
    )

    assert decision.candidate is None
    assert decision.status == "PROVIDER_UNAVAILABLE"
