from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.domain import MarketType, MatchResult, PlanStatus, ResultStatus
from score_fourfold.mail import Mailer, render_recommendation, render_settlement
from score_fourfold.provider import _had_options, parse_normalized_matches
from score_fourfold.service import ScoreFourfoldService
from score_fourfold.strategy import select_accumulator, select_had_accumulator

from .helpers import make_match, make_recommendation, make_settings


TZ = ZoneInfo("Asia/Shanghai")


class FixedClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeProvider:
    def __init__(self, matches):
        self.matches = matches
        self.match_calls = 0

    def get_matches(self):
        self.match_calls += 1
        return self.matches

    def get_results(self, *_):
        return {}


class HadStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path("data")
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=TZ)

    def test_prefers_sixfold_then_falls_back_to_fourfold(self):
        settings = make_settings(self.tmp_path, min_had_joint_probability=0.001)
        six = [make_match(i, self.now, league=f"联赛{i}") for i in range(1, 7)]
        result = select_had_accumulator(six, self.now, settings)
        self.assertIsNotNone(result.recommendation)
        assert result.recommendation is not None
        self.assertEqual(result.recommendation.pass_size, 6)
        self.assertEqual(result.recommendation.market, MarketType.HAD)
        self.assertTrue(result.recommendation.plan_id.startswith("HAD6-"))
        self.assertTrue(all(leg.score.label == "主胜" for leg in result.recommendation.legs))

        four = six[:4]
        result = select_had_accumulator(four, self.now, settings)
        self.assertIsNotNone(result.recommendation)
        assert result.recommendation is not None
        self.assertEqual(result.recommendation.pass_size, 4)
        self.assertTrue(result.recommendation.plan_id.startswith("HAD4-"))

    def test_same_day_can_create_crs_and_had_plans(self):
        root = Path("data")
        database_path = root / f"test_had_dual_{self._testMethodName}.db"
        preview = root / f"test_had_dual_{self._testMethodName}_mail"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        if preview.exists():
            for child in preview.iterdir():
                child.unlink()
            preview.rmdir()
        settings = make_settings(
            root,
            database_path=database_path,
            mail_preview_dir=preview,
            min_had_joint_probability=0.001,
        )
        database = Database(database_path)
        database.initialize()
        matches = [make_match(i, self.now, league=f"联赛{i}") for i in range(1, 7)]
        service = ScoreFourfoldService(
            settings,
            database,
            FakeProvider(matches),
            Mailer(settings, clock=FixedClock(self.now)),
            clock=FixedClock(self.now),
        )
        outcome = service.recommend(self.now)
        self.assertEqual(outcome.status, "created")
        self.assertEqual(database.count_plans_for_recommendation_date("2026-07-14"), 2)
        self.assertEqual(
            database.count_plans_for_recommendation_market("2026-07-14", MarketType.CRS),
            1,
        )
        self.assertEqual(
            database.count_plans_for_recommendation_market("2026-07-14", MarketType.HAD),
            1,
        )
        second = service.recommend(self.now)
        self.assertEqual(second.status, "duplicate")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        if preview.exists():
            for child in preview.iterdir():
                child.unlink()
            preview.rmdir()

    def test_had_settlement_uses_win_draw_loss_mapping(self):
        database_path = Path("data") / f"test_had_settle_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        database = Database(database_path)
        database.initialize()
        matches = [make_match(i, self.now, league=f"联赛{i}") for i in range(1, 5)]
        recommendation = make_recommendation(
            self.now, matches, market=MarketType.HAD, pass_size=4
        )
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertIn("[胜平负4串1]", subject)
        self.assertIn("推荐结果", html_body)
        database.create_plan_with_mail(
            recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=self.now + timedelta(hours=5),
        )
        plan = database.get_plan(recommendation.plan_id)
        assert plan is not None
        self.assertEqual(plan.market, MarketType.HAD)
        results = {
            match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 2, 0)
            for match in matches
        }
        database.update_leg_results(recommendation.plan_id, results)
        plan = database.get_plan(recommendation.plan_id)
        assert plan is not None
        settlement = ScoreFourfoldService._build_settlement(plan, self.now)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.WON)
        _, settle_text, _ = render_settlement(plan, settlement, database.summary())
        self.assertIn("模拟净收益", settle_text)

        lost_results = dict(results)
        lost_results[matches[0].match_id] = MatchResult(
            matches[0].match_id, ResultStatus.FINAL, 0, 1
        )
        database.update_leg_results(recommendation.plan_id, lost_results)
        plan = database.get_plan(recommendation.plan_id)
        assert plan is not None
        lost = ScoreFourfoldService._build_settlement(plan, self.now)
        assert lost is not None
        self.assertEqual(lost.status, PlanStatus.LOST)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)

    def test_had_option_normalization_and_json_parse(self):
        options = _had_options({"h": "1.80", "d": "3.50", "a": "4.20"})
        self.assertEqual([option.code for option in options], ["h", "d", "a"])
        self.assertEqual(sum((option.probability for option in options), Decimal("0")), Decimal("1"))
        payload = {
            "matches": [
                {
                    "match_id": "had-1",
                    "match_num": "周二001",
                    "business_date": "2026-07-14",
                    "league": {"abbName": "测试联赛"},
                    "home": {"abbName": "主队"},
                    "away": {"abbName": "客队"},
                    "start_at": "2026-07-14T20:00:00+08:00",
                    "markets": {
                        "crs": {
                            "updateTime": "2026-07-14 11:55:00",
                            "outcomes": [
                                {"code": "s01s00", "labelZh": "1:0", "odds": 6.5, "noVigProb": 0.13}
                            ],
                        },
                        "had": {
                            "updateTime": "2026-07-14 11:55:00",
                            "outcomes": [
                                {"code": "home", "labelZh": "主胜", "odds": 1.8, "noVigProb": 0.5},
                                {"code": "draw", "labelZh": "平", "odds": 3.5, "noVigProb": 0.3},
                                {"code": "away", "labelZh": "客胜", "odds": 4.5, "noVigProb": 0.2},
                            ],
                        },
                    },
                }
            ]
        }
        matches = parse_normalized_matches(payload, TZ)
        self.assertEqual(len(matches), 1)
        self.assertEqual([option.code for option in matches[0].had_options], ["h", "d", "a"])
        self.assertTrue(matches[0].had_betting_all_up)

    def test_crs_path_still_independent(self):
        settings = make_settings(self.tmp_path)
        matches = [make_match(i, self.now) for i in range(1, 5)]
        crs = select_accumulator(matches, self.now, settings)
        self.assertEqual(len(crs), 1)
        self.assertEqual(crs[0].recommendation.pass_size, 2)
        self.assertTrue(all(item.recommendation.market is MarketType.CRS for item in crs))


if __name__ == "__main__":
    unittest.main()
