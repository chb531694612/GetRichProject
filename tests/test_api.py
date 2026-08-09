from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from score_fourfold.api import _leg_result
from score_fourfold.database import StoredLeg
from score_fourfold.domain import MarketType, ResultStatus


class PlanLegResultApiTests(unittest.TestCase):
    def _leg(
        self,
        label: str,
        *,
        status: ResultStatus = ResultStatus.FINAL,
        home: int | None = 2,
        away: int | None = 1,
    ) -> StoredLeg:
        return StoredLeg(
            position=1,
            match_id="match-1",
            match_num="周日001",
            business_date="2026-08-09",
            league="测试联赛",
            home="主队",
            away="客队",
            start_at=datetime(2026, 8, 9, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            snapshot_fetched_at=None,
            score_code="test",
            score_label=label,
            odds=Decimal("2.00"),
            probability=Decimal("0.50"),
            result_status=status,
            result_home=home,
            result_away=away,
            official_status="final",
        )

    def test_each_market_returns_market_result_and_hit_verdict(self):
        crs = _leg_result(self._leg("2:1"), MarketType.CRS)
        had = _leg_result(self._leg("主胜"), MarketType.HAD)
        ttg = _leg_result(self._leg("2"), MarketType.TTG)

        self.assertEqual((crs["market_result"], crs["verdict"]), ("比分 2:1", "命中"))
        self.assertEqual((had["market_result"], had["verdict"]), ("胜平负 主胜", "命中"))
        self.assertEqual((ttg["market_result"], ttg["verdict"]), ("进球数 3球", "未中"))
        self.assertFalse(ttg["hit"])

    def test_void_result_is_explicitly_not_counted(self):
        result = _leg_result(
            self._leg("主胜", status=ResultStatus.VOID, home=None, away=None),
            MarketType.HAD,
        )

        self.assertEqual(result["market_result"], "比赛无效")
        self.assertEqual(result["verdict"], "不计入串关")
        self.assertIsNone(result["hit"])

