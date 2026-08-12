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
from .provider import ProviderError, SportteryPageResultProvider
from .settings_store import RecommendationProfile, SettingsRepository
from .strategy import BASE_STAKE, calculate_prize, select_market_plans


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
        settings_repository: SettingsRepository | None = None,
    ):
        self.settings = settings
        self.database = database
        self.provider = provider
        self.mailer = mailer
        self.settings_repository = settings_repository
        self._clock = clock or (lambda: datetime.now(settings.timezone))
        self._recommend_lock = Lock()

    def now(self) -> datetime:
        return self._clock().astimezone(self.settings.timezone)

    def refresh_runtime_settings(self) -> Settings:
        if self.settings_repository is None:
            return self.settings
        current = self.settings_repository.effective_settings()
        self.settings = current
        if hasattr(self.provider, "settings"):
            self.provider.settings = current
        self.mailer.settings = current
        return current

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
            self.refresh_runtime_settings()
            return self._recommend_locked(now)

    def try_recommend(self, now: datetime) -> JobOutcome:
        if not self._recommend_lock.acquire(blocking=False):
            return JobOutcome("busy", "推荐任务正在执行，请稍后刷新页面查看结果")
        try:
            self.refresh_runtime_settings()
            return self._recommend_locked(now)
        finally:
            self._recommend_lock.release()

    def _recommendation_profiles(self) -> dict[str, RecommendationProfile]:
        if self.settings_repository is not None:
            profiles = self.settings_repository.recommendation_profiles()
            if profiles:
                return profiles
        had_sizes = tuple(self.settings.had_pass_sizes) or (6, 5, 4)
        return {
            MarketType.CRS.value: RecommendationProfile("crs", True, 2, 5, 1),
            MarketType.HAD.value: RecommendationProfile(
                "had",
                self.settings.had_enabled,
                min(had_sizes),
                max(had_sizes),
                1,
            ),
            MarketType.TTG.value: RecommendationProfile("ttg", False, 2, 6, 1),
        }

    def _create_market_plan(
        self,
        *,
        market: MarketType,
        matches: list,
        wall_after_fetch: datetime,
        recommendation_date: str,
        profile: RecommendationProfile,
    ) -> JobOutcome:
        existing_count = self.database.count_plans_for_recommendation_market(
            recommendation_date, market
        )
        market_limit = profile.plan_count
        if existing_count >= market_limit:
            return JobOutcome(
                "duplicate",
                f"推荐日{recommendation_date}已有{market.label_zh}计划，未重复生成",
            )
        remaining = max(0, market_limit - existing_count)
        ai_runtime = None
        if self.settings_repository is not None:
            try:
                ai_runtime = self.settings_repository.active_model_runtime()
            except Exception:
                # A model or secret configuration problem must never block the
                # core recommendation flow.
                ai_runtime = None
        selections = select_market_plans(
            matches,
            wall_after_fetch,
            self.settings,
            market=market,
            min_pass_size=profile.min_pass_size,
            max_pass_size=profile.max_pass_size,
            plan_count=remaining,
            ai_runtime=ai_runtime,
        )
        created_plans: list[str] = []
        no_recommendation_reasons: list[str] = []
        for selection in selections:
            recommendation = selection.recommendation
            if recommendation is None:
                no_recommendation_reasons.append(selection.reason)
                continue
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
                market_limit=market_limit,
            )
            if created:
                created_plans.append(recommendation.plan_id)
        if created_plans:
            return JobOutcome(
                "created",
                f"已创建{len(created_plans)}张{market.label_zh}串关计划："
                + "、".join(created_plans),
            )
        if self.database.count_plans_for_recommendation_market(
            recommendation_date, market
        ) >= market_limit:
            return JobOutcome(
                "duplicate",
                f"推荐日{recommendation_date}的{market.label_zh}计划已达到{market_limit}张",
            )
        if no_recommendation_reasons:
            return JobOutcome("no-recommendation", no_recommendation_reasons[0])
        return JobOutcome("no-recommendation", f"未能生成{market.label_zh}串关计划")

    def _recommend_locked(self, now: datetime) -> JobOutcome:
        # ``now`` is accepted for CLI/test compatibility, but production safety
        # checks always read the injected real clock so --now cannot bypass 18:00.
        wall_now = self.now()
        if not self._recommendation_window_open(wall_now):
            self.database.add_log("recommend", "推荐窗口已关闭，未生成计划", "已超过今日推荐启动或邮件安全截止时间")
            return JobOutcome("closed", "已超过今日推荐启动或邮件安全截止时间，未请求赔率、未生成计划")
        recommendation_date = wall_now.date().isoformat()
        profiles = self._recommendation_profiles()
        pending_profiles = [
            profile
            for profile in profiles.values()
            if profile.enabled
            and self.database.count_plans_for_recommendation_market(
                recommendation_date, MarketType(profile.market)
            ) < profile.plan_count
        ]
        if not pending_profiles:
            self.database.add_log("recommend", f"推荐日{recommendation_date}的已启用计划均已达到配置数量")
            return JobOutcome("duplicate", f"推荐日{recommendation_date}的已启用计划均已达到配置数量")

        if hasattr(self.provider, "include_ttg"):
            self.provider.include_ttg = any(
                profile.market == MarketType.TTG.value for profile in pending_profiles
            )

        self.database.add_log("recommend", f"开始生成{recommendation_date}推荐", f"待生成玩法: {', '.join(p.market.upper() for p in pending_profiles)}")
        all_matches = self.provider.get_matches()
        self.database.add_log("recommend", f"数据源返回{len(all_matches)}场比赛")

        # The provider request can cross the deadline, so check the clock again
        # before selecting or writing anything.
        wall_after_fetch = self.now()
        if not self._recommendation_window_open(wall_after_fetch):
            self.database.add_log("recommend", "数据返回时已超过邮件安全截止时间，未生成计划")
            return JobOutcome("closed", "数据返回时已超过邮件安全截止时间，未生成计划")

        outcomes = []
        for profile in pending_profiles:
            market = MarketType(profile.market)
            blocked = self.database.unsettled_match_ids(market)
            market_matches = [
                match for match in all_matches if match.match_id not in blocked
            ]
            outcome = self._create_market_plan(
                market=market,
                matches=market_matches,
                wall_after_fetch=wall_after_fetch,
                recommendation_date=recommendation_date,
                profile=profile,
            )
            self.database.add_log("recommend", f"{market.label_zh}: {outcome.detail}")
            outcomes.append(outcome)
        created = [item for item in outcomes if item.status == "created"]
        duplicates = [item for item in outcomes if item.status == "duplicate"]
        missing = [item for item in outcomes if item.status == "no-recommendation"]
        details = "；".join(item.detail for item in outcomes)
        if created and (missing or duplicates):
            self.database.add_log("recommend", f"推荐部分完成，新建{len(created)}张计划", details)
            return JobOutcome("partial", details)
        if created:
            self.database.add_log("recommend", f"推荐完成，新建{len(created)}张计划", details)
            return JobOutcome("created", details)
        if duplicates and not missing:
            self.database.add_log("recommend", "推荐完成，所有计划已存在", details)
            return JobOutcome("duplicate", details)
        if missing and duplicates:
            self.database.add_log("recommend", "推荐部分完成，部分已存在", details)
            return JobOutcome("partial", details)
        self.database.add_log("recommend", "推荐完成，未生成新计划", details)
        return JobOutcome("no-recommendation", details)

    def finalize_recommendation_day(self, now: datetime) -> JobOutcome:
        self.refresh_runtime_settings()
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
        if plan.market is MarketType.TTG:
            if result.home_score is None or result.away_score is None:
                return False
            total = result.home_score + result.away_score
            actual = "7+" if total >= 7 else str(total)
            return actual == leg.score_label
        selected = cls._selected_score(leg)
        return selected == (result.home_score, result.away_score)

    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize a team name for fuzzy matching (remove spaces, trim, casefold)."""
        return re.sub(r"\s+", "", name).strip().casefold()

    @staticmethod
    def _match_num_digits(match_num: str) -> str:
        """Extract the numeric part from a match number (e.g. '周二004' -> '004')."""
        return re.sub(r"\D", "", match_num)

    @classmethod
    def _match_by_team(
        cls,
        leg_home: str,
        leg_away: str,
        leg_match_num: str,
        team_index: dict[tuple[str, str], str],
        partial_index: list[tuple[str, str, str, str]],
    ) -> str | None:
        """Find a result match_id by team name, with a partial-match fallback.

        * Full match: both home and away team names match.
        * Partial match: one team name matches AND the numeric part of the
          match number matches.  This handles transliteration differences
          like 沙巴巴库 vs 萨巴赫.
        """
        norm_home = cls._normalize_team(leg_home)
        norm_away = cls._normalize_team(leg_away)
        full_key = (norm_home, norm_away)
        if full_key in team_index:
            return team_index[full_key]
        leg_digits = cls._match_num_digits(leg_match_num)
        if not leg_digits:
            return None
        for res_home, res_away, res_num, res_mid in partial_index:
            if res_mid in team_index.values() and team_index.get(full_key):
                continue
            num_match = leg_digits == res_num and bool(res_num)
            home_match = norm_home and norm_home == res_home
            away_match = norm_away and norm_away == res_away
            if (home_match or away_match) and num_match:
                return res_mid
        return None

    @classmethod
    def _build_settlement(cls, plan: StoredPlan, now: datetime) -> Settlement | None:
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
        # Early loss: if any leg already has a FINAL result that missed the
        # prediction the whole ticket is lost regardless of remaining PENDING legs.
        lost = any(
            result.status == ResultStatus.FINAL and not cls._leg_hit(plan, leg, result)
            for leg, result in zip(plan.legs, leg_results, strict=True)
        )
        if lost:
            return Settlement(
                plan_id=plan.plan_id,
                status=PlanStatus.LOST,
                settled_at=now,
                gross_prize=Decimal("0.00"),
                tax=Decimal("0.00"),
                net_prize=Decimal("0.00"),
                net_profit=-BASE_STAKE,
                leg_results=leg_results,
            )
        # No loss yet — wait until every leg has a non-PENDING result.
        if any(leg.result_status == ResultStatus.PENDING for leg in plan.legs):
            return None
        active_legs = sum(result.status == ResultStatus.FINAL for result in leg_results)
        if active_legs == 0:
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

    def _store_settlement(self, plan: StoredPlan, now: datetime, settlement: Settlement | None = None) -> bool:
        if settlement is None:
            settlement = self._build_settlement(plan, now)
        if settlement is None:
            return False
        summary = self.database.summary()
        current_profit = Decimal(str(summary.get("baseline_profit", "0.00")))
        summary["baseline_profit"] = str(
            (current_profit + settlement.net_profit).quantize(Decimal("0.00"))
        )
        subject, text_body, html_body = render_settlement(plan, settlement, summary)
        return self.database.settle_plan_with_mail(
            settlement,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    def settle_plan(self, plan_id: str, now: datetime | None = None) -> JobOutcome:
        """Update and settle exactly one plan, with an official-page fallback."""
        self.refresh_runtime_settings()
        wall_now = (now or self.now()).astimezone(self.settings.timezone)
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return JobOutcome("missing", f"计划 {plan_id} 不存在")
        if plan.status not in {PlanStatus.PENDING, PlanStatus.VOID}:
            return JobOutcome("duplicate", f"计划 {plan_id} 已结算，无需重复更新")

        latest_start = max(leg.start_at for leg in plan.legs)
        delay = timedelta(minutes=self.settings.result_check_delay_minutes)
        if latest_start + delay > wall_now:
            return JobOutcome("early", f"计划 {plan_id} 尚未到赛果检查时间")

        self.database.add_log("settle", f"开始结算计划{plan_id}", f"{len(plan.legs)}场比赛")
        start_date = min(leg.start_at.date() for leg in plan.legs)
        results: dict[str, MatchResult] = {}
        primary_error = ""
        try:
            results.update(self.provider.get_results(start_date, wall_now.date()))
        except Exception as exc:
            primary_error = str(exc)

        missing_legs = [
            leg
            for leg in plan.legs
            if leg.match_id not in results
            or results[leg.match_id].status in {ResultStatus.PENDING, ResultStatus.VOID}
        ]
        fallback_count = 0
        if missing_legs:
            page_provider = SportteryPageResultProvider(self.settings)
            for leg in missing_legs:
                try:
                    result = page_provider.get_result_for_leg(leg)
                except Exception:
                    continue
                if result is not None:
                    results[leg.match_id] = result
                    fallback_count += 1
        if fallback_count:
            self.database.add_log("settle", f"官网详情兜底获取{fallback_count}场赛果")

        # Fallback: match by team name when match_id is not found (e.g. data source changed ID format).
        team_index: dict[tuple[str, str], str] = {}
        partial_index: list[tuple[str, str, str, str]] = []
        for mid, res in results.items():
            if res.home_team and res.away_team:
                key = (self._normalize_team(res.home_team), self._normalize_team(res.away_team))
                team_index.setdefault(key, mid)
                partial_index.append((
                    self._normalize_team(res.home_team),
                    self._normalize_team(res.away_team),
                    self._match_num_digits(res.match_num),
                    mid,
                ))
        migrated = 0
        leg_id_map: dict[str, str] = {}  # old_match_id -> new_match_id
        for leg in plan.legs:
            if leg.match_id in results and results[leg.match_id].status not in {ResultStatus.PENDING, ResultStatus.VOID}:
                continue
            if not leg.home or not leg.away:
                continue
            matched_mid = self._match_by_team(
                leg.home, leg.away, leg.match_num, team_index, partial_index,
            )
            if matched_mid and matched_mid in results:
                if leg.match_id != matched_mid:
                    self.database.update_leg_match_id(plan_id, leg.match_id, matched_mid)
                    leg_id_map[leg.match_id] = matched_mid
                    migrated += 1
        if migrated:
            self.database.add_log(
                "settle",
                f"计划{plan_id}按队名兜底匹配{migrated}场赛果",
                "match_id格式不一致，已更新为新ID",
            )

        relevant: dict[str, MatchResult] = {}
        for leg in plan.legs:
            effective_id = leg_id_map.get(leg.match_id, leg.match_id)
            if effective_id in results:
                relevant[effective_id] = results[effective_id]
        if relevant:
            self.database.update_leg_results(plan_id, relevant)
        else:
            detail = f"计划 {plan_id} 未取得任何新赛果，请稍后重试"
            if primary_error:
                detail += "；官方赛果接口暂时不可用"
            self.database.add_log("settle", f"计划{plan_id}未取得新赛果", detail)
            return JobOutcome("missing-results", detail)

        refreshed = self.database.get_plan(plan_id)
        if refreshed is None:
            return JobOutcome("missing", f"计划 {plan_id} 已不存在")
        unresolved = [
            leg.match_num or leg.match_id
            for leg in refreshed.legs
            if leg.result_status is ResultStatus.PENDING
        ]
        if unresolved:
            detail = (
                f"计划 {plan_id} 已更新 {len(relevant)} 场，仍有 {len(unresolved)} 场未公布："
                + "、".join(unresolved)
            )
            if primary_error and not relevant:
                detail += "；官方赛果接口暂时不可用"
            self.database.add_log("settle", f"计划{plan_id}仍有{len(unresolved)}场未公布", detail)
            return JobOutcome("partial" if relevant else "missing-results", detail)

        created = self._store_settlement(refreshed, wall_now)
        final_plan = self.database.get_plan(plan_id)
        if not created or final_plan is None or final_plan.status is PlanStatus.PENDING:
            self.database.add_log("settle", f"计划{plan_id}结算写入失败")
            return JobOutcome("error", f"计划 {plan_id} 赛果已更新，但结算写入失败")
        source_note = f"，其中官网详情兜底 {fallback_count} 场" if fallback_count else ""
        self.database.add_log("settle", f"计划{plan_id}结算完成", f"状态: {final_plan.status.value}{source_note}")
        return JobOutcome(
            "ok",
            f"计划 {plan_id} 已更新 {len(relevant)} 场并完成结算{source_note}，状态：{final_plan.status.value}",
        )

    def settle(self, now: datetime) -> JobOutcome:
        self.refresh_runtime_settings()
        plans = self.database.pending_plans()
        if not plans:
            return JobOutcome("idle", "没有待结算计划")
        delay = timedelta(minutes=self.settings.result_check_delay_minutes)
        due_plans = [plan for plan in plans if max(leg.start_at for leg in plan.legs) + delay <= now]
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
        if not active_plans and not [p for p in plans if p.plan_id not in {e.plan_id for e in expired_plans}]:
            self.database.add_log("settle", f"结算跳过，{expired_count}张计划超过30天需人工复核")
            return JobOutcome("needs-review", f"{expired_count}张计划超过30天仍未结算，需要人工复核")
        expired_ids = {p.plan_id for p in expired_plans}
        plans_to_check = [p for p in plans if p.plan_id not in expired_ids]
        due_label = f"；{len(active_plans)}张已达结算时间" if active_plans else ""
        self.database.add_log(
            "settle",
            f"开始刷新{len(plans_to_check)}张计划赛果{due_label}",
        )
        start_date = max(
            earliest_allowed,
            min(leg.start_at.date() for plan in plans_to_check for leg in plan.legs),
        )
        end_date = now.date()
        results = self.provider.get_results(start_date, end_date)
        self.database.add_log("settle", f"数据源返回{len(results)}场比赛赛果")
        # Build indices for fallback matching when match_id formats differ.
        team_index: dict[tuple[str, str], str] = {}
        partial_index: list[tuple[str, str, str, str]] = []
        for mid, res in results.items():
            if res.home_team and res.away_team:
                key = (self._normalize_team(res.home_team), self._normalize_team(res.away_team))
                team_index.setdefault(key, mid)
                partial_index.append((
                    self._normalize_team(res.home_team),
                    self._normalize_team(res.away_team),
                    self._match_num_digits(res.match_num),
                    mid,
                ))
        settled_count = 0
        updated_count = 0
        for plan in plans_to_check:
            relevant = {leg.match_id: results[leg.match_id] for leg in plan.legs if leg.match_id in results}
            # Fallback: match by team name when match_id is not found (e.g. data source changed ID format).
            unmatched = [leg for leg in plan.legs if leg.match_id not in relevant]
            migrated = 0
            for leg in unmatched:
                if not leg.home or not leg.away:
                    continue
                matched_mid = self._match_by_team(
                    leg.home, leg.away, leg.match_num, team_index, partial_index,
                )
                if matched_mid and matched_mid in results:
                    self.database.update_leg_match_id(plan.plan_id, leg.match_id, matched_mid)
                    leg_match_id = matched_mid
                    relevant[leg_match_id] = results[matched_mid]
                    migrated += 1
            if migrated:
                self.database.add_log(
                    "settle",
                    f"计划{plan.plan_id}按队名兜底匹配{migrated}场赛果",
                    "match_id格式不一致，已更新为新ID",
                )
            if relevant:
                self.database.update_leg_results(plan.plan_id, relevant)
                updated_count += len(relevant)

            # Attempt settlement after updating leg results.
            # _build_settlement handles early-loss (even with PENDING legs
            # remaining).  Non-due plans may only settle as LOST; WON / VOID
            # still require all matches to pass the delay threshold.
            refreshed = self.database.get_plan(plan.plan_id)
            if refreshed is None:
                continue
            settlement = self._build_settlement(refreshed, now)
            if settlement is None:
                continue
            if plan.plan_id not in active_ids and settlement.status != PlanStatus.LOST:
                continue
            if self._store_settlement(refreshed, now, settlement):
                settled_count += 1
                self.database.add_log(
                    "settle",
                    f"计划{plan.plan_id}结算完成",
                    f"状态: {settlement.status.value}",
                )
        detail = f"更新{updated_count}条赛果，完成{settled_count}张计划结算"
        if not active_plans and settled_count == 0 and updated_count:
            detail += "（尚未到整体结算时间）"
        if expired_count:
            detail += f"；另有{expired_count}张超过30天需人工复核"
        self.database.add_log("settle", f"结算完成", detail)
        return JobOutcome("ok", detail)

    def send_mail(self, now: datetime) -> JobOutcome:
        self.refresh_runtime_settings()
        sent, failed = flush_outbox(self.database, self.mailer, now)
        status = "ok" if failed == 0 else "partial"
        if sent or failed:
            self.database.add_log("mail", f"邮件发送完成", f"成功{sent}封，失败{failed}封")
            detail = f"邮件发送{sent}封，失败{failed}封"
        else:
            detail = "无待发邮件"
        return JobOutcome(status, detail)

    def test_mail(self, now: datetime) -> JobOutcome:
        self.refresh_runtime_settings()
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
