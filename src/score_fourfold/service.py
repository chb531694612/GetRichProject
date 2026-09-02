from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Callable

from .ai_analyzer import AIAnalysisError, query_results_via_ai
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


@dataclass(frozen=True, slots=True)
class _ResultIndexes:
    team_index: dict[tuple[str, str], str]
    partial_index: list[tuple[str, str, str, str]]
    num_date_index: dict[tuple[str, str], str]


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
        if ai_runtime is None and not self.settings.qwen_api_key:
            return JobOutcome(
                "no-recommendation",
                f"{market.label_zh}计划需要 AI 预测，但当前没有启用可用的大模型",
            )
        try:
            selections = select_market_plans(
                matches,
                wall_after_fetch,
                self.settings,
                market=market,
                min_pass_size=profile.min_pass_size,
                max_pass_size=profile.max_pass_size,
                plan_count=remaining,
                ai_runtime=ai_runtime,
                ai_required=True,
                history_context=self.database.ai_history_context(market),
            )
        except AIAnalysisError as exc:
            self.database.add_log(
                "ai",
                f"{market.label_zh}计划未生成：AI预测失败",
                str(exc),
            )
            return JobOutcome(
                "no-recommendation",
                f"{market.label_zh}计划未生成：AI预测失败（{exc}）",
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
    def _result_matches_leg(cls, result: MatchResult, leg) -> bool:
        """Guard against match_id collisions between the odds and results feeds.

        The official odds API (used at recommendation time) and the results
        API (used at settlement time) maintain *independent* match_id number
        spaces.  The same stored match_id can therefore resolve to a
        completely different fixture in the results feed (e.g. 周六016 of a
        previous sales period vs 周日016 of the next).  Only accept a result
        for a leg when the fixtures plausibly match.
        """
        if result.home_team and result.away_team:
            home_ok = cls._team_names_match(
                cls._normalize_team(result.home_team), cls._normalize_team(leg.home)
            )
            away_ok = cls._team_names_match(
                cls._normalize_team(result.away_team), cls._normalize_team(leg.away)
            )
            if home_ok and away_ok:
                return True
            # Partial transliteration variance (e.g. 雷克维京 vs 雷克雅未克维京人):
            # one team matches exactly AND the match number digits agree.
            if result.match_num and leg.match_num:
                digits_ok = cls._match_num_digits(result.match_num) == cls._match_num_digits(
                    leg.match_num
                )
                if digits_ok and (home_ok or away_ok):
                    return True
            return False
        # Results without team names (e.g. a degraded third-party feed) can
        # only be cross-checked by the match number digits.
        if result.match_num and leg.match_num:
            return cls._match_num_digits(result.match_num) == cls._match_num_digits(leg.match_num)
        return True

    @staticmethod
    def _team_names_match(a: str, b: str) -> bool:
        """Exact or containment match for two normalized team names.

        Handles the official feed's varying naming: a stored abbreviation like
        ``巴黎圣曼`` / ``维拉`` must match the full names ``巴黎圣日尔曼`` /
        ``阿斯顿维拉``, while a stored full name like ``帕尔梅拉斯`` matches
        the feed's full name exactly.
        """
        if not a or not b:
            return False
        if a == b:
            return True
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return len(shorter) >= 2 and shorter in longer

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

        * Full match: both home and away team names match (exact or containment).
        * Partial match: one team name matches AND the numeric part of the
          match number matches.  This handles transliteration differences
          like 沙巴巴库 vs 萨巴赫 and abbreviations like 巴黎圣曼 vs 巴黎圣日尔曼.
        """
        norm_home = cls._normalize_team(leg_home)
        norm_away = cls._normalize_team(leg_away)
        full_key = (norm_home, norm_away)
        if full_key in team_index:
            return team_index[full_key]
        leg_digits = cls._match_num_digits(leg_match_num)
        # Both team names containment-match (abbreviation) — strongest team signal.
        for res_home, res_away, res_num, res_mid in partial_index:
            if cls._team_names_match(norm_home, res_home) and cls._team_names_match(norm_away, res_away):
                if leg_digits and res_num and leg_digits == res_num:
                    return res_mid
        if not leg_digits:
            return None
        for res_home, res_away, res_num, res_mid in partial_index:
            if res_mid in team_index.values() and team_index.get(full_key):
                continue
            num_match = leg_digits == res_num and bool(res_num)
            home_match = cls._team_names_match(norm_home, res_home)
            away_match = cls._team_names_match(norm_away, res_away)
            if (home_match or away_match) and num_match:
                return res_mid
        return None

    @classmethod
    def _build_result_indexes(cls, results: dict[str, MatchResult]) -> _ResultIndexes:
        """Build all fallback indexes used to reconcile stale match_ids."""
        team_index: dict[tuple[str, str], str] = {}
        partial_index: list[tuple[str, str, str, str]] = []
        num_date_index: dict[tuple[str, str], str] = {}
        for mid, res in results.items():
            if res.home_team and res.away_team:
                key = (cls._normalize_team(res.home_team), cls._normalize_team(res.away_team))
                team_index.setdefault(key, mid)
                partial_index.append((key[0], key[1], cls._match_num_digits(res.match_num), mid))
            digits = cls._match_num_digits(res.match_num)
            if digits and res.match_date:
                num_date_index.setdefault((digits, res.match_date), mid)
        return _ResultIndexes(team_index, partial_index, num_date_index)

    @classmethod
    def _resolve_leg_match_id(cls, leg, indexes: _ResultIndexes) -> str | None:
        """Find a result match_id for a leg whose stored match_id is stale.

        Priority: official match number + kickoff date (deterministic), then
        team-name matching.  Only call for legs whose own ``match_id`` is not
        present in the current results feed.
        """
        if not leg.home or not leg.away:
            return None
        digits = cls._match_num_digits(leg.match_num)
        if digits and leg.start_at:
            mid = indexes.num_date_index.get((digits, leg.start_at.date().isoformat()))
            if mid:
                return mid
        return cls._match_by_team(
            leg.home, leg.away, leg.match_num, indexes.team_index, indexes.partial_index
        )

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
        # Only send settlement notification email for winning plans.
        # Lost and void plans are still stored but no email is queued.
        subject, text_body, html_body = render_settlement(plan, settlement, summary)
        if settlement.status is not PlanStatus.WON:
            return self.database.settle_plan_only(settlement)
        return self.database.settle_plan_with_mail(
            settlement,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    def _active_ai_runtime(self):
        """Resolve the currently enabled AI runtime, or None when unavailable."""
        if self.settings_repository is None:
            return None
        try:
            return self.settings_repository.active_model_runtime()
        except ValueError:
            return None

    def settle_plan(self, plan_id: str, now: datetime | None = None) -> JobOutcome:
        """Update and settle exactly one plan, with official-page and AI fallbacks.

        A plan that is already settled (e.g. early-loss) is still accepted as
        long as it has PENDING legs; those legs are filled in without re-settling.
        """
        self.refresh_runtime_settings()
        wall_now = (now or self.now()).astimezone(self.settings.timezone)
        plan = self.database.get_plan(plan_id)
        if plan is None:
            return JobOutcome("missing", f"计划 {plan_id} 不存在")
        has_pending_legs = any(leg.result_status is ResultStatus.PENDING for leg in plan.legs)
        already_settled = plan.status not in {PlanStatus.PENDING, PlanStatus.VOID}
        if already_settled and not has_pending_legs:
            return JobOutcome("duplicate", f"计划 {plan_id} 已结算且无待定场次，无需重复更新")

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

        # Fallback: reconcile stale match_ids via official match number + date,
        # then team name (data source changed ID format or team naming).
        indexes = self._build_result_indexes(results)
        migrated = 0
        leg_id_map: dict[str, str] = {}  # old_match_id -> new_match_id
        for leg in plan.legs:
            if leg.match_id in results and results[leg.match_id].status not in {ResultStatus.PENDING, ResultStatus.VOID}:
                continue
            matched_mid = self._resolve_leg_match_id(leg, indexes)
            if matched_mid and matched_mid in results and self._result_matches_leg(results[matched_mid], leg):
                if leg.match_id != matched_mid:
                    self.database.update_leg_match_id(plan_id, leg.match_id, matched_mid)
                    leg_id_map[leg.match_id] = matched_mid
                    migrated += 1
        if migrated:
            self.database.add_log(
                "settle",
                f"计划{plan_id}按场次号/队名兜底匹配{migrated}场赛果",
                "match_id格式不一致，已更新为新ID",
            )

        relevant: dict[str, MatchResult] = {}
        for leg in plan.legs:
            effective_id = leg_id_map.get(leg.match_id, leg.match_id)
            if effective_id in results and self._result_matches_leg(results[effective_id], leg):
                relevant[effective_id] = results[effective_id]
        if relevant:
            self.database.update_leg_results(plan_id, relevant)

        # AI fallback: for legs still PENDING after the fetched data, ask the AI
        # (with mandatory web search) for their final result.
        refreshed = self.database.get_plan(plan_id)
        if refreshed is None:
            return JobOutcome("missing", f"计划 {plan_id} 已不存在")
        ai_count = 0
        pending_after_fetch = [leg for leg in refreshed.legs if leg.result_status is ResultStatus.PENDING]
        if pending_after_fetch:
            runtime = self._active_ai_runtime()
            if runtime is not None or self.settings.qwen_api_key:
                ai_results = query_results_via_ai(pending_after_fetch, self.settings, runtime)
                if ai_results:
                    self.database.update_leg_results(plan_id, ai_results)
                    ai_count = len(ai_results)
                    self.database.add_log(
                        "settle",
                        f"计划{plan_id}通过AI联网查询更新{ai_count}场赛果",
                        "、".join(ai_results.keys()),
                    )
                    refreshed = self.database.get_plan(plan_id)
                    if refreshed is None:
                        return JobOutcome("missing", f"计划 {plan_id} 已不存在")

        updated_total = len(relevant) + ai_count
        if already_settled:
            # Already settled (e.g. early-loss): only fill remaining legs.
            unresolved = [
                leg.match_num or leg.match_id
                for leg in refreshed.legs
                if leg.result_status is ResultStatus.PENDING
            ]
            if updated_total == 0:
                detail = f"计划 {plan_id} 未取得任何新赛果，请稍后重试"
                if primary_error:
                    detail += "；官方赛果接口暂时不可用"
                self.database.add_log("settle", f"计划{plan_id}未取得新赛果", detail)
                return JobOutcome("missing-results", detail)
            if unresolved:
                detail = (
                    f"计划 {plan_id} 已更新 {updated_total} 场赛果，仍有 {len(unresolved)} 场未公布："
                    + "、".join(unresolved)
                )
                self.database.add_log("settle", f"计划{plan_id}仍有{len(unresolved)}场未公布", detail)
                return JobOutcome("partial", detail)
            detail = f"计划 {plan_id} 已补齐 {updated_total} 场赛果（计划已结算，状态不变）"
            self.database.add_log("settle", f"计划{plan_id}赛果已补齐", detail)
            return JobOutcome("ok", detail)

        if updated_total == 0:
            detail = f"计划 {plan_id} 未取得任何新赛果，请稍后重试"
            if primary_error:
                detail += "；官方赛果接口暂时不可用"
            self.database.add_log("settle", f"计划{plan_id}未取得新赛果", detail)
            return JobOutcome("missing-results", detail)

        # Try settlement even when some legs are still PENDING — early loss
        # detection in _build_settlement may settle the plan as LOST immediately.
        created = self._store_settlement(refreshed, wall_now)
        if created:
            final_plan = self.database.get_plan(plan_id)
            if final_plan is not None and final_plan.status is not PlanStatus.PENDING:
                notes = []
                if fallback_count:
                    notes.append(f"官网详情兜底 {fallback_count} 场")
                if ai_count:
                    notes.append(f"AI联网查询 {ai_count} 场")
                source_note = f"，其中{'、'.join(notes)}" if notes else ""
                early = "（提前loss结算）" if any(leg.result_status == ResultStatus.PENDING for leg in refreshed.legs) else ""
                self.database.add_log("settle", f"计划{plan_id}结算完成{early}", f"状态: {final_plan.status.value}{source_note}")
                return JobOutcome("ok", f"计划 {plan_id} 已更新 {updated_total} 场并完成结算{source_note}，状态：{final_plan.status.value}")
        unresolved = [
            leg.match_num or leg.match_id
            for leg in refreshed.legs
            if leg.result_status is ResultStatus.PENDING
        ]
        if unresolved:
            detail = (
                f"计划 {plan_id} 已更新 {updated_total} 场，仍有 {len(unresolved)} 场未公布："
                + "、".join(unresolved)
            )
            if primary_error and updated_total == 0:
                detail += "；官方赛果接口暂时不可用"
            self.database.add_log("settle", f"计划{plan_id}仍有{len(unresolved)}场未公布", detail)
            return JobOutcome("partial" if updated_total else "missing-results", detail)
        self.database.add_log("settle", f"计划{plan_id}结算写入失败")
        return JobOutcome("error", f"计划 {plan_id} 赛果已更新，但结算写入失败")

    def settle(self, now: datetime) -> JobOutcome:
        self.refresh_runtime_settings()
        plans = self.database.pending_plans()
        earliest_allowed = now.date() - timedelta(days=29)
        if not plans:
            # Still fetch results if there are recently-settled plans
            # with PENDING legs that haven't been filled in yet.
            current_date = now.date()
            has_stale = bool(self.database.settled_plans_with_pending_legs(earliest_allowed, current_date))
            if not has_stale:
                return JobOutcome("idle", "没有待结算计划")
            # Let the results-fetch path below pick up those stale legs.
            plans = []
        delay = timedelta(minutes=self.settings.result_check_delay_minutes)
        due_plans = [plan for plan in plans if max(leg.start_at for leg in plan.legs) + delay <= now]
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
            has_stale = bool(self.database.settled_plans_with_pending_legs(earliest_allowed, now.date()))
            if not has_stale:
                self.database.add_log("settle", f"结算跳过，{expired_count}张计划超过30天需人工复核")
                return JobOutcome("needs-review", f"{expired_count}张计划超过30天仍未结算，需要人工复核")
        expired_ids = {p.plan_id for p in expired_plans}
        plans_to_check = [p for p in plans if p.plan_id not in expired_ids]
        end_date = now.date()
        # Also update legs in already-settled plans that still have PENDING
        # results.  Query with the full retention window so the results fetch
        # below also covers those plans' unresolved matches — their business
        # date can be earlier than the newest pending plan's first kickoff.
        past_plans = self.database.settled_plans_with_pending_legs(earliest_allowed, end_date)
        existing_ids = {p.plan_id for p in plans_to_check}
        for pp in past_plans:
            if pp.plan_id not in existing_ids:
                plans_to_check.append(pp)
        if plans_to_check:
            start_date = max(
                earliest_allowed,
                min(leg.start_at.date() for plan in plans_to_check for leg in plan.legs),
            )
        else:
            start_date = earliest_allowed
        results = self.provider.get_results(start_date, end_date)
        self.database.add_log("settle", f"数据源返回{len(results)}场比赛赛果")
        if plans_to_check:
            due_label = f"；{len(active_plans)}张已达结算时间" if active_plans else ""
            stale_label = f"；{len(past_plans)}张已结算计划待补赛果" if past_plans else ""
            self.database.add_log(
                "settle",
                f"开始刷新{len(plans_to_check)}张计划赛果{due_label}{stale_label}",
            )
        # Build fallback indexes for reconciling stale match_ids.
        indexes = self._build_result_indexes(results)
        settled_count = 0
        updated_count = 0
        for plan in plans_to_check:
            relevant = {
                leg.match_id: results[leg.match_id]
                for leg in plan.legs
                if leg.match_id in results and self._result_matches_leg(results[leg.match_id], leg)
            }
            unmatched = [leg for leg in plan.legs if leg.match_id not in relevant]
            migrated = 0
            for leg in unmatched:
                matched_mid = self._resolve_leg_match_id(leg, indexes)
                if matched_mid and matched_mid in results and self._result_matches_leg(results[matched_mid], leg):
                    self.database.update_leg_match_id(plan.plan_id, leg.match_id, matched_mid)
                    relevant[matched_mid] = results[matched_mid]
                    migrated += 1
            if migrated:
                self.database.add_log(
                    "settle",
                    f"计划{plan.plan_id}按场次号/队名兜底匹配{migrated}场赛果",
                    "match_id格式不一致，已更新为新ID",
                )
            if relevant:
                self.database.update_leg_results(plan.plan_id, relevant)
                updated_count += len(relevant)

            # Only attempt settlement for plans that are still PENDING.
            # Already-settled plans (e.g. early-loss) only get their
            # remaining leg results filled in here.
            if plan.status != PlanStatus.PENDING:
                continue

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
