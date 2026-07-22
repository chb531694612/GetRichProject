from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.analyzer import (
    analyze_had_options,
    analyze_score_options,
    estimate_expected_goals,
)
from score_fourfold.domain import ScoreOption
from score_fourfold.strategy import _best_score

from .helpers import make_match, make_settings


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def _complete_match(self):
        home_wins = [
            "1:0", "2:0", "2:1", "3:0", "3:1", "3:2",
            "4:0", "4:1", "4:2", "5:0", "5:1", "5:2",
        ]
        draws = ["0:0", "1:1", "2:2", "3:3"]
        away_wins = [f"{away}:{home}" for home, away in (
            score.split(":") for score in home_wins
        )]
        probabilities = {label: "0.02" for label in home_wins + draws + away_wins}
        probabilities.update({"1:0": "0.12", "2:0": "0.11", "2:1": "0.10"})
        options = tuple(
            ScoreOption(
                code=label.replace(":", ""),
                label=label,
                odds=Decimal("5") if label == "1:0" else Decimal("6"),
                probability=Decimal(probability),
            )
            for label, probability in probabilities.items()
        ) + (
            ScoreOption("other-home", "胜其它", Decimal("10"), Decimal("0.10"), True),
            ScoreOption("other-draw", "平其它", Decimal("30"), Decimal("0.03"), True),
            ScoreOption("other-away", "负其它", Decimal("25"), Decimal("0.04"), True),
        )
        return replace(make_match(1, self.now), score_options=options)

    def test_estimates_expected_goals_and_blends_both_markets(self):
        match = self._complete_match()
        rates = estimate_expected_goals(match)
        self.assertIsNotNone(rates)
        assert rates is not None
        self.assertGreater(rates[0], rates[1])

        score_options = analyze_score_options(match, 0.35)
        had_options = analyze_had_options(match, 0.35)
        self.assertNotEqual(score_options[0].probability, match.score_options[0].probability)
        self.assertNotEqual(had_options[0].probability, match.had_options[0].probability)
        self.assertAlmostEqual(float(sum(option.probability for option in had_options)), 1.0)

    def test_model_can_override_the_lowest_odds_score(self):
        match = self._complete_match()
        settings = make_settings(
            Path("data"),
            poisson_model_weight=1.0,
            min_score_probability=0.0,
        )
        selected = _best_score(match, settings)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertNotEqual(selected.label, "1:0")

    def test_incomplete_score_market_falls_back_unchanged(self):
        match = make_match(1, self.now)
        self.assertIsNone(estimate_expected_goals(match))
        self.assertIs(analyze_score_options(match, 0.35), match.score_options)
        self.assertIs(analyze_had_options(match, 0.35), match.had_options)


if __name__ == "__main__":
    unittest.main()
