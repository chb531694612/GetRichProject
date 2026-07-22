from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ResultStatus(StrEnum):
    PENDING = "pending"
    FINAL = "final"
    VOID = "void"


class PlanStatus(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    VOID = "void"


class MarketType(StrEnum):
    CRS = "crs"
    HAD = "had"

    @property
    def label_zh(self) -> str:
        return "比分" if self is MarketType.CRS else "胜平负"


@dataclass(frozen=True, slots=True)
class ScoreOption:
    code: str
    label: str
    odds: Decimal
    probability: Decimal
    is_other: bool = False


@dataclass(frozen=True, slots=True)
class Match:
    match_id: str
    match_num: str
    business_date: str
    league: str
    home: str
    away: str
    start_at: datetime
    odds_updated_at: datetime | None
    score_options: tuple[ScoreOption, ...]
    snapshot_fetched_at: datetime | None = None
    betting_all_up: bool = True
    supported_pass_sizes: frozenset[int] = frozenset({2, 3, 4})
    status: str = ""
    had_options: tuple[ScoreOption, ...] = ()
    had_betting_all_up: bool = False
    had_supported_pass_sizes: frozenset[int] = frozenset({4, 5, 6})
    had_odds_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    match_id: str
    status: ResultStatus
    home_score: int | None = None
    away_score: int | None = None
    official_status: str = ""

    @property
    def score_label(self) -> str | None:
        if self.home_score is None or self.away_score is None:
            return None
        return f"{self.home_score}:{self.away_score}"

    @property
    def had_label(self) -> str | None:
        if self.home_score is None or self.away_score is None:
            return None
        if self.home_score > self.away_score:
            return "主胜"
        if self.home_score < self.away_score:
            return "客胜"
        return "平"


@dataclass(frozen=True, slots=True)
class SelectedLeg:
    match: Match
    score: ScoreOption


@dataclass(frozen=True, slots=True)
class Recommendation:
    plan_id: str
    business_date: str
    created_at: datetime
    legs: tuple[SelectedLeg, ...]
    stake: Decimal
    combined_odds: Decimal
    joint_probability: Decimal
    gross_prize: Decimal
    tax: Decimal
    net_prize: Decimal
    strategy_version: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    market: MarketType = MarketType.CRS
    ai_summary: str = ""

    @property
    def recommendation_date(self) -> str:
        return self.created_at.date().isoformat()

    @property
    def issue_date(self) -> str:
        return self.business_date

    @property
    def pass_size(self) -> int:
        return len(self.legs)


@dataclass(frozen=True, slots=True)
class Settlement:
    plan_id: str
    status: PlanStatus
    settled_at: datetime
    gross_prize: Decimal
    tax: Decimal
    net_prize: Decimal
    net_profit: Decimal
    leg_results: tuple[MatchResult, ...]
