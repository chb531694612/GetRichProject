from __future__ import annotations

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
from decimal import Decimal
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .auth import verify_password
from .config import Settings
from .ai_analyzer import AIAnalysisError, analyze_plan_from_leg_data
from .database import Database, StoredLeg, StoredPlan
from .domain import MarketType, PlanStatus, ResultStatus
from .mail import render_stored_recommendation


LOGGER = logging.getLogger("score_fourfold.web")
MAX_FORM_BYTES = 4096
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,100}$")
SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")
COOKIE_NAME = "__Host-score_session"
LOGIN_TOKEN_MAX_AGE_SECONDS = 600
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
MAX_LOGIN_CLIENTS = 1024
MAX_SESSIONS = 128

STYLE = """
:root{color-scheme:light;--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#e5eaf1;
--blue:#2563eb;--green:#047857;--red:#b42318;--amber:#b54708;--shadow:0 8px 28px rgba(23,32,51,.07)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,
"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.55}.wrap{width:min(96vw,1680px);max-width:none;margin:auto;padding:28px 18px 60px}
.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}.top h1{margin:0;font-size:28px}
.muted{color:var(--muted)}.small{font-size:13px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.metric,.panel,.plan{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}
.metric{padding:18px}.metric .value{font-size:27px;font-weight:750;margin-top:6px}.positive{color:var(--green)}.negative{color:var(--red)}
.panel{padding:20px;margin-top:16px}.action{display:flex;justify-content:space-between;align-items:center;gap:20px}
button,.button{border:0;border-radius:10px;padding:11px 18px;background:var(--blue);color:white;font-weight:700;cursor:pointer;text-decoration:none}
button:disabled{background:#98a2b3;cursor:not-allowed}.flash{padding:13px 16px;border-radius:10px;margin-bottom:16px;border:1px solid}
.flash.ok{background:#ecfdf3;border-color:#abefc6;color:#067647}.flash.warn{background:#fffaeb;border-color:#fedf89;color:#93370d}
.flash.error{background:#fef3f2;border-color:#fecdca;color:#b42318}.section-title{display:flex;justify-content:space-between;align-items:end;margin:28px 0 12px}
.date-nav{display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap}
.date-nav .button[disabled]{background:#98a2b3;cursor:not-allowed;opacity:.6}
.date-current{font-weight:700;font-size:15px;margin:0 4px}
.section-title h2{margin:0;font-size:21px}.plans{display:grid;gap:16px}.plan{overflow:hidden}.plan-head{padding:17px 20px;display:flex;justify-content:space-between;gap:14px;align-items:flex-start;border-bottom:1px solid var(--line)}
.plan-title{font-size:18px;font-weight:750}.badges{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.badge{border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;background:#eef2f6;color:#344054}
.badge.green{background:#ecfdf3;color:#067647}.badge.red{background:#fef3f2;color:#b42318}.badge.amber{background:#fffaeb;color:#93370d}
.plan-money{display:flex;gap:24px;flex-wrap:wrap;padding:13px 20px;background:#fafbfc;border-bottom:1px solid var(--line)}
.plan-money strong{display:block;font-size:17px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:1250px}th,td{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}th{color:var(--muted);font-size:12px;background:#fcfcfd}
tr:last-child td{border-bottom:0}.empty{text-align:center;padding:50px 20px;color:var(--muted)}.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:30px}
.pick-cell{min-width:220px}.ai-cell{min-width:290px;max-width:360px;font-size:13px;line-height:1.5}.ai-choice{padding:8px 10px;border-radius:9px;background:#eff6ff;border:1px solid #bfdbfe}.ai-choice .reason{margin-top:4px;color:var(--muted)}.ai-choice button{margin-top:7px;padding:6px 10px;border-radius:7px;font-size:12px}.inline-edit{margin-top:7px}.inline-edit summary{cursor:pointer;color:var(--blue);font-size:12px}.inline-edit-controls{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}.inline-edit-controls button{padding:5px 9px;border-radius:6px;font-size:12px}.edit-score-select{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:5px 7px;max-width:165px;background:#fff}
.ai-loading{padding:9px 11px;border-radius:9px;background:#fffaeb;border:1px solid #fedf89;color:#93370d;font-weight:700}.working-status{display:none;margin-top:7px;color:#93370d;font-weight:700}.working-status.visible{display:block}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:999;justify-content:center;align-items:center;padding:20px}.modal-overlay:target,.modal-overlay.open{display:flex}
.modal-box{background:var(--card);border-radius:16px;max-width:700px;max-height:80vh;overflow-y:auto;padding:28px;box-shadow:0 12px 40px rgba(0,0,0,.18)}.modal-box h3{margin:0 0 12px}.modal-box .close{float:right;font-size:22px;text-decoration:none;color:var(--muted);line-height:1}.modal-box .close:hover{color:var(--ink)}
.plan-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.plan-actions button,.plan-actions .btn-sm{border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none}.plan-actions button.danger,.plan-actions .btn-sm.danger{color:var(--red);border-color:#fecdca;background:#fef3f2}.plan-actions button.danger:hover,.plan-actions .btn-sm.danger:hover{background:#fecdca}.plan-actions button.warn,.plan-actions .btn-sm.warn{color:var(--amber);border-color:#fedf89;background:#fffaeb}
.leg-del-btn{background:none;border:0;color:var(--red);cursor:pointer;font-size:12px;padding:2px 6px;border-radius:4px}.leg-del-btn:hover{background:#fef3f2}
.login-wrap{max-width:430px;margin:9vh auto 0;padding:18px}.login-card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px;box-shadow:var(--shadow)}
.login-card h1{margin:0 0 6px}.field{margin-top:18px}.field label{display:block;font-weight:700;margin-bottom:6px}.field input{width:100%;border:1px solid #cfd6e1;border-radius:10px;padding:11px 12px;font:inherit}.login-card button{width:100%;margin-top:22px}
.logout{display:inline;margin-left:12px}.logout button{padding:7px 11px;background:#475467;font-size:12px}
@media(max-width:820px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.top,.action{display:block}.action form{margin-top:14px}.plan-head{display:block}.badges{justify-content:flex-start;margin-top:8px}}
@media(max-width:480px){.grid{grid-template-columns:1fr}.wrap{padding:18px 12px 40px}.metric .value{font-size:24px}}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f} 元"


def _leg_result(leg: StoredLeg, *, market: MarketType = MarketType.CRS) -> tuple[str, str, str]:
    if leg.result_status is ResultStatus.PENDING:
        return "待公布", "待定", "amber"
    if leg.result_status is ResultStatus.VOID:
        return "无效场次", "无效", "amber"
    actual = f"{leg.result_home}:{leg.result_away}"
    if market is MarketType.HAD:
        if leg.result_home is None or leg.result_away is None:
            return actual, "待定", "amber"
        if leg.result_home > leg.result_away:
            outcome = "主胜"
        elif leg.result_home < leg.result_away:
            outcome = "客胜"
        else:
            outcome = "平"
        hit = outcome == leg.score_label
        return f"{actual}（{outcome}）", ("命中" if hit else "未中"), ("green" if hit else "red")
    hit = actual == leg.score_label.replace("：", ":")
    return actual, ("命中" if hit else "未中"), ("green" if hit else "red")


def _delivery_label(plan: StoredPlan) -> tuple[str, str]:
    return {
        "sent": ("邮件已提交，计入2元账本", "green"),
        "queued": ("邮件待发送，暂不计账", "amber"),
        "previewed": ("仅生成本地预览，不计账", "amber"),
        "expired": ("推荐已过期，不计账", "red"),
        "failed": ("邮件发送失败，不计账", "red"),
    }.get(plan.delivery_status, (plan.delivery_status, "amber"))


def _plan_status_label(plan: StoredPlan) -> tuple[str, str]:
    if plan.delivery_status != "sent":
        return "未形成有效购买记录", "red"
    return {
        PlanStatus.PENDING: ("等待赛果", "amber"),
        PlanStatus.WON: ("中奖", "green"),
        PlanStatus.LOST: ("未中奖", "red"),
        PlanStatus.VOID: ("全部无效/退款", "amber"),
    }[plan.status]


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
    ):
        self.settings = settings
        self.database = database
        self.trigger_recommendation = trigger_recommendation
        self.provider = provider
        self.wake_mailer = wake_mailer
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
        self._analysis_tasks: dict[str, BackgroundTask] = {}
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
        if not self.settings.ai_analysis_enabled:
            return ("warn", "AI分析未启用，请设置 QWEN_API_KEY 并开启 AI_ANALYSIS_ENABLED")
        if not self.settings.qwen_api_key:
            return ("warn", "未配置 QWEN_API_KEY")
        started_at = self.now()
        with self._lock:
            current = self._analysis_tasks.get(plan_id)
            if current is not None and current.status == "running":
                return (
                    "warn",
                    f"计划 {plan_id} 正在后台进行 AI 分析，请勿重复提交；预计 1–10 分钟后刷新查看。",
                )
            self._analysis_tasks[plan_id] = BackgroundTask(
                "running",
                "warn",
                f"计划 {plan_id} 正在后台进行 AI 分析；预计 1–10 分钟后刷新查看。",
                started_at,
            )
        threading.Thread(
            target=self._run_ai_analysis_task,
            args=(plan_id, started_at),
            name=f"ai-analysis-{plan_id[:12]}",
            daemon=True,
        ).start()
        return (
            "ok",
            f"计划 {plan_id} 已提交后台 AI 分析，无需等待；预计 1–10 分钟后回来刷新查看。",
        )

    def _run_ai_analysis_task(self, plan_id: str, started_at: datetime) -> None:
        level = "error"
        detail = f"计划 {plan_id} 的 AI 分析后台执行异常，请查看日志。"
        try:
            level, detail = self.trigger_ai_analysis(plan_id)
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
        if not self.settings.ai_analysis_enabled:
            return ("warn", "AI分析未启用，请设置 QWEN_API_KEY 并开启 AI_ANALYSIS_ENABLED")
        if not self.settings.qwen_api_key:
            return ("warn", "未配置 QWEN_API_KEY")
        if self.provider is not None:
            get_matches = getattr(self.provider, "get_matches", None)
            if callable(get_matches):
                try:
                    matches = list(get_matches())
                    self.database.replace_plan_leg_options(plan_id, matches)
                    refreshed_plan = self.database.get_plan(plan_id)
                    if refreshed_plan is not None:
                        plan = refreshed_plan
                except Exception as exc:
                    LOGGER.warning("Could not refresh options for AI plan %s: %s", plan_id, exc)
        unavailable = [leg.match_num for leg in plan.legs if len(leg.options) < 2]
        if unavailable:
            return (
                "warn",
                "以下比赛没有足够的真实可选项，暂时无法生成可替换建议："
                + "、".join(unavailable),
            )
        try:
            analysis = analyze_plan_from_leg_data(plan.legs, plan.market, self.settings)
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
            return ("error", f"AI推荐失败：{exc}")
        except Exception as exc:
            LOGGER.exception("AI analysis of plan %s failed", plan_id)
            return ("error", f"AI分析失败：{exc}")
        if not stored:
            return ("warn", f"计划 {plan_id} 已不存在，AI结果未保存")
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
        leg = next((item for item in plan.legs if item.match_id == match_id), None)
        if leg is None:
            return ("warn", f"未找到比赛 {match_id}")
        option = next((item for item in leg.options if item.code == option_code), None)
        if option is None:
            return ("warn", "该推荐选项不存在或已经失效，请重新运行AI分析刷新选项")
        if not self.database.update_plan_leg_option(plan_id, match_id, option_code):
            return ("error", f"修改比赛 {match_id} 的推荐失败")
        mail_detail = self._refresh_mail_after_plan_change(plan_id)
        return (
            "ok",
            f"已将比赛 {leg.match_num} 的推荐修改为 {option.label}，赔率和奖金已重新计算{mail_detail}",
        )

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
<style>{STYLE}</style></head><body><main class="login-wrap">{flash}<section class="login-card">
<h1>登录个人看板</h1>{hint}<form method="post" action="/login">
<input type="hidden" name="login_csrf" value="{_e(self.new_login_token())}">
<div class="field"><label for="username">用户名</label><input id="username" name="username" maxlength="64" autocomplete="username" required autofocus></div>
<div class="field"><label for="password">密码</label><input id="password" name="password" type="password" maxlength="512" autocomplete="current-password" required></div>
<button type="submit">登录</button></form></section></main></body></html>"""

    def render(self, *, message: str = "", level: str = "ok", csrf_token: str = "", date: str = "") -> str:
        now = datetime.now(self.settings.timezone)
        script_nonce = secrets.token_urlsafe(18)
        # SSH mode is already restricted to loopback requests and has no login
        # session. A non-empty marker keeps its management controls available;
        # public mode still requires the real session CSRF token.
        if not self.public_mode and not csrf_token:
            csrf_token = "ssh-loopback"
        summary = self.database.summary()
        all_dates = self.database.recommendation_dates()
        if date and date in all_dates:
            selected_date = date
        elif all_dates:
            selected_date = all_dates[0]
        else:
            selected_date = ""
        if selected_date:
            plans = self.database.plans_for_recommendation_date(selected_date)
        else:
            plans = []
        date_index = all_dates.index(selected_date) if selected_date in all_dates else -1
        prev_date = all_dates[date_index + 1] if date_index >= 0 and date_index + 1 < len(all_dates) else ""
        next_date = all_dates[date_index - 1] if date_index > 0 else ""
        settled_stake = Decimal(str(summary["settled_stake"]))
        profit = Decimal(str(summary["baseline_profit"]))
        settled = int(summary["plans_won"]) + int(summary["plans_lost"]) + int(summary["plans_void"])
        decisive = int(summary["plans_won"]) + int(summary["plans_lost"])
        hit_rate = (Decimal(int(summary["plans_won"])) / Decimal(decisive) * 100) if decisive else None
        roi = (profit / settled_stake * 100) if settled_stake else None
        profit_class = "positive" if profit > 0 else ("negative" if profit < 0 else "")

        cutoff = datetime.combine(now.date(), self.settings.recommendation_deadline, tzinfo=self.settings.timezone)
        cutoff -= timedelta(minutes=self.settings.recommendation_send_buffer_minutes)
        time_open = (
            now.timetz().replace(tzinfo=None) < self.settings.recommendation_latest_start
            and now < cutoff
        )
        recommendation_date = now.date().isoformat()
        crs_exists = self.database.has_plan_for_recommendation_market(
            recommendation_date, MarketType.CRS
        )
        had_exists = (
            not self.settings.had_enabled
            or self.database.has_plan_for_recommendation_market(
                recommendation_date, MarketType.HAD
            )
        )
        today_complete = crs_exists and had_exists
        if today_complete:
            action_reason = "今日比分与胜平负计划均已生成，不能重复生成。"
        elif not time_open:
            action_reason = "已超过今日手动推荐时间，明天可再次尝试。"
        elif crs_exists:
            action_reason = "今日比分计划已存在；将尝试补生成胜平负计划。仍会执行所有筛选和截止规则，手动操作间隔至少5分钟。"
        elif had_exists and self.settings.had_enabled:
            action_reason = "今日胜平负计划已存在；将尝试补生成比分计划。仍会执行所有筛选和截止规则，手动操作间隔至少5分钟。"
        else:
            action_reason = "使用服务器当前真实赔率尝试比分与胜平负推荐；仍会执行所有筛选和截止规则，手动操作间隔至少5分钟。"
        action_enabled = time_open and not today_complete
        recommendation_task = self.recommendation_task()
        recommendation_running = (
            recommendation_task is not None and recommendation_task.status == "running"
        )
        if recommendation_running:
            action_enabled = False
            action_reason = "今日全部推荐正在后台生成；预计 1–40 分钟后刷新页面查看结果。"
        request_id, signature = self.new_request()

        flash = ""
        safe_level = level if level in {"ok", "warn", "error"} else "warn"
        if message:
            flash = f'<div class="flash {safe_level}">{_e(message[:800])}</div>'
        background_result = ""
        if recommendation_task is not None and recommendation_task.status == "finished":
            task_level = (
                recommendation_task.level
                if recommendation_task.level in {"ok", "warn", "error"}
                else "warn"
            )
            background_result = (
                f'<div class="flash {task_level}">后台推荐结果：'
                f'{_e(recommendation_task.detail[:800])}</div>'
            )

        plan_cards = "".join(self._render_plan(plan, csrf_token) for plan in plans)
        if not plan_cards:
            if selected_date:
                plan_cards = f'<div class="panel empty">{_e(selected_date)} 没有推荐记录。</div>'
            else:
                plan_cards = '<div class="panel empty">还没有推荐记录。服务生成第一张计划后会显示在这里。</div>'
        date_nav = ""
        if all_dates:
            prev_link = f'<a class="button" href="/?date={_e(prev_date)}">← 更早</a>' if prev_date else '<span class="button" disabled>← 更早</span>'
            next_link = f'<a class="button" href="/?date={_e(next_date)}">更新 →</a>' if next_date else '<span class="button" disabled>更新 →</span>'
            refresh_href = f"/?date={_e(selected_date)}" if selected_date else "/"
            date_label = _e(selected_date) if selected_date else "暂无记录"
            date_nav = f'<div class="date-nav">{prev_link}<span class="date-current">{date_label}（第 {date_index + 1} / {len(all_dates)} 天）</span>{next_link}<a class="button" href="{refresh_href}">刷新</a></div>'
        disabled = "" if action_enabled else " disabled"
        recommend_button_text = "推荐生成中…" if recommendation_running else "立即尝试今日全部推荐"
        working_class = "working-status visible" if recommendation_running else "working-status"

        logout = ""
        if self.public_mode:
            logout = (
                '<form class="logout" method="post" action="/logout">'
                f'<input type="hidden" name="csrf_token" value="{_e(csrf_token)}">'
                '<button type="submit">退出登录</button></form>'
            )
        footer_access = "已通过HTTPS加密并要求登录。" if self.public_mode else "网页仅通过SSH安全通道访问。"

        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>比分串关个人看板</title>
<style>{STYLE}</style></head><body><main class="wrap">
{flash}{background_result}<div class="top"><div><h1>比分串关个人看板</h1><div class="muted">推荐记录、赛果和2元基准模拟账本</div></div>
<div class="muted small">服务器时间：{_e(now.strftime('%Y-%m-%d %H:%M:%S'))}{logout}</div></div>
<section class="grid">
<div class="metric"><div class="muted">已计入计划</div><div class="value">{_e(summary['plans_total'])}</div><div class="small muted">比分 {summary.get('plans_crs', 0)} · 胜平负 {summary.get('plans_had', 0)}</div></div>
<div class="metric"><div class="muted">累计模拟投入</div><div class="value">{_e(summary['baseline_stake'])} 元</div><div class="small muted">只统计邮件已提交的计划</div></div>
<div class="metric"><div class="muted">已结算净盈亏</div><div class="value {profit_class}">{_e(summary['baseline_profit'])} 元</div><div class="small muted">累计返还 {summary['baseline_return']} 元 · 已结算回报率 {_e(f'{roi:.2f}%' if roi is not None else '—')}</div></div>
<div class="metric"><div class="muted">整票命中率</div><div class="value">{_e(f'{hit_rate:.2f}%' if hit_rate is not None else '—')}</div><div class="small muted">已结算 {settled} · 待结算 {summary['plans_pending']}</div></div>
</section>
<section class="panel action"><div><strong>手动尝试今日推荐</strong><div class="muted small">{_e(action_reason)}</div></div>
<form id="recommend-form" method="post" action="/actions/recommend"><input type="hidden" name="request_id" value="{_e(request_id)}">
<input type="hidden" name="signature" value="{_e(signature)}"><input type="hidden" name="csrf_token" value="{_e(csrf_token)}"><button id="recommend-submit" type="submit"{disabled}>{_e(recommend_button_text)}</button><div id="recommend-working" class="{working_class}">推荐正在后台生成（含 AI 分析），无需停留在本页；预计 1–40 分钟后刷新查看。</div></form></section>
<div class="section-title"><div><h2>推荐记录</h2><div class="muted small">按推荐日期浏览，每天一个比分一个胜平负</div></div></div>
{date_nav}
<section class="plans">{plan_cards}</section>
<div class="footer">理论奖金按推荐时固定奖金快照计算；实际返还、税额和兑奖以官方赛果及实体票为准。{footer_access}</div>
</main>
<script nonce="{script_nonce}">
function openModal(id){{var e=document.getElementById(id);if(e)e.classList.add('open')}}
function closeModal(id){{var e=document.getElementById(id);if(e){{e.classList.remove('open');window.location.hash='_'}}}}
function postAction(path,data){{var f=document.createElement('form');f.method='POST';f.action=path;data.csrf_token={json.dumps(csrf_token)};Object.keys(data).forEach(function(k){{var i=document.createElement('input');i.type='hidden';i.name=k;i.value=data[k];f.appendChild(i)}});document.body.appendChild(f);f.submit()}}
function delPlan(pid){{if(confirm('确定删除整张计划 '+pid+' 吗？此操作不可恢复。'))postAction('/actions/delete-plan',{{plan_id:pid}})}}
function delLeg(pid,mid){{if(confirm('确定从 '+pid+' 中删除比赛 '+mid+' 吗？'))postAction('/actions/delete-leg',{{plan_id:pid,match_id:mid}})}}
function runAIAnalysis(target){{var plan=target.closest('.plan');target.disabled=true;target.textContent='AI 分析中…';if(plan){{plan.querySelectorAll('.ai-cell').forEach(function(cell){{cell.innerHTML='<div class="ai-loading">本场比赛 AI 分析中，请稍候…</div>'}});var status=plan.querySelector('[data-ai-status]');if(status){{status.textContent='AI 正在联网分析，最长可能需要约 10 分钟，请勿重复点击。';status.classList.add('visible')}}}}postAction('/actions/analyze-plan',{{plan_id:target.dataset.planId}})}}
function updateLeg(target,optionCode){{if(!optionCode)return;if(confirm('确定将这场推荐修改为 '+target.dataset.optionLabel+' 吗？计划赔率和奖金会自动重算。'))postAction('/actions/update-leg',{{plan_id:target.dataset.planId,match_id:target.dataset.matchId,option_code:optionCode}})}}
var recommendForm=document.getElementById('recommend-form');if(recommendForm)recommendForm.addEventListener('submit',function(){{var button=document.getElementById('recommend-submit');var status=document.getElementById('recommend-working');if(button){{button.disabled=true;button.textContent='推荐生成中…'}}if(status)status.classList.add('visible')}})
document.addEventListener('click',function(event){{var target=event.target.closest('[data-action]');if(!target)return;event.preventDefault();var action=target.dataset.action;if(action==='delete-plan')delPlan(target.dataset.planId);else if(action==='delete-leg')delLeg(target.dataset.planId,target.dataset.matchId);else if(action==='analyze-plan')runAIAnalysis(target);else if(action==='replace-ai')updateLeg(target,target.dataset.optionCode);else if(action==='save-leg'){{var select=document.getElementById(target.dataset.selectId);if(select){{target.dataset.optionLabel=select.options[select.selectedIndex].text;updateLeg(target,select.value)}}}}else if(action==='open-modal')openModal(target.dataset.modalId);else if(action==='close-modal')closeModal(target.dataset.modalId)}})
</script>
</body></html>"""

    def _render_plan(self, plan: StoredPlan, csrf_token: str = "") -> str:
        delivery_text, delivery_class = _delivery_label(plan)
        status_text, status_class = _plan_status_label(plan)
        cross_day = len({leg.business_date for leg in plan.legs}) > 1
        market_label = plan.market.label_zh
        pick_header = "推荐比分" if plan.market is MarketType.CRS else "推荐结果"
        pid = plan.plan_id
        analysis_task = self.analysis_task(pid)
        analysis_running = analysis_task is not None and analysis_task.status == "running"

        suggestion_by_match = {
            suggestion.match_id: suggestion for suggestion in plan.ai_suggestions
        }
        modal_id = f"ai-modal-{pid[:12]}"

        # Build plan action buttons (delete whole plan)
        del_btn = ""
        ai_btn = ""
        if csrf_token and not analysis_running:
            del_btn = (
                f'<button type="button" data-action="delete-plan" data-plan-id="{_e(pid)}" '
                f'class="btn-sm danger">删除整张计划</button>'
            )
            ai_btn = (
                f'<button type="button" data-action="analyze-plan" data-plan-id="{_e(pid)}" '
                f'class="btn-sm" style="background:var(--blue);color:#fff;border-color:var(--blue)">AI分析并推荐</button>'
            )
            if plan.ai_summary:
                ai_btn += (
                    f' <button type="button" data-action="open-modal" data-modal-id="{modal_id}" '
                    'class="btn-sm">查看总体分析</button>'
                )
            ai_btn += " <span class='muted small' data-ai-status>（AI联网分析最长可能需要约10分钟，完成后自动刷新）</span>"
        elif csrf_token:
            ai_btn = (
                f'<button type="button" class="btn-sm" disabled>AI 分析中…</button> '
                "<span class='working-status visible' data-ai-status>"
                "正在后台逐场分析；无需停留在本页，预计 1–10 分钟后刷新查看。</span>"
            )
        if analysis_task is not None and analysis_task.status == "finished":
            result_class = (
                analysis_task.level
                if analysis_task.level in {"ok", "warn", "error"}
                else "warn"
            )
            ai_btn += (
                f' <span class="flash {result_class}" style="padding:5px 9px;margin:0">'
                f'后台分析结果：{_e(analysis_task.detail[:500])}</span>'
            )

        if plan.delivery_status != "sent":
            return f"""<article class="plan"><div class="plan-head"><div>
<div class="plan-title">{_e(plan.recommendation_date)} · {_e(market_label)}{plan.pass_size}串1</div>
<div class="muted small">{_e(pid)} · 生成于 {_e(plan.created_at.strftime('%Y-%m-%d %H:%M:%S'))}</div></div>
<div class="badges"><span class="badge {delivery_class}">{_e(delivery_text)}</span><span class="badge {status_class}">{_e(status_text)}</span></div></div>
<div class="panel" style="margin:0;border:0;box-shadow:none;border-radius:0">购买内容已隐藏。只有推荐邮件成功提交后，页面才会显示球队、选项和固定奖金，并计入2元模拟账本。</div>
<div class="plan-actions" style="padding:8px 20px">{ai_btn} {del_btn}</div></article>"""
        actual_return = _money(plan.settled_net_prize) if plan.status != PlanStatus.PENDING else "等待结算"
        profit = _money(plan.net_profit) if plan.net_profit is not None else "等待结算"
        rows: list[str] = []
        for leg_index, leg in enumerate(plan.legs):
            actual, verdict, verdict_class = _leg_result(leg, market=plan.market)
            del_leg_btn = ""
            if csrf_token and not analysis_running and plan.market is MarketType.CRS:
                del_leg_btn = (
                    f' <button type="button" data-action="delete-leg" data-plan-id="{_e(pid)}" '
                    f'data-match-id="{_e(leg.match_id)}" class="leg-del-btn" title="删除此场">x</button>'
                )
            select_id = f"edit-{pid[:12]}-{leg_index}"
            option_items = "".join(
                f'<option value="{_e(option.code)}"'
                f'{" selected" if option.code == leg.score_code else ""}>'
                f'{_e(option.label)}（SP {_e(option.odds)}）</option>'
                for option in leg.options
            )
            edit_html = ""
            if csrf_token and not analysis_running and len(leg.options) > 1:
                edit_html = (
                    '<details class="inline-edit"><summary>手动修改推荐</summary>'
                    '<div class="inline-edit-controls">'
                    f'<select class="edit-score-select" id="{_e(select_id)}">{option_items}</select>'
                    f'<button type="button" data-action="save-leg" data-plan-id="{_e(pid)}" '
                    f'data-match-id="{_e(leg.match_id)}" data-select-id="{_e(select_id)}" '
                    'data-option-label="">保存修改</button></div></details>'
                )
            elif csrf_token and not analysis_running:
                edit_html = '<div class="muted small">运行AI分析可刷新本场可选项</div>'

            suggestion = suggestion_by_match.get(leg.match_id)
            if analysis_running:
                ai_cell = '<div class="ai-loading">本场比赛正在后台 AI 分析中…</div>'
            elif suggestion is None:
                ai_cell = '<span class="muted">点击下方“AI分析并推荐”生成本场建议</span>'
            else:
                same_pick = suggestion.option_code == leg.score_code
                replace_control = (
                    '<span class="badge green">与当前推荐一致</span>'
                    if same_pick
                    else (
                        f'<button type="button" data-action="replace-ai" data-plan-id="{_e(pid)}" '
                        f'data-match-id="{_e(leg.match_id)}" '
                        f'data-option-code="{_e(suggestion.option_code)}" '
                        f'data-option-label="{_e(suggestion.option_label)}">替换为此推荐</button>'
                    )
                )
                ai_cell = (
                    '<div class="ai-choice">'
                    f'<strong>AI建议：{_e(suggestion.option_label)}</strong> '
                    f'<span class="muted">SP {_e(suggestion.odds)}</span>'
                    f'<div class="reason">{_e(suggestion.reason or "基于当前赔率结构综合判断")}</div>'
                    f'{replace_control}</div>'
                )
            rows.append(
                "<tr>"
                f"<td>{_e(leg.match_num)}<br><span class='muted small'>{_e(leg.league)}</span></td>"
                f"<td>{_e(leg.start_at.strftime('%Y-%m-%d %H:%M'))}</td>"
                f"<td>{_e(leg.home)} vs {_e(leg.away)}</td>"
                f"<td class='pick-cell'><strong>{_e(leg.score_label)}</strong><br><span class='muted small'>SP {_e(leg.odds)}</span>{del_leg_btn}{edit_html}</td>"
                f"<td>{_e(actual)}</td><td><span class='badge {verdict_class}'>{_e(verdict)}</span></td>"
                f'<td class="ai-cell">{ai_cell}</td>'
                "</tr>"
            )
        # Build AI modal if exists
        ai_modal = ""
        if plan.ai_summary:
            ai_modal = (
                f'<div class="modal-overlay" id="{modal_id}"><div class="modal-box">'
                f'<button type="button" data-action="close-modal" data-modal-id="{modal_id}" class="close">&times;</button>'
                f'<h3>AI分析 — {_e(pid)}</h3>'
                f'<p style="white-space:pre-wrap;line-height:1.7">{_e(plan.ai_summary)}</p>'
                f'</div></div>'
            )

        return f"""<article class="plan"><div class="plan-head"><div>
<div class="plan-title">{_e(plan.recommendation_date)} · {_e(market_label)}{plan.pass_size}串1</div>
<div class="muted small">{_e(pid)} · 生成于 {_e(plan.created_at.strftime('%Y-%m-%d %H:%M:%S'))} · 最晚比赛编号日期 {_e(plan.issue_date)}</div></div>
<div class="badges"><span class="badge {delivery_class}">{_e(delivery_text)}</span><span class="badge {status_class}">{_e(status_text)}</span>
{('<span class="badge amber">跨两个比赛编号日期</span>' if cross_day else '')}</div></div>
<div class="plan-money"><div><span class="muted small">2元理论税前返还</span><strong>{_money(plan.gross_prize)}</strong></div>
<div><span class="muted small">实际税后返还</span><strong>{actual_return}</strong></div>
<div><span class="muted small">本期净盈亏</span><strong>{profit}</strong></div></div>
<div class="table-wrap"><table><thead><tr><th>编号/联赛</th><th>开赛时间</th><th>对阵</th><th>{_e(pick_header)}</th><th>赛果</th><th>结果</th><th>AI逐场推荐</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<div class="plan-actions" style="padding:8px 20px">{ai_btn} {del_btn}</div>{ai_modal}</article>"""


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
        ) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            script_policy = f"; script-src 'nonce-{script_nonce}'" if script_nonce else ""
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                f"frame-ancestors 'none'; base-uri 'none'{script_policy}",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if application.public_mode:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for key, value in extra_headers:
                self.send_header(key, value)

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
            nonce_match = re.search(r'<script nonce="([A-Za-z0-9_-]{16,64})">', body)
            script_nonce = nonce_match.group(1) if nonce_match else ""
            self.send_response(status)
            if close:
                extra_headers += (("Connection", "close"),)
                self.close_connection = True
            self._headers(content_type, len(payload), extra_headers, script_nonce=script_nonce)
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

        def _redirect(self, message: str, level: str) -> None:
            location = "/?" + urlencode({"message": message[:800], "level": level})
            self._redirect_to(location)

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

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._send(200, "ok\n", "text/plain; charset=utf-8")
                return
            if not self._request_envelope_ok():
                self._send(403, "<h1>403</h1><p>请求地址或HTTPS代理校验失败。</p>")
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
            session: WebSession | None = None
            if application.public_mode:
                authenticated = self._session()
                if authenticated is None:
                    self._redirect_to("/login")
                    return
                _, session = authenticated
            query = parse_qs(parsed.query, keep_blank_values=False)
            message = query.get("message", [""])[0]
            level = query.get("level", ["ok"])[0]
            date = query.get("date", [""])[0]
            self._send(
                200,
                application.render(
                    message=message,
                    level=level,
                    csrf_token=session.csrf_token if session is not None else "",
                    date=date,
                ),
            )

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in {"/login", "/logout", "/actions/recommend", "/actions/delete-plan", "/actions/delete-leg", "/actions/analyze-plan", "/actions/update-leg"}:
                self._discard_request_body()
                self._send(404, "<h1>404</h1>", close=True)
                return

            # Validate Host/proxy/origin before parsing or executing every state
            # changing route, including the v0.7 plan-management actions.
            if not self._request_envelope_ok() or not self._state_origin_ok():
                self._discard_request_body()
                self._send(403, "<h1>403</h1><p>请求地址、HTTPS代理或来源校验失败。</p>", close=True)
                return

            # CSRF validation for authenticated actions
            token = ""
            session: WebSession | None = None
            if application.public_mode:
                authenticated = self._session()
                if path in {"/logout", "/actions/recommend", "/actions/delete-plan", "/actions/delete-leg", "/actions/analyze-plan", "/actions/update-leg"}:
                    if authenticated is None:
                        self._discard_request_body()
                        self._redirect_to("/login")
                        return
                    token, session = authenticated

            # ---- handles /actions/analyze-plan ----
            if path == "/actions/analyze-plan":
                form = self._read_form()
                if form is None:
                    return
                values = self._form_values(form, {"plan_id", "csrf_token"})
                if values is None:
                    return
                if session is not None:
                    csrf = values.get("csrf_token", "")
                    if not application.verify_session_csrf(session, csrf):
                        self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                        return
                level, detail = application.queue_ai_analysis(values["plan_id"])
                self._redirect(detail, level)
                return

            # ---- handles /actions/delete-plan ----
            if path == "/actions/delete-plan":
                form = self._read_form()
                if form is None:
                    return
                values = self._form_values(form, {"plan_id", "csrf_token"})
                if values is None:
                    return
                if session is not None:
                    csrf = values.get("csrf_token", "")
                    if not application.verify_session_csrf(session, csrf):
                        self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                        return
                level, detail = application.trigger_delete_plan(values["plan_id"])
                self._redirect(detail, level)
                return

            # ---- handles /actions/delete-leg ----
            if path == "/actions/delete-leg":
                form = self._read_form()
                if form is None:
                    return
                values = self._form_values(form, {"plan_id", "match_id", "csrf_token"})
                if values is None:
                    return
                if session is not None:
                    csrf = values.get("csrf_token", "")
                    if not application.verify_session_csrf(session, csrf):
                        self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                        return
                level, detail = application.trigger_delete_leg(values["plan_id"], values["match_id"])
                self._redirect(detail, level)
                return
            # ---- handles /actions/update-leg ----
            if path == "/actions/update-leg":
                form = self._read_form()
                if form is None:
                    return
                values = self._form_values(
                    form, {"plan_id", "match_id", "option_code", "csrf_token"}
                )
                if values is None:
                    return
                if session is not None:
                    csrf = values.get("csrf_token", "")
                    if not application.verify_session_csrf(session, csrf):
                        self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                        return
                level, detail = application.trigger_update_leg(
                    values["plan_id"], values["match_id"], values["option_code"]
                )
                self._redirect(detail, level)
                return
            if path == "/login" and not application.public_mode:
                self._discard_request_body()
                self._send(404, "<h1>404</h1>", close=True)
                return
            if path == "/login":
                form = self._read_form()
                if form is None:
                    return
                values = self._form_values(form, {"login_csrf", "username", "password"})
                if values is None:
                    return
                login_csrf = values["login_csrf"]
                if not application.verify_login_token(login_csrf):
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
                    result, retry_after = application.check_credentials(
                        client_ip, username, password
                    )
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
                self._send(
                    401,
                    application.render_login(message="用户名或密码错误。"),
                )
                return

            # ---- handles /logout and /actions/recommend (shared auth flow) ----
            form = self._read_form()
            if form is None:
                return
            if path == "/logout":
                expected_names = {"csrf_token"}
                allowed_extra: set[str] = set()
            elif application.public_mode:
                expected_names = {"request_id", "signature", "csrf_token"}
                allowed_extra = set()
            else:
                expected_names = {"request_id", "signature"}
                allowed_extra = {"csrf_token"}
            values = self._form_values(
                form,
                expected_names,
                allowed_extra=allowed_extra,
            )
            if values is None:
                return
            # Use the session variable already set up above
            if session is not None:
                supplied_csrf = values.get("csrf_token", "")
                if not application.verify_session_csrf(session, supplied_csrf):
                    self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                    return
                if path == "/logout":
                    application.revoke_session(token)
                    self._redirect_to(
                        "/login",
                        extra_headers=(("Set-Cookie", application.session_cookie("", delete=True)),),
                    )
                    return

            request_id = values["request_id"]
            signature = values["signature"]
            if not application.verify_request(request_id, signature):
                self._send(403, "<h1>403</h1><p>操作令牌无效，请刷新页面后重试。</p>")
                return
            try:
                status, detail = application.queue_recommendation(request_id)
            except Exception:
                LOGGER.exception("manual recommendation handler failed")
                self._redirect("手动推荐执行异常，错误通知已进入邮件队列。", "error")
                return
            level = status if status in {"ok", "warn", "error"} else "error"
            self._redirect(detail, level)

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
