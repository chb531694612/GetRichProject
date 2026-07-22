from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from score_fourfold.config import Settings
from score_fourfold.domain import MarketType, Match, Recommendation, ScoreOption, SelectedLeg
from score_fourfold.strategy import calculate_prize


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_provider="json",
        sporttery_odds_url="https://example.invalid/odds",
        sporttery_results_url="https://example.invalid/results",
        okooo_base_url="https://www.okooo.com",
        json_data_file=tmp_path / "demo-data.json",
        timezone_name="Asia/Shanghai",
        automatic_analysis_enabled=True,
        poisson_model_weight=0.35,
        min_lead_minutes=60,
        max_lookahead_hours=48,
        max_odds_age_minutes=60,
        min_score_probability=0.05,
        min_joint_probability=0.00001,
        max_matches_per_league=2,
        allow_other_scores=False,
        max_plans_per_business_date=1,
        send_no_recommendation=True,
        recommendation_times=(time(10, 0), time(14, 0), time(17, 30)),
        recommendation_latest_start=time(17, 45),
        recommendation_deadline=time(18, 0),
        recommendation_send_buffer_minutes=10,
        had_enabled=True,
        had_pass_sizes=(6, 5, 4),
        min_had_probability=0.40,
        min_had_joint_probability=0.01,
        database_path=tmp_path / "test.db",
        poll_interval_seconds=60,
        result_check_delay_minutes=150,
        http_timeout_seconds=3,
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=8080,
        web_access_mode="ssh",
        web_public_origin="",
        web_username="owner",
        web_password_hash="",
        web_trust_proxy_headers=False,
        web_session_hours=12,
        mail_to="test@example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="",
        smtp_auth_code="",
        mail_from="",
        mail_dry_run=True,
        mail_preview_dir=tmp_path / "mail",
        qwen_api_key="",
        qwen_api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
        qwen_model="qwen3.7-max",
        ai_analysis_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


def make_had_options(
    *,
    home_odds: str = "1.80",
    home_probability: str = "0.50",
    draw_odds: str = "3.50",
    draw_probability: str = "0.28",
    away_odds: str = "4.50",
    away_probability: str = "0.22",
) -> tuple[ScoreOption, ...]:
    return (
        ScoreOption("h", "主胜", Decimal(home_odds), Decimal(home_probability), False),
        ScoreOption("d", "平", Decimal(draw_odds), Decimal(draw_probability), False),
        ScoreOption("a", "客胜", Decimal(away_odds), Decimal(away_probability), False),
    )


def make_match(
    number: int,
    now: datetime,
    *,
    league: str | None = None,
    business_date: str = "2026-07-14",
    probability: str = "0.12",
    odds: str = "7.00",
    include_other: bool = True,
    include_had: bool = True,
    had_home_probability: str = "0.50",
) -> Match:
    options = [
        ScoreOption("s01s00", "1:0", Decimal(odds), Decimal(probability), False),
        ScoreOption("s01s01", "1:1", Decimal("7.50"), Decimal("0.11"), False),
    ]
    if include_other:
        options.append(ScoreOption("s1sh", "胜其它", Decimal("4.00"), Decimal("0.30"), True))
    had_options = make_had_options(home_probability=had_home_probability) if include_had else ()
    return Match(
        match_id=str(1000 + number),
        match_num=f"周二{number:03d}",
        business_date=business_date,
        league=league or f"联赛{number % 3}",
        home=f"主队{number}",
        away=f"客队{number}",
        start_at=now + timedelta(hours=3 + number / 10),
        odds_updated_at=now - timedelta(minutes=5),
        score_options=tuple(options),
        betting_all_up=True,
        had_options=had_options,
        had_betting_all_up=bool(had_options),
        had_supported_pass_sizes=frozenset({4, 5, 6}),
        had_odds_updated_at=now - timedelta(minutes=5) if had_options else None,
    )


def make_recommendation(
    now: datetime,
    matches: list[Match],
    *,
    market: MarketType = MarketType.CRS,
    pass_size: int | None = None,
) -> Recommendation:
    size = pass_size or (4 if market is MarketType.CRS else min(6, len(matches)))
    if market is MarketType.HAD:
        legs = tuple(
            SelectedLeg(match=match, score=match.had_options[0]) for match in matches[:size]
        )
        prefix = f"HAD{size}"
    else:
        legs = tuple(
            SelectedLeg(match=match, score=match.score_options[0]) for match in matches[:size]
        )
        prefix = f"BF{size}"
    combined = Decimal("1")
    joint = Decimal("1")
    for leg in legs:
        combined *= leg.score.odds
        joint *= leg.score.probability
    gross, tax, net = calculate_prize(combined, active_legs=len(legs))
    return Recommendation(
        plan_id=f"{prefix}-TEST-0001",
        business_date=max(match.business_date for match in matches[: len(legs)]),
        created_at=now,
        legs=legs,
        stake=Decimal("2.00"),
        combined_odds=combined,
        joint_probability=joint,
        gross_prize=gross,
        tax=tax,
        net_prize=net,
        strategy_version="test",
        market=market,
    )
