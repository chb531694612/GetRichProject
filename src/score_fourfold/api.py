from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .ai_models import public_provider_catalog
from .database import StoredLeg, StoredPlan
from .domain import MarketType, PlanStatus, ResultStatus
from .settings_store import SettingsRepository


@dataclass(frozen=True, slots=True)
class ApiTask:
    task_id: str
    kind: str
    target_id: str
    status: str
    level: str
    detail: str
    started_at: datetime
    finished_at: datetime | None = None


def _leg_result(leg: StoredLeg, market: MarketType) -> dict[str, Any]:
    if leg.result_status is ResultStatus.PENDING:
        return {
            "status": "pending",
            "score": "",
            "outcome": "",
            "market_result": "待公布",
            "verdict": "待定",
            "hit": None,
        }
    if leg.result_status is ResultStatus.VOID:
        return {
            "status": "void",
            "score": "",
            "outcome": "无效",
            "market_result": "比赛无效",
            "verdict": "不计入串关",
            "hit": None,
        }
    score = f"{leg.result_home}:{leg.result_away}"
    if leg.result_home is None or leg.result_away is None:
        return {
            "status": "pending",
            "score": "",
            "outcome": "",
            "market_result": "待公布",
            "verdict": "待定",
            "hit": None,
        }
    if market is MarketType.HAD:
        outcome = "主胜" if leg.result_home > leg.result_away else (
            "客胜" if leg.result_home < leg.result_away else "平"
        )
        hit = outcome == leg.score_label
    elif market is MarketType.TTG:
        total = leg.result_home + leg.result_away
        outcome = "7+" if total >= 7 else str(total)
        hit = outcome == leg.score_label
    else:
        outcome = score
        hit = score == leg.score_label.replace("：", ":")
    market_result = {
        MarketType.CRS: f"比分 {outcome}",
        MarketType.HAD: f"胜平负 {outcome}",
        MarketType.TTG: f"进球数 {outcome}球",
    }[market]
    return {
        "status": "final",
        "score": score,
        "outcome": outcome,
        "market_result": market_result,
        "verdict": "命中" if hit else "未中",
        "hit": hit,
    }


def serialize_plan(plan: StoredPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "market": plan.market.value,
        "market_label": plan.market.label_zh,
        "recommendation_date": plan.recommendation_date,
        "issue_date": plan.issue_date,
        "pass_size": plan.pass_size,
        "created_at": plan.created_at.isoformat(),
        "status": plan.status.value,
        "delivery_status": plan.delivery_status,
        "purchased": plan.purchased,
        "ticket_image_url": f"/tickets/{plan.ticket_image}" if plan.ticket_image else "",
        "stake": str(plan.stake),
        "combined_odds": str(plan.combined_odds),
        "joint_probability": str(plan.joint_probability),
        "gross_prize": str(plan.gross_prize),
        "tax": str(plan.tax),
        "net_prize": str(plan.net_prize),
        "settled_net_prize": str(plan.settled_net_prize) if plan.settled_net_prize is not None else "",
        "net_profit": str(plan.net_profit) if plan.net_profit is not None else "",
        "ai_summary": plan.ai_summary,
        "legs": [
            {
                "position": leg.position,
                "match_id": leg.match_id,
                "match_num": leg.match_num,
                "business_date": leg.business_date,
                "league": leg.league,
                "home": leg.home,
                "away": leg.away,
                "start_at": leg.start_at.isoformat(),
                "pick_code": leg.score_code,
                "pick_label": leg.score_label,
                "odds": str(leg.odds),
                "probability": str(leg.probability),
                "official_status": leg.official_status,
                "result": _leg_result(leg, plan.market),
                "options": [
                    {
                        "code": option.code,
                        "label": option.label,
                        "odds": str(option.odds),
                        "probability": str(option.probability),
                    }
                    for option in leg.options
                ],
                "ai_suggestion": next(
                    (
                        {
                            "code": suggestion.option_code,
                            "label": suggestion.option_label,
                            "reason": suggestion.reason,
                        }
                        for suggestion in plan.ai_suggestions
                        if suggestion.match_id == leg.match_id
                    ),
                    None,
                ),
            }
            for leg in plan.legs
        ],
    }


class DashboardAPI:
    def __init__(self, application: Any):
        self.application = application
        self.database = application.database
        self.settings_repository: SettingsRepository | None = getattr(
            application, "settings_repository", None
        )
        self._task_lock = threading.Lock()
        self._tasks: dict[str, ApiTask] = {}

    @staticmethod
    def _filters(query: dict[str, list[str]]) -> dict[str, object]:
        filters: dict[str, object] = {}
        for key in ("market", "status", "date", "q"):
            value = query.get(key, [""])[0].strip()
            if value:
                filters[key] = value
        purchased = query.get("purchased", [""])[0].strip().lower()
        if purchased:
            if purchased not in {"0", "1", "true", "false"}:
                raise ValueError("invalid purchased filter")
            filters["purchased"] = purchased in {"1", "true"}
        return filters

    def plans(self, query: dict[str, list[str]]) -> dict[str, Any]:
        page_raw = query.get("page", ["1"])[0]
        per_page_raw = query.get("per_page", ["8"])[0]
        page = int(page_raw) if page_raw.isdigit() else 1
        per_page = int(per_page_raw) if per_page_raw.isdigit() else 8
        filters = self._filters(query)
        plans, total = self.database.filtered_plans(
            filters,
            page=page,
            per_page=per_page,
        )
        safe_per_page = max(1, min(per_page, 50))
        pages = max(1, math.ceil(total / safe_per_page))
        safe_page = min(max(1, page), pages)
        if safe_page != page:
            plans, _ = self.database.filtered_plans(
                filters,
                page=safe_page,
                per_page=safe_per_page,
            )
        return {
            "items": [serialize_plan(plan) for plan in plans],
            "pagination": {
                "page": safe_page,
                "per_page": safe_per_page,
                "total": total,
                "pages": pages,
            },
            "summary": self.database.filtered_summary(filters),
            "filters": filters,
        }

    def calendar(self, query: dict[str, list[str]]) -> dict[str, Any]:
        now = self.application.now()
        year_raw = query.get("year", [str(now.year)])[0]
        month_raw = query.get("month", [str(now.month)])[0]
        if not year_raw.isdigit() or not month_raw.isdigit():
            raise ValueError("invalid calendar month")
        year, month = int(year_raw), int(month_raw)
        filters = self._filters(query)
        return {
            "year": year,
            "month": month,
            "days": self.database.filtered_calendar_stats(year, month, filters),
        }

    def summary_from_values(self, values: object) -> dict[str, int | str]:
        if not isinstance(values, dict):
            return self.database.filtered_summary({})
        query: dict[str, list[str]] = {}
        for key in ("market", "status", "date", "q", "purchased"):
            value = values.get(key)
            if isinstance(value, bool):
                query[key] = ["true" if value else "false"]
            elif value is not None and str(value).strip():
                query[key] = [str(value)]
        return self.database.filtered_summary(self._filters(query))

    def plan_task(self, query: dict[str, list[str]]) -> dict[str, Any]:
        kind = query.get("kind", [""])[0].strip()
        plan_id = query.get("plan_id", [""])[0].strip()
        if kind not in {"settle", "ai"} or not plan_id:
            raise ValueError("invalid plan task")
        task_key = f"settle-{plan_id}" if kind == "settle" else plan_id
        task = self.application.analysis_task(task_key)
        response: dict[str, Any] = {
            "kind": kind,
            "plan_id": plan_id,
            "status": task.status if task is not None else "idle",
            "level": task.level if task is not None else "warn",
            "detail": task.detail if task is not None else "任务不存在或尚未开始",
            "summary": self.database.filtered_summary(self._filters(query)),
        }
        if task is not None and task.finished_at is not None:
            response["finished_at"] = task.finished_at.isoformat()
            plan = self.database.get_plan(plan_id)
            response["plan"] = serialize_plan(plan) if plan is not None else None
        return response

    def bootstrap(self, csrf_token: str) -> dict[str, Any]:
        request_id, signature = self.application.new_request()
        return {
            "title": "个人看板",
            "csrf_token": csrf_token or "ssh-loopback",
            "recommendation_request": {
                "request_id": request_id,
                "signature": signature,
            },
            "public_mode": self.application.public_mode,
            "now": self.application.now().isoformat(),
        }

    def logs(self, query: dict[str, list[str]]) -> dict[str, Any]:
        category = query.get("category", [""])[0].strip()
        limit_raw = query.get("limit", ["200"])[0]
        offset_raw = query.get("offset", ["0"])[0]
        try:
            limit = max(1, min(500, int(limit_raw)))
            offset = max(0, int(offset_raw))
        except ValueError:
            limit, offset = 200, 0
        return self.database.query_logs(category, limit, offset)

    def settings(self) -> dict[str, Any]:
        if self.settings_repository is None:
            raise RuntimeError("settings repository is unavailable")
        snapshot = self.settings_repository.public_snapshot()
        snapshot["providers"] = public_provider_catalog()
        return snapshot

    def update_settings_section(self, section: str, payload: dict[str, Any]) -> dict[str, Any]:
        repository = self.settings_repository
        if repository is None:
            raise RuntimeError("settings repository is unavailable")
        if section == "recommendations":
            repository.update_recommendation_profiles(payload)
        elif section == "recipients":
            values = payload.get("recipients")
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError("recipients must be a string list")
            repository.update_recipients(values)
        elif section == "mail":
            repository.update_mail_settings(
                payload,
                new_auth_code=str(payload.get("new_auth_code", "")),
            )
        elif section == "runtime":
            repository.update_runtime_settings(payload)
        else:
            raise ValueError("unknown settings section")
        if self.application.wake_mailer is not None:
            self.application.wake_mailer()
        return self.settings()

    def save_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings_repository is None:
            raise RuntimeError("settings repository is unavailable")
        model_id = self.settings_repository.save_model_config(
            provider=str(payload.get("provider", "")),
            display_name=str(payload.get("display_name", "")),
            base_url=str(payload.get("base_url", "")),
            model_name=str(payload.get("model_name", "")),
            api_key=str(payload.get("api_key", "")),
            model_config_id=str(payload.get("id", "")),
        )
        return {"model_config_id": model_id, "settings": self.settings()}

    def queue_model_test(self, model_config_id: str) -> ApiTask:
        if self.settings_repository is None:
            raise RuntimeError("settings repository is unavailable")
        with self._task_lock:
            for task in self._tasks.values():
                if task.kind == "model-test" and task.target_id == model_config_id and task.status == "running":
                    return task
            task = ApiTask(
                task_id=uuid.uuid4().hex,
                kind="model-test",
                target_id=model_config_id,
                status="running",
                level="warn",
                detail="正在测试 API、模型和强制联网搜索",
                started_at=self.application.now(),
            )
            self._tasks[task.task_id] = task
        threading.Thread(
            target=self._run_model_test,
            args=(task.task_id, model_config_id),
            name=f"model-test-{model_config_id[:12]}",
            daemon=True,
        ).start()
        return task

    def _run_model_test(self, task_id: str, model_config_id: str) -> None:
        assert self.settings_repository is not None
        success, detail = self.settings_repository.test_and_activate_model(model_config_id)
        finished = self.application.now()
        with self._task_lock:
            current = self._tasks[task_id]
            self._tasks[task_id] = ApiTask(
                current.task_id,
                current.kind,
                current.target_id,
                "finished",
                "ok" if success else "error",
                detail,
                current.started_at,
                finished,
            )
        if self.application.wake_mailer is not None:
            self.application.wake_mailer()

    @staticmethod
    def serialize_task(task: ApiTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "kind": task.kind,
            "target_id": task.target_id,
            "status": task.status,
            "level": task.level,
            "detail": task.detail,
            "started_at": task.started_at.isoformat(),
            "finished_at": task.finished_at.isoformat() if task.finished_at else "",
        }

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._task_lock:
            task = self._tasks.get(task_id)
        return self.serialize_task(task) if task else None

    def delete_model(self, model_config_id: str) -> bool:
        if self.settings_repository is None:
            raise RuntimeError("settings repository is unavailable")
        return self.settings_repository.delete_model_config(model_config_id)
