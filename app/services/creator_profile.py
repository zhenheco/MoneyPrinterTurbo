"""Validation for consent metadata used by creator voice and avatar assets."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_CREATOR_PROFILE_BYTES = 64 * 1024
_ASSET_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_ASSET_TYPES = ("voice", "avatar")
_SENSITIVE_KEYS = {
    "access_token",
    "biometric_material",
    "credential",
    "credentials",
    "file",
    "image_data",
    "path",
    "raw_media",
    "refresh_token",
    "secret",
    "storage_path",
    "token",
    "voice_data",
}


class CreatorProfileError(ValueError):
    """The creator profile is invalid or contains data outside its contract."""


def _text(value: Any, field: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CreatorProfileError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise CreatorProfileError(f"{field} exceeds {max_length} characters")
    return normalized


def _reject_sensitive_keys(value: Any, location: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _SENSITIVE_KEYS:
                raise CreatorProfileError(
                    f"{location}.{normalized_key} is not allowed in creator profile metadata"
                )
            _reject_sensitive_keys(nested_value, f"{location}.{normalized_key}")
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            _reject_sensitive_keys(nested_value, f"{location}[{index}]")


def _validate_expiry(value: Any, field: str, now: datetime) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise CreatorProfileError(f"{field} must be an ISO-8601 string or empty")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CreatorProfileError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CreatorProfileError(f"{field} must include a timezone")
    if parsed.astimezone(timezone.utc) <= now:
        raise CreatorProfileError(f"{field} has expired")
    return value


def _validate_asset_metadata(
    asset_type: str,
    value: Any,
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CreatorProfileError(f"{asset_type} metadata must be an object")

    asset_ref = _text(value.get("asset_ref"), f"{asset_type}.asset_ref", max_length=128)
    if not _ASSET_REF_PATTERN.fullmatch(asset_ref):
        raise CreatorProfileError(
            f"{asset_type}.asset_ref must be an opaque reference without path separators"
        )
    if value.get("consent_status") != "explicit_granted":
        raise CreatorProfileError(
            f"{asset_type}.consent_status must be explicit_granted"
        )
    usage_scope = _text(value.get("usage_scope"), f"{asset_type}.usage_scope")
    source = _text(value.get("source"), f"{asset_type}.source", max_length=128)
    if value.get("manual_review_status") != "approved":
        raise CreatorProfileError(
            f"{asset_type}.manual_review_status must be approved"
        )
    expires_at = _validate_expiry(value.get("expires_at", ""), f"{asset_type}.expires_at", now)
    revoked_at = value.get("revoked_at")
    if revoked_at not in (None, ""):
        raise CreatorProfileError(f"{asset_type}.revoked_at must be empty")

    return {
        "asset_ref": asset_ref,
        "consent_status": "explicit_granted",
        "usage_scope": usage_scope,
        "source": source,
        "expires_at": expires_at,
        "revoked_at": None,
        "manual_review_status": "approved",
    }


def validate_creator_profile(
    profile: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and return a redacted, render-safe creator profile."""
    if not isinstance(profile, Mapping):
        raise CreatorProfileError("creator profile must be a JSON object")
    _reject_sensitive_keys(profile)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    normalized: dict[str, Any] = {
        "creator_profile_id": _text(profile.get("creator_profile_id"), "creator_profile_id", max_length=128),
        "tenant_id": _text(profile.get("tenant_id"), "tenant_id", max_length=128),
        "brand_id": _text(profile.get("brand_id"), "brand_id", max_length=128),
    }
    for asset_type in _PROFILE_ASSET_TYPES:
        normalized[asset_type] = _validate_asset_metadata(
            asset_type,
            profile.get(asset_type),
            current_time,
        )
    return normalized


def load_creator_profile(
    profile_file: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load only consent metadata from a bounded local JSON file."""
    requested_file = (profile_file or "").strip()
    if not requested_file:
        raise CreatorProfileError("creator profile file cannot be empty")
    resolved_file = os.path.realpath(os.path.expanduser(requested_file))
    try:
        file_size = os.path.getsize(resolved_file)
    except OSError as exc:
        raise CreatorProfileError("creator profile file does not exist") from exc
    if file_size > MAX_CREATOR_PROFILE_BYTES:
        raise CreatorProfileError("creator profile file exceeds the 64 KB limit")
    try:
        with Path(resolved_file).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CreatorProfileError("creator profile file must be valid UTF-8 JSON") from exc
    return validate_creator_profile(payload, now=now)
