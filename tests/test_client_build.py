from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.client_build import (
    ClientBuildFingerprint,
    load_client_profiles,
    match_client_profile,
    fingerprint_client,
    session_supports,
)


class ClientBuildTests(unittest.TestCase):
    def test_session_capability_requires_exact_profile(self) -> None:
        session = type("Session", (), {"exe_path": r"D:\game\xajh.exe"})()
        profile = load_client_profiles()[0]
        with patch("app.core.client_build.profile_for_client", return_value=profile):
            self.assertTrue(session_supports(session, "package.read"))
            self.assertTrue(session_supports(session, "task.complete.live"))
            self.assertTrue(session_supports(session, "task.accept.live"))
        self.assertFalse(session_supports(object(), "package.read"))

    @patch("app.core.client_build.fingerprint_client")
    def test_profile_lookup_does_not_cache_by_path(self, fingerprint) -> None:
        profile = load_client_profiles()[0]
        fingerprint.side_effect = [
            profile.fingerprint,
            ClientBuildFingerprint(
                sha256="11" * 32,
                size=profile.fingerprint.size,
                machine=profile.fingerprint.machine,
                timestamp=profile.fingerprint.timestamp,
                image_base=profile.fingerprint.image_base,
                image_size=profile.fingerprint.image_size,
            ),
        ]
        from app.core.client_build import profile_for_client

        self.assertIs(profile_for_client(r"D:\game\xajh.exe"), profile)
        self.assertIsNone(profile_for_client(r"D:\game\xajh.exe"))
        self.assertEqual(fingerprint.call_count, 2)

    def test_known_profile_requires_exact_hash_and_size(self) -> None:
        profile = load_client_profiles()[0]
        self.assertIs(match_client_profile(profile.fingerprint), profile)
        wrong = ClientBuildFingerprint(
            sha256="00" * 32,
            size=profile.fingerprint.size,
            machine=profile.fingerprint.machine,
            timestamp=profile.fingerprint.timestamp,
            image_base=profile.fingerprint.image_base,
            image_size=profile.fingerprint.image_size,
        )
        self.assertIsNone(match_client_profile(wrong))

    def test_installed_client_matches_when_present(self) -> None:
        path = Path(r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe")
        if not path.is_file():
            self.skipTest("xajh.exe is not installed")
        fp = fingerprint_client(path)
        self.assertEqual(
            fp.sha256,
            "21A8BC0406181655EE0C097AC73C3C39CF1DC8E91FEB6B84DF352B300CF6CAB9",
        )
        self.assertTrue(fp.is_x86)


if __name__ == "__main__":
    unittest.main()
