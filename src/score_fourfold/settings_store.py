from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .database import Database
from .domain import MarketType


SETTINGS_SCHEMA_VERSION = 1
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True, slots=True)
class RecommendationProfile:
    market: str
    enabled: bool
    min_pass_size: int
    max_pass_size: int
    plan_count: int


class SecretCipher:
    """Encrypt secrets stored in SQLite while keeping the master key outside it."""

    def __init__(self, master_key: str):
        if not master_key:
            raise ValueError("SETTINGS_MASTER_KEY is empty")
        try:
            self._fernet = Fernet(master_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("SETTINGS_MASTER_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise ValueError("stored secret cannot be decrypted with SETTINGS_MASTER_KEY") from exc


class SettingsRepository:
    def __init__(self, database: Database, legacy: Settings):
        self.database = database
        self.legacy = legacy
        self._cipher = (
            SecretCipher(legacy.settings_master_key)
            if legacy.settings_master_key
            else None
        )

    @staticmethod
    def _split_recipients(raw: str) -> list[str]:
        recipients: list[str] = []
        seen: set[str] = set()
        for value in re.split(r"[,;\n]", raw):
            email = value.strip().lower()
            if not email or email in seen:
                continue
            if not EMAIL.fullmatch(email):
                raise ValueError(f"invalid legacy mail recipient: {email}")
            seen.add(email)
            recipients.append(email)
        return recipients

    @staticmethod
    def _time_text(value: Any) -> str:
        return value.strftime("%H:%M")

    def _legacy_fingerprint(self) -> str:
        safe_values = {
            "mail_to": self.legacy.mail_to,
            "smtp_host": self.legacy.smtp_host,
            "smtp_port": self.legacy.smtp_port,
            "smtp_username": self.legacy.smtp_username,
            "smtp_auth_configured": bool(self.legacy.smtp_auth_code),
            "mail_from": self.legacy.mail_from,
            "mail_dry_run": self.legacy.mail_dry_run,
            "recommendation_times": [
                self._time_text(value) for value in self.legacy.recommendation_times
            ],
            "recommendation_first_mail_time": self._time_text(
                self.legacy.recommendation_first_mail_time
            ),
            "recommendation_latest_start": self._time_text(
                self.legacy.recommendation_latest_start
            ),
            "recommendation_deadline": self._time_text(
                self.legacy.recommendation_deadline
            ),
            "recommendation_send_buffer_minutes": (
                self.legacy.recommendation_send_buffer_minutes
            ),
            "poll_interval_seconds": self.legacy.poll_interval_seconds,
            "result_check_delay_minutes": self.legacy.result_check_delay_minutes,
            "send_no_recommendation": self.legacy.send_no_recommendation,
            "had_enabled": self.legacy.had_enabled,
            "had_pass_sizes": list(self.legacy.had_pass_sizes),
            "ai_analysis_enabled": self.legacy.ai_analysis_enabled,
            "qwen_api_key_configured": bool(self.legacy.qwen_api_key),
            "qwen_api_url": self.legacy.qwen_api_url,
            "qwen_model": self.legacy.qwen_model,
            "ai_http_timeout_seconds": self.legacy.ai_http_timeout_seconds,
        }
        payload = json.dumps(safe_values, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _protected_secret(self, value: str, env_name: str) -> tuple[str, str]:
        if not value:
            return "", env_name
        if self._cipher is None:
            # Existing deployments remain functional until a master key is
            # configured.  The secret stays only in the legacy environment.
            return "", env_name
        return self._cipher.encrypt(value), ""

    def initialize_from_legacy(self, now: datetime | None = None) -> bool:
        initialized_at = (now or datetime.now(self.legacy.timezone)).isoformat()
        recipients = self._split_recipients(self.legacy.mail_to)
        if not recipients:
            raise ValueError("legacy MAIL_TO contains no valid recipient")

        had_sizes = tuple(self.legacy.had_pass_sizes) or (6, 5, 4)
        profiles = (
            RecommendationProfile(MarketType.CRS.value, True, 2, 5, 3),
            RecommendationProfile(
                MarketType.HAD.value,
                self.legacy.had_enabled,
                min(had_sizes),
                max(had_sizes),
                1,
            ),
            RecommendationProfile("ttg", False, 2, 6, 1),
        )
        smtp_ciphertext, smtp_env = self._protected_secret(
            self.legacy.smtp_auth_code,
            "SMTP_AUTH_CODE",
        )
        qwen_ciphertext, qwen_env = self._protected_secret(
            self.legacy.qwen_api_key,
            "QWEN_API_KEY",
        )

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_meta = connection.execute(
                "SELECT schema_version FROM settings_meta WHERE singleton_id = 1"
            ).fetchone()
            if existing_meta is not None:
                return False

            managed_tables = (
                "recommendation_profiles",
                "email_recipients",
                "notification_settings",
                "ai_model_configs",
                "ai_runtime_settings",
                "runtime_settings",
            )
            for table in managed_tables:
                count = int(
                    connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                        "count"
                    ]
                )
                if count:
                    raise RuntimeError(
                        "settings migration is incomplete; managed tables are not empty"
                    )

            for profile in profiles:
                connection.execute(
                    """
                    INSERT INTO recommendation_profiles
                        (market, enabled, min_pass_size, max_pass_size, plan_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile.market,
                        int(profile.enabled),
                        profile.min_pass_size,
                        profile.max_pass_size,
                        profile.plan_count,
                        initialized_at,
                    ),
                )
            for position, email in enumerate(recipients):
                connection.execute(
                    "INSERT INTO email_recipients (email, enabled, position) VALUES (?, 1, ?)",
                    (email, position),
                )

            connection.execute(
                """
                INSERT INTO notification_settings
                    (singleton_id, smtp_host, smtp_port, smtp_username,
                     smtp_auth_ciphertext, smtp_auth_env, mail_from,
                     mail_dry_run, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.legacy.smtp_host,
                    self.legacy.smtp_port,
                    self.legacy.smtp_username,
                    smtp_ciphertext,
                    smtp_env,
                    self.legacy.mail_from,
                    int(self.legacy.mail_dry_run),
                    initialized_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_model_configs
                    (model_config_id, provider, display_name, base_url, model_name,
                     api_key_ciphertext, api_key_env, web_search_required,
                     created_at, updated_at)
                VALUES ('legacy-qwen', 'qwen', '阿里云百炼千问', ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    self.legacy.qwen_api_url,
                    self.legacy.qwen_model,
                    qwen_ciphertext,
                    qwen_env,
                    initialized_at,
                    initialized_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_runtime_settings
                    (singleton_id, enabled, active_model_config_id,
                     http_timeout_seconds, updated_at)
                VALUES (1, ?, 'legacy-qwen', ?, ?)
                """,
                (
                    int(self.legacy.ai_analysis_enabled),
                    self.legacy.ai_http_timeout_seconds,
                    initialized_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_settings
                    (singleton_id, recommendation_times_json,
                     recommendation_first_mail_time, recommendation_latest_start,
                     recommendation_deadline, recommendation_send_buffer_minutes,
                     poll_interval_seconds, result_check_delay_minutes,
                     send_no_recommendation, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    json.dumps(
                        [self._time_text(value) for value in self.legacy.recommendation_times]
                    ),
                    self._time_text(self.legacy.recommendation_first_mail_time),
                    self._time_text(self.legacy.recommendation_latest_start),
                    self._time_text(self.legacy.recommendation_deadline),
                    self.legacy.recommendation_send_buffer_minutes,
                    self.legacy.poll_interval_seconds,
                    self.legacy.result_check_delay_minutes,
                    int(self.legacy.send_no_recommendation),
                    initialized_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO settings_meta
                    (singleton_id, schema_version, initialized_at, legacy_fingerprint)
                VALUES (1, ?, ?, ?)
                """,
                (SETTINGS_SCHEMA_VERSION, initialized_at, self._legacy_fingerprint()),
            )
        return True

    def recommendation_profiles(self) -> dict[str, RecommendationProfile]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recommendation_profiles ORDER BY market"
            ).fetchall()
        return {
            row["market"]: RecommendationProfile(
                market=row["market"],
                enabled=bool(row["enabled"]),
                min_pass_size=int(row["min_pass_size"]),
                max_pass_size=int(row["max_pass_size"]),
                plan_count=int(row["plan_count"]),
            )
            for row in rows
        }

    def public_snapshot(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            meta = connection.execute(
                "SELECT * FROM settings_meta WHERE singleton_id = 1"
            ).fetchone()
            recipients = connection.execute(
                "SELECT email, enabled FROM email_recipients ORDER BY position, id"
            ).fetchall()
            notification = connection.execute(
                "SELECT * FROM notification_settings WHERE singleton_id = 1"
            ).fetchone()
            models = connection.execute(
                "SELECT * FROM ai_model_configs ORDER BY created_at, model_config_id"
            ).fetchall()
            ai_runtime = connection.execute(
                "SELECT * FROM ai_runtime_settings WHERE singleton_id = 1"
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_settings WHERE singleton_id = 1"
            ).fetchone()
        if meta is None or notification is None or ai_runtime is None or runtime is None:
            raise RuntimeError("settings have not been initialized")
        return {
            "schema_version": int(meta["schema_version"]),
            "initialized_at": meta["initialized_at"],
            "profiles": {
                key: {
                    "enabled": value.enabled,
                    "min_pass_size": value.min_pass_size,
                    "max_pass_size": value.max_pass_size,
                    "plan_count": value.plan_count,
                }
                for key, value in self.recommendation_profiles().items()
            },
            "recipients": [
                {"email": row["email"], "enabled": bool(row["enabled"])}
                for row in recipients
            ],
            "mail": {
                "smtp_host": notification["smtp_host"],
                "smtp_port": int(notification["smtp_port"]),
                "smtp_username": notification["smtp_username"],
                "smtp_auth_configured": bool(
                    notification["smtp_auth_ciphertext"] or notification["smtp_auth_env"]
                ),
                "mail_from": notification["mail_from"],
                "mail_dry_run": bool(notification["mail_dry_run"]),
            },
            "ai": {
                "enabled": bool(ai_runtime["enabled"]),
                "active_model_config_id": ai_runtime["active_model_config_id"],
                "http_timeout_seconds": int(ai_runtime["http_timeout_seconds"]),
                "models": [
                    {
                        "id": row["model_config_id"],
                        "provider": row["provider"],
                        "display_name": row["display_name"],
                        "base_url": row["base_url"],
                        "model_name": row["model_name"],
                        "api_key_configured": bool(
                            row["api_key_ciphertext"] or row["api_key_env"]
                        ),
                        "last_test_status": row["last_test_status"],
                        "last_test_detail": row["last_test_detail"],
                        "last_tested_at": row["last_tested_at"],
                    }
                    for row in models
                ],
            },
            "runtime": {
                "recommendation_times": json.loads(runtime["recommendation_times_json"]),
                "recommendation_first_mail_time": runtime[
                    "recommendation_first_mail_time"
                ],
                "recommendation_latest_start": runtime["recommendation_latest_start"],
                "recommendation_deadline": runtime["recommendation_deadline"],
                "recommendation_send_buffer_minutes": int(
                    runtime["recommendation_send_buffer_minutes"]
                ),
                "poll_interval_seconds": int(runtime["poll_interval_seconds"]),
                "result_check_delay_minutes": int(runtime["result_check_delay_minutes"]),
                "send_no_recommendation": bool(runtime["send_no_recommendation"]),
            },
            "secret_storage_ready": self._cipher is not None,
        }

    def resolve_secret(self, ciphertext: str, env_name: str) -> str:
        if ciphertext:
            if self._cipher is None:
                raise ValueError("SETTINGS_MASTER_KEY is required to decrypt stored secrets")
            return self._cipher.decrypt(ciphertext)
        return os.getenv(env_name, "").strip() if env_name else ""
