from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from score_fourfold.auth import hash_password
from score_fourfold.config import Settings


class WebSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(
            "correct horse battery staple",
            salt=bytes.fromhex("00112233445566778899aabbccddeeff"),
        )

    def _public_env(self, **overrides: str) -> dict[str, str]:
        values = {
            "WEB_ACCESS_MODE": "public",
            "WEB_HOST": "0.0.0.0",
            "WEB_PUBLIC_ORIGIN": "https://8.8.8.8",
            "WEB_USERNAME": "owner",
            "WEB_PASSWORD_HASH": self.password_hash,
            "WEB_TRUST_PROXY_HEADERS": "true",
            "WEB_SESSION_HOURS": "12",
        }
        values.update(overrides)
        return values

    def test_ssh_mode_keeps_authentication_optional(self):
        with patch.dict(os.environ, {"WEB_ACCESS_MODE": "ssh"}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.web_access_mode, "ssh")
        self.assertFalse(settings.web_password_hash)
        self.assertFalse(settings.web_trust_proxy_headers)

    def test_public_mode_accepts_complete_https_ipv4_configuration(self):
        with patch.dict(os.environ, self._public_env(), clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.web_public_origin, "https://8.8.8.8")
        self.assertEqual(settings.web_session_hours, 12)
        self.assertEqual(settings.recommendation_first_mail_time.strftime("%H:%M"), "15:00")

    def test_first_recommendation_mail_must_precede_cutoff(self):
        with patch.dict(
            os.environ,
            {"RECOMMENDATION_FIRST_MAIL_TIME": "17:50"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "FIRST_MAIL_TIME"):
                Settings.from_env()

    def test_public_mode_accepts_https_domain_for_openresty(self):
        with patch.dict(
            os.environ,
            self._public_env(WEB_PUBLIC_ORIGIN="https://cchbin.site"),
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.web_public_origin, "https://cchbin.site")

    def test_public_mode_rejects_missing_proxy_trust_or_password_hash(self):
        for override in (
            {"WEB_TRUST_PROXY_HEADERS": "false"},
            {"WEB_PASSWORD_HASH": "invalid"},
        ):
            with self.subTest(override=override):
                with patch.dict(os.environ, self._public_env(**override), clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env()

    def test_public_mode_rejects_non_docker_listener(self):
        with patch.dict(
            os.environ,
            self._public_env(WEB_HOST="127.0.0.1"),
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "WEB_HOST=0.0.0.0"):
                Settings.from_env()

    def test_public_mode_rejects_bad_hostname_private_ip_path_and_nonstandard_port(self):
        origins = (
            "http://8.8.8.8",
            "https://localhost",
            "https://bad_host.example.com",
            "https://192.168.1.10",
            "https://8.8.8.8/login",
            "https://8.8.8.8:8443",
        )
        for origin in origins:
            with self.subTest(origin=origin):
                with patch.dict(
                    os.environ,
                    self._public_env(WEB_PUBLIC_ORIGIN=origin),
                    clear=True,
                ):
                    with self.assertRaises(ValueError):
                        Settings.from_env()

    def test_access_mode_username_and_session_bounds_are_validated(self):
        cases = (
            {"WEB_ACCESS_MODE": "internet"},
            {"WEB_USERNAME": "bad username"},
            {"WEB_SESSION_HOURS": "169"},
        )
        for override in cases:
            with self.subTest(override=override):
                with patch.dict(os.environ, self._public_env(**override), clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env()

    def test_ai_enabled_requires_key_and_https_endpoint(self):
        with patch.dict(os.environ, {"AI_ANALYSIS_ENABLED": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "QWEN_API_KEY"):
                Settings.from_env()
        with patch.dict(
            os.environ,
            {
                "AI_ANALYSIS_ENABLED": "true",
                "QWEN_API_KEY": "secret",
                "QWEN_API_URL": "http://api.example.test/responses",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "https URL"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
