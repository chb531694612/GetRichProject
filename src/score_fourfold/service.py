from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Callable

from .config import Settings
from .database import Database, StoredPlan
from .domain import MarketType, MatchResult, PlanStatus, ResultStatus, Settlement
from .mail import (
    Mailer,
    flush_outbox,
    render_error,
    render_no_recommendation,
    render_recommendation,
    render_mail_test,
    render_settlement,
)
from .strategy import BASE_STAKE, calculate_prize, select_accumulator, select_had_accumulator


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: str
    detail: str


class ScoreFourfoldService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        provider,
        mailer: Mailer,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.database = database
        self.provider = provider
        self.mailer = mailer
        self._clock = clock or (lambda: datetime.now(settings.timezone))
        self._recommend_lock = Lock()

    def now(self) -> datetime:
        return self._clock().astimezone(self.settings.timezone)

    def _recommendation_mail_cutoff(self, day: date) -> datetime:
        deadline = datetime.combine(day, self.settings.recommendation_deadline, tzinfo=self.settings.timezone)
        return deadline - timedelta(minutes=self.settings.recommendation_send_buffer_minutes)

    def _recommendation_first_mail_at(self, day: date) -> datetime:
        return datetime.combine(
            day,
            self.settings.recommendation_first_mail_time,
            tzinfo=self.settings.timezone,
        )

    def _recommendation_window_open(self, now: datetime) -> bool:
        local_time = now.timetz().replace(tzinfo=None)
        return (
            local_time < self.settings.recommendation_latest_start
            and now < self._recommendation_mail_cutoff(now.date())
        )

    def recommend(self, now: datetime) -> JobOutcome:
        with self._recommend_lock:
            return self._recommend_locked(now)

    def try_recommend(self, now: datetime) -> JobOutcome:
        if not self._recommend_lock.acquire(blocking=False):
            return JobOutcome("busy", "推荐任务正在执行，请稍后刷新页面查看结果")
        try:
            return self._recommend_locked(now)
        finally:
            self._recommend_lock.release()

    def _create_market_plan(
        self,
        *,
        market: MarketType,
        matches: list,
        wall_after_fetch: datetime,
        recommendation_date: str,
        max_crs_plans: int = 1,
    ) -> JobOutcome:
        existing_count = self.database.count_plans_for_recommendation_market(
            recommendation_date, market
        )
        market_limit = max_crs_plans if market is MarketType.CRS else 1
        if existing_count >= market_limit:
            return JobOutcome(
                "duplicate",
                f"推荐日{recommendation_date}已有{market.label_zh}计划，未重复生成",
            )
        if market is MarketType.CRS:
            selections = select_accumulator(matches, wall_after_fetch, self.settings)
            created_plans: list[str] = []
            no_recommendation_reasons: list[str] = []
            remaining = max(0, max_crs_plans - existing_count)
            for selection in selections:
                if len(created_plans) >= remaining:
                    break
                rec = selection.recommendation
                if rec is None:
                    no_recommendation_reasons.append(selection.reason)
                    continue
                subject, text_body, html_body = render_recommendation(rec)
                created = self.database.create_plan_with_mail(
                    rec,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    expires_at=self._recommendation_mail_cutoff(wall_after_fetch.date()),
                    not_before=max(
                        rec.created_at,
                        self._recommendation_first_mail_at(wall_after_fetch.date()),
                    ),
                )
                if created:
                    created_plans.append(rec.plan_id)
            if created_plans:
                plan_list = "、".join(created_plans)
                return JobOutcome(
                    "created",
                    f"已创建{len(created_plans)}张{market.label_zh}串关计划：{plan_list}",
                )
            if no_recommendation_reasons:
                return JobOutcome("no-recommendation", no_recommendation_reasons[0])
            return JobOutcome("no-recommendation", "未能生成任何比分串关计划")
        else:
            selection = select_had_accumulator(matches, wall_after_fetch, self.settings)
            recommendation = selection.recommendation
            if recommendation is None:
                return JobOutcome("no-recommendation", selection.reason)
            subject, text_body, html_body = render_recommendation(recommendation)
            created = self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=self._recommendation_mail_cutoff(wall_after_fetch.date()),
                not_before=max(
                    recommendation.created_at,
                    self._recommendation_first_mail_at(wall_after_fetch.date()),
                ),
            )
            if not created:
                return JobOutcome(
                    "duplicate",
                    f"推荐日{recommendation_date}已有{market.label_zh}计划或计划已存在",
                )
            return JobOutcome(
                "created",
                f"已创建{market.label_zh}{recommendation.pass_size}串1计划{recommendation.plan_id}，"
                f"2元理论税前奖金{recommendation.gross_prize}元",
            )

    def _recommend_locked(self, now: datetime) -> JobOutcome:
        # ``now`` is accepted for CLI/test compatibility, but production safety
        # checks always read the injected real clock so --now cannot bypass 18:00.
        wall_now = self.now()
        if not self._recommendation_window_open(wall_now):
            return JobOutcome("closed", "已超过今日推荐启动或邮件安全截止时间，未请求赔率、未生成计划")
        recommendation_date = wall_now.date().isoformat()
        max_crs_plans = 1
        # CRS: allow up to max_crs_plans plans per day
        crs_pending = self.database.count_plans_for_recommendation_market(
            recommendation_date, MarketType.CRS
        ) < max_crs_plans
        had_pending = (
            self.settings.had_enabled
            and not self.database.has_plan_for_recommendation_market(
                recommendation_date, MarketType.HAD
            )
        )
        if not crs_pending and not had_pending:
            return JobOutcome("duplicate", f"推荐日{recommendation_date}的比分与胜平负计划均已存在")

        all_matches = self.provider.get_matches()

        # The provider request can cross the deadline, so check the clock again
        # before selecting or writing anything.
        wall_after_fetch = self.now()
        if not self._recommendation_window_open(wall_after_fetch):
            return JobOutcome("closed", "数据返回时已超过邮件安全截止时间，未生成计划")

        outcomes = []
        if crs_pending:
            blocked = self.database.unsettled_match_ids(MarketType.CRS)
            crs_matches = [match for match in all_matches if match.match_id not in blocked]
            outcomes.append(
                self._create_market_plan(
                    market=MarketType.CRS,
                    matches=crs_matches,
                    wall_after_fetch=wall_after_fetch,
                    recommendation_date=recommendation_date,
                    max_crs_plans=max_crs_plans,
                )
            )
        if had_pending:
            blocked = self.database.unsettled_match_ids(MarketType.HAD)
            had_matches = [match for match in all_matches if match.match_id not in blocked]
            outcomes.append(
                self._create_market_plan(
                    market=MarketType.HAD,
                    matches=had_matches,
                    wall_after_fetch=wall_after_fetch,
                    recommendation_date=recommendation_date,
                )
            )
        created = [item for item in outcomes if item.status == "created"]
        duplicates = [item for item in outcomes if item.status == "duplicate"]
        missing = [item for item in outcomes if item.status == "no-recommendation"]
        details = "；".join(item.detail for item in outcomes)
        if created and (missing or duplicates):
            return JobOutcome("partial", details)
        if created:
            return JobOutcome("created", details)
        if duplicates and not missing:
            return JobOutcome("duplicate", details)
        if missing and duplicates:
            return JobOutcome("partial", details)
        return JobOutcome("no-recommendation", details)

    def finalize_recommendation_day(self, now: datetime) -> JobOutcome:
        wall_now = self.now()
        deadline = datetime.combine(
            wall_now.date(), self.settings.recommendation_deadline, tzinfo=self.settings.timezone
        )
        if wall_now < deadline:
            return JobOutcome("idle", "尚未到18:00日终确认时间")
        recommendation_date = wall_now.date().isoformat()
        if self.database.has_sent_recommendation_on(recommendation_date):
            return JobOutcome("ok", "今日购买推荐已在截止前发送，无需发送无推荐通知")
        if not self.settings.send_no_recommendation:
            return JobOutcome("idle", "今日没有已送达推荐，且无推荐通知已关闭")
        reason = "18:00前没有成功送达有效购买推荐（可能因合格比赛不足、数据源异常或邮件过期）"
        subject, text_body, html_body = render_no_recommendation(
            wall_now.date(), reason, wall_now
        )
        created = self.database.enqueue_mail(
            dedupe_key=f"no-recommendation:{recommendation_date}",
            kind="no-recommendation",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            created_at=wall_now,
            priority=50,
        )
        return JobOutcome(
            "created" if created else "duplicate",
            "已生成今日无有效购买推荐通知" if created else "今日无推荐通知已存在",
        )

    @staticmethod
    def _selected_score(leg) -> tuple[int, int] | None:
        code_match = re.fullmatch(r"s(\d{2})s(\d{2})", leg.score_code)
        if code_match:
            return int(code_match.group(1)), int(code_match.group(2))
        label_match = re.fullmatch(r"\s*(\d{1,2})\s*[:：-]\s*(\d{1,2})\s*", leg.score_label)
        if label_match:
            return int(label_match.group(1)), int(label_match.group(2))
        return None

    @staticmethod
    def _had_outcome_from_result(result: MatchResult) -> str | None:
        return result.had_label

    @classmethod
    def _leg_hit(cls, plan: StoredPlan, leg, result: MatchResult) -> bool:
        if result.status != ResultStatus.FINAL:
            return False
        if plan.market is MarketType.HAD:
            actual = cls._had_outcome_from_result(result)
            return actual is not None and actual == leg.score_label
        selected = cls._selected_score(leg)
        return selected == (result.home_score, result.away_score)

    @classmethod
    def _build_settlement(cls, plan: StoredPlan, now: datetime) -> Settlement | None:
        if any(leg.result_status == ResultStatus.PENDING for leg in plan.legs):
            return None
        leg_results = tuple(
            MatchResult(
                match_id=leg.match_id,
                status=leg.result_status,
                home_score=leg.result_home,
                away_score=leg.result_away,
                official_status=leg.official_status,
            )
            for leg in plan.legs
        )
        lost = any(
            result.status == ResultStatus.FINAL and not cls._leg_hit(plan, leg, result)
            for leg, result in zip(plan.legs, leg_results, strict=True)
        )
        active_legs = sum(result.status == ResultStatus.FINAL for result in leg_results)
        if lost:
            status = PlanStatus.LOST
            gross = tax = net = Decimal("0.00")
        elif active_legs == 0:
            status = PlanStatus.VOID
            gross = net = BASE_STAKE
            tax = Decimal("0.00")
        else:
            status = PlanStatus.WON
            combined_odds = Decimal("1")
            for leg, result in zip(plan.legs, leg_results, strict=True):
                if result.status == ResultStatus.FINAL:
                    combined_odds *= leg.odds
            gross, tax, net = calculate_prize(combined_odds, active_legs=active_legs)
        return Settlement(
            plan_id=plan.plan_id,
            status=status,
            settled_at=now,
            gross_prize=gross,
            tax=tax,
            net_prize=net,
            net_profit=(net - BASE_STAKE).quantize(Decimal("0.00")),
            leg_results=leg_results,
        )

    def settle(self, now: datetime) -> JobOutcome:
        plans = self.database.pending_plans()
        if not plans:
            return JobOutcome("idle", "没有待结算计划")
        delay = timedelta(minutes=self.settings.result_check_delay_minutes)
        due_plans = [plan for plan in plans if max(leg.start_at for leg in plan.legs) + delay <= now]
        if not due_plans:
            return JobOutcome("idle", "待结算计划尚未到赛果检查时间")
        earliest_allowed = now.date() - timedelta(days=29)
        active_plans = [
            plan for plan in due_plans if max(leg.start_at.date() for leg in plan.legs) >= earliest_allowed
        ]
        active_ids = {plan.plan_id for plan in active_plans}
        expired_plans = [plan for plan in due_plans if plan.plan_id not in active_ids]
        expired_count = len(expired_plans)
        for expired in expired_plans:
            message = f"计划{expired.plan_id}超过30天仍未取得完整官方赛果，请人工复核"
            subject, text_body, html_body = render_error("settlement-needs-review", message, now)
            self.database.enqueue_mail(
                dedupe_key=f"settlement-needs-review:{expired.plan_id}",
                kind="needs-review",
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                created_at=now,
            )
        if not active_plans:
            return JobOutcome("needs-review", f"{expired_count}张计划超过30天仍未结算，需要人工复核")
        start_date = max(
            earliest_allowed,
            min(leg.start_at.date() for plan in active_plans for leg in plan.legs),
        )
        end_date = now.date()
        results = self.provider.get_results(start_date, end_date)
        settled_count = 0
        updated_count = 0
        for plan in active_plans:
            relevant = {leg.match_id: results[leg.match_id] for leg in plan.legs if leg.match_id in results}
            if relevant:
                self.database.update_leg_results(plan.plan_id, relevant)
                updated_count += len(relevant)
            refreshed = self.database.get_plan(plan.plan_id)
            if refreshed is None:
                continue
            settlement = self._build_settlement(refreshed, now)
            if settlement is None:
                continue
            summary = self.database.summary()
            current_profit = Decimal(str(summary.get("baseline_profit", "0.00")))
            summary["baseline_profit"] = str((current_profit + settlement.net_profit).quantize(Decimal("0.00")))
            subject, text_body, html_body = render_settlement(refreshed, settlement, summary)
            if self.database.settle_plan_with_mail(
                settlement,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            ):
                settled_count += 1
        detail = f"更新{updated_count}条赛果，完成{settled_count}张计划结算"
        if expired_count:
            detail += f"；另有{expired_count}张超过30天需人工复核"
        return JobOutcome("ok", detail)

    def send_mail(self, now: datetime) -> JobOutcome:
        sent, failed = flush_outbox(self.database, self.mailer, now)
        status = "ok" if failed == 0 else "partial"
        return JobOutcome(status, f"邮件发送{sent}封，失败{failed}封")

    def test_mail(self, now: datetime) -> JobOutcome:
        subject, text_body, html_body = render_mail_test(now)
        self.mailer.send(
            email_id=0,
            dedupe_key=f"mail-test:{now.isoformat()}",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        destination = "本地预览目录" if self.settings.mail_dry_run else self.settings.mail_to
        return JobOutcome("ok", f"测试邮件已发送到{destination}")

    def enqueue_error(self, job_name: str, error: Exception, now: datetime) -> None:
        message = f"{type(error).__name__}: {error}"
        subject, text_body, html_body = render_error(job_name, message, now)
        self.database.enqueue_mail(
            dedupe_key=f"error:{job_name}:{now.strftime('%Y%m%d%H')}",
            kind="error",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            created_at=now,
        )
