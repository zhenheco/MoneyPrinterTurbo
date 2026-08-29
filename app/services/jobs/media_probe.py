"""What a media file on disk actually is — bytes, type, size, decodability.

The isolation layer for issue #8, in the same shape as
:mod:`app.services.jobs.voice_adapter`: everything the import stage must not
have to know about magic bytes and ffmpeg lives here, and nothing here
persists anything.

Two deliberate deviations from what PLAN-001 row 8 and SPEC-001 §7 name, both
measured on 2026-08-29 against this worktree rather than assumed:

* **Row 8 says 復用 ``video_material.py``. It is not reused.** That helper
  validates the *extension* only — JPEG bytes named ``mislabeled.png`` are
  accepted, and mp4 bytes named ``.png`` are accepted — enforces no numeric
  dimension or duration limit beyond ``> 0``, computes no sha256, needs ffmpeg
  even for a still image, writes into the global ``storage/local_videos/``
  directory and returns a bare basename. The human has already placed the file
  at the manifest's ``import_dir``; routing it through a shared staging
  directory and back would be two non-atomic moves for no validation gain, and
  §7 rules 2–5 would still have to be written from scratch. So they are
  written here instead.

* **§7 rule 5 says ffprobe. This module uses ffmpeg.** ``ffprobe`` is not
  available everywhere this test suite runs: the Windows CI leg gets its
  decoder from the ``imageio-ffmpeg`` wheel, which ships exactly one binary
  (``ffmpeg``) and exposes no ffprobe accessor, and ``app/`` has no
  ``get_ffprobe_binary``. "ffprobe decodable" therefore means, operationally:
  ffmpeg read the whole file to a null muxer and exited 0. That is a stricter
  check than ffprobe's, not a weaker one — ffprobe reads the container header,
  this decodes every frame.

The load-bearing rule, learned in issue #6: **a missing decoder and an
unreadable file must never produce the same answer.** When a decoder is
present and refuses the file, :func:`probe` raises. When there is no decoder at
all, it returns facts with ``decoded=False`` and no dimensions, and the caller
refuses on that — it never falls back to trusting the file.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

from app.utils import utils

#: The two container types the §6.1 generation manifest ever asks for
#: (``scene_planner._MEDIA_SHAPE``), and their magic prefixes. Hand-rolled on
#: purpose: this environment has no MIME library at all — ``magic``,
#: ``filetype`` and ``puremagic`` are all absent, ``imghdr`` is gone in 3.13
#: (and CI runs a 3.13 leg), and ``mimetypes.guess_type`` reads the filename,
#: which is the thing being checked. Two types do not justify a dependency.
#: Extend this and :data:`EXTENSION_MIME_TYPES` together if the manifest ever
#: emits a third.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MP4_FTYP_OFFSET = 4
MP4_FTYP = b"ftyp"
#: ``ftyp`` marks any ISOBMFF file, not an mp4 — a QuickTime ``.mov`` carries
#: it too, and was measured being recorded as ``video/mp4`` (brand ``qt  ``).
#: §7 rule 7 asks for observed provenance, so the major brand is checked as
#: well. These are the brands an mp4 muxer actually writes; a container outside
#: the set is reported as unrecognised rather than guessed at.
MP4_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash"}
)

IMAGE_PNG = "image/png"
VIDEO_MP4 = "video/mp4"

#: Which extension the manifest's ``expected_filename`` must carry for a given
#: sniffed type. §7 rule 2 is a *double* check: the bytes and the name have to
#: agree, so a file that is really a PNG named ``.mp4`` is refused just as
#: firmly as an mp4 named ``.png``.
EXTENSION_MIME_TYPES = {".png": IMAGE_PNG, ".mp4": VIDEO_MP4}

#: §7 rule 3, none of which anything upstream enforces. The render target is
#: 1080x1920 (PLAN-001 row 9), and every value below is stated against it.
#:
#: 200 MiB: a 90 s 1080x1920 H.264 clip at a generous 18 Mbit/s is ~200 MB, and
#: a V0 short is well under 90 s. Bigger than this is a camera master or an
#: intermediate, not a scene deliverable. (``video_material``'s own ceiling is
#: 500 MiB, which is the whole upload batch rather than one scene.)
MAX_ASSET_BYTES = 200 * 1024 * 1024
#: 320 px: scaling that to the 1080 px render width is a 3.4x upscale, which is
#: already visibly soft. Below it the asset cannot carry the frame.
MIN_DIMENSION = 320
#: 7680 px: 8K. Anything wider is a source file; the renderer would decode it
#: at full size before scaling it down to 1080x1920.
MAX_DIMENSION = 7680
#: 500 ms: shorter than the shortest usable cut — a scene that flashes.
MIN_VIDEO_DURATION_MS = 500
#: 120 s: twice the longest V0 target duration. A longer file is the wrong
#: file, not a long scene.
MAX_VIDEO_DURATION_MS = 120_000

#: ffmpeg is given a hard wall so a malformed container cannot hang the stage.
_PROBE_TIMEOUT_SECONDS = 120

#: ``Stream #0:0 ... , 1920x1080 [SAR 1:1 DAR 16:9], ...`` — the first WxH pair
#: on the video stream line. SAR/DAR use ``:`` and cannot match.
_STREAM_PATTERN = re.compile(r"^\s*Stream .*: Video: .*?, (\d+)x(\d+)", re.MULTILINE)
_DURATION_PATTERN = re.compile(r"^\s*Duration: (\d+):(\d\d):(\d\d)\.(\d\d)", re.MULTILINE)


class MediaProbeError(ValueError):
    """The file is not usable media. Carries ``retryable`` like ``VoiceTransportError``."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class MediaFacts:
    """What was measured about one file. Every field is observed, never assumed."""

    bytes: int
    sha256: str
    mime: str
    width: int
    height: int
    duration_ms: Optional[int]
    #: True only when a decoder read the whole file. ``False`` means *this host
    #: has no decoder*, never "the decoder said no" — that case raises.
    decoded: bool


def sniffed_mime(path) -> Optional[str]:
    """The type of the *bytes*, never of the filename. ``None`` if unrecognised."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(16)
    except OSError as error:
        raise MediaProbeError("media file is not readable") from error
    if header.startswith(PNG_MAGIC):
        return IMAGE_PNG
    if (
        header[MP4_FTYP_OFFSET : MP4_FTYP_OFFSET + len(MP4_FTYP)] == MP4_FTYP
        and header[MP4_FTYP_OFFSET + len(MP4_FTYP) : MP4_FTYP_OFFSET + 8] in MP4_BRANDS
    ):
        return VIDEO_MP4
    return None


def decoder_available() -> bool:
    """Is there an ffmpeg on this host at all?

    Same implementation and same reason as ``voice_adapter.decoder_available``:
    ``utils.get_ffmpeg_binary`` falls back to the bare string ``"ffmpeg"`` when
    it finds nothing, so its answer is checked against the filesystem rather
    than taken at face value.
    """
    binary = utils.get_ffmpeg_binary()
    if not binary:
        return False
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def _decode(path) -> Tuple[int, int, Optional[int]]:
    """``(width, height, duration_ms)`` from a full decode, or raise.

    ``-f null -`` reads every frame and writes nothing. ``-xerror`` is what
    makes that a *verdict* rather than a best effort: measured 2026-08-29,
    ffmpeg exits 0 on a faststart mp4 truncated to 60% of its bytes — it
    decodes the 42 frames it can reach, prints ``Invalid NAL unit size`` and
    calls that a success, while ``Duration`` still reads the header's full
    4000 ms. With ``-xerror`` the same file exits 183, and clean media still
    exits 0. Without it, rule 6 (不完整下載) does not hold and the recorded
    ``duration_ms`` is a number nobody's bytes support.
    """
    binary = utils.get_ffmpeg_binary()
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-nostdin", "-xerror", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise MediaProbeError(
            f"the decoder did not finish within {_PROBE_TIMEOUT_SECONDS}s"
        ) from error
    except OSError as error:  # pragma: no cover - decoder_available() ran first
        raise MediaProbeError(f"the decoder could not be run: {error}") from error

    report = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise MediaProbeError(
            "the decoder cannot read this file; it is truncated or is not the "
            "container it claims to be"
        )
    stream = _STREAM_PATTERN.search(report)
    if stream is None:
        raise MediaProbeError("the file decoded but carries no video stream")
    width, height = int(stream.group(1)), int(stream.group(2))

    duration = _DURATION_PATTERN.search(report)
    if duration is None:
        # ``Duration: N/A`` — a single still image. Not an error.
        return width, height, None
    hours, minutes, seconds, centiseconds = (int(part) for part in duration.groups())
    total_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + centiseconds * 10
    return width, height, total_ms


def probe(path) -> MediaFacts:
    """Measure one file: size, checksum, sniffed type, dimensions, duration.

    Raises :class:`MediaProbeError` for a missing, empty, oversized,
    unrecognised or undecodable file. The byte ceiling is the one §7 limit
    enforced here, because it is the one that decides whether the rest of this
    function is worth running; the dimension and duration judgements belong to
    :mod:`app.services.jobs.asset_import`, which knows what the manifest asked
    for.
    """
    if not os.path.isfile(path):
        raise MediaProbeError("media file does not exist")
    size = os.path.getsize(path)
    if size <= 0:
        # §7 rule 6: an empty file is the signature of an interrupted copy.
        raise MediaProbeError("media file is empty")
    if size > MAX_ASSET_BYTES:
        # Checked here, before ``file_sha256`` reads every byte and ffmpeg
        # decodes every frame: a 5 GB drop should cost one ``stat``, not a full
        # read plus the 120 s decoder wall, to be told it is too big.
        raise MediaProbeError(f"{size} bytes exceeds the {MAX_ASSET_BYTES} byte limit")

    mime = sniffed_mime(path)
    if mime is None:
        raise MediaProbeError(
            "media file is neither PNG nor MP4 by its magic bytes, whatever it "
            "is named"
        )

    facts = dict(bytes=size, sha256=file_sha256(path), mime=mime)
    if not decoder_available():
        # No decoder on this host at all. Report that, with no dimensions to
        # hand back — never the ones a header claims, and never ``decoded=True``.
        return MediaFacts(width=0, height=0, duration_ms=None, decoded=False, **facts)
    width, height, duration_ms = _decode(path)
    return MediaFacts(
        width=width, height=height, duration_ms=duration_ms, decoded=True, **facts
    )


def file_sha256(path) -> str:
    """§7 rule 4. Chunked, so an oversized file is still measured honestly
    rather than hashed from a truncated read that would report a checksum for
    bytes nobody has."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MediaProbeError("media file is not readable") from error
    return digest.hexdigest()
