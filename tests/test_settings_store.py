from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from score_fourfold.database import Database
from score_fourfold.settings_store import SettingsRepository

from .helpers import make_settings


class SettingsRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_settings_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    def test_initializes_all_legacy_values_once_and_encrypts_secrets(self):
        master_key = Fernet.generate_key().decode("ascii")
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_to="First@Example.com,second@example.com",
            smtp_username="mailer@example.com",
            smtp_auth_code="smtp-secret",
            qwen_api_key="qwen-secret",
            ai_analysis_enabled=True,
            settings_master_key=master_key,
        )
        repository = SettingsRepository(self.database, settings)

        self.assertTrue(repository.initialize_from_legacy(self.now))
        self.assertFalse(repository.initialize_from_legacy(self.now))
        snapshot = repository.public_snapshot()

        self.assertEqual(
            snapshot["recipients"],
            [
                {"email": "first@example.com", "enabled": True},
                {"email": "second@example.com", "enabled": True},
            ],
        )
        self.assertEqual(snapshot["profiles"]["crs"]["plan_count"], 3)
        self.assertEqual(snapshot["profiles"]["crs"]["min_pass_size"], 2)
        self.assertEqual(snapshot["profiles"]["crs"]["max_pass_size"], 5)
        self.assertTrue(snapshot["profiles"]["had"]["enabled"])
        self.assertFalse(snapshot["profiles"]["ttg"]["enabled"])
        self.assertEqual(snapshot["runtime"]["recommendation_times"], ["10:00", "14:00", "17:30"])
        self.assertEqual(snapshot["ai"]["active_model_config_id"], "legacy-qwen")
        self.assertTrue(snapshot["ai"]["models"][0]["api_key_configured"])
        self.assertTrue(snapshot["secret_storage_ready"])

        with self.database.connect() as connection:
            model = connection.execute(
                "SELECT api_key_ciphertext, api_key_env FROM ai_model_configs"
            ).fetchone()
            mail = connection.execute(
                "SELECT smtp_auth_ciphertext, smtp_auth_env FROM notification_settings"
            ).fetchone()
        self.assertNotEqual(model["api_key_ciphertext"], "qwen-secret")
        self.assertEqual(model["api_key_env"], "")
        self.assertNotEqual(mail["smtp_auth_ciphertext"], "smtp-secret")
        self.assertEqual(mail["smtp_auth_env"], "")
        self.assertEqual(
            repository.resolve_secret(model["api_key_ciphertext"], model["api_key_env"]),
            "qwen-secret",
        )

    def test_without_master_key_keeps_legacy_secrets_in_environment_only(self):
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            smtp_auth_code="legacy-smtp",
            qwen_api_key="legacy-qwen",
            settings_master_key="",
        )
        repository = SettingsRepository(self.database, settings)
        repository.initialize_from_legacy(self.now)
        with self.database.connect() as connection:
            model = connection.execute(
                "SELECT api_key_ciphertext, api_key_env FROM ai_model_configs"
            ).fetchone()
            mail = connection.execute(
                "SELECT smtp_auth_ciphertext, smtp_auth_env FROM notification_settings"
            ).fetchone()
        self.assertEqual(model["api_key_ciphertext"], "")
        self.assertEqual(model["api_key_env"], "QWEN_API_KEY")
        self.assertEqual(mail["smtp_auth_ciphertext"], "")
        self.assertEqual(mail["smtp_auth_env"], "SMTP_AUTH_CODE")
        with patch.dict("os.environ", {"QWEN_API_KEY": "legacy-qwen"}):
            self.assertEqual(repository.resolve_secret("", "QWEN_API_KEY"), "legacy-qwen")
        self.assertFalse(repository.public_snapshot()["secret_storage_ready"])

    def test_partial_initialization_is_rejected_without_overwrite(self):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO email_recipients (email, enabled, position) VALUES ('keep@example.com', 1, 0)"
            )
        repository = SettingsRepository(
            self.database,
            make_settings(self.root, database_path=self.database_path),
        )
        with self.assertRaises(RuntimeError):
            repository.initialize_from_legacy(self.now)
        with self.database.connect() as connection:
            meta = connection.execute("SELECT * FROM settings_meta").fetchall()
            recipients = connection.execute("SELECT email FROM email_recipients").fetchall()
        self.assertEqual(meta, [])
        self.assertEqual([row["email"] for row in recipients], ["keep@example.com"])


if __name__ == "__main__":
    unittest.main()
