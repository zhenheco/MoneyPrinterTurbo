"""Single-call TTS adapter for the jobs pipeline.

Do not call :func:`app.services.voice.tts` from a stage. Measured on
2026-08-28 against this worktree:

* It **never raises for a provider failure** — an absent credential, an invalid
  credential and a blocked network all return ``None`` (``voice.py:850`` and
  the equivalent branch in every other provider). A caller that treats the
  return as merely optional writes a zero-byte artifact and advances the job as
  if synthesis had worked, which is exactly what SPEC-001 §7 item 6
  (「確認素材不是空檔或不完整下載」) forbids.
* It carries **two incompatible timeline shapes**. The Edge path fills
  ``SubMaker.cues`` with ``datetime.timedelta``; every other path fills the
  legacy ``subs``/``offset`` pair in **100-nanosecond ticks**. Measured: a 3.0 s
  synthesis produced ``offset == [(0, 15000000), (15000000, 30000000)]``, i.e.
  10,000,000 ticks per second. Dividing by 1000 instead of 10,000 records a
  three-second voice as thirty thousand seconds.
* Its ``voice_file`` argument is the **third** positional for some providers and
  the **fourth** for others, so a positional call silently swaps the output path
  with the rate. Everything here calls by keyword.
* It retries **three times internally** for every provider but Gemini, so one
  call through this adapter authorises up to three real provider requests. The
  cost ceiling in :mod:`app.services.jobs.master_voice` is sized for that.

This module is the isolation layer, in the same shape as
:mod:`app.services.jobs.llm_adapter`. It does not persist anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

from app.services import voice

#: ``SubMaker.offset`` is in 100-nanosecond ticks. Measured, not assumed.
TICKS_PER_SECOND = 10_000_000
TICKS_PER_MS = TICKS_PER_SECOND // 1000

#: How far the timeline may disagree with the decoded audio before the take is
#: rejected. SiliconFlow fabricates a flat one-second timeline when it cannot
#: decode what it downloaded (``voice.py:935-947``), so a timeline that does not
#: describe the audio is the signature of a truncated download, not a rounding
#: difference.
_DURATION_TOLERANCE = 0.25


class VoiceTransportError(RuntimeError):
    """The upstream TTS call failed before returning a usable take."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class VoiceSegment:
    """One spoken span, in integer milliseconds from the start of the audio."""

    index: int
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class VoiceTake:
    """One synthesised master voice: the file on disk plus its timeline."""

    audio_path: str
    bytes: int
    duration_ms: int
    #: ``"measured"`` when the duration came from decoding the audio,
    #: ``"timeline"`` when no decoder was available and the provider's own
    #: timeline had to be taken at its word. Downstream stages need to know
    #: which, because only the first proves the artifact is really there.
    duration_source: str
    segments: Tuple[VoiceSegment, ...]


def _segments_from_cues(cues: Sequence[Any]) -> List[VoiceSegment]:
    """The Edge path: ``cues[].start``/``.end`` are ``datetime.timedelta``."""
    segments: List[VoiceSegment] = []
    for index, cue in enumerate(cues, start=1):
        text = str(getattr(cue, "content", "") or "").strip()
        if not text:
            continue
        segments.append(
            VoiceSegment(
                index=len(segments) + 1,
                text=text,
                start_ms=int(cue.start.total_seconds() * 1000),
                end_ms=int(cue.end.total_seconds() * 1000),
            )
        )
    return segments


def _segments_from_legacy(subs: Sequence[Any], offset: Sequence[Any]) -> List[VoiceSegment]:
    """Every non-Edge path: ``offset`` holds 100-nanosecond tick pairs."""
    segments: List[VoiceSegment] = []
    for text, span in zip(subs, offset):
        cleaned = str(text or "").strip()
        if not cleaned or not isinstance(span, (tuple, list)) or len(span) != 2:
            continue
        start_ticks, end_ticks = span
        segments.append(
            VoiceSegment(
                index=len(segments) + 1,
                text=cleaned,
                start_ms=int(start_ticks) // TICKS_PER_MS,
                end_ms=int(end_ticks) // TICKS_PER_MS,
            )
        )
    return segments


def timeline_segments(sub_maker: Any) -> List[VoiceSegment]:
    """Normalise whichever timeline shape ``sub_maker`` carries into integer ms.

    The Edge cues are preferred when present: they are real word boundaries
    reported by the service, whereas the legacy pair is usually apportioned
    from the text by the repository itself.
    """
    cues = getattr(sub_maker, "cues", None) or []
    segments = _segments_from_cues(cues)
    if segments:
        return segments
    return _segments_from_legacy(
        getattr(sub_maker, "subs", None) or [],
        getattr(sub_maker, "offset", None) or [],
    )


def _measure(audio_path: str) -> float:
    """Decoded duration in seconds, or ``0.0`` when nothing could read it.

    ``voice.get_audio_duration`` never raises: a missing file, an undecodable
    file and a host with no ffmpeg all come back as ``0.0``.
    """
    try:
        return float(voice.get_audio_duration(audio_path))
    except Exception:  # pragma: no cover - the helper is documented not to raise
        return 0.0


def synthesize(
    *,
    text: str,
    voice_name: str,
    voice_file: str,
    voice_rate: float = 1.0,
) -> VoiceTake:
    """Make one ``voice.tts`` call and return a take proven to be non-empty.

    Raises :class:`VoiceTransportError` rather than ever handing back an empty
    or unbelievable artifact. The checks are the point of this function:

    * ``tts`` returned ``None`` — the only failure signal it has.
    * The file is missing or zero bytes. The default Edge path gates success on
      a non-empty subtitle stream and never checks that an audio chunk arrived
      (``voice.py:802``), so a boundary-only stream yields a valid-looking
      ``SubMaker`` over a zero-byte MP3.
    * The timeline is empty, or ends at zero.
    * The timeline and the decoded audio disagree by more than
      :data:`_DURATION_TOLERANCE`, when the audio could be decoded at all.
    """
    if not isinstance(text, str) or not text.strip():
        # azure_tts_v1 and siliconflow_tts call ``text.strip()`` outside their
        # retry blocks, so a None or empty text is an AttributeError from deep
        # inside the provider rather than a clean refusal.
        raise VoiceTransportError(
            "master voice needs non-empty narration", retryable=False
        )
    if not isinstance(voice_name, str) or not voice_name.strip():
        raise VoiceTransportError("voice_name must be a non-empty string", retryable=False)

    try:
        sub_maker = voice.tts(
            text=text,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=voice_file,
        )
    except FileNotFoundError as error:
        # The no-voice branch shells out to ffmpeg with no guard, and
        # ``get_ffmpeg_binary`` falls back to the bare string "ffmpeg".
        raise VoiceTransportError(
            f"tts could not run its encoder: {error}", retryable=False
        ) from error
    except Exception as error:  # noqa: BLE001 - the boundary is the point
        raise VoiceTransportError(f"tts raised {type(error).__name__}: {error}") from error

    if sub_maker is None:
        raise VoiceTransportError(
            f"tts returned no take for voice {voice_name!r}; "
            "the provider reports every failure this way"
        )

    if not os.path.isfile(voice_file):
        raise VoiceTransportError("tts reported success but wrote no audio file")
    size = os.path.getsize(voice_file)
    if size <= 0:
        raise VoiceTransportError("tts wrote a zero-byte audio file")

    segments = timeline_segments(sub_maker)
    if not segments:
        raise VoiceTransportError("tts returned a take with no timeline")
    timeline_ms = max(segment.end_ms for segment in segments)
    if timeline_ms <= 0:
        raise VoiceTransportError("tts returned a timeline that ends at zero")

    measured_seconds = _measure(voice_file)
    if measured_seconds > 0:
        measured_ms = int(measured_seconds * 1000)
        drift = abs(measured_ms - timeline_ms) / max(measured_ms, timeline_ms)
        if drift > _DURATION_TOLERANCE:
            raise VoiceTransportError(
                f"timeline says {timeline_ms} ms but the audio decodes to "
                f"{measured_ms} ms; the take does not describe its own audio"
            )
        duration_ms, duration_source = measured_ms, "measured"
    else:
        duration_ms, duration_source = timeline_ms, "timeline"

    return VoiceTake(
        audio_path=voice_file,
        bytes=size,
        duration_ms=duration_ms,
        duration_source=duration_source,
        segments=tuple(segments),
    )


def resolve_identity(voice_name: str) -> Tuple[str, str]:
    """``(provider_id, model)`` for the ProviderEvent and the AssetRecord."""
    name = str(voice_name or "")
    if voice.is_no_voice(name):
        return "no_voice", name
    for prefix in ("siliconflow", "gemini", "mimo", "minimax", "elevenlabs", "chatterbox"):
        if name.lower().startswith(f"{prefix}:"):
            return prefix, name
    if voice.is_azure_v2_voice(name):
        return "azure_tts_v2", name
    return "edge_tts", name


#: Providers that cost nothing per call. Recording a placeholder ceiling for
#: these would spend a job's budget on something that is free and make
#: ``job.actual_cost_usd`` wrong in a direction §10 cares about just as much as
#: a fabricated zero: an unknown must never become 0, but a *known* zero must
#: not become an invented non-zero either.
FREE_PROVIDERS = frozenset({"edge_tts", "no_voice"})


def is_free(provider_id: str) -> bool:
    return provider_id in FREE_PROVIDERS


#: What the V0 default path actually writes. edge-tts streams MP3 and the
#: repository stores those bytes verbatim, with no transcode.
#:
#: This is a single pair rather than a per-provider table on purpose: only
#: ``mimo_tts`` honours the requested extension, and MiniMax can be configured
#: to return WAV, so a table keyed on provider would still be wrong for a
#: configured format. Naming the file after the bytes actually received needs
#: container sniffing, which is a decision issue #8 (Asset Import) owns — see
#: the handoff.
AUDIO_EXTENSION = ".mp3"
AUDIO_MIME_TYPE = "audio/mpeg"
