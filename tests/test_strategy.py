from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from score_fourfold.strategy import calculate_prize, select_fourfold

from .helpers import make_match, make_settings


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path("data")
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_selects_four_exact_scores_and_excludes_other(self):
        settings = make_settings(self.tmp_path)
        matches = [make_match(i, self.now) for i in range(1, 7)]
        result = select_fourfold(matches, self.now, settings)
        self.assertIsNotNone(result.recommendation)
        recommendation = result.recommendation
        assert recommendation is not None
        self.assertEqual(len(recommendation.legs), 4)
        self.assertEqual(recommendation.stake, Decimal("2.00"))
        self.assertTrue(all(not leg.score.is_other for leg in recommendation.legs))
        self.assertTrue(all(leg.score.label == "1:0" for leg in recommendation.legs))

    def test_selects_threefold_with_only_three_matches(self):
        settings = make_settings(self.tmp_path)
        result = select_fourfold([make_match(i, self.now) for i in range(1, 4)], self.now, settings)
        self.assertIsNotNone(result.recommendation)
        assert result.recommendation is not None
        self.assertEqual(len(result.recommendation.legs), 3)
        self.assertTrue(result.recommendation.plan_id.startswith("BF3-"))
        self.assertEqual(result.recommendation.stake, Decimal("2.00"))

    def test_rejects_four_from_one_league_when_limit_is_two(self):
        settings = make_settings(self.tmp_path, max_matches_per_league=2)
        matches = [make_match(i, self.now, league="同一联赛") for i in range(1, 5)]
        result = select_fourfold(matches, self.now, settings)
        self.assertIsNone(result.recommendation)

    def test_excludes_missing_or_future_odds_timestamp_then_uses_threefold(self):
        settings = make_settings(self.tmp_path)
        matches = [make_match(i, self.now) for i in range(1, 5)]
        matches[0] = replace(matches[0], odds_updated_at=None)
        recommendation = select_fourfold(matches, self.now, settings).recommendation
        assert recommendation is not None
        self.assertEqual(len(recommendation.legs), 3)
        self.assertNotIn(matches[0].match_id, {leg.match.match_id for leg in recommendation.legs})

        matches[0] = replace(matches[0], odds_updated_at=self.now + timedelta(minutes=10))
        recommendation = select_fourfold(matches, self.now, settings).recommendation
        assert recommendation is not None
        self.assertEqual(len(recommendation.legs), 3)

    def test_combines_two_business_dates_when_today_has_too_few_matches(self):
        settings = make_settings(self.tmp_path)
        matches = [make_match(i, self.now) for i in range(1, 3)]
        matches.extend(
            make_match(i, self.now, business_date="2026-07-15") for i in range(3, 5)
        )
        recommendation = select_fourfold(matches, self.now, settings).recommendation
        assert recommendation is not None
        self.assertEqual(len(recommendation.legs), 4)
        self.assertEqual({leg.match.business_date for leg in recommendation.legs}, {"2026-07-14", "2026-07-15"})
        self.assertEqual(recommendation.recommendation_date, "2026-07-14")
        self.assertEqual(recommendation.issue_date, "2026-07-15")

    def test_threefold_requires_official_three_by_one_formula(self):
        settings = make_settings(self.tmp_path)
        matches = [
            replace(make_match(i, self.now), supported_pass_sizes=frozenset({4}))
            for i in range(1, 4)
        ]
        self.assertIsNone(select_fourfold(matches, self.now, settings).recommendation)

    def test_rejects_duplicate_match_ids(self):
        settings = make_settings(self.tmp_path)
        matches = [make_match(i, self.now) for i in range(1, 5)]
        matches[3] = replace(matches[3], match_id=matches[0].match_id)
        self.assertIsNone(select_fourfold(matches, self.now, settings).recommendation)

    def test_ai_analysis_is_added_to_generated_recommendation(self):
        settings = make_settings(
            self.tmp_path,
            ai_analysis_enabled=True,
            deepseek_api_key="secret",
        )
        matches = [make_match(i, self.now) for i in range(1, 5)]
        with patch(
            "score_fourfold.strategy.analyze_matches",
            return_value="赔率结构正常，仍需注意临场变化。",
        ) as mocked:
            recommendation = select_fourfold(matches, self.now, settings).recommendation

        assert recommendation is not None
        mocked.assert_called_once()
        self.assertEqual(recommendation.ai_summary, "赔率结构正常，仍需注意临场变化。")
        self.assertFalse(any("DeepSeek AI 分析摘要" in note for note in recommendation.notes))

    def test_tax_threshold_and_cap(self):
        gross, tax, net = calculate_prize(Decimal("4999"))
        self.assertEqual(gross, Decimal("9998.00"))
        self.assertEqual(tax, Decimal("0.00"))
        self.assertEqual(net, Decimal("9998.00"))

        gross, tax, net = calculate_prize(Decimal("5001"))
        self.assertEqual(gross, Decimal("10002.00"))
        self.assertEqual(tax, Decimal("2000.40"))
        self.assertEqual(net, Decimal("8001.60"))

        gross, tax, net = calculate_prize(Decimal("999999"), active_legs=4)
        self.assertEqual(gross, Decimal("500000.00"))
        self.assertEqual(tax, Decimal("100000.00"))
        self.assertEqual(net, Decimal("400000.00"))

        gross, _, _ = calculate_prize(Decimal("3.3975"), active_legs=3)
        self.assertEqual(gross, Decimal("6.80"))


if __name__ == "__main__":
    unittest.main()
