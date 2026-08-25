import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import creator_profile


def valid_profile():
    return {
        "creator_profile_id": "creator-001",
        "tenant_id": "zhenhe",
        "brand_id": "zhenhe-ai",
        "voice": {
            "asset_ref": "asset-voice-001",
            "consent_status": "explicit_granted",
            "usage_scope": "zhenhe-ai V0 short videos",
            "source": "user_recording",
            "expires_at": "",
            "revoked_at": None,
            "manual_review_status": "approved",
        },
        "avatar": {
            "asset_ref": "asset-avatar-001",
            "consent_status": "explicit_granted",
            "usage_scope": "zhenhe-ai V0 short videos",
            "source": "user_provided_still",
            "expires_at": "",
            "revoked_at": None,
            "manual_review_status": "approved",
        },
    }


class TestCreatorProfile(unittest.TestCase):
    def test_valid_profile_returns_safe_metadata(self):
        profile = valid_profile()

        normalized = creator_profile.validate_creator_profile(profile)

        self.assertEqual(normalized["creator_profile_id"], "creator-001")
        self.assertEqual(normalized["voice"]["asset_ref"], "asset-voice-001")
        self.assertEqual(normalized["avatar"]["manual_review_status"], "approved")
        self.assertNotIn("raw_media", normalized)

    def test_missing_explicit_consent_is_rejected(self):
        profile = valid_profile()
        profile["voice"]["consent_status"] = "pending"

        with self.assertRaises(creator_profile.CreatorProfileError) as raised:
            creator_profile.validate_creator_profile(profile)

        self.assertIn("explicit_granted", str(raised.exception))

    def test_expired_revoked_or_unreviewed_asset_is_rejected(self):
        for field, value in (
            ("expires_at", "2020-01-01T00:00:00+00:00"),
            ("revoked_at", "2026-08-01T00:00:00+00:00"),
            ("manual_review_status", "pending"),
        ):
            with self.subTest(field=field):
                profile = valid_profile()
                profile["avatar"][field] = value

                with self.assertRaises(creator_profile.CreatorProfileError):
                    creator_profile.validate_creator_profile(profile)

    def test_sensitive_payload_and_path_like_asset_ref_are_rejected(self):
        profile = valid_profile()
        profile["voice"]["asset_ref"] = "../../voice.wav"
        profile["avatar"]["raw_media"] = "base64-data-must-not-be-here"

        with self.assertRaises(creator_profile.CreatorProfileError):
            creator_profile.validate_creator_profile(profile)

    def test_load_profile_reads_json_without_persisting_media(self):
        profile = valid_profile()
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_path = Path(temp_dir, "creator-profile.json")
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            loaded = creator_profile.load_creator_profile(str(profile_path))

        self.assertEqual(loaded["creator_profile_id"], "creator-001")
        self.assertEqual(profile["voice"]["asset_ref"], "asset-voice-001")


if __name__ == "__main__":
    unittest.main()
