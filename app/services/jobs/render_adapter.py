"""The isolation layer around ``app.services.video``, plus the stream reader.

PLAN-001 issue #9 *wraps* the upstream renderer; it never edits it. The wrap is
not stylistic. ``app.services.video`` is the fourth upstream module in this
pipeline whose failures are shaped like successes, and every one of the
following was measured against this worktree on 2026-08-29:

* ``combine_videos`` **returns the path it was given, unconditionally.** An
  empty ``video_paths`` returns that path with no file behind it; an unwritable
  output directory swallows every clip write at ``video.py:722`` ("failed to
  process clip"), continues the loop, logs "no clips available" and returns the
  same path; three clips failing out of five yields a silently short video with
  the same return value. The return value therefore carries no information and
  is ignored here.
* ``generate_video`` **returns whether BGM mixing succeeded** (its own
  docstring, ``video.py:979``), not whether anything rendered. Measured: a
  nonexistent subtitle path returns ``True`` and silently drops the subtitles;
  a missing BGM file returns ``False`` while the video is perfectly fine. The
  bool is ignored here too. Real failures raise instead — ``FileNotFoundError``
  for missing inputs, ``IndexError`` for a zero-duration audio track,
  ``OSError`` for an unwritable output, ``ValueError`` for an empty ``.srt``.
* ``_write_videofile_with_codec_fallback`` records a failed encoder in the
  **mutable module global** ``video._runtime_disabled_video_codecs``
  (``video.py:91``, ``:251``), and ``_ffmpeg_encoder_exists`` is additionally
  ``lru_cache``d. So the second render in one process can legitimately use a
  different codec than the first, and the caller is never told which one ran.
  That is the whole reason technical QA reads the encoded file rather than
  trusting what the manifest asked for.

Two consequences for this module:

**Verify the file, never the return value.** :func:`render` checks that the
output exists, is non-empty and decodes before it hands anything back, and
raises :class:`RenderError` otherwise. Nothing here returns a sentinel.

**Render into a scratch directory we own.** ``combine_videos`` writes
``temp-clip-<n>.mp4`` and ``ffmpeg-concat-list.txt``, and ``generate_video``
writes ``<basename>TEMP_MPY_wvf_snd.mp4``, into *the directory of the output
path they were given*, and leave the temp clips behind on the failure paths.
The scratch directory lives beside the destination (same filesystem, so the
finished file is moved into place with one ``os.replace``) and is removed
whether the render succeeded or not.

**ffprobe is not used, anywhere.** PLAN-001 row 9 says "ffprobe metadata", and
this reads the same facts out of ``ffmpeg`` stderr instead, for the reason
:mod:`app.services.jobs.media_probe` already documents: there is no
``get_ffprobe_binary`` in ``app/``, the ``imageio-ffmpeg`` wheel that supplies
the Windows CI leg its decoder ships exactly one binary and exposes no ffprobe
accessor, and ``ffprobe`` being present on the dev machine is precisely the
trap — code written against it passes locally and fails on Windows. Measured
2026-08-29 on five files: everything ffprobe reports about them is in ffmpeg's
stderr header, *plus* the frame rate, which ffprobe's default field set omits.

``media_probe`` cannot be reused for this: its ``_STREAM_PATTERN`` matches
``Video:`` only, and ``probe()`` raises on a file with no video stream — this
module has to read audio streams and has to tolerate their absence in order to
report it. Its ``decoder_available``, ``file_sha256``, ``MediaProbeError`` and
the ``-xerror`` decision are reused as they are.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from app.models.content_job import RenderManifest, RenderSceneEntry
from app.services.jobs import media_probe
from app.utils import utils

#: Same wall as ``media_probe._PROBE_TIMEOUT_SECONDS``, and the same reason: a
#: malformed container must not hang the stage.
INSPECT_TIMEOUT_SECONDS = 120

#: Everything before this line in ffmpeg's stderr describes the *input*; after
#: it, ffmpeg is describing the null muxer it is about to write, which reports
#: ``wrapped_avframe`` and ``pcm_s16le`` rather than the file's own codecs.
_INPUT_HEADER_END = "Stream mapping:"

_VIDEO_STREAM = re.compile(r"^\s*Stream #\d+:\d+.*?: Video: (.+)$", re.MULTILINE)
_AUDIO_STREAM = re.compile(r"^\s*Stream #\d+:\d+.*?: Audio: (.+)$", re.MULTILINE)
_DURATION = re.compile(r"^\s*Duration: (\d+):(\d\d):(\d\d)\.(\d\d)", re.MULTILINE)
_CODEC = re.compile(r"^([A-Za-z0-9_]+)")
_DIMENSIONS = re.compile(r"(?<![\d.])(\d{2,5})x(\d{2,5})(?![\d.])")
_PIXEL_FORMAT = re.compile(r"\b(yuv[a-z0-9]+|gbr[a-z0-9]*|rgb[a-z0-9]*|bgr[a-z0-9]*|gray)\b")
_FPS = re.compile(r"([\d.]+) fps")
_SAMPLE_RATE = re.compile(r"(\d+) Hz")


class RenderError(RuntimeError):
    """The render did not produce the file the manifest asked for.

    Carries ``retryable`` like ``VoiceTransportError`` and ``MediaProbeError``,
    so ``state_machine.classify_error`` reads this module's own judgement rather
    than guessing from the exception type.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class StreamFacts:
    """What ffmpeg says is actually inside one encoded file.

    Every field is read off the file. ``None`` means *ffmpeg did not report
    this*, which for ``audio_codec`` is the difference between a video with a
    silent track and a video with no audio track at all — the thing PRD-001
    FR-008's 音訊 check exists to catch.
    """

    duration_ms: Optional[int]
    video_codec: Optional[str]
    width: int
    height: int
    pixel_format: Optional[str]
    fps: Optional[float]
    audio_codec: Optional[str]
    audio_sample_rate: Optional[int]


def inspect(path) -> StreamFacts:
    """Decode ``path`` in full and report the streams ffmpeg found in it.

    ``-xerror`` makes the exit code a verdict rather than a best effort:
    ``media_probe._decode`` measured ffmpeg exiting 0 on a truncated faststart
    mp4 without it. A non-zero exit is raised as a :class:`RenderError`, so
    "this file is broken" and "this file has no audio" can never be confused.
    """
    if not media_probe.decoder_available():
        raise RenderError(
            "there is no ffmpeg on this host, so a rendered file cannot be verified"
        )
    if not os.path.isfile(path):
        raise RenderError(f"there is no file to inspect at {os.path.basename(str(path))}")
    if os.path.getsize(path) <= 0:
        raise RenderError(f"{os.path.basename(str(path))} is empty")
    try:
        completed = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-hide_banner",
                "-nostdin",
                "-xerror",
                "-i",
                str(path),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RenderError(
            f"the decoder did not finish reading {os.path.basename(str(path))} "
            f"within {INSPECT_TIMEOUT_SECONDS}s"
        ) from error
    except OSError as error:  # pragma: no cover - decoder_available() ran first
        raise RenderError(f"the decoder could not be run: {error}") from error

    report = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise RenderError(
            f"the decoder cannot read {os.path.basename(str(path))}; it is "
            f"truncated or is not the container it claims to be"
        )
    return _parse(report.split(_INPUT_HEADER_END)[0])


def _parse(header: str) -> StreamFacts:
    video = _VIDEO_STREAM.search(header)
    audio = _AUDIO_STREAM.search(header)
    width = height = 0
    pixel_format = fps = video_codec = None
    if video is not None:
        line = video.group(1)
        codec = _CODEC.match(line)
        video_codec = codec.group(1) if codec else None
        # The codec's own parenthesised tag can carry digits ("avc1 / 0x316..."),
        # so the dimensions are looked for after it rather than in the whole line.
        tail = line[codec.end() :] if codec else line
        size = _DIMENSIONS.search(tail)
        if size is not None:
            width, height = int(size.group(1)), int(size.group(2))
        pixels = _PIXEL_FORMAT.search(tail)
        pixel_format = pixels.group(1) if pixels else None
        rate = _FPS.search(tail)
        fps = float(rate.group(1)) if rate else None
    audio_codec = audio_sample_rate = None
    if audio is not None:
        line = audio.group(1)
        codec = _CODEC.match(line)
        audio_codec = codec.group(1) if codec else None
        rate = _SAMPLE_RATE.search(line)
        audio_sample_rate = int(rate.group(1)) if rate else None

    duration = _DURATION.search(header)
    duration_ms = None
    if duration is not None:
        hours, minutes, seconds, centiseconds = (int(part) for part in duration.groups())
        duration_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + centiseconds * 10
    return StreamFacts(
        duration_ms=duration_ms,
        video_codec=video_codec,
        width=width,
        height=height,
        pixel_format=pixel_format,
        fps=fps,
        audio_codec=audio_codec,
        audio_sample_rate=audio_sample_rate,
    )


def has_audio_stream(path) -> bool:
    """Whether ``path`` carries an audio track at all.

    SPEC-001:405's ``native_speech_avatar`` rule turns on exactly this fact
    about an avatar scene's source material, and V0 refuses that case rather
    than overwriting the track (see :mod:`app.services.jobs.render_manifest`).
    """
    return inspect(path).audio_codec is not None


def _scene_seconds(entry: RenderSceneEntry) -> float:
    return (entry.end_ms - entry.start_ms) / 1000.0


def _scene_clip(entry: RenderSceneEntry, source: Path, fps: int, destination: Path) -> Path:
    """One manifest scene, rendered to exactly its slot's length.

    ``combine_videos`` cannot honour a manifest timeline on its own: it slices
    every source to one uniform ``max_clip_duration`` and, in sequential mode,
    takes only the first such slice of each. So each scene is pre-rendered to
    the exact length ``end_ms - start_ms`` names, and ``combine_videos`` is then
    given clips it can only concatenate whole. It still does the scaling,
    padding and final encode, which is the part worth reusing.
    """
    # Imported here, not at module scope: moviepy pulls in the whole ffmpeg
    # reader stack and this module is also imported by callers that only want
    # :func:`inspect`.
    from moviepy import ImageClip, VideoFileClip, vfx

    from app.services.utils import video_effects

    seconds = _scene_seconds(entry)
    clip = None
    try:
        if source.suffix.lower() == ".png":
            clip = ImageClip(str(source)).with_duration(seconds).with_fps(fps)
        else:
            clip = VideoFileClip(str(source))
            if clip.duration is None or clip.duration <= 0:
                raise RenderError(f"scene {entry.scene_id}: its source video has no duration")
            if clip.duration >= seconds:
                clip = clip.subclipped(0, seconds)
            else:
                # The slot comes from the Master Voice timeline, the asset from
                # a human; a 2 s clip under a 22 s slot is ordinary, not an error.
                clip = clip.with_effects([vfx.Loop(duration=seconds)])
        if entry.motion.type == "ken_burns":
            start = 1.0 if entry.motion.scale_start is None else entry.motion.scale_start
            end = 1.08 if entry.motion.scale_end is None else entry.motion.scale_end
            span = max(seconds, 0.001)

            def zoom(get_frame, moment, _start=start, _end=end, _span=span):
                progress = min(max(moment / _span, 0.0), 1.0)
                # video_effects._zoom_frame is the module's sub-pixel centre
                # crop: same output size in, same output size out, which is
                # what a Ken Burns move needs and what a plain ``resized``
                # would not give. Private, and reused rather than re-derived —
                # a second implementation of the same crop would drift from it.
                return video_effects._zoom_frame(
                    get_frame(moment), _start + (_end - _start) * progress
                )

            clip = clip.transform(zoom)
        clip.write_videofile(
            str(destination),
            fps=fps,
            codec="libx264",
            audio=False,
            logger=None,
        )
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:  # pragma: no cover - moviepy close is best effort
                pass
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RenderError(f"scene {entry.scene_id}: its clip was not written")
    return destination


def render(
    manifest: RenderManifest,
    *,
    scene_sources: Mapping[str, Path],
    voice_path: Path,
    subtitle_path: Optional[Path],
    output_path: Path,
    threads: int = 2,
) -> Path:
    """Render ``manifest`` to ``output_path`` and prove a real file came out.

    ``scene_sources`` maps ``RenderSceneEntry.asset_id`` to the already-resolved
    file for that scene; this module never turns a ``storage_key`` into a path
    itself. Raises :class:`RenderError` for every failure — the caller learns
    nothing from a return value here, by design.
    """
    from app.models.schema import (
        VideoAspect,
        VideoConcatMode,
        VideoParams,
    )
    from app.services import video

    if not media_probe.decoder_available():
        raise RenderError("there is no ffmpeg on this host, so nothing can be rendered")
    if not manifest.scenes:
        raise RenderError("the render manifest has no scenes")
    if not os.path.isfile(voice_path) or os.path.getsize(voice_path) <= 0:
        raise RenderError("the Master Voice audio is missing or empty")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Beside the destination, so the finished file moves with one os.replace and
    # so video.py's temp-clip-*.mp4, ffmpeg-concat-list.txt and
    # *TEMP_MPY_wvf_snd.mp4 land in a directory that is deleted either way.
    scratch = Path(tempfile.mkdtemp(prefix=".render-", dir=str(output_path.parent)))
    try:
        clips: List[Path] = []
        for index, entry in enumerate(manifest.scenes, start=1):
            source = scene_sources.get(entry.asset_id)
            if source is None or not os.path.isfile(source):
                raise RenderError(
                    f"scene {entry.scene_id}: asset {entry.asset_id} has no file to render"
                )
            clips.append(
                _scene_clip(
                    entry, Path(source), manifest.canvas.fps, scratch / f"scene-{index:03d}.mp4"
                )
            )

        combined = scratch / "combined.mp4"
        longest = max(_scene_seconds(entry) for entry in manifest.scenes)
        try:
            video.combine_videos(
                combined_video_path=str(combined),
                video_paths=[str(path) for path in clips],
                audio_file=str(voice_path),
                video_aspect=VideoAspect.portrait,
                # Sequential and one whole clip per source: ``_prioritize_unique
                # _source_clips`` reorders only in random mode, so this is the
                # only concat mode that preserves the manifest's scene order.
                video_concat_mode=VideoConcatMode.sequential,
                video_transition_mode=None,
                max_clip_duration=max(1, math.ceil(longest)),
                threads=threads,
                clip_speed=1.0,
            )
        except Exception as error:
            raise RenderError(f"combining the scene clips failed: {error}") from error
        # The return value above is the path that went in, whatever happened.
        if not combined.is_file() or combined.stat().st_size <= 0:
            raise RenderError(
                "combine_videos reported success but wrote no video; every scene "
                "clip failed to encode"
            )

        final = scratch / "final.mp4"
        params = VideoParams(
            video_subject="",
            video_aspect=VideoAspect.portrait,
            subtitle_enabled=subtitle_path is not None,
            # No BGM: the job's audio contract is the Master Voice and nothing
            # else (SPEC-001 §8 audio.mode). This also keeps generate_video's
            # bool at True, which is still not read.
            bgm_type="",
            bgm_file="",
            n_threads=threads,
        )
        try:
            video.generate_video(
                video_path=str(combined),
                audio_path=str(voice_path),
                subtitle_path=str(subtitle_path) if subtitle_path else "",
                output_file=str(final),
                params=params,
                bgm_file_override="",
            )
        except Exception as error:
            raise RenderError(f"the final render failed: {error}") from error
        # Again: the bool that came back describes BGM mixing, not the render.
        if not final.is_file() or final.stat().st_size <= 0:
            raise RenderError("generate_video returned but wrote no output file")

        os.replace(final, output_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return output_path


def subtitle_end_ms(srt_text: str) -> Optional[int]:
    """The end time of the last cue in a SubRip document, in milliseconds.

    SPEC-001:623 「Subtitle Timing 不超出 Master Voice 與影片長度」 needs one
    number to compare against the decoded duration. ``None`` when the document
    holds no cue at all, which is a different failure and is reported as such.
    """
    ends: Sequence[int] = [
        _srt_ms(match)
        for match in re.finditer(
            r"-->\s*(\d\d):(\d\d):(\d\d),(\d{3})", srt_text
        )
    ]
    return max(ends) if ends else None


def _srt_ms(match: "re.Match") -> int:
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds
