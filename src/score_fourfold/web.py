from __future__ import annotations

import gzip
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .auth import verify_password
from .config import Settings
from .ai_analyzer import AIAnalysisError, analyze_plan_from_leg_data
from .api import DashboardAPI, serialize_plan
from .database import Database, StoredPlan
from .domain import MarketType, PlanStatus
from .mail import render_stored_recommendation
from .settings_store import SettingsRepository


LOGGER = logging.getLogger("score_fourfold.web")
MAX_FORM_BYTES = 4096
MAX_JSON_BYTES = 32768
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,100}$")
SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")
COOKIE_NAME = "__Host-score_session"
LOGIN_TOKEN_MAX_AGE_SECONDS = 600
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
MAX_LOGIN_CLIENTS = 1024
MAX_SESSIONS = 128

LOGIN_STYLE = """
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#172033;background:#f4f6f9;--blue:#246bfd;--muted:#667085;--line:#e4e9f0;--card:#fff}
*{box-sizing:border-box}body{margin:0}button,input{font:inherit}.login-wrap{max-width:430px;margin:9vh auto 0;padding:18px}.login-card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px;box-shadow:0 8px 28px rgba(30,45,70,.08)}.login-card h1{margin:0 0 6px}.field{margin-top:18px}.field label{display:block;font-weight:700;margin-bottom:6px}.field input{width:100%;border:1px solid #cfd6e1;border-radius:10px;padding:11px 12px}.login-card button{width:100%;margin-top:22px;border:0;border-radius:10px;padding:11px 14px;background:var(--blue);color:#fff;font-weight:700}.muted{color:var(--muted)}.small{font-size:12px}.flash{border:1px solid #fecdca;border-radius:10px;padding:12px;margin-bottom:12px;background:#fef3f2;color:#b42318}
"""

def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


@dataclass(frozen=True, slots=True)
class WebSession:
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BackgroundTask:
    status: str
    level: str
    detail: str
    started_at: datetime
    finished_at: datetime | None = None


def _origin(value: str) -> tuple[str, str] | None:
    """Return a normalized (scheme, netloc) only for an origin without a path."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return parsed.scheme.lower(), netloc


def _referer_origin(value: str) -> tuple[str, str] | None:
    """Extract an origin from a Referer, which normally includes a path."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return parsed.scheme.lower(), netloc


class DashboardApplication:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        trigger_recommendation: Callable[[str], tuple[str, str]],
        secret: bytes | None = None,
        provider: object | None = None,
        wake_mailer: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        trigger_settle: Callable[[], tuple[str, str]] | None = None,
        trigger_settle_plan: Callable[[str], tuple[str, str]] | None = None,
        settings_repository: SettingsRepository | None = None,
    ):
        self.settings = settings
        self.database = database
        self.trigger_recommendation = trigger_recommendation
        self.provider = provider
        self.wake_mailer = wake_mailer
        self.trigger_settle = trigger_settle
        self.trigger_settle_plan = trigger_settle_plan
        self.settings_repository = settings_repository
        self.ticket_image_dir = Path(getattr(settings, "ticket_image_dir", "data/ticket-images"))
        self._clock = clock or (lambda: datetime.now(self.settings.timezone))
        self._secret = secret or secrets.token_bytes(32)
        self.access_mode = getattr(settings, "web_access_mode", "ssh")
        self.public_origin = getattr(settings, "web_public_origin", "").rstrip("/")
        self.username = getattr(settings, "web_username", "")
        self.password_hash = getattr(settings, "web_password_hash", "")
        self.trust_proxy_headers = bool(getattr(settings, "web_trust_proxy_headers", False))
        self.session_hours = int(getattr(settings, "web_session_hours", 12))
        self._lock = threading.Lock()
        self._sessions: dict[str, WebSession] = {}
        self._login_failures: dict[str, list[datetime]] = {}
        self._password_workers = threading.BoundedSemaphore(2)
        self._recommendation_task: BackgroundTask | None = None
        self._settle_task: BackgroundTask | None = None
        self._analysis_tasks: dict[str, BackgroundTask] = {}
        # Only one plan's AI analysis may call the model at a time.  DeepSeek /
        # Qwen thinking models exhaust their output budget when several plans
        # are analyzed concurrently, which produces empty/incomplete responses.
        # Queued plans wait on this semaphore inside their own worker thread.
        self._ai_analysis_semaphore = threading.BoundedSemaphore(1)
        # Upper bound on plans waiting behind a running AI analysis.
        self._ai_analysis_max_queue = 3
        if self.public_mode:
            parsed_origin = _origin(self.public_origin)
            if parsed_origin is None or parsed_origin[0] != "https":
                raise ValueError("public dashboard requires WEB_PUBLIC_ORIGIN=https://host[:port]")
            if not self.username or not self.password_hash:
                raise ValueError("public dashboard requires WEB_USERNAME and WEB_PASSWORD_HASH")
            if not self.trust_proxy_headers:
                raise ValueError("public dashboard requires WEB_TRUST_PROXY_HEADERS=true")
            self._public_scheme, self._public_netloc = parsed_origin
        else:
            self._public_scheme, self._public_netloc = "", ""

    @property
    def public_mode(self) -> bool:
        return self.access_mode == "public"

    def now(self) -> datetime:
        return self._clock().astimezone(self.settings.timezone)

    def _refresh_mail_after_plan_change(self, plan_id: str) -> str:
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return "；计划已不存在，未生成更新邮件"
        changed_at = self.now()
        recommendation_day = datetime.fromisoformat(plan.recommendation_date).date()
        first_send_at = datetime.combine(
            recommendation_day,
            self.settings.recommendation_first_mail_time,
            tzinfo=self.settings.timezone,
        )
        deadline = datetime.combine(
            recommendation_day,
            self.settings.recommendation_deadline,
            tzinfo=self.settings.timezone,
        )
        expires_at = deadline - timedelta(
            minutes=self.settings.recommendation_send_buffer_minutes
        )
        subject, text_body, html_body = render_stored_recommendation(plan)
        result = self.database.refresh_recommendation_mail(
            plan_id,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            changed_at=changed_at,
            first_send_at=first_send_at,
            expires_at=expires_at,
        )
        if result in {"refreshed", "queued"} and self.wake_mailer is not None:
            self.wake_mailer()
        return {
            "refreshed": "；尚未发出的首封推荐邮件已同步更新",
            "queued": "；最新版推荐邮件已重新排队发送",
            "expired": "；已超过当天推荐邮件截止时间，未重新发送",
            "missing": "；计划已不存在，未生成更新邮件",
        }[result]

    def new_request(self) -> tuple[str, str]:
        request_id = secrets.token_urlsafe(24)
        signature = hmac.new(self._secret, request_id.encode("ascii"), hashlib.sha256).hexdigest()
        return request_id, signature

    def verify_request(self, request_id: str, signature: str) -> bool:
        if not REQUEST_ID.fullmatch(request_id):
            return False
        expected = hmac.new(self._secret, request_id.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def queue_recommendation(self, request_id: str) -> tuple[str, str]:
        """Start a manual recommendation in the background and return immediately."""
        started_at = self.now()
        with self._lock:
            if (
                self._recommendation_task is not None
                and self._recommendation_task.status == "running"
            ):
                return (
                    "warn",
                    "今日全部推荐正在后台生成，请勿重复提交；预计 1–40 分钟后刷新查看。",
                )
            self._recommendation_task = BackgroundTask(
                "running",
                "warn",
                "今日全部推荐正在后台生成；预计 1–40 分钟后刷新查看。",
                started_at,
            )
        threading.Thread(
            target=self._run_recommendation_task,
            args=(request_id, started_at),
            name=f"manual-recommend-{request_id[:12]}",
            daemon=True,
        ).start()
        return (
            "ok",
            "已提交后台生成，无需停留在当前页面；预计 1–40 分钟后回来刷新查看。",
        )

    def _run_recommendation_task(self, request_id: str, started_at: datetime) -> None:
        level = "error"
        detail = "手动推荐后台执行异常，错误通知已进入邮件队列。"
        try:
            status, detail = self.trigger_recommendation(request_id)
            level = (
                "ok"
                if status in {"created", "duplicate"}
                else (
                    "warn"
                    if status
                    in {
                        "busy",
                        "cooldown",
                        "closed",
                        "no-recommendation",
                        "partial",
                    }
                    else "error"
                )
            )
        except Exception:
            LOGGER.exception("background manual recommendation failed")
        finished_at = self.now()
        with self._lock:
            current = self._recommendation_task
            if current is not None and current.started_at == started_at:
                self._recommendation_task = BackgroundTask(
                    "finished", level, detail, started_at, finished_at
                )

    def queue_ai_analysis(self, plan_id: str) -> tuple[str, str]:
        """Start one plan's AI analysis in the background and return immediately."""
        if self.database.get_plan(plan_id) is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if self.settings_repository is not None:
            try:
                if self.settings_repository.active_model_runtime() is None:
                    return ("warn", "尚未启用可用的大模型，请先在设置中保存并通过调用测试")
            except ValueError as exc:
                return ("warn", f"大模型配置不可用：{exc}")
        else:
            if not self.settings.ai_analysis_enabled:
                return ("warn", "AI分析未启用，请设置 QWEN_API_KEY 并开启 AI_ANALYSIS_ENABLED")
            if not self.settings.qwen_api_key:
                return ("warn", "未配置 QWEN_API_KEY")
        started_at = self.now()
        with self._lock:
            current = self._analysis_tasks.get(plan_id)
            if current is not None and current.status in {"running", "queued"}:
                return (
                    "warn",
                    f"计划 {plan_id} 正在后台进行 AI 分析（或已在队列中），请勿重复提交；预计 1–10 分钟后刷新查看。",
                )
            # Serialize model calls across plans: only one analysis may run at
            # a time, the rest wait in a bounded queue so a burst of manual
            # clicks can never overwhelm the upstream model with concurrent
            # thinking-mode requests.
            active = any(
                task.status in {"running", "queued"}
                for task in self._analysis_tasks.values()
            )
            waiting = sum(
                1
                for task in self._analysis_tasks.values()
                if task.status == "queued"
            )
            if active and waiting >= self._ai_analysis_max_queue:
                return (
                    "warn",
                    f"AI 分析队列已满（{self._ai_analysis_max_queue} 个任务等待中），请稍后再试。",
                )
            if active:
                status, level, detail = (
                    "queued",
                    "warn",
                    f"计划 {plan_id} 已加入 AI 分析队列，排在 {waiting + 1} 个任务之后；请稍后刷新查看。",
                )
            else:
                status, level, detail = (
                    "running",
                    "warn",
                    f"计划 {plan_id} 正在后台进行 AI 分析；预计 1–10 分钟后刷新查看。",
                )
            self._analysis_tasks[plan_id] = BackgroundTask(
                status, level, detail, started_at
            )
        threading.Thread(
            target=self._run_ai_analysis_task,
            args=(plan_id, started_at),
            name=f"ai-analysis-{plan_id[:12]}",
            daemon=True,
        ).start()
        if active:
            self.database.add_log(
                "ai",
                f"计划{plan_id}加入AI分析队列",
                f"排在{waiting + 1}个任务之后",
                plan_id,
            )
            return ("warn", detail)
        return (
            "ok",
            f"计划 {plan_id} 已提交后台 AI 分析，无需等待；预计 1–10 分钟后回来刷新查看。",
        )

    def _run_ai_analysis_task(self, plan_id: str, started_at: datetime) -> None:
        level = "error"
        detail = f"计划 {plan_id} 的 AI 分析后台执行异常，请查看日志。"
        try:
            # Wait for any previously queued analysis to finish.  The thread
            # blocks here (daemon), so concurrent submissions never call the
            # model simultaneously.
            acquired = self._ai_analysis_semaphore.acquire(timeout=3600)
            if not acquired:
                detail = f"计划 {plan_id} 的 AI 分析等待超时（超过 1 小时仍未轮到），请重新提交。"
                with self._lock:
                    current = self._analysis_tasks.get(plan_id)
                    if current is not None and current.started_at == started_at:
                        self._analysis_tasks[plan_id] = BackgroundTask(
                            "finished", "error", detail, started_at, self.now()
                        )
                return
            try:
                # 拿到执行权后把排队任务升级为 running，前端可即时感知进展。
                with self._lock:
                    current = self._analysis_tasks.get(plan_id)
                    if current is not None and current.status == "queued":
                        self._analysis_tasks[plan_id] = BackgroundTask(
                            "running",
                            "warn",
                            f"计划 {plan_id} 正在后台进行 AI 分析；预计 1–10 分钟后刷新查看。",
                            started_at,
                        )
                        self.database.add_log(
                            "ai",
                            f"计划{plan_id}开始AI分析",
                            "前序任务已完成，本任务开始执行",
                            plan_id,
                        )
                level, detail = self.trigger_ai_analysis(plan_id)
            finally:
                self._ai_analysis_semaphore.release()
        except Exception:
            LOGGER.exception("background AI analysis of plan %s failed", plan_id)
        finished_at = self.now()
        with self._lock:
            current = self._analysis_tasks.get(plan_id)
            if current is not None and current.started_at == started_at:
                self._analysis_tasks[plan_id] = BackgroundTask(
                    "finished", level, detail, started_at, finished_at
                )

    def recommendation_task(self) -> BackgroundTask | None:
        with self._lock:
            return self._recommendation_task

    def queue_settle(self) -> tuple[str, str]:
        """Start a manual settlement in the background and return immediately."""
        if self.trigger_settle is None:
            return ("warn", "赛果手动更新未配置")
        started_at = self.now()
        with self._lock:
            if (
                self._settle_task is not None
                and self._settle_task.status == "running"
            ):
                return (
                    "warn",
                    "赛果更新正在后台执行，请勿重复提交；预计 1–5 分钟后刷新查看。",
                )
            self._settle_task = BackgroundTask(
                "running",
                "warn",
                "正在后台更新赛果并结算计划；预计 1–5 分钟后刷新查看。",
                started_at,
            )
        threading.Thread(
            target=self._run_settle_task,
            args=(started_at,),
            name="manual-settle",
            daemon=True,
        ).start()
        return (
            "ok",
            "已提交后台赛果更新，无需等待；预计 1–5 分钟后刷新查看。",
        )

    def _run_settle_task(self, started_at: datetime) -> None:
        level = "error"
        detail = "赛果更新后台执行异常。"
        try:
            level, detail = self.trigger_settle()
        except Exception:
            LOGGER.exception("background settle failed")
        finished_at = self.now()
        with self._lock:
            current = self._settle_task
            if current is not None and current.started_at == started_at:
                self._settle_task = BackgroundTask(
                    "finished", level, detail, started_at, finished_at
                )

    def settle_task(self) -> BackgroundTask | None:
        with self._lock:
            return self._settle_task

    def queue_settle_plan(self, plan_id: str) -> tuple[str, str]:
        """Start a per-plan settlement in the background and return immediately."""
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if plan.status not in {PlanStatus.PENDING, PlanStatus.VOID}:
            return ("warn", f"计划 {plan_id} 已结算，无需重复更新")
        if self.trigger_settle_plan is None:
            return ("warn", "赛果手动更新未配置")
        started_at = self.now()
        with self._lock:
            current = self._analysis_tasks.get(f"settle-{plan_id}")
            if current is not None and current.status == "running":
                return (
                    "warn",
                    f"计划 {plan_id} 的赛果更新正在后台执行，请勿重复提交。",
                )
            self._analysis_tasks[f"settle-{plan_id}"] = BackgroundTask(
                "running",
                "warn",
                f"计划 {plan_id} 正在后台更新赛果并尝试结算。",
                started_at,
            )
        threading.Thread(
            target=self._run_settle_plan_task,
            args=(plan_id, started_at),
            name=f"settle-plan-{plan_id[:12]}",
            daemon=True,
        ).start()
        return (
            "ok",
            f"已提交后台更新计划 {plan_id} 的赛果，无需等待；预计 1–3 分钟后查看结果。",
        )

    def _run_settle_plan_task(self, plan_id: str, started_at: datetime) -> None:
        level = "error"
        detail = f"计划 {plan_id} 的赛果更新后台执行异常。"
        try:
            level, detail = self.trigger_settle_plan(plan_id)
        except Exception:
            LOGGER.exception("background per-plan settle failed for %s", plan_id)
        finished_at = self.now()
        with self._lock:
            current = self._analysis_tasks.get(f"settle-{plan_id}")
            if current is not None and current.started_at == started_at:
                self._analysis_tasks[f"settle-{plan_id}"] = BackgroundTask(
                    "finished", level, detail, started_at, finished_at
                )

    def trigger_mark_purchased(self, plan_id: str, purchased: bool) -> tuple[str, str]:
        """Mark or unmark a plan as purchased. Purchased pending plans are
        locked from leg adjustments until purchase is cancelled."""
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if not self.database.set_purchased(plan_id, purchased):
            return ("error", f"更新计划 {plan_id} 购买标记失败")
        if purchased:
            self.database.add_log("recommend", f"计划{plan_id}标记为已购买，已锁定调整")
            return ("ok", f"已标记购买计划 {plan_id}，场次调整已锁定")
        if plan.status in {PlanStatus.PENDING, PlanStatus.VOID}:
            self.database.add_log("recommend", f"计划{plan_id}取消购买，恢复可调整")
            return ("ok", f"已取消购买标记计划 {plan_id}，场次调整已恢复")
        return ("ok", f"已取消购买标记计划 {plan_id}")

    def trigger_upload_ticket(self, plan_id: str, filename: str, data: bytes) -> tuple[str, str]:
        """Save a ticket image for a plan."""
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if len(data) > 2 * 1024 * 1024:
            return ("warn", "图片大小不能超过 2MB")
        ext = self._image_extension(data, filename)
        if not ext:
            return ("warn", "仅支持 JPG、PNG、GIF、WebP 格式的图片")
        safe_name = f"{plan_id.replace('/', '_')}.{ext}"
        self.ticket_image_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.ticket_image_dir / safe_name
        image_path.write_bytes(data)
        if self.database.set_ticket_image(plan_id, safe_name):
            return ("ok", f"实票图片已上传并标记购买计划 {plan_id}")
        return ("error", f"保存计划 {plan_id} 的实票图片失败")

    @staticmethod
    def _image_extension(data: bytes, filename: str) -> str | None:
        """Determine a safe image extension from the file content via magic bytes."""
        if len(data) >= 3 and data[:3] == b'\xff\xd8\xff':
            return "jpg"
        if len(data) >= 8 and data[:8] == b'\x89PNG\r\n\x1a\n':
            return "png"
        if len(data) >= 6 and data[:6] in (b'GIF87a', b'GIF89a'):
            return "gif"
        if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return "webp"
        lowered = filename.lower()
        for ext in ("jpg", "jpeg", "png", "gif", "webp"):
            if lowered.endswith(f".{ext}"):
                return "jpg" if ext == "jpeg" else ext
        return None

    def analysis_task(self, plan_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._analysis_tasks.get(plan_id)

    def trigger_delete_plan(self, plan_id: str) -> tuple[str, str]:
        """Delete entire plan and return (level, detail)."""
        task = self.analysis_task(plan_id)
        if task is not None and task.status == "running":
            return ("warn", f"计划 {plan_id} 正在进行 AI 分析，请完成后再删除")
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        deleted = self.database.delete_plan(plan_id)
        if deleted:
            return ("ok", f"已删除计划 {plan_id}")
        return ("error", f"删除计划 {plan_id} 失败")

    def trigger_delete_leg(self, plan_id: str, match_id: str) -> tuple[str, str]:
        """Delete a single leg from a plan and recalculate stats."""
        task = self.analysis_task(plan_id)
        if task is not None and task.status == "running":
            return ("warn", f"计划 {plan_id} 正在进行 AI 分析，请完成后再修改")
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if plan.purchased and plan.status in {PlanStatus.PENDING, PlanStatus.VOID}:
            return ("warn", f"计划 {plan_id} 已标记购买，场次调整已锁定，请先取消购买标记")
        if len(plan.legs) <= 1:
            return ("warn", "计划只剩一场比赛，请使用删除整张计划")
        deleted = self.database.delete_plan_leg(plan_id, match_id)
        if not deleted:
            return ("warn", f"未找到比赛 {match_id}")
        self.database.update_plan_after_leg_delete(plan_id)
        mail_detail = self._refresh_mail_after_plan_change(plan_id)
        return ("ok", f"已从计划 {plan_id} 中删除比赛 {match_id}，统计数据已更新{mail_detail}")

    def trigger_ai_analysis(self, plan_id: str) -> tuple[str, str]:
        """Run AI analysis and persist one validated suggestion per plan leg."""
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        ai_runtime = None
        if self.settings_repository is not None:
            try:
                ai_runtime = self.settings_repository.active_model_runtime()
            except ValueError as exc:
                return ("warn", f"大模型配置不可用：{exc}")
            if ai_runtime is None:
                return ("warn", "尚未启用可用的大模型，请先在设置中保存并通过调用测试")
        else:
            if not self.settings.ai_analysis_enabled:
                return ("warn", "AI分析未启用，请设置 QWEN_API_KEY 并开启 AI_ANALYSIS_ENABLED")
            if not self.settings.qwen_api_key:
                return ("warn", "未配置 QWEN_API_KEY")
        self.database.add_log("ai", f"开始AI分析计划{plan_id}", f"{len(plan.legs)}场比赛, 玩法: {plan.market}", plan_id)
        if self.provider is not None:
            get_matches = getattr(self.provider, "get_matches", None)
            if callable(get_matches):
                try:
                    matches = list(get_matches())
                    self.database.replace_plan_leg_options(plan_id, matches)
                    refreshed_plan = self.database.get_plan(plan_id)
                    if refreshed_plan is not None:
                        plan = refreshed_plan
                    self.database.add_log("ai", f"刷新{len(matches)}场比赛赔率数据", plan_id=plan_id)
                except Exception as exc:
                    LOGGER.warning("Could not refresh options for AI plan %s: %s", plan_id, exc)
                    self.database.add_log("ai", f"刷新赔率数据失败: {exc}", plan_id=plan_id)
        unavailable = [leg.match_num for leg in plan.legs if len(leg.options) < 2]
        if unavailable:
            self.database.add_log("ai", f"AI分析中止，比赛{', '.join(unavailable)}可选项不足", plan_id=plan_id)
            return (
                "warn",
                "以下比赛没有足够的真实可选项，暂时无法生成可替换建议："
                + "、".join(unavailable),
            )
        model_name = getattr(ai_runtime, "model_name", None) or self.settings.qwen_model if ai_runtime else self.settings.qwen_model
        self.database.add_log("ai", f"正在调用AI模型({model_name})...", plan_id=plan_id)
        try:
            analysis = analyze_plan_from_leg_data(
                plan.legs,
                plan.market,
                self.settings,
                runtime=ai_runtime,
                history_context=self.database.ai_history_context(plan.market),
            )
            self.database.add_log("ai", f"AI返回{len(analysis.suggestions)}场推荐", plan_id=plan_id)
            stored = self.database.update_ai_analysis(
                plan_id,
                analysis.summary,
                [
                    (
                        suggestion.match_id,
                        suggestion.option_code,
                        "AI预测："
                        + suggestion.pick_label
                        + (f"。{suggestion.reason}" if suggestion.reason else ""),
                    )
                    for suggestion in analysis.suggestions
                ],
            )
        except AIAnalysisError as exc:
            LOGGER.warning("AI recommendation of plan %s failed: %s", plan_id, exc)
            self.database.add_log("ai", f"AI分析失败: {exc}", plan_id=plan_id)
            return ("error", f"AI推荐失败：{exc}")
        except Exception as exc:
            LOGGER.exception("AI analysis of plan %s failed", plan_id)
            self.database.add_log("ai", f"AI分析异常: {exc}", plan_id=plan_id)
            return ("error", f"AI分析失败：{exc}")
        if not stored:
            self.database.add_log("ai", f"AI分析结果未保存，计划{plan_id}已不存在", plan_id=plan_id)
            return ("warn", f"计划 {plan_id} 已不存在，AI结果未保存")
        self.database.add_log("ai", f"AI分析完成，结果已保存", f"计划{plan_id}, {len(analysis.suggestions)}场推荐", plan_id)
        return ("ok", f"AI分析和逐场推荐已完成，请选择是否替换计划 {plan_id}")

    def trigger_update_leg(
        self, plan_id: str, match_id: str, option_code: str
    ) -> tuple[str, str]:
        """Apply a manual or AI-proposed option to one plan leg."""
        task = self.analysis_task(plan_id)
        if task is not None and task.status == "running":
            return ("warn", f"计划 {plan_id} 正在进行 AI 分析，请完成后再修改")
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return ("warn", f"计划 {plan_id} 不存在")
        if plan.purchased and plan.status in {PlanStatus.PENDING, PlanStatus.VOID}:
            return ("warn", f"计划 {plan_id} 已标记购买，场次调整已锁定，请先取消购买标记")
        leg = next((item for item in plan.legs if item.match_id == match_id), None)
        if leg is None:
            return ("warn", f"未找到比赛 {match_id}")
        option = next((item for item in leg.options if item.code == option_code), None)
        if option is None:
            return ("warn", "该推荐选项不存在或已经失效，请重新运行AI分析刷新选项")
        if not self.database.update_plan_leg_option(plan_id, match_id, option_code):
            return ("error", f"修改比赛 {match_id} 的推荐失败")
        # 不再自动刷新邮件；用户可在页面上手动推送
        return (
            "ok",
            '已将比赛 {0} 的推荐修改为 {1}，赔率和奖金已重新计算。如需邮件推送请点击页面上的「推送邮件」按钮。'.format(leg.match_num, option.label),
        )

    def trigger_push_mail(self, plan_id: str) -> tuple[str, str]:
        """Manual push of the recommendation email for a plan."""
        plan = self.database.get_plan(plan_id)
        if plan is None:
            self.database.add_log("mail", f"手动推送失败，计划{plan_id}不存在")
            return ("warn", f"计划 {plan_id} 不存在")
        subject, text_body, html_body = render_stored_recommendation(plan)
        changed_at = self.now()
        recommendation_day = datetime.fromisoformat(plan.recommendation_date).date()
        first_send_at = datetime.combine(
            recommendation_day,
            self.settings.recommendation_first_mail_time,
            tzinfo=self.settings.timezone,
        )
        deadline = datetime.combine(
            recommendation_day,
            self.settings.recommendation_deadline,
            tzinfo=self.settings.timezone,
        )
        expires_at = deadline - timedelta(
            minutes=self.settings.recommendation_send_buffer_minutes
        )
        result = self.database.ensure_recommendation_mail(
            plan_id,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            changed_at=changed_at,
            first_send_at=first_send_at,
            expires_at=expires_at,
        )
        if result in {"refreshed", "queued"} and self.wake_mailer is not None:
            self.wake_mailer()
        self.database.add_log("mail", f"手动推送计划{plan_id}", result)
        labels = {
            "refreshed": "推荐邮件已更新，等待发送",
            "queued": "推荐邮件已排队发送",
            "expired": "已超过当天邮件截止时间，无法推送",
            "missing": "计划不存在",
        }
        return ("ok" if result in {"refreshed", "queued"} else "warn", labels.get(result, result))

    def new_login_token(self) -> str:
        issued_at = int(self.now().timestamp())
        nonce = secrets.token_urlsafe(18)
        payload = f"{issued_at}.{nonce}"
        signature = hmac.new(
            self._secret, f"login:{payload}".encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def verify_login_token(self, token: str) -> bool:
        try:
            timestamp_text, nonce, signature = token.split(".", 2)
            issued_at = int(timestamp_text)
        except (TypeError, ValueError):
            return False
        if not REQUEST_ID.fullmatch(nonce) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            return False
        age = int(self.now().timestamp()) - issued_at
        if age < -60 or age > LOGIN_TOKEN_MAX_AGE_SECONDS:
            return False
        payload = f"{timestamp_text}.{nonce}"
        expected = hmac.new(
            self._secret, f"login:{payload}".encode("ascii"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _prune_locked(self, now: datetime) -> None:
        expired_sessions = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for key in expired_sessions:
            self._sessions.pop(key, None)
        cutoff = now - timedelta(seconds=LOGIN_FAILURE_WINDOW_SECONDS)
        for client, attempts in list(self._login_failures.items()):
            remaining = [attempt for attempt in attempts if attempt > cutoff]
            if remaining:
                self._login_failures[client] = remaining
            else:
                self._login_failures.pop(client, None)

    def login_retry_after(self, client_ip: str) -> int:
        now = self.now()
        with self._lock:
            self._prune_locked(now)
            attempts = self._login_failures.get(client_ip, [])
            if len(attempts) < LOGIN_MAX_FAILURES:
                return 0
            remaining = LOGIN_FAILURE_WINDOW_SECONDS - (now - attempts[0]).total_seconds()
            return max(1, math.ceil(remaining))

    def check_credentials(self, client_ip: str, username: str, password: str) -> tuple[str, int]:
        retry_after = self.login_retry_after(client_ip)
        if retry_after:
            return "limited", retry_after
        if not self._password_workers.acquire(blocking=False):
            return "busy", 2
        try:
            password_ok = verify_password(password, self.password_hash)
            supplied = hashlib.sha256(username.encode("utf-8")).digest()
            configured = hashlib.sha256(self.username.encode("utf-8")).digest()
            username_ok = hmac.compare_digest(supplied, configured)
            valid = password_ok and username_ok
        except (TypeError, ValueError):
            LOGGER.exception("dashboard password verification failed")
            valid = False
        finally:
            self._password_workers.release()

        now = self.now()
        with self._lock:
            self._prune_locked(now)
            if valid:
                self._login_failures.pop(client_ip, None)
                return "ok", 0
            if client_ip not in self._login_failures and len(self._login_failures) >= MAX_LOGIN_CLIENTS:
                oldest = min(
                    self._login_failures,
                    key=lambda key: self._login_failures[key][-1],
                )
                self._login_failures.pop(oldest, None)
            attempts = self._login_failures.setdefault(client_ip, [])
            attempts.append(now)
        return "invalid", 0

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create_session(self) -> tuple[str, WebSession]:
        token = secrets.token_urlsafe(32)
        session = WebSession(
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self.now() + timedelta(hours=self.session_hours),
        )
        with self._lock:
            self._prune_locked(self.now())
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].expires_at)
                self._sessions.pop(oldest, None)
            self._sessions[self._session_key(token)] = session
        return token, session

    def get_session(self, token: str) -> WebSession | None:
        if not SESSION_TOKEN.fullmatch(token):
            return None
        now = self.now()
        with self._lock:
            self._prune_locked(now)
            return self._sessions.get(self._session_key(token))

    def revoke_session(self, token: str) -> None:
        if not SESSION_TOKEN.fullmatch(token):
            return
        with self._lock:
            self._sessions.pop(self._session_key(token), None)

    def session_cookie(self, token: str, *, delete: bool = False) -> str:
        if delete:
            return (
                f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
            )
        max_age = self.session_hours * 60 * 60
        return (
            f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    @staticmethod
    def verify_session_csrf(session: WebSession, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(session.csrf_token, supplied)

    def render_login(self, *, message: str = "", retry_after: int = 0) -> str:
        flash = ""
        if message:
            flash = f'<div class="flash error">{_e(message[:300])}</div>'
        hint = (
            f'<div class="muted small">请等待约 {math.ceil(retry_after / 60)} 分钟后再试。</div>'
            if retry_after
            else '<div class="muted small">请输入服务器管理员为你设置的账号和密码。</div>'
        )
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录个人看板</title>
<style>{LOGIN_STYLE}</style></head><body><main class="login-wrap">{flash}<section class="login-card">
<h1>登录个人看板</h1>{hint}<form method="post" action="/login">
<input type="hidden" name="login_csrf" value="{_e(self.new_login_token())}">
<div class="field"><label for="username">用户名</label><input id="username" name="username" maxlength="64" autocomplete="username" required autofocus></div>
<div class="field"><label for="password">密码</label><input id="password" name="password" type="password" maxlength="512" autocomplete="current-password" required></div>
<button type="submit">登录</button></form></section></main></body></html>"""


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, max_workers: int = 8, **kwargs):
        self._workers = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()


def _loopback_host(raw_host: str) -> bool:
    try:
        hostname = urlsplit(f"//{raw_host}").hostname
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def build_handler(application: DashboardApplication):
    dashboard_api = DashboardAPI(application)
    dashboard_static = Path(__file__).with_name("static") / "dashboard"
    development_static = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dashboard_static.is_dir() and development_static.is_dir():
        dashboard_static = development_static

    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ScoreDashboard"
        sys_version = ""

        def _headers(
            self,
            content_type: str,
            length: int,
            extra_headers: tuple[tuple[str, str], ...] = (),
            *,
            script_nonce: str = "",
            vary_encoding: bool = False,
        ) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if vary_encoding:
                self.send_header("Vary", "Accept-Encoding")
            script_policy = f"; script-src 'nonce-{script_nonce}'" if script_nonce else ""
            img_policy = "; img-src 'self'" if script_nonce else ""
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                f"frame-ancestors 'none'; base-uri 'none'{script_policy}{img_policy}",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if application.public_mode:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for key, value in extra_headers:
                self.send_header(key, value)

        def _accepts_gzip(self) -> bool:
            encoding = self.headers.get("Accept-Encoding", "")
            return "gzip" in [part.strip().lower() for part in encoding.split(",")]

        def _gzip_payload(self, payload: bytes, min_length: int = 1024) -> tuple[bytes, tuple[tuple[str, str], ...], bool]:
            if len(payload) < min_length or not self._accepts_gzip():
                return payload, (), False
            compressed = gzip.compress(payload, compresslevel=6)
            if len(compressed) >= len(payload):
                return payload, (), False
            return compressed, (("Content-Encoding", "gzip"),), True

        def _send(
            self,
            status: int,
            body: str,
            content_type: str = "text/html; charset=utf-8",
            *,
            extra_headers: tuple[tuple[str, str], ...] = (),
            close: bool = False,
        ) -> None:
            payload = body.encode("utf-8")
            payload, encoding_headers, varied = self._gzip_payload(payload)
            nonce_match = re.search(r'<script nonce="([A-Za-z0-9_-]{16,64})">', body)
            script_nonce = nonce_match.group(1) if nonce_match else ""
            self.send_response(status)
            if close:
                extra_headers += (("Connection", "close"),)
                self.close_connection = True
            self._headers(
                content_type,
                len(payload),
                extra_headers + encoding_headers,
                script_nonce=script_nonce,
                vary_encoding=varied,
            )
            self.end_headers()
            self.wfile.write(payload)

        def _redirect_to(
            self,
            location: str,
            *,
            extra_headers: tuple[tuple[str, str], ...] = (),
            close: bool = False,
        ) -> None:
            headers = (("Location", location),) + extra_headers
            self.send_response(303)
            if close:
                headers += (("Connection", "close"),)
                self.close_connection = True
            self._headers("text/plain; charset=utf-8", 0, headers)
            self.end_headers()

        def _serve_dashboard_file(self, relative_path: str) -> bool:
            target = (dashboard_static / relative_path).resolve()
            static_root = dashboard_static.resolve()
            if target != static_root and static_root not in target.parents:
                self._send(404, "<h1>404</h1>")
                return True
            if not target.is_file():
                return False
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(target.suffix.lower(), "application/octet-stream")
            payload = target.read_bytes()
            compressible = content_type.startswith(("text/", "application/javascript", "application/json")) or content_type == "image/svg+xml"
            if compressible:
                payload, encoding_headers, varied = self._gzip_payload(payload)
            else:
                encoding_headers, varied = (), False
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store" if target.suffix == ".html" else "public, max-age=31536000, immutable")
            for key, value in encoding_headers:
                self.send_header(key, value)
            if varied:
                self.send_header("Vary", "Accept-Encoding")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(payload)
            return True

        def _json_response(
            self,
            data: dict,
            status: int = 200,
            *,
            extra_headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            body = json.dumps(data, ensure_ascii=False)
            payload = body.encode("utf-8")
            payload, encoding_headers, varied = self._gzip_payload(payload)
            self.send_response(status)
            self._headers(
                "application/json; charset=utf-8",
                len(payload),
                extra_headers + encoding_headers,
                vary_encoding=varied,
            )
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self) -> dict | None:
            if self.headers.get_all("Transfer-Encoding", []):
                self._json_response({"level": "error", "detail": "不支持分块请求"}, 400)
                return None
            raw_content_type = self._single_header("Content-Type", required=True)
            if raw_content_type is None or raw_content_type.split(";", 1)[0].strip().lower() != "application/json":
                self._discard_request_body()
                self._json_response({"level": "error", "detail": "请求必须使用 JSON"}, 415)
                return None
            raw_length = self._single_header("Content-Length", required=True)
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                self._json_response({"level": "error", "detail": "请求长度无效"}, 400)
                return None
            length = int(raw_length)
            if length <= 0 or length > MAX_JSON_BYTES:
                self._json_response({"level": "error", "detail": "请求内容为空或过大"}, 413)
                return None
            try:
                raw_body = self.rfile.read(length)
                if len(raw_body) != length:
                    raise ValueError("incomplete body")
                value = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._json_response({"level": "error", "detail": "JSON 格式无效"}, 400)
                return None
            if not isinstance(value, dict):
                self._json_response({"level": "error", "detail": "JSON 根节点必须是对象"}, 400)
                return None
            return value

        def _api_session(self) -> WebSession | None:
            if not application.public_mode:
                return None
            authenticated = self._session()
            return authenticated[1] if authenticated is not None else None

        def _api_get(self, parsed) -> None:
            session = self._api_session()
            if application.public_mode and session is None:
                self._json_response({"level": "error", "detail": "登录已失效"}, 401)
                return
            query = parse_qs(parsed.query, keep_blank_values=False)
            try:
                if parsed.path == "/api/v1/bootstrap":
                    data = dashboard_api.bootstrap(session.csrf_token if session else "")
                elif parsed.path == "/api/v1/plans":
                    data = dashboard_api.plans(query)
                elif parsed.path == "/api/v1/calendar":
                    data = dashboard_api.calendar(query)
                elif parsed.path == "/api/v1/settings":
                    data = dashboard_api.settings()
                elif parsed.path == "/api/v1/plan-task":
                    data = dashboard_api.plan_task(query)
                elif parsed.path == "/api/v1/logs":
                    data = dashboard_api.logs(query)
                elif parsed.path.startswith("/api/v1/tasks/"):
                    task_id = parsed.path.removeprefix("/api/v1/tasks/")
                    data = dashboard_api.task(task_id)
                    if data is None:
                        self._json_response({"level": "error", "detail": "任务不存在"}, 404)
                        return
                else:
                    self._json_response({"level": "error", "detail": "接口不存在"}, 404)
                    return
            except (ValueError, RuntimeError) as exc:
                self._json_response({"level": "error", "detail": str(exc)}, 400)
                return
            except Exception:
                LOGGER.exception("dashboard API GET failed: %s", parsed.path)
                self._json_response({"level": "error", "detail": "读取数据失败"}, 500)
                return
            self._json_response({"level": "ok", "data": data})

        def _api_post(self, path: str) -> None:
            authenticated = self._session() if application.public_mode else None
            token = authenticated[0] if authenticated is not None else ""
            session = authenticated[1] if authenticated is not None else None
            if application.public_mode and session is None:
                self._discard_request_body()
                self._json_response({"level": "error", "detail": "登录已失效"}, 401)
                return
            supplied_csrf = self._single_header("X-CSRF-Token", required=True)
            expected_csrf = session.csrf_token if session is not None else "ssh-loopback"
            if supplied_csrf is None or not hmac.compare_digest(supplied_csrf, expected_csrf):
                self._discard_request_body()
                self._json_response({"level": "error", "detail": "操作令牌无效，请刷新页面后重试"}, 403)
                return
            if path == "/api/v1/actions/upload-ticket":
                self._api_post_upload_ticket()
                return
            payload = self._read_json()
            if payload is None:
                return
            if path == "/api/v1/logout":
                if not application.public_mode:
                    self._json_response({"level": "error", "detail": "当前模式无需退出登录"}, 400)
                    return
                application.revoke_session(token)
                self._json_response(
                    {"level": "ok", "detail": "已安全退出", "data": {}},
                    extra_headers=(("Set-Cookie", application.session_cookie("", delete=True)),),
                )
                return
            try:
                level, detail = "ok", "操作完成"
                data: dict[str, object] = {}
                if path == "/api/v1/actions/recommend":
                    request_id = str(payload.get("request_id", ""))
                    signature = str(payload.get("signature", ""))
                    if not application.verify_request(request_id, signature):
                        raise ValueError("推荐操作令牌无效，请刷新页面后重试")
                    level, detail = application.queue_recommendation(request_id)
                elif path == "/api/v1/actions/analyze-plan":
                    level, detail = application.queue_ai_analysis(str(payload.get("plan_id", "")))
                elif path == "/api/v1/actions/settle-plan":
                    level, detail = application.queue_settle_plan(str(payload.get("plan_id", "")))
                elif path == "/api/v1/actions/delete-plan":
                    level, detail = application.trigger_delete_plan(str(payload.get("plan_id", "")))
                elif path == "/api/v1/actions/delete-leg":
                    level, detail = application.trigger_delete_leg(
                        str(payload.get("plan_id", "")), str(payload.get("match_id", ""))
                    )
                elif path == "/api/v1/actions/update-leg":
                    level, detail = application.trigger_update_leg(
                        str(payload.get("plan_id", "")),
                        str(payload.get("match_id", "")),
                        str(payload.get("option_code", "")),
                    )
                elif path == "/api/v1/actions/mark-purchased":
                    level, detail = application.trigger_mark_purchased(
                        str(payload.get("plan_id", "")), bool(payload.get("purchased", False))
                    )
                elif path == "/api/v1/actions/push-mail":
                    level, detail = application.trigger_push_mail(str(payload.get("plan_id", "")))
                elif path == "/api/v1/actions/delete-ticket":
                    level, detail = dashboard_api.delete_ticket(str(payload.get("plan_id", "")))
                elif path.startswith("/api/v1/settings/"):
                    section = path.removeprefix("/api/v1/settings/")
                    if section == "models":
                        data = dashboard_api.save_model(payload)
                        detail = "大模型配置已保存，请执行调用测试后启用"
                    elif section == "model-test":
                        task = dashboard_api.queue_model_test(str(payload.get("model_config_id", "")))
                        data = dashboard_api.serialize_task(task)
                        detail = task.detail
                        level = task.level
                    elif section == "model-delete":
                        dashboard_api.delete_model(str(payload.get("model_config_id", "")))
                        data = dashboard_api.settings()
                        detail = "大模型配置已删除"
                    elif section == "model-activate":
                        data = dashboard_api.activate_model(str(payload.get("model_config_id", "")))
                        detail = "已切换为当前使用的大模型"
                    else:
                        data = dashboard_api.update_settings_section(section, payload)
                        detail = "设置已保存并立即生效"
                else:
                    self._json_response({"level": "error", "detail": "接口不存在"}, 404)
                    return
                if path.startswith("/api/v1/actions/"):
                    plan_id = str(payload.get("plan_id", ""))
                    if plan_id:
                        plan = application.database.get_plan(plan_id)
                        if plan is not None:
                            data["plan"] = serialize_plan(plan)
                        elif path == "/api/v1/actions/delete-plan":
                            data["deleted"] = True
                    data["summary"] = dashboard_api.summary_from_values(
                        payload.get("filters", {})
                    )
            except (ValueError, RuntimeError) as exc:
                self._json_response({"level": "error", "detail": str(exc)}, 400)
                return
            except Exception:
                LOGGER.exception("dashboard API POST failed: %s", path)
                self._json_response({"level": "error", "detail": "操作失败，请查看服务日志"}, 500)
                return
            self._json_response({"level": level, "detail": detail, "data": data})

        def _api_post_upload_ticket(self) -> None:
            """Handle multipart upload for ``/api/v1/actions/upload-ticket``.

            CSRF is already validated via the ``X-CSRF-Token`` header in
            ``_api_post`` before this runs, so only the multipart body and the
            plan/ticket fields need to be checked here.
            """
            fields = self._read_multipart_form()
            if fields is None:
                return
            plan_id_val = fields.get("plan_id", "")
            if not isinstance(plan_id_val, str) or not plan_id_val:
                self._json_response({"level": "error", "detail": "缺少计划编号"}, 400)
                return
            file_field = fields.get("ticket_image")
            if not isinstance(file_field, tuple) or len(file_field) != 2:
                self._json_response({"level": "error", "detail": "未收到图片文件"}, 400)
                return
            filename, file_data = file_field
            level, detail = application.trigger_upload_ticket(plan_id_val, str(filename), file_data)
            data: dict[str, object] = {"summary": dashboard_api.summary_from_values({})}
            plan = application.database.get_plan(plan_id_val)
            if plan is not None:
                data["plan"] = serialize_plan(plan)
            self._json_response({"level": level, "detail": detail, "data": data})

        def _single_header(self, name: str, *, required: bool = False) -> str | None:
            values = self.headers.get_all(name, [])
            if len(values) > 1 or (required and len(values) != 1):
                return None
            if not values:
                return "" if not required else None
            value = values[0].strip()
            return value or None

        def _request_envelope_ok(self) -> bool:
            host = self._single_header("Host", required=True)
            if host is None:
                return False
            if not application.public_mode:
                return _loopback_host(host)
            if "," in host or " " in host:
                return False
            parsed_host = _origin(f"https://{host}")
            if parsed_host != (application._public_scheme, application._public_netloc):
                return False
            proto = self._single_header("X-Forwarded-Proto", required=True)
            if proto != "https":
                return False
            return self._client_ip() is not None

        def _state_origin_ok(self) -> bool:
            host = self._single_header("Host", required=True)
            if host is None:
                return False
            origin_values = self.headers.get_all("Origin", [])
            referer_values = self.headers.get_all("Referer", [])
            if len(origin_values) > 1 or len(referer_values) > 1:
                return False
            candidate = origin_values[0].strip() if origin_values else ""
            if not candidate and referer_values:
                candidate = referer_values[0].strip()
                parsed_candidate = _referer_origin(candidate)
            else:
                parsed_candidate = _origin(candidate) if candidate else None
            if application.public_mode:
                if not candidate:
                    return False
                return parsed_candidate == (
                    application._public_scheme,
                    application._public_netloc,
                )
            if not candidate:
                return True
            parsed = parsed_candidate
            expected = _origin(f"http://{host}")
            return parsed is not None and expected is not None and parsed[1] == expected[1]

        def _client_ip(self) -> str | None:
            if not application.public_mode:
                return str(self.client_address[0])
            values = self.headers.get_all("X-Forwarded-For", [])
            if len(values) != 1:
                return None
            value = values[0].strip()
            if not value or "," in value:
                return None
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                return None

        def _session(self) -> tuple[str, WebSession] | None:
            cookie_headers = self.headers.get_all("Cookie", [])
            if len(cookie_headers) != 1:
                return None
            cookies = SimpleCookie()
            try:
                cookies.load(cookie_headers[0])
            except CookieError:
                return None
            morsel = cookies.get(COOKIE_NAME)
            if morsel is None:
                return None
            token = morsel.value
            session = application.get_session(token)
            return (token, session) if session is not None else None

        def _discard_request_body(self) -> None:
            """Drain a bounded request body so early rejects do not reset the client."""

            if self.headers.get_all("Transfer-Encoding", []):
                return
            raw_length = self._single_header("Content-Length")
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                return
            try:
                length = int(raw_length)
            except ValueError:
                return
            if length <= 0:
                return
            # Cap the drain so a hostile Content-Length cannot stall a worker forever.
            remaining = min(length, MAX_FORM_BYTES)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(2048, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                return

        def _read_form(self) -> dict[str, list[str]] | None:
            if self.headers.get_all("Transfer-Encoding", []):
                self._send(400, "<h1>400</h1>", close=True)
                return None
            raw_content_type = self._single_header("Content-Type", required=True)
            if raw_content_type is None:
                self._discard_request_body()
                self._send(400, "<h1>400</h1>", close=True)
                return None
            content_type = raw_content_type.split(";", 1)[0].strip().lower()
            if content_type != "application/x-www-form-urlencoded":
                self._discard_request_body()
                self._send(415, "<h1>415</h1>", close=True)
                return None
            raw_length = self._single_header("Content-Length", required=True)
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                self._send(400, "<h1>400</h1>", close=True)
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            if length <= 0 or length > MAX_FORM_BYTES:
                # Oversized bodies are rejected without draining the rest of an
                # attacker-controlled stream; Connection: close still ends the request.
                self._send(413, "<h1>413</h1>", close=True)
                return None
            try:
                raw_body = self.rfile.read(length)
                if len(raw_body) != length:
                    self._send(400, "<h1>400</h1>", close=True)
                    return None
                raw_form = raw_body.decode("utf-8")
            except UnicodeDecodeError:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            try:
                return parse_qs(
                    raw_form,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=12,
                )
            except ValueError:
                self._send(400, "<h1>400</h1>", close=True)
                return None

        def _form_values(
            self,
            form: dict[str, list[str]],
            expected_names: set[str],
            *,
            allowed_extra: set[str] | None = None,
        ) -> dict[str, str] | None:
            names = set(form)
            extras = allowed_extra or set()
            if not expected_names.issubset(names):
                self._send(400, "<h1>400</h1>", close=True)
                return None
            if names - expected_names - extras:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            if any(len(values) != 1 for values in form.values()):
                self._send(400, "<h1>400</h1>", close=True)
                return None
            return {name: form[name][0] for name in names}

        def _serve_ticket_image(self, filename: str) -> None:
            """Serve a ticket image file from the configured directory."""
            safe_pattern = re.compile(r"^[A-Za-z0-9_\-]+\.(jpg|png|gif|webp)$")
            if not safe_pattern.fullmatch(filename):
                self._send(404, "<h1>404</h1>")
                return
            image_path = application.ticket_image_dir / filename
            if not image_path.is_file():
                self._send(404, "<h1>404</h1>")
                return
            ext = filename.rsplit(".", 1)[-1].lower()
            content_types = {
                "jpg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
            }
            content_type = content_types.get(ext, "application/octet-stream")
            try:
                data = image_path.read_bytes()
            except OSError:
                self._send(404, "<h1>404</h1>")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def _read_multipart_form(self) -> dict[str, object] | None:
            """Parse a multipart/form-data body for file uploads.

            Returns a dict mapping field names to either ``str`` (text fields) or
            ``tuple[str, bytes]`` (filename, file data). Returns ``None`` on error.
            """
            raw_content_type = self._single_header("Content-Type", required=True)
            if raw_content_type is None:
                self._discard_request_body()
                self._send(400, "<h1>400</h1>", close=True)
                return None
            content_type = raw_content_type.split(";", 1)[0].strip().lower()
            if content_type != "multipart/form-data":
                self._discard_request_body()
                self._send(415, "<h1>415</h1>", close=True)
                return None
            # Extract boundary
            boundary = None
            for part in raw_content_type.split(";")[1:]:
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):]
                    if len(boundary) >= 2 and boundary[0] == '"' and boundary[-1] == '"':
                        boundary = boundary[1:-1]
                    break
            if not boundary:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            raw_length = self._single_header("Content-Length", required=True)
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                self._send(400, "<h1>400</h1>", close=True)
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            max_upload = 3 * 1024 * 1024  # 3MB
            if length <= 0 or length > max_upload:
                self._send(413, "<h1>413</h1>", close=True)
                return None
            try:
                body = self.rfile.read(length)
            except OSError:
                self._send(400, "<h1>400</h1>", close=True)
                return None
            boundary_bytes = ("--" + boundary).encode("ascii")
            fields: dict[str, object] = {}
            segments = body.split(boundary_bytes)
            for segment in segments:
                if not segment or segment == b"--" or segment == b"--\r\n":
                    continue
                if segment.startswith(b"\r\n"):
                    segment = segment[2:]
                if segment.endswith(b"\r\n"):
                    segment = segment[:-2]
                if not segment:
                    continue
                header_end = segment.find(b"\r\n\r\n")
                if header_end == -1:
                    continue
                header_text = segment[:header_end].decode("utf-8", errors="replace")
                content = segment[header_end + 4:]
                name = None
                filename = None
                for line in header_text.split("\r\n"):
                    lower = line.lower()
                    if lower.startswith("content-disposition:"):
                        name_match = re.search(r'name="([^"]*)"', line)
                        if name_match:
                            name = name_match.group(1)
                        file_match = re.search(r'filename="([^"]*)"', line)
                        if file_match:
                            filename = file_match.group(1)
                if name is None:
                    continue
                if filename is not None:
                    fields[name] = (filename, content)
                else:
                    fields[name] = content.decode("utf-8", errors="replace")
            return fields

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._send(200, "ok\n", "text/plain; charset=utf-8")
                return
            if not self._request_envelope_ok():
                self._send(403, "<h1>403</h1><p>请求地址或HTTPS代理校验失败。</p>")
                return
            if parsed.path.startswith("/api/v1/"):
                self._api_get(parsed)
                return
            if parsed.path.startswith("/assets/"):
                if not self._serve_dashboard_file(parsed.path.lstrip("/")):
                    self._send(404, "<h1>404</h1>")
                return
            # Serve ticket images
            if parsed.path.startswith("/tickets/"):
                self._serve_ticket_image(parsed.path[len("/tickets/"):])
                return
            if parsed.path == "/login" and application.public_mode:
                if self._session() is not None:
                    self._redirect_to("/")
                    return
                self._send(200, application.render_login())
                return
            if parsed.path != "/":
                self._send(404, "<h1>404</h1>")
                return
            if application.public_mode:
                authenticated = self._session()
                if authenticated is None:
                    self._redirect_to("/login")
                    return
            if self._serve_dashboard_file("index.html"):
                return
            LOGGER.error("Vue dashboard build is missing; refusing to fall back to removed legacy UI")
            self._send(503, "<h1>503</h1><p>前端资源缺失，请重新构建应用。</p>")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/v1/"):
                if not self._request_envelope_ok() or not self._state_origin_ok():
                    self._discard_request_body()
                    self._json_response(
                        {"level": "error", "detail": "请求地址、HTTPS代理或来源校验失败"},
                        403,
                    )
                    return
                self._api_post(path)
                return
            if path != "/login":
                self._discard_request_body()
                self._send(404, "<h1>404</h1>", close=True)
                return
            if not self._request_envelope_ok() or not self._state_origin_ok():
                self._discard_request_body()
                self._send(403, "<h1>403</h1><p>请求地址、HTTPS代理或来源校验失败。</p>", close=True)
                return
            if not application.public_mode:
                self._discard_request_body()
                self._send(404, "<h1>404</h1>", close=True)
                return
            form = self._read_form()
            if form is None:
                return
            values = self._form_values(form, {"login_csrf", "username", "password"})
            if values is None:
                return
            if not application.verify_login_token(values["login_csrf"]):
                self._send(403, "<h1>403</h1><p>登录页面已失效，请刷新后重试。</p>")
                return
            client_ip = self._client_ip()
            if client_ip is None:
                self._send(403, "<h1>403</h1><p>代理来源校验失败。</p>")
                return
            username = values["username"]
            password = values["password"]
            if len(username) > 64 or len(password) > 512:
                result, retry_after = "invalid", 0
            else:
                result, retry_after = application.check_credentials(client_ip, username, password)
            if result == "ok":
                token, _ = application.create_session()
                self._redirect_to(
                    "/",
                    extra_headers=(("Set-Cookie", application.session_cookie(token)),),
                )
                return
            if result in {"limited", "busy"}:
                self._send(
                    429,
                    application.render_login(
                        message="登录尝试过于频繁，请稍后再试。",
                        retry_after=retry_after,
                    ),
                    extra_headers=(("Retry-After", str(retry_after)),),
                )
                return
            self._send(401, application.render_login(message="用户名或密码错误。"))
        def log_message(self, fmt: str, *args) -> None:
            LOGGER.info("%s - %s", self._client_ip() or self.client_address[0], fmt % args)

    return DashboardHandler


class DashboardServer:
    def __init__(self, settings: Settings, application: DashboardApplication):
        self.httpd = LimitedThreadingHTTPServer(
            (settings.web_host, settings.web_port), build_handler(application)
        )
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="score-fourfold-web",
            daemon=True,
        )

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)
