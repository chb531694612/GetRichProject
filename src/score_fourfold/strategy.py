from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN

from .ai_analyzer import analyze_matches
from .analyzer import analyze_had_options, analyze_score_options, estimate_expected_goals
from .config import Settings
from .domain import MarketType, Match, Recommendation, ScoreOption, SelectedLeg


BASE_STAKE = Decimal("2.00")
TAX_THRESHOLD = Decimal("10000.00")
TAX_RATE = Decimal("0.20")
STRATEGY_VERSION = "market-poisson-hybrid-v1"
HAD_STRATEGY_VERSION = "had-market-poisson-hybrid-v1"
MARKET_ONLY_STRATEGY_VERSION = "market-implied-v3"
HAD_MARKET_ONLY_STRATEGY_VERSION = "had-market-implied-v1"


@dataclass(frozen=True, slots=True)
class SelectionResult:
    recommendation: Recommendation | None
    reason: str
    eligible_matches: int
    candidate_combinations: int


def _money(value: Decimal) -> Decimal:
    # Official Jingcai fixed-prize calculations use round-half-to-even (五成双).
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def prize_cap(active_legs: int) -> Decimal:
    if active_legs <= 0:
        return BASE_STAKE
    if active_legs == 1:
        return Decimal("100000.00")
    if active_legs <= 3:
        return Decimal("200000.00")
    if active_legs <= 5:
        return Decimal("500000.00")
    return Decimal("1000000.00")


def calculate_prize(combined_odds: Decimal, *, active_legs: int = 4) -> tuple[Decimal, Decimal, Decimal]:
    """Return gross prize, estimated tax and net prize for the 2-yuan baseline."""
    if active_legs == 0:
        gross = BASE_STAKE
    else:
        gross = min(_money(BASE_STAKE * combined_odds), prize_cap(active_legs))
    tax = _money(gross * TAX_RATE) if gross > TAX_THRESHOLD else Decimal("0.00")
    return gross, tax, _money(gross - tax)


def _best_score(match: Match, settings: Settings) -> ScoreOption | None:
    options = (
        analyze_score_options(match, settings.poisson_model_weight)
        if settings.automatic_analysis_enabled
        else match.score_options
    )
    candidates = [
        score
        for score in options
        if settings.allow_other_scores or not score.is_other
    ]
    if not candidates:
        return None
    # Highest blended probability; lower odds is only a deterministic tiebreaker.
    return max(candidates, key=lambda score: (score.probability, -score.odds))


def _best_had(match: Match, settings: Settings) -> ScoreOption | None:
    if len(match.had_options) != 3:
        return None
    options = (
        analyze_had_options(match, settings.poisson_model_weight)
        if settings.automatic_analysis_enabled
        else match.had_options
    )
    candidates = list(options)
    if not candidates:
        return None
    return max(candidates, key=lambda option: (option.probability, -option.odds))


def _candidate_matches(
    matches: list[Match],
    now: datetime,
    settings: Settings,
    *,
    market: MarketType,
) -> list[tuple[Match, ScoreOption]]:
    earliest = now + timedelta(minutes=settings.min_lead_minutes)
    latest = now + timedelta(hours=settings.max_lookahead_hours)
    max_age = timedelta(minutes=settings.max_odds_age_minutes)
    candidates: list[tuple[Match, ScoreOption]] = []
    for match in matches:
        if not (earliest <= match.start_at <= latest):
            continue
        if market is MarketType.CRS:
            if not match.betting_all_up:
                continue
            odds_updated_at = match.odds_updated_at
            best = _best_score(match, settings)
        else:
            if not match.had_betting_all_up:
                continue
            odds_updated_at = match.had_odds_updated_at or match.odds_updated_at
            best = _best_had(match, settings)
        if odds_updated_at is None or best is None:
            continue
        odds_clock_skew = now - odds_updated_at
        if odds_clock_skew < timedelta(minutes=-5):
            continue
        snapshot_at = match.snapshot_fetched_at or odds_updated_at
        age = now - snapshot_at
        if age < timedelta(minutes=-5) or age > max_age:
            continue
        candidates.append((match, best))
    return candidates


def _business_day(match: Match, now: datetime) -> date:
    digits = "".join(character for character in match.business_date if character.isdigit())
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            pass
    return match.start_at.astimezone(now.tzinfo).date()


def _unique_candidates(
    candidates: list[tuple[Match, ScoreOption]],
) -> list[tuple[Match, ScoreOption]]:
    unique: dict[str, tuple[Match, ScoreOption]] = {}
    for item in sorted(candidates, key=lambda value: (value[0].start_at, value[0].match_num)):
        unique.setdefault(item[0].match_id, item)
    return list(unique.values())


def _supported_pass_sizes(match: Match, market: MarketType) -> frozenset[int]:
    if market is MarketType.HAD:
        return match.had_supported_pass_sizes
    return match.supported_pass_sizes


def _best_combinations(
    candidates: list[tuple[Match, ScoreOption]],
    pass_size: int,
    settings: Settings,
    *,
    market: MarketType,
) -> tuple[list[tuple[Decimal, Decimal, tuple[tuple[Match, ScoreOption], ...]]], int]:
    """Return all valid combinations sorted by combined_odds descending for diversity."""
    valid: list[tuple[Decimal, Decimal, tuple[tuple[Match, ScoreOption], ...]]] = []
    inspected = 0
    for combination in itertools.combinations(candidates, pass_size):
        inspected += 1
        if any(pass_size not in _supported_pass_sizes(match, market) for match, _ in combination):
            continue
        league_counts = Counter(match.league for match, _ in combination)
        if league_counts and max(league_counts.values()) > settings.max_matches_per_league:
            continue
        joint_probability = Decimal("1")
        combined_odds = Decimal("1")
        for _, score in combination:
            joint_probability *= score.probability
            combined_odds *= score.odds
        valid.append((joint_probability, combined_odds, combination))
    # Sort by combined_odds descending (highest payout first) for diversity;
    # joint_probability is the tiebreaker.
    valid.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return valid, inspected


def _build_recommendation(
    selected: tuple[Decimal, Decimal, tuple[tuple[Match, ScoreOption], ...]],
    now: datetime,
    settings: Settings,
    eligible_matches: int,
    inspected: int,
    *,
    market: MarketType,
) -> SelectionResult:
    joint_probability, combined_odds, raw_combination = selected
    combination = tuple(sorted(raw_combination, key=lambda item: (item[0].start_at, item[0].match_num)))
    pass_size = len(combination)
    issue_date = max(_business_day(match, now) for match, _ in combination).isoformat()
    recommendation_date = now.date().isoformat()
    signature = "|".join(
        [
            recommendation_date,
            market.value,
            issue_date,
            str(pass_size),
            *(f"{match.match_id}:{score.code}" for match, score in combination),
        ]
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
    analysis_active = (
        settings.automatic_analysis_enabled
        and settings.poisson_model_weight > 0
        and any(estimate_expected_goals(match) is not None for match, _ in combination)
    )
    analysis_note = (
        f"已启用本地泊松自动分析：数据完整时根据比分市场估算双方预期进球，"
        f"模型权重{settings.poisson_model_weight:.0%}；数据不足的场次自动回退市场概率"
        if analysis_active
        else "本地泊松自动分析已关闭，本次仅使用市场隐含概率"
    )
    if market is MarketType.HAD:
        plan_id = f"HAD{pass_size}-{now:%Y%m%d}-{digest}"
        strategy_version = (
            HAD_STRATEGY_VERSION
            if analysis_active
            else HAD_MARKET_ONLY_STRATEGY_VERSION
        )
        notes = [
            "每场仅选择胜平负中的一个结果：主胜、平或客胜",
            analysis_note,
            "分析依赖赔率结构，不包含实时伤停和首发信息；它不能保证盈利",
            "奖金按邮件快照计算，实际兑奖以实体票固定奖金和官方规则为准",
        ]
        reason = f"已生成一张2元胜平负{pass_size}串1基准计划"
    else:
        plan_id = f"BF{pass_size}-{now:%Y%m%d}-{digest}"
        strategy_version = (
            STRATEGY_VERSION
            if analysis_active
            else MARKET_ONLY_STRATEGY_VERSION
        )
        notes = [
            "每场仅选择一个明确比分，排除胜其他、平其他、负其他",
            analysis_note,
            "分析依赖赔率结构，不包含实时伤停和首发信息；它不能保证盈利",
            "奖金按邮件快照计算，实际兑奖以实体票固定奖金和官方规则为准",
        ]
        reason = f"已生成一张2元比分{pass_size}串1基准计划"
    gross, tax, net = calculate_prize(combined_odds, active_legs=pass_size)
    days = {_business_day(match, now) for match, _ in combination}
    if len(days) > 1:
        notes.append("本计划包含两个比赛编号日期，必须在最早一场停止销售前一次性到实体终端确认并购买")
    ai_summary = ""
    if settings.ai_analysis_enabled:
        ai_summary = analyze_matches(combination, market, settings)
    recommendation = Recommendation(
        plan_id=plan_id,
        business_date=issue_date,
        created_at=now,
        legs=tuple(SelectedLeg(match=match, score=score) for match, score in combination),
        stake=BASE_STAKE,
        combined_odds=combined_odds,
        joint_probability=joint_probability,
        gross_prize=gross,
        tax=tax,
        net_prize=net,
        strategy_version=strategy_version,
        notes=tuple(notes),
        market=market,
        ai_summary=ai_summary,
    )
    return SelectionResult(
        recommendation,
        reason,
        eligible_matches,
        inspected,
    )


def _select_for_market(
    matches: list[Match],
    now: datetime,
    settings: Settings,
    *,
    market: MarketType,
    pass_sizes: tuple[int, ...],
    fallback_pass_size: int | None = None,
) -> SelectionResult:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    raw_candidates = _candidate_matches(matches, now, settings, market=market)
    if len({match.match_id for match, _ in raw_candidates}) != len(raw_candidates):
        return SelectionResult(None, "数据源包含重复比赛ID，已停止本轮推荐", len(raw_candidates), 0)
    candidates = _unique_candidates(raw_candidates)
    today = now.date()
    today_pool = [item for item in candidates if _business_day(item[0], now) == today]
    inspected = 0
    market_label = market.label_zh

    def try_pool(pool: list[tuple[Match, ScoreOption]]) -> SelectionResult | None:
        nonlocal inspected
        for pass_size in pass_sizes:
            if len(pool) < pass_size:
                continue
            valid_combinations, count = _best_combinations(
                pool,
                pass_size,
                settings,
                market=market,
            )
            inspected += count
            if valid_combinations:
                return _build_recommendation(
                    valid_combinations[0], now, settings, len(candidates), inspected, market=market
                )
        if (
            fallback_pass_size is not None
            and len(pool) == fallback_pass_size
            and fallback_pass_size not in pass_sizes
        ):
            valid_combinations, count = _best_combinations(
                pool,
                fallback_pass_size,
                settings,
                market=market,
            )
            inspected += count
            if valid_combinations:
                return _build_recommendation(
                    valid_combinations[0], now, settings, len(candidates), inspected, market=market
                )
        return None

    result = try_pool(today_pool)
    if result is not None:
        return result

    tomorrow = today + timedelta(days=1)
    two_day_pool = [
        item for item in candidates if today <= _business_day(item[0], now) <= tomorrow
    ]
    result = try_pool(two_day_pool)
    if result is not None:
        return result

    minimum = min(pass_sizes) if pass_sizes else 4
    if fallback_pass_size is not None:
        minimum = min(minimum, fallback_pass_size)
    if len(two_day_pool) < minimum:
        reason = (
            f"今天和明天合计只有{len(two_day_pool)}场符合{market_label}时间、赔率和销售条件"
        )
    else:
        reason = (
            f"两天内有{len(two_day_pool)}场{market_label}候选，但没有通过玩法"
            "和联赛集中度条件的有效串关"
        )
    return SelectionResult(None, reason, len(candidates), inspected)


def _select_crs_multi(
    matches: list[Match],
    now: datetime,
    settings: Settings,
    *,
    max_plans: int = 1,
) -> list[SelectionResult]:
    """Select up to max_plans CRS accumulators across 4x1/3x1/2x1."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    raw_candidates = _candidate_matches(matches, now, settings, market=MarketType.CRS)
    if len({match.match_id for match, _ in raw_candidates}) != len(raw_candidates):
        return [SelectionResult(None, "数据源包含重复比赛ID，已停止本轮推荐", len(raw_candidates), 0)]
    candidates = _unique_candidates(raw_candidates)
    today = now.date()
    today_pool = [item for item in candidates if _business_day(item[0], now) == today]
    tomorrow = today + timedelta(days=1)
    two_day_pool = [
        item for item in candidates if today <= _business_day(item[0], now) <= tomorrow
    ]
    results: list[SelectionResult] = []
    total_inspected = 0
    market_label = MarketType.CRS.label_zh

    # Produce one plan for each supported size, in the advertised order.
    for pass_size in (2, 3, 4):
        if len(results) >= max_plans:
            break
        valid_combinations = []
        inspected = 0
        # Keep same-day plans together whenever possible; only extend into the
        # next business date when today's pool cannot produce that pass size.
        pools = [today_pool]
        if two_day_pool != today_pool:
            pools.append(two_day_pool)
        for pool in pools:
            if len(pool) < pass_size:
                continue
            valid_combinations, count = _best_combinations(
                pool, pass_size, settings, market=MarketType.CRS
            )
            inspected += count
            if valid_combinations:
                break
        total_inspected += inspected
        if valid_combinations:
            # Take the combination with the highest combined_odds (first after sort).
            result = _build_recommendation(
                valid_combinations[0],
                now,
                settings,
                len(candidates),
                inspected,
                market=MarketType.CRS,
            )
            results.append(result)

    if not results:
        if len(two_day_pool) < 2:
            reason = f"今天和明天合计只有{len(two_day_pool)}场符合{market_label}时间、赔率和销售条件"
        else:
            reason = (
                f"两天内有{len(two_day_pool)}场{market_label}候选，但没有通过玩法"
                "和联赛集中度条件的有效串关"
            )
        results.append(SelectionResult(None, reason, len(candidates), total_inspected))

    return results


def select_accumulator(matches: list[Match], now: datetime, settings: Settings) -> list[SelectionResult]:
    """Choose one CRS accumulator across 4x1, 3x1, 2x1."""
    return _select_crs_multi(matches, now, settings, max_plans=1)


def select_had_accumulator(
    matches: list[Match], now: datetime, settings: Settings
) -> SelectionResult:
    """Choose one HAD accumulator, preferring 6x1 then 5x1 then 4x1."""
    if not settings.had_enabled:
        return SelectionResult(None, "胜平负推荐已关闭", 0, 0)
    return _select_for_market(
        matches,
        now,
        settings,
        market=MarketType.HAD,
        pass_sizes=tuple(settings.had_pass_sizes),
    )


def select_fourfold(matches: list[Match], now: datetime, settings: Settings) -> SelectionResult:
    """Backward-compatible public name retained for version 0.1 callers."""
    # Historical callers expect one SelectionResult and prefer the largest
    # available accumulator. Multi-plan callers must use select_accumulator().
    return _select_for_market(
        matches,
        now,
        settings,
        market=MarketType.CRS,
        pass_sizes=(4,),
        fallback_pass_size=3,
    )
