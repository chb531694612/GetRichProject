from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .ai_models import (
    AIModelRuntime,
    DEFAULT_SYSTEM_PROMPT,
    PROVIDER_BY_CODE,
    set_prompt_overrides,
    test_model,
    validate_runtime,
)
from .ai_analyzer import (
    DEFAULT_PLAN_REQUIREMENTS,
    DEFAULT_SUMMARY_REQUIREMENTS,
)
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
        try:
            self._sync_prompt_overrides()
        except Exception:
            # 数据库尚未初始化或读取失败时保持内置默认，绝不能阻断启动。
            set_prompt_overrides()

    def _prompt_row(self) -> Any:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_prompt_settings WHERE singleton_id = 1"
            ).fetchone()
            if row is not None:
                return row
            timestamp = datetime.now(self.legacy.timezone).isoformat()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO ai_prompt_settings
                    (singleton_id, system_prompt, plan_requirements,
                     summary_requirements, updated_at)
                VALUES (1, '', '', '', ?)
                """,
                (timestamp,),
            )
            return connection.execute(
                "SELECT * FROM ai_prompt_settings WHERE singleton_id = 1"
            ).fetchone()

    def ai_prompt_settings(self) -> dict[str, Any]:
        row = self._prompt_row()
        return {
            "system_prompt": row["system_prompt"],
            "plan_requirements": row["plan_requirements"],
            "summary_requirements": row["summary_requirements"],
            "updated_at": row["updated_at"],
        }

    def update_ai_prompt_settings(
        self,
        values: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        fields = (
            ("system_prompt", "系统提示词"),
            ("plan_requirements", "计划推荐分析要求"),
            ("summary_requirements", "总结分析要求"),
        )
        cleaned: dict[str, str] = {}
        for key, label in fields:
            text = str(values.get(key, "") or "").strip()
            if len(text) > 8000:
                raise ValueError(f"{label}不能超过8000字")
            cleaned[key] = text
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ai_prompt_settings
                    (singleton_id, system_prompt, plan_requirements,
                     summary_requirements, updated_at)
                VALUES (1, '', '', '', ?)
                ON CONFLICT(singleton_id) DO NOTHING
                """,
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE ai_prompt_settings
                SET system_prompt = ?, plan_requirements = ?,
                    summary_requirements = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (
                    cleaned["system_prompt"],
                    cleaned["plan_requirements"],
                    cleaned["summary_requirements"],
                    timestamp,
                ),
            )
        self._sync_prompt_overrides()

    def _sync_prompt_overrides(self) -> None:
        row = self._prompt_row()
        set_prompt_overrides(
            row["system_prompt"],
            row["plan_requirements"],
            row["summary_requirements"],
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

    def update_recommendation_profiles(
        self,
        values: dict[str, dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> None:
        expected = {item.value for item in MarketType}
        if set(values) != expected:
            raise ValueError("必须同时提交比分、胜平负和进球数配置")
        profiles: list[RecommendationProfile] = []
        for market in MarketType:
            raw = values[market.value]
            enabled = raw.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError(f"{market.label_zh}启用状态无效")
            try:
                minimum = int(raw.get("min_pass_size"))
                maximum = int(raw.get("max_pass_size"))
                plan_count = int(raw.get("plan_count"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{market.label_zh}串关配置必须是整数") from exc
            if minimum < 2 or maximum > 8 or minimum > maximum:
                raise ValueError(f"{market.label_zh}串关范围必须在2至8之间")
            if plan_count < 1 or plan_count > 20:
                raise ValueError(f"{market.label_zh}生成计划数必须在1至20之间")
            profiles.append(
                RecommendationProfile(
                    market.value,
                    enabled,
                    minimum,
                    maximum,
                    plan_count,
                )
            )
        if not any(profile.enabled for profile in profiles):
            raise ValueError("至少启用一种推荐类型")
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for profile in profiles:
                connection.execute(
                    """
                    UPDATE recommendation_profiles
                    SET enabled = ?, min_pass_size = ?, max_pass_size = ?,
                        plan_count = ?, updated_at = ?
                    WHERE market = ?
                    """,
                    (
                        int(profile.enabled),
                        profile.min_pass_size,
                        profile.max_pass_size,
                        profile.plan_count,
                        timestamp,
                        profile.market,
                    ),
                )

    def update_recipients(self, recipients: list[str]) -> None:
        normalized = self._split_recipients(",".join(recipients))
        if not normalized:
            raise ValueError("至少配置一个推送邮箱")
        if len(normalized) > 20:
            raise ValueError("推送邮箱不能超过20个")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM email_recipients")
            for position, email in enumerate(normalized):
                connection.execute(
                    "INSERT INTO email_recipients (email, enabled, position) VALUES (?, 1, ?)",
                    (email, position),
                )

    def update_mail_settings(
        self,
        values: dict[str, Any],
        *,
        new_auth_code: str = "",
        now: datetime | None = None,
    ) -> None:
        host = str(values.get("smtp_host", "")).strip()
        username = str(values.get("smtp_username", "")).strip()
        mail_from = str(values.get("mail_from", "")).strip()
        dry_run = values.get("mail_dry_run")
        try:
            port = int(values.get("smtp_port"))
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP端口必须是整数") from exc
        if not host or len(host) > 253 or port < 1 or port > 65535:
            raise ValueError("SMTP服务器或端口无效")
        if not isinstance(dry_run, bool):
            raise ValueError("邮件预览开关无效")
        if mail_from and not EMAIL.fullmatch(mail_from.lower()):
            raise ValueError("发件邮箱格式无效")
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT * FROM notification_settings WHERE singleton_id = 1"
            ).fetchone()
        if current is None:
            raise RuntimeError("settings have not been initialized")
        ciphertext = current["smtp_auth_ciphertext"]
        env_name = current["smtp_auth_env"]
        if new_auth_code:
            if self._cipher is None:
                raise ValueError("保存新的 SMTP 授权码前必须配置 SETTINGS_MASTER_KEY")
            ciphertext = self._cipher.encrypt(new_auth_code.strip())
            env_name = ""
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notification_settings
                SET smtp_host = ?, smtp_port = ?, smtp_username = ?,
                    smtp_auth_ciphertext = ?, smtp_auth_env = ?, mail_from = ?,
                    mail_dry_run = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (
                    host,
                    port,
                    username,
                    ciphertext,
                    env_name,
                    mail_from,
                    int(dry_run),
                    timestamp,
                ),
            )

    @staticmethod
    def _parse_clock(value: Any, field: str) -> time:
        try:
            parsed = time.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{field}必须是HH:MM格式") from exc
        return parsed.replace(second=0, microsecond=0)

    def update_runtime_settings(
        self,
        values: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        raw_times = values.get("recommendation_times")
        if not isinstance(raw_times, list) or not raw_times:
            raise ValueError("至少配置一个推荐生成时间")
        recommendation_times = sorted(
            {self._parse_clock(value, "推荐生成时间") for value in raw_times}
        )
        first_mail = self._parse_clock(
            values.get("recommendation_first_mail_time"),
            "首封邮件时间",
        )
        latest_start = self._parse_clock(
            values.get("recommendation_latest_start"),
            "最迟生成时间",
        )
        deadline = self._parse_clock(values.get("recommendation_deadline"), "最迟推送时间")
        try:
            buffer_minutes = int(values.get("recommendation_send_buffer_minutes"))
            poll_seconds = int(values.get("poll_interval_seconds"))
            result_delay = int(values.get("result_check_delay_minutes"))
        except (TypeError, ValueError) as exc:
            raise ValueError("运行时间配置必须是整数") from exc
        send_no_recommendation = values.get("send_no_recommendation")
        if not isinstance(send_no_recommendation, bool):
            raise ValueError("无推荐通知开关无效")
        if buffer_minutes < 0 or buffer_minutes > 120:
            raise ValueError("邮件安全缓冲必须在0至120分钟之间")
        if poll_seconds < 60 or poll_seconds > 86400:
            raise ValueError("轮询间隔必须在60至86400秒之间")
        if result_delay < 90 or result_delay > 1440:
            raise ValueError("赛果检查延迟必须在90至1440分钟之间")
        base = datetime(2000, 1, 1)
        first_dt = datetime.combine(base.date(), first_mail)
        latest_dt = datetime.combine(base.date(), latest_start)
        deadline_dt = datetime.combine(base.date(), deadline)
        cutoff_dt = deadline_dt - timedelta(minutes=buffer_minutes)
        last_slot_dt = datetime.combine(base.date(), recommendation_times[-1])
        if last_slot_dt >= latest_dt or latest_dt >= deadline_dt:
            raise ValueError("最后生成时间必须早于最迟生成时间，且最迟生成必须早于推送截止")
        if first_dt >= cutoff_dt or last_slot_dt >= cutoff_dt:
            raise ValueError("首封邮件和最后生成时间必须早于邮件安全截止")
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE runtime_settings
                SET recommendation_times_json = ?, recommendation_first_mail_time = ?,
                    recommendation_latest_start = ?, recommendation_deadline = ?,
                    recommendation_send_buffer_minutes = ?, poll_interval_seconds = ?,
                    result_check_delay_minutes = ?, send_no_recommendation = ?,
                    updated_at = ?
                WHERE singleton_id = 1
                """,
                (
                    json.dumps([self._time_text(value) for value in recommendation_times]),
                    self._time_text(first_mail),
                    self._time_text(latest_start),
                    self._time_text(deadline),
                    buffer_minutes,
                    poll_seconds,
                    result_delay,
                    int(send_no_recommendation),
                    timestamp,
                ),
            )

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
        models = self._migrate_model_endpoints(models)
        try:
            prompt_row = self._prompt_row()
        except Exception:
            prompt_row = {
                "system_prompt": "",
                "plan_requirements": "",
                "summary_requirements": "",
                "updated_at": "",
            }
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
            "ai_prompts": {
                "system_prompt": prompt_row["system_prompt"],
                "plan_requirements": prompt_row["plan_requirements"],
                "summary_requirements": prompt_row["summary_requirements"],
                "updated_at": prompt_row["updated_at"],
                "defaults": {
                    "system_prompt": DEFAULT_SYSTEM_PROMPT,
                    "plan_requirements": DEFAULT_PLAN_REQUIREMENTS,
                    "summary_requirements": DEFAULT_SUMMARY_REQUIREMENTS,
                },
            },
            "secret_storage_ready": self._cipher is not None,
        }

    def effective_settings(self) -> Settings:
        with self.database.connect() as connection:
            recipients = connection.execute(
                "SELECT email FROM email_recipients WHERE enabled = 1 ORDER BY position, id"
            ).fetchall()
            notification = connection.execute(
                "SELECT * FROM notification_settings WHERE singleton_id = 1"
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_settings WHERE singleton_id = 1"
            ).fetchone()
            ai_runtime = connection.execute(
                "SELECT * FROM ai_runtime_settings WHERE singleton_id = 1"
            ).fetchone()
        if notification is None or runtime is None or ai_runtime is None:
            return self.legacy
        recommendation_times = tuple(
            self._parse_clock(value, "推荐生成时间")
            for value in json.loads(runtime["recommendation_times_json"])
        )
        values: dict[str, Any] = {
            "mail_to": ",".join(row["email"] for row in recipients),
            "smtp_host": notification["smtp_host"],
            "smtp_port": int(notification["smtp_port"]),
            "smtp_username": notification["smtp_username"],
            "smtp_auth_code": self.resolve_secret(
                notification["smtp_auth_ciphertext"],
                notification["smtp_auth_env"],
            ),
            "mail_from": notification["mail_from"],
            "mail_dry_run": bool(notification["mail_dry_run"]),
            "recommendation_times": recommendation_times,
            "recommendation_first_mail_time": self._parse_clock(
                runtime["recommendation_first_mail_time"], "首封邮件时间"
            ),
            "recommendation_latest_start": self._parse_clock(
                runtime["recommendation_latest_start"], "最迟生成时间"
            ),
            "recommendation_deadline": self._parse_clock(
                runtime["recommendation_deadline"], "最迟推送时间"
            ),
            "recommendation_send_buffer_minutes": int(
                runtime["recommendation_send_buffer_minutes"]
            ),
            "poll_interval_seconds": int(runtime["poll_interval_seconds"]),
            "result_check_delay_minutes": int(runtime["result_check_delay_minutes"]),
            "send_no_recommendation": bool(runtime["send_no_recommendation"]),
            "ai_analysis_enabled": bool(ai_runtime["enabled"]),
            "ai_http_timeout_seconds": int(ai_runtime["http_timeout_seconds"]),
        }
        active_id = ai_runtime["active_model_config_id"]
        if active_id:
            model = self.model_runtime(active_id)
            values.update(
                qwen_api_key=model.api_key,
                qwen_api_url=model.base_url,
                qwen_model=model.model_name,
            )
        return replace(self.legacy, **values)

    def resolve_secret(self, ciphertext: str, env_name: str) -> str:
        if ciphertext:
            if self._cipher is None:
                raise ValueError("SETTINGS_MASTER_KEY is required to decrypt stored secrets")
            return self._cipher.decrypt(ciphertext)
        return os.getenv(env_name, "").strip() if env_name else ""

    def _migrate_model_endpoints(self, models: list[Any]) -> list[Any]:
        """Auto-rewrite legacy endpoints that now expose native web search.

        DeepSeek moved its built-in ``web_search`` tool from the standard
        Chat Completions endpoint (where it is only a function-calling stub)
        to a dedicated ``/responses`` endpoint.  Old configurations saved
        against ``/chat/completions`` must be transparently upgraded so users
        do not have to manually re-save their model after this change ships.
        """
        pending: list[tuple[str, str]] = []
        rewritten: list[Any] = []
        for row in models:
            row_dict = dict(row)
            if (
                row_dict.get("provider") == "deepseek"
                and row_dict.get("base_url", "").rstrip("/").endswith("/chat/completions")
            ):
                new_url = "https://api.deepseek.com/responses"
                if row_dict["base_url"] != new_url:
                    pending.append((row_dict["model_config_id"], new_url))
                    row_dict["base_url"] = new_url
                    # 旧测试是针对旧端点的，重置以强制用户重新测试新接口。
                    row_dict["last_test_status"] = "untested"
                    row_dict["last_test_detail"] = ""
                    row_dict["last_tested_at"] = None
            rewritten.append(row_dict)
        if pending:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for config_id, new_url in pending:
                    connection.execute(
                        """
                        UPDATE ai_model_configs
                        SET base_url = ?,
                            last_test_status = 'untested',
                            last_test_detail = '',
                            last_tested_at = NULL
                        WHERE model_config_id = ?
                        """,
                        (new_url, config_id),
                    )
        return rewritten

    def model_runtime(self, model_config_id: str) -> AIModelRuntime:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_model_configs WHERE model_config_id = ?",
                (model_config_id,),
            ).fetchone()
        if row is None:
            raise ValueError("模型配置不存在")
        return AIModelRuntime(
            config_id=row["model_config_id"],
            provider=row["provider"],
            base_url=row["base_url"],
            model_name=row["model_name"],
            api_key=self.resolve_secret(
                row["api_key_ciphertext"],
                row["api_key_env"],
            ),
        )

    def active_model_runtime(self) -> AIModelRuntime | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT enabled, active_model_config_id FROM ai_runtime_settings WHERE singleton_id = 1"
            ).fetchone()
        if row is None or not bool(row["enabled"]) or not row["active_model_config_id"]:
            return None
        return self.model_runtime(row["active_model_config_id"])

    def save_model_config(
        self,
        *,
        provider: str,
        display_name: str,
        base_url: str,
        model_name: str,
        api_key: str = "",
        model_config_id: str = "",
        now: datetime | None = None,
    ) -> str:
        provider = provider.strip().lower()
        spec = PROVIDER_BY_CODE.get(provider)
        if spec is None:
            raise ValueError("不支持的大模型供应商")
        display_name = display_name.strip() or spec.name
        base_url = base_url.strip()
        model_name = model_name.strip()
        if len(display_name) > 80 or len(model_name) > 160 or len(base_url) > 500:
            raise ValueError("模型配置字段过长")
        config_id = model_config_id.strip() or secrets.token_urlsafe(12)
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", config_id):
            raise ValueError("模型配置 ID 无效")

        existing = None
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ai_model_configs WHERE model_config_id = ?",
                (config_id,),
            ).fetchone()
        if model_config_id and existing is None:
            raise ValueError("模型配置不存在")

        api_key_ciphertext = existing["api_key_ciphertext"] if existing else ""
        api_key_env = existing["api_key_env"] if existing else ""
        if api_key:
            if self._cipher is None:
                raise ValueError("保存新的 API Key 前必须配置 SETTINGS_MASTER_KEY")
            api_key_ciphertext = self._cipher.encrypt(api_key.strip())
            api_key_env = ""
        runtime_key = api_key.strip() or self.resolve_secret(api_key_ciphertext, api_key_env)
        runtime = AIModelRuntime(config_id, provider, base_url, model_name, runtime_key)
        # Saving may include providers that cannot yet be activated, but URL,
        # model and credential shape must still be safe and complete.
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
            or not model_name
        ):
            raise ValueError("模型 API 地址必须使用 HTTPS，且模型名不能为空")
        if not runtime.api_key:
            raise ValueError("API Key 未配置")

        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO ai_model_configs
                        (model_config_id, provider, display_name, base_url, model_name,
                         api_key_ciphertext, api_key_env, web_search_required,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        config_id,
                        provider,
                        display_name,
                        base_url,
                        model_name,
                        api_key_ciphertext,
                        api_key_env,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE ai_model_configs
                    SET provider = ?, display_name = ?, base_url = ?, model_name = ?,
                        api_key_ciphertext = ?, api_key_env = ?,
                        last_test_status = 'untested', last_test_detail = '',
                        last_tested_at = NULL, updated_at = ?
                    WHERE model_config_id = ?
                    """,
                    (
                        provider,
                        display_name,
                        base_url,
                        model_name,
                        api_key_ciphertext,
                        api_key_env,
                        timestamp,
                        config_id,
                    ),
                )
        return config_id

    def test_and_activate_model(
        self,
        model_config_id: str,
        *,
        tester=test_model,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        runtime = self.model_runtime(model_config_id)
        try:
            validate_runtime(runtime)
            detail = str(tester(runtime, self.legacy.ai_http_timeout_seconds))
            success = True
        except Exception as exc:
            detail = str(exc)[:500] or type(exc).__name__
            success = False

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ai_model_configs
                SET last_test_status = ?, last_test_detail = ?,
                    last_tested_at = ?, updated_at = ?
                WHERE model_config_id = ?
                """,
                (
                    "passed" if success else "failed",
                    detail,
                    timestamp,
                    timestamp,
                    model_config_id,
                ),
            )
            if success:
                connection.execute(
                    """
                    UPDATE ai_runtime_settings
                    SET enabled = 1, active_model_config_id = ?, updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (model_config_id, timestamp),
                )
        return success, detail

    def set_active_model_config(
        self,
        model_config_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Switch the active AI model without re-running the test.

        The caller must have already validated the model (see
        :meth:`test_and_activate_model`).  This split keeps the activation UX
        independent from a fresh network probe: a user can flip between any
        model that has previously passed the forced web-search check.
        """

        config_id = model_config_id.strip()
        if not config_id:
            raise ValueError("模型配置 ID 不能为空")
        timestamp = (now or datetime.now(self.legacy.timezone)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT provider, last_test_status FROM ai_model_configs WHERE model_config_id = ?",
                (config_id,),
            ).fetchone()
            if row is None:
                raise ValueError("模型配置不存在")
            if row["last_test_status"] != "passed":
                raise ValueError(
                    "该模型尚未通过测试，请先点击“测试并启用”验证后再切换"
                )
            spec = PROVIDER_BY_CODE.get(row["provider"])
            if spec is None or not spec.native_web_search or spec.protocol != "responses":
                raise ValueError(
                    "该供应商当前不支持项目要求的强制联网搜索，无法作为当前模型"
                )
            connection.execute(
                """
                UPDATE ai_runtime_settings
                SET enabled = 1, active_model_config_id = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (config_id, timestamp),
            )

    def delete_model_config(self, model_config_id: str) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runtime = connection.execute(
                "SELECT active_model_config_id FROM ai_runtime_settings WHERE singleton_id = 1"
            ).fetchone()
            if runtime and runtime["active_model_config_id"] == model_config_id:
                raise ValueError("当前启用模型不能删除，请先测试并启用另一个模型")
            cursor = connection.execute(
                "DELETE FROM ai_model_configs WHERE model_config_id = ?",
                (model_config_id,),
            )
            return cursor.rowcount > 0
