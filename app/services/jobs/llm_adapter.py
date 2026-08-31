# Do not call app.services.llm.generate_script here: llm.py:538-539 greedily
# removes bracketed JSON arrays, and its retry loop can spend beyond the jobs
# budget gate. This adapter is the isolation layer for upstream LLM signatures.
"""Single-call structured JSON adapter for the jobs pipeline.

The duration budget is a prompt constraint only. Deliberately no enforcement
and no length-repair retry: the repair loop exists for schema failures, and
rejecting a schema-valid script for being long would spend a second paid call
on every generation and could loop. Whether the prompt alone is enough is a
question for the next real trial, not something to pre-empt with machinery.
"""

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


# 4.8 characters per second is measured from real Edge TTS zh-TW narration
# (4.9 / 4.85 / 4.51 across three full 150-second trial videos). No other
# language was measured, so non-Chinese jobs get the seconds and no
# per-language character rate — an invented number would be worse than none.
#
# Those trials ran on zh-TW-HsiaoChenNeural-Female at rate 1.0 and
# ``master_voice`` now ships zh-TW-YunJheNeural-Male at 0.9, so it was
# re-measured 2026-08-31 on a 194-character narration through real Edge TTS,
# all four combinations:
#
#     HsiaoChen 1.0  43.24s  4.49 chars/sec     YunJhe 1.0  39.17s  4.95
#     HsiaoChen 0.9  48.04s  4.04               YunJhe 0.9  43.52s  4.46
#
# YunJhe is naturally about 10% faster than HsiaoChen, and 0.9 gives almost
# exactly that back: the shipped pair runs within 0.7% of the baseline this
# constant was measured on, so 4.8 still holds. Change the voice or the rate
# and re-measure on a full-length narration — the same pair measured ~96% on a
# short clip, which was leading-and-trailing silence, not signal.
_ZH_CHARS_PER_SECOND = 4.8

#: The narration floor SPEC-001 §4.4 implies. Scene planning must produce 8
#: scenes, and ``scene_planner`` only cuts a unit that leaves both halves at
#: least ``_MIN_UNIT_CHARS`` (6) long, so 8 x 12 characters is the arithmetic
#: minimum. Measured: 72 characters plans into 8 scenes, 66 raises
#: ``ScenePlanError``. A script that undershoots its own budget therefore parks
#: the job at ``MANUAL_ACTION_REQUIRED``, and §5.2 makes ``SCENE_PLANNING`` no
#: return target — so the prompt states this floor as well as the target.
MIN_NARRATION_CHARS = 8 * 12


def _duration_constraint(target_duration_sec: int, language: str) -> str:
    constraint = (
        f"Length: the spoken narration must fit {target_duration_sec} seconds. "
        "Only hook, body, conclusion and cta are read aloud, so this budget "
        "applies to those four fields together. Do not pad title, "
        "target_audience, core_message, claims, sources or risk_flags to "
        "compensate — they are never spoken."
    )
    if language.casefold().startswith("zh"):
        budget = round(target_duration_sec * _ZH_CHARS_PER_SECOND)
        constraint += (
            f" At about {_ZH_CHARS_PER_SECOND} characters per second that is "
            f"roughly {budget} characters in total across those four fields, "
            f"and never fewer than {MIN_NARRATION_CHARS} characters — a shorter "
            "script cannot be split into the eight scenes this pipeline requires."
        )
    return constraint


def _build_prompt(
    topic: str, language: str, repair_prompt: str, target_duration_sec: int
) -> str:
    prompt = f"""Generate a video script about this topic:
{topic}

Language: {language}

{_duration_constraint(target_duration_sec, language)}

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
    target_duration_sec: int,
    repair_prompt: str = "",
    app_config: Mapping[str, Any] | None = None,
) -> str:
    """Make exactly one provider call and return its untouched JSON text."""
    response = llm._generate_response(
        prompt=_build_prompt(topic, language, repair_prompt, target_duration_sec),
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
