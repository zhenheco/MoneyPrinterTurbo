"""Safe ingestion of user-provided video and image materials."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from loguru import logger
from moviepy import ImageClip, VideoFileClip

from app.utils import utils


# Short generated scenes are normally well below this limit, while a 500 MB cap
# still leaves room for high-quality avatar exports without allowing an
# unbounded request to consume the renderer's storage.
MAX_VIDEO_MATERIAL_UPLOAD_BYTES = 500 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
INTERNAL_UPLOAD_PREFIX = ".video-material-upload-"
SUPPORTED_VIDEO_MATERIAL_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


class VideoMaterialUploadError(ValueError):
    """The uploaded material is invalid or exceeds the upload contract."""


class VideoMaterialServiceError(RuntimeError):
    """FFmpeg or storage infrastructure is unavailable."""


def _remove_staged_file(file_path: str) -> None:
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            f"failed to remove staged video material: error={str(exc)}"
        )


def _stage_upload(filename: str, source: BinaryIO) -> tuple[str, str, int]:
    safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or any(ord(character) < 32 for character in safe_name)
    ):
        raise VideoMaterialUploadError("invalid video material filename")
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_VIDEO_MATERIAL_EXTENSIONS:
        supported = ", ".join(
            extension.removeprefix(".").upper()
            for extension in SUPPORTED_VIDEO_MATERIAL_EXTENSIONS
        )
        raise VideoMaterialUploadError(
            f"unsupported video material format; supported formats: {supported}"
        )

    try:
        target_dir = utils.storage_dir("local_videos", create=True)
    except OSError as exc:
        raise VideoMaterialServiceError(
            "failed to prepare video material storage"
        ) from exc
    temp_path = ""
    total_bytes = 0
    try:
        try:
            source.seek(0)
        except (AttributeError, OSError) as exc:
            raise VideoMaterialUploadError("video material upload is not seekable") from exc

        descriptor, temp_path = tempfile.mkstemp(
            prefix=INTERNAL_UPLOAD_PREFIX,
            suffix=suffix,
            dir=target_dir,
        )
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise VideoMaterialUploadError("video material upload must be binary")
                total_bytes += len(chunk)
                if total_bytes > MAX_VIDEO_MATERIAL_UPLOAD_BYTES:
                    raise VideoMaterialUploadError(
                        "video material file exceeds the 500 MB limit"
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total_bytes == 0:
            raise VideoMaterialUploadError("video material file is empty")
        return safe_name, temp_path, total_bytes
    except Exception as exc:
        _remove_staged_file(temp_path)
        if isinstance(exc, VideoMaterialUploadError):
            raise
        if isinstance(exc, OSError):
            raise VideoMaterialServiceError(
                "failed to stage video material upload"
            ) from exc
        raise
    finally:
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass


def _run_ffmpeg_validation(file_path: str, suffix: str) -> None:
    command = [
        utils.get_ffmpeg_binary(),
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        file_path,
        "-map",
        "0:v:0",
    ]
    if suffix in IMAGE_EXTENSIONS:
        command.extend(["-frames:v", "1"])
    command.extend(["-f", "null", "-"])

    try:
        decoded = subprocess.run(
            command,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoMaterialServiceError(
            "FFmpeg video material validation timed out"
        ) from exc
    except OSError as exc:
        raise VideoMaterialServiceError(
            "failed to run FFmpeg for video material validation"
        ) from exc

    if decoded.returncode != 0:
        raise VideoMaterialUploadError(
            "uploaded file must contain a decodable image or video stream"
        )


def _validate_dimensions_and_timing(file_path: str, suffix: str) -> None:
    try:
        if suffix in IMAGE_EXTENSIONS:
            clip = ImageClip(file_path)
        else:
            clip = VideoFileClip(file_path, audio=False)
    except Exception as exc:
        raise VideoMaterialUploadError(
            "uploaded media metadata is invalid"
        ) from exc

    try:
        width, height = clip.size
        if width <= 0 or height <= 0:
            raise VideoMaterialUploadError("uploaded media dimensions are invalid")
        if suffix not in IMAGE_EXTENSIONS:
            duration = float(clip.duration or 0)
            fps = float(clip.fps or 0)
            if duration <= 0 or fps <= 0:
                raise VideoMaterialUploadError(
                    "uploaded video must have positive duration and frame rate"
                )
    except (TypeError, ValueError) as exc:
        raise VideoMaterialUploadError("uploaded media metadata is invalid") from exc
    finally:
        try:
            clip.close()
        except Exception:
            pass


def _validate_media(file_path: str, filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    _run_ffmpeg_validation(file_path, suffix)
    _validate_dimensions_and_timing(file_path, suffix)


def save_video_material_upload(filename: str, source: BinaryIO) -> str:
    """Validate and atomically persist an uploaded local material."""
    safe_name, temp_path, total_bytes = _stage_upload(filename, source)
    target_dir = os.path.dirname(temp_path)
    target_name = safe_name
    target_path = os.path.join(target_dir, target_name)
    if os.path.exists(target_path):
        target_name = (
            f"{Path(safe_name).stem}-{uuid4().hex[:12]}"
            f"{Path(safe_name).suffix}"
        )
        target_path = os.path.join(target_dir, target_name)

    try:
        _validate_media(temp_path, safe_name)
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise VideoMaterialServiceError(
                "failed to persist video material upload"
            ) from exc
        temp_path = ""
        logger.info(
            f"video material uploaded: original_name={safe_name}, "
            f"stored_name={target_name}, size={total_bytes} bytes"
        )
        return target_name
    finally:
        _remove_staged_file(temp_path)
