# Do not call app.services.llm.generate_script here: llm.py:538-539 greedily
# removes bracketed JSON arrays, and its retry loop can spend beyond the jobs
# budget gate. This adapter is the isolation layer for upstream LLM signatures.
"""Single-call structured JSON adapter for the jobs pipeline."""

from __future__ import annotations

from typing import Any, Mapping

from app.services import llm


class LlmTransportError(RuntimeError):
    """The upstream LLM call failed before returning a usable payload."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


_NON_RETRYABLE_ERROR_MARKERS = (
    "api_key is not set",
    "401",
    "403",
    "unauthorized",
    "invalid api key",
)


def _build_prompt(topic: str, language: str, repair_prompt: str) -> str:
    prompt = f"""Generate a video script about this topic:
{topic}

Language: {language}

Return exactly one JSON object and no markdown or surrounding text. The object
must contain exactly these fields:
- title: string
- target_audience: string
- core_message: string
- hook: string
- body: array of strings
- conclusion: string
- cta: string
- claims: array of strings
- sources: array of strings
- risk_flags: array of strings
""".strip()
    if repair_prompt:
        prompt += f"\n\nRepair requirements:\n{repair_prompt}"
    return prompt


def generate_script(
    *,
    topic: str,
    language: str,
    repair_prompt: str = "",
    app_config: Mapping[str, Any] | None = None,
) -> str:
    """Make exactly one provider call and return its untouched JSON text."""
    response = llm._generate_response(
        prompt=_build_prompt(topic, language, repair_prompt),
        app_config=app_config,
    )
    if not response or not response.strip():
        raise LlmTransportError("LLM returned an empty response")
    if response.startswith("Error: "):
        message = response.removeprefix("Error: ").strip()
        normalized_message = message.casefold()
        retryable = not any(
            marker in normalized_message for marker in _NON_RETRYABLE_ERROR_MARKERS
        )
        raise LlmTransportError(
            message or "LLM transport failed", retryable=retryable
        )
    return response
