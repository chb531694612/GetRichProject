from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.domain import MatchResult, PlanStatus, ResultStatus
from score_fourfold.mail import render_recommendation, render_settlement
from score_fourfold.service import ScoreFourfoldService

from .helpers import make_match, make_recommendation


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path("data")
        self.database_path = self.tmp_path / f"test_settlement_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.matches = [make_match(i, self.now, odds="2.00") for i in range(1, 5)]
        self.recommendation = make_recommendation(self.now, self.matches)
        subject, text_body, html_body = render_recommendation(self.recommendation)
        self.database.create_plan_with_mail(
            self.recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=self.now + timedelta(hours=5),
        )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)

    def _refresh_after(self, results):
        self.database.update_leg_results(self.recommendation.plan_id, {r.match_id: r for r in results})
        plan = self.database.get_plan(self.recommendation.plan_id)
        assert plan is not None
        return ScoreFourfoldService._build_settlement(plan, self.now)

    def test_all_four_hit(self):
        settlement = self._refresh_after(
            [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        )
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.WON)
        self.assertEqual(settlement.gross_prize, Decimal("32.00"))
        self.assertEqual(settlement.net_profit, Decimal("30.00"))

    def test_one_loss_loses_ticket(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        results[2] = MatchResult(self.matches[2].match_id, ResultStatus.FINAL, 0, 0)
        settlement = self._refresh_after(results)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.LOST)
        self.assertEqual(settlement.net_profit, Decimal("-2.00"))

    def test_one_void_recalculates_as_threefold(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        results[0] = MatchResult(self.matches[0].match_id, ResultStatus.VOID)
        settlement = self._refresh_after(results)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.WON)
        self.assertEqual(settlement.gross_prize, Decimal("16.00"))
        plan = self.database.get_plan(self.recommendation.plan_id)
        assert plan is not None
        _, text_body, _ = render_settlement(plan, settlement, self.database.summary())
        self.assertIn("按3串1重算", text_body)
        self.assertIn("模拟净收益", text_body)

    def test_pending_leg_prevents_settlement(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches[:3]]
        self.assertIsNone(self._refresh_after(results))


if __name__ == "__main__":
    unittest.main()
