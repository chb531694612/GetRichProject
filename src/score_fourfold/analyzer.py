from __future__ import annotations

import math
import re
from dataclasses import replace
from decimal import Decimal

from .domain import Match, ScoreOption


EXACT_SCORE = re.compile(r"^(\d+):(\d+)$")
MAX_POISSON_GOALS = 10
HOME_WIN_SCORES = {
    "1:0", "2:0", "2:1", "3:0", "3:1", "3:2",
    "4:0", "4:1", "4:2", "5:0", "5:1", "5:2",
}
EXPECTED_EXACT_SCORES = (
    HOME_WIN_SCORES
    | {"0:0", "1:1", "2:2", "3:3"}
    | {f"{away}:{home}" for home, away in (score.split(":") for score in HOME_WIN_SCORES)}
)


def _score_coordinates(option: ScoreOption) -> tuple[float, float] | None:
    match = EXACT_SCORE.fullmatch(option.label.strip())
    if match is not None:
        return float(match.group(1)), float(match.group(2))
    if "胜" in option.label:
        return 4.5, 1.5
    if "平" in option.label:
        return 4.0, 4.0
    if "负" in option.label:
        return 1.5, 4.5
    return None


def estimate_expected_goals(match: Match) -> tuple[float, float] | None:
    """Estimate both teams' scoring rates from the complete CRS market."""
    exact_labels = {
        option.label.strip()
        for option in match.score_options
        if EXACT_SCORE.fullmatch(option.label.strip())
    }
    other_outcomes = {
        outcome
        for option in match.score_options
        if option.is_other
        for outcome in ("h", "d", "a")
        if ("胜" in option.label and outcome == "h")
        or ("平" in option.label and outcome == "d")
        or ("负" in option.label and outcome == "a")
    }
    if exact_labels != EXPECTED_EXACT_SCORES or other_outcomes != {"h", "d", "a"}:
        return None

    weighted_home = 0.0
    weighted_away = 0.0
    total_probability = 0.0
    for option in match.score_options:
        coordinates = _score_coordinates(option)
        if coordinates is None or option.probability <= 0:
            continue
        probability = float(option.probability)
        weighted_home += coordinates[0] * probability
        weighted_away += coordinates[1] * probability
        total_probability += probability
    if total_probability <= 0:
        return None

    # Bounds avoid unstable distributions when an upstream market is malformed.
    home_rate = min(4.5, max(0.15, weighted_home / total_probability))
    away_rate = min(4.5, max(0.15, weighted_away / total_probability))
    return home_rate, away_rate


def _poisson(rate: float, goals: int) -> float:
    return math.exp(-rate) * rate**goals / math.factorial(goals)


def _blend(option: ScoreOption, model_probability: float, weight: float) -> ScoreOption:
    probability = (1.0 - weight) * float(option.probability) + weight * model_probability
    return replace(option, probability=Decimal(str(probability)))


def analyze_score_options(match: Match, weight: float) -> tuple[ScoreOption, ...]:
    rates = estimate_expected_goals(match)
    if rates is None or weight <= 0:
        return match.score_options
    home_rate, away_rate = rates
    exact_scores = {
        (int(coordinates.group(1)), int(coordinates.group(2)))
        for option in match.score_options
        if (coordinates := EXACT_SCORE.fullmatch(option.label.strip())) is not None
    }
    other_probabilities = {"h": 0.0, "d": 0.0, "a": 0.0}
    for home_goals in range(MAX_POISSON_GOALS + 1):
        for away_goals in range(MAX_POISSON_GOALS + 1):
            if (home_goals, away_goals) in exact_scores:
                continue
            probability = _poisson(home_rate, home_goals) * _poisson(away_rate, away_goals)
            outcome = "h" if home_goals > away_goals else "a" if home_goals < away_goals else "d"
            other_probabilities[outcome] += probability

    model_probabilities: list[float] = []
    for option in match.score_options:
        coordinates = EXACT_SCORE.fullmatch(option.label.strip())
        if coordinates is not None:
            home_goals = int(coordinates.group(1))
            away_goals = int(coordinates.group(2))
            model_probability = _poisson(home_rate, home_goals) * _poisson(away_rate, away_goals)
        elif "胜" in option.label:
            model_probability = other_probabilities["h"]
        elif "平" in option.label:
            model_probability = other_probabilities["d"]
        elif "负" in option.label:
            model_probability = other_probabilities["a"]
        else:
            return match.score_options
        model_probabilities.append(model_probability)

    total = sum(model_probabilities)
    if total <= 0:
        return match.score_options
    return tuple(
        _blend(option, model_probability / total, weight)
        for option, model_probability in zip(match.score_options, model_probabilities, strict=True)
    )


def analyze_had_options(match: Match, weight: float) -> tuple[ScoreOption, ...]:
    rates = estimate_expected_goals(match)
    if rates is None or weight <= 0 or len(match.had_options) != 3:
        return match.had_options
    home_rate, away_rate = rates
    model = {"h": 0.0, "d": 0.0, "a": 0.0}
    for home_goals in range(MAX_POISSON_GOALS + 1):
        home_probability = _poisson(home_rate, home_goals)
        for away_goals in range(MAX_POISSON_GOALS + 1):
            probability = home_probability * _poisson(away_rate, away_goals)
            code = "h" if home_goals > away_goals else "a" if home_goals < away_goals else "d"
            model[code] += probability
    total = sum(model.values())
    if total <= 0:
        return match.had_options
    return tuple(
        _blend(option, model.get(option.code, 0.0) / total, weight)
        for option in match.had_options
    )
