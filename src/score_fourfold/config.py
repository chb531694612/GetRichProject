from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .auth import valid_password_hash


WEB_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a small dotenv file without overwriting real environment variables."""
    env_path = Path(path).expanduser()
    if not env_path.is_absolute():
        env_path = (Path.cwd() / env_path).resolve()
    if not env_path.exists():
        return
    os.environ.setdefault("SCORE_FOURFOLD_BASE_DIR", str(env_path.parent))
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    value = float(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = Path(os.getenv("SCORE_FOURFOLD_BASE_DIR", str(Path.cwd()))).expanduser().resolve()
    return (base / path).resolve()


def _time(name: str, default: str) -> time:
    raw = os.getenv(name, default).strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must use HH:MM, for example 17:30") from exc


def _times(name: str, default: str) -> tuple[time, ...]:
    raw_values = [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]
    if not raw_values:
        raise ValueError(f"{name} must contain at least one HH:MM value")
    parsed = tuple(sorted({_time_value(value, name) for value in raw_values}))
    return parsed


def _time_value(raw: str, name: str) -> time:
    try:
        hour_text, minute_text = raw.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain comma-separated HH:MM values") from exc


def _pass_sizes(name: str, default: str, *, allowed: set[int]) -> tuple[int, ...]:
    raw_values = [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]
    if not raw_values:
        raise ValueError(f"{name} must contain at least one pass size")
    parsed: list[int] = []
    for value in raw_values:
        try:
            size = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must contain comma-separated integers") from exc
        if size not in allowed:
            raise ValueError(f"{name} values must be one of {sorted(allowed)}")
        if size not in parsed:
            parsed.append(size)
    return tuple(sorted(parsed, reverse=True))



@dataclass(frozen=True, slots=True)
class Settings:
    data_provider: str
    sporttery_odds_url: str
    sporttery_results_url: str
    okooo_base_url: str
    json_data_file: Path
    timezone_name: str
    automatic_analysis_enabled: bool
    poisson_model_weight: float
    min_lead_minutes: int
    max_lookahead_hours: int
    max_odds_age_minutes: int
    min_score_probability: float
    min_joint_probability: float
    max_matches_per_league: int
    allow_other_scores: bool
    max_plans_per_business_date: int
    send_no_recommendation: bool
    recommendation_times: tuple[time, ...]
    recommendation_latest_start: time
    recommendation_deadline: time
    recommendation_send_buffer_minutes: int
    had_enabled: bool
    had_pass_sizes: tuple[int, ...]
    min_had_probability: float
    min_had_joint_probability: float
    database_path: Path
    poll_interval_seconds: int
    result_check_delay_minutes: int
    http_timeout_seconds: int
    web_enabled: bool
    web_host: str
    web_port: int
    web_access_mode: str
    web_public_origin: str
    web_username: str
    web_password_hash: str
    web_trust_proxy_headers: bool
    web_session_hours: int
    mail_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_auth_code: str
    mail_from: str
    mail_dry_run: bool
    mail_preview_dir: Path
    deepseek_api_key: str
    deepseek_api_url: str
    deepseek_model: str
    ai_analysis_enabled: bool

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @classmethod
    def from_env(cls) -> "Settings":
        data_provider = os.getenv("DATA_PROVIDER", "sporttery").strip().lower()
        if data_provider not in {"sporttery", "json", "okooo"}:
            raise ValueError("DATA_PROVIDER must be sporttery, json, or okooo")
        max_plans_per_business_date = _int("MAX_PLANS_PER_BUSINESS_DATE", 1, minimum=1)
        if max_plans_per_business_date != 1:
            raise ValueError("each market still allows at most one plan per recommendation day")
        settings = cls(
            data_provider=data_provider,
            sporttery_odds_url=os.getenv(
                "SPORTTERY_ODDS_URL",
                "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry",
            ).strip(),
            sporttery_results_url=os.getenv(
                "SPORTTERY_RESULTS_URL",
                "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry",
            ).strip(),
            okooo_base_url=os.getenv(
                "OKOOO_BASE_URL",
                "https://www.okooo.com",
            ).strip().rstrip("/"),
            json_data_file=_path(os.getenv("JSON_DATA_FILE", "examples/demo-data.json")),
            timezone_name=os.getenv("TIMEZONE", "Asia/Shanghai").strip(),
            automatic_analysis_enabled=_bool("AUTOMATIC_ANALYSIS_ENABLED", True),
            poisson_model_weight=_float("POISSON_MODEL_WEIGHT", 0.35, minimum=0.0),
            min_lead_minutes=_int("MIN_LEAD_MINUTES", 60, minimum=0),
            max_lookahead_hours=_int("MAX_LOOKAHEAD_HOURS", 48, minimum=1),
            max_odds_age_minutes=_int("MAX_ODDS_AGE_MINUTES", 120, minimum=1),
            min_score_probability=_float("MIN_SCORE_PROBABILITY", 0.02, minimum=0.0),
            min_joint_probability=_float("MIN_JOINT_PROBABILITY", 0.0001, minimum=0.0),
            max_matches_per_league=_int("MAX_MATCHES_PER_LEAGUE", 2, minimum=1),
            allow_other_scores=_bool("ALLOW_OTHER_SCORES", False),
            max_plans_per_business_date=max_plans_per_business_date,
            send_no_recommendation=_bool("SEND_NO_RECOMMENDATION", True),
            recommendation_times=_times("RECOMMENDATION_TIMES", "10:00,14:00,17:30"),
            recommendation_latest_start=_time("RECOMMENDATION_LATEST_START", "17:45"),
            recommendation_deadline=_time("RECOMMENDATION_DEADLINE", "18:00"),
            recommendation_send_buffer_minutes=_int(
                "RECOMMENDATION_SEND_BUFFER_MINUTES", 10, minimum=0
            ),
            had_enabled=_bool("HAD_ENABLED", True),
            had_pass_sizes=_pass_sizes("HAD_PASS_SIZES", "6,5,4", allowed={4, 5, 6}),
            min_had_probability=_float("MIN_HAD_PROBABILITY", 0.28, minimum=0.0),
            min_had_joint_probability=_float("MIN_HAD_JOINT_PROBABILITY", 0.01, minimum=0.0),
            database_path=_path(os.getenv("DATABASE_PATH", "data/score_fourfold.db")),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 1800, minimum=60),
            result_check_delay_minutes=_int("RESULT_CHECK_DELAY_MINUTES", 150, minimum=90),
            http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", 20, minimum=1),
            web_enabled=_bool("WEB_ENABLED", True),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            web_port=_int("WEB_PORT", 8080, minimum=1),
            web_access_mode=os.getenv("WEB_ACCESS_MODE", "ssh").strip().lower(),
            web_public_origin=os.getenv("WEB_PUBLIC_ORIGIN", "").strip().rstrip("/"),
            web_username=os.getenv("WEB_USERNAME", "owner").strip(),
            web_password_hash=os.getenv("WEB_PASSWORD_HASH", "").strip(),
            web_trust_proxy_headers=_bool("WEB_TRUST_PROXY_HEADERS", False),
            web_session_hours=_int("WEB_SESSION_HOURS", 12, minimum=1),
            mail_to=os.getenv("MAIL_TO", "531694612@qq.com").strip(),
            smtp_host=os.getenv("SMTP_HOST", "smtp.qq.com").strip(),
            smtp_port=_int("SMTP_PORT", 465, minimum=1),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_auth_code=os.getenv("SMTP_AUTH_CODE", "").strip(),
            mail_from=os.getenv("MAIL_FROM", "").strip(),
            mail_dry_run=_bool("MAIL_DRY_RUN", True),
            mail_preview_dir=_path(os.getenv("MAIL_PREVIEW_DIR", "data/mail-preview")),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            deepseek_api_url=os.getenv(
                "DEEPSEEK_API_URL",
                "https://api.deepseek.com/v1/chat/completions",
            ).strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
            ai_analysis_enabled=_bool("AI_ANALYSIS_ENABLED", False),
        )
        # Construct once so an invalid timezone fails at startup.
        settings.timezone
        if settings.timezone_name != "Asia/Shanghai":
            raise ValueError("TIMEZONE must be Asia/Shanghai so the 18:00 deadline is safe")
        if settings.recommendation_times[-1] >= settings.recommendation_latest_start:
            raise ValueError("the last RECOMMENDATION_TIMES value must be before RECOMMENDATION_LATEST_START")
        if settings.recommendation_latest_start >= settings.recommendation_deadline:
            raise ValueError("RECOMMENDATION_LATEST_START must be before RECOMMENDATION_DEADLINE")
        cutoff_minutes = (
            settings.recommendation_deadline.hour * 60
            + settings.recommendation_deadline.minute
            - settings.recommendation_send_buffer_minutes
        )
        last_slot_minutes = settings.recommendation_times[-1].hour * 60 + settings.recommendation_times[-1].minute
        if cutoff_minutes <= last_slot_minutes:
            raise ValueError("the recommendation mail cutoff must be after the final recommendation time")
        if settings.min_score_probability > 1 or settings.min_joint_probability > 1:
            raise ValueError("probability thresholds must be <= 1")
        if settings.min_had_probability > 1 or settings.min_had_joint_probability > 1:
            raise ValueError("HAD probability thresholds must be <= 1")
        if settings.poisson_model_weight > 1:
            raise ValueError("POISSON_MODEL_WEIGHT must be <= 1")
        if settings.ai_analysis_enabled:
            if not settings.deepseek_api_key:
                raise ValueError("AI_ANALYSIS_ENABLED=true requires DEEPSEEK_API_KEY")
            ai_url = urlsplit(settings.deepseek_api_url)
            if ai_url.scheme != "https" or not ai_url.netloc:
                raise ValueError("DEEPSEEK_API_URL must be an https URL")
            if not settings.deepseek_model:
                raise ValueError("DEEPSEEK_MODEL must not be empty")
        if settings.web_port > 65535:
            raise ValueError("WEB_PORT must be <= 65535")
        if not settings.web_host:
            raise ValueError("WEB_HOST must not be empty")
        if settings.web_access_mode not in {"ssh", "public"}:
            raise ValueError("WEB_ACCESS_MODE must be ssh or public")
        if settings.web_session_hours > 168:
            raise ValueError("WEB_SESSION_HOURS must be <= 168")
        if settings.web_access_mode == "public":
            if not settings.web_trust_proxy_headers:
                raise ValueError("public web access requires WEB_TRUST_PROXY_HEADERS=true")
            if settings.web_host != "0.0.0.0":
                raise ValueError("public web access requires WEB_HOST=0.0.0.0 for the Caddy container")
            if not WEB_USERNAME.fullmatch(settings.web_username):
                raise ValueError(
                    "WEB_USERNAME must contain 1-64 letters, digits, dots, underscores or hyphens"
                )
            if not valid_password_hash(settings.web_password_hash):
                raise ValueError("public web access requires a valid WEB_PASSWORD_HASH")
            try:
                public_origin = urlsplit(settings.web_public_origin)
            except ValueError as exc:
                raise ValueError("WEB_PUBLIC_ORIGIN must be a valid https origin") from exc
            if (
                public_origin.scheme != "https"
                or not public_origin.hostname
                or public_origin.username is not None
                or public_origin.password is not None
                or public_origin.path
                or public_origin.query
                or public_origin.fragment
            ):
                raise ValueError(
                    "WEB_PUBLIC_ORIGIN must contain only https://host or https://host:port"
                )
            try:
                public_port = public_origin.port
            except ValueError as exc:
                raise ValueError("WEB_PUBLIC_ORIGIN contains an invalid port") from exc
            if public_port not in {None, 443}:
                raise ValueError("WEB_PUBLIC_ORIGIN must use the default HTTPS port 443")
            try:
                public_ip = ipaddress.ip_address(public_origin.hostname)
            except ValueError as exc:
                raise ValueError("WEB_PUBLIC_ORIGIN host must be the server public IPv4 address") from exc
            if public_ip.version != 4 or not public_ip.is_global:
                raise ValueError("WEB_PUBLIC_ORIGIN host must be the server public IPv4 address")
        return settings

    def validate_mail(self) -> list[str]:
        errors: list[str] = []
        if not self.mail_to:
            errors.append("MAIL_TO is empty")
        if not self.mail_dry_run:
            if not self.smtp_username:
                errors.append("SMTP_USERNAME is empty")
            if not self.smtp_auth_code:
                errors.append("SMTP_AUTH_CODE is empty")
        return errors
