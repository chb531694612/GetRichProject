from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.domain import MarketType, MatchResult, PlanStatus, ResultStatus
from score_fourfold.mail import render_recommendation
from score_fourfold.provider import _ttg_options, parse_normalized_matches
from score_fourfold.service import ScoreFourfoldService
from score_fourfold.strategy import select_market_plans

from .helpers import make_match, make_recommendation, make_settings


class TotalGoalsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_ttg_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    def test_ttg_options_require_all_eight_outcomes(self):
        options = _ttg_options({f"s{i}": str(3 + i) for i in range(8)})
        self.assertEqual([option.label for option in options], ["0", "1", "2", "3", "4", "5", "6", "7+"])
        self.assertEqual(_ttg_options({"s0": "3.0"}), ())

    def test_normalized_provider_parses_ttg_market(self):
        payload = {
            "matches": [
                {
                    "match_id": "ttg-1",
                    "match_num": "周日001",
                    "business_date": "2026-08-09",
                    "league": "测试联赛",
                    "home": "主队",
                    "away": "客队",
                    "start_at": "2026-08-09T20:00:00+08:00",
                    "markets": {
                        "ttg": {
                            "updateTime": "2026-08-09 11:55:00",
                            "outcomes": [
                                {"code": f"s{i}", "labelZh": "7+" if i == 7 else str(i), "odds": 3 + i}
                                for i in range(8)
                            ],
                        }
                    },
                    "ttg_betting_all_up": True,
                    "ttg_supported_pass_sizes": [2, 3, 4, 5, 6],
                }
            ]
        }
        match = parse_normalized_matches(payload, self.now.tzinfo)[0]
        self.assertEqual(len(match.ttg_options), 8)
        self.assertTrue(match.ttg_betting_all_up)
        self.assertEqual(match.ttg_options[-1].label, "7+")

    def test_ttg_plan_selection_and_settlement(self):
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            max_matches_per_league=4,
        )
        matches = [
            make_match(
                i,
                self.now,
                league=f"联赛{i}",
                business_date="2026-08-09",
            )
            for i in range(1, 4)
        ]
        selected = select_market_plans(
            matches,
            self.now,
            settings,
            market=MarketType.TTG,
            min_pass_size=2,
            max_pass_size=3,
            plan_count=2,
        )
        self.assertEqual(len(selected), 1)
        recommendation = selected[0].recommendation
        assert recommendation is not None
        self.assertEqual(recommendation.market, MarketType.TTG)
        self.assertTrue(recommendation.plan_id.startswith("TTG3-"))

        # Persist a deterministic ticket selecting 0 goals, then settle it as lost.
        recommendation = make_recommendation(
            self.now,
            matches,
            market=MarketType.TTG,
            pass_size=3,
        )
        database = Database(self.database_path)
        database.initialize()
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=self.now + timedelta(hours=5),
            )
        )
        database.update_leg_results(
            recommendation.plan_id,
            {
                match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 4, 3)
                for match in matches
            },
        )
        plan = database.get_plan(recommendation.plan_id)
        assert plan is not None
        settlement = ScoreFourfoldService._build_settlement(plan, self.now)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.LOST)


if __name__ == "__main__":
    unittest.main()
