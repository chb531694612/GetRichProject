from __future__ import annotations

import json
import html as html_lib
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .domain import Match, MatchResult, ResultStatus, ScoreOption


CRS_CODE = re.compile(r"^s(\d{2})s(\d{2})$")
OTHER_SCORE_CODES = {
    "s1sh": "胜其它",
    "s1sd": "平其它",
    "s1sa": "负其它",
}
EXPECTED_CRS_CODES = {
    "s01s00", "s02s00", "s02s01", "s03s00", "s03s01", "s03s02",
    "s04s00", "s04s01", "s04s02", "s05s00", "s05s01", "s05s02",
    "s00s00", "s01s01", "s02s02", "s03s03",
    "s00s01", "s00s02", "s01s02", "s00s03", "s01s03", "s02s03",
    "s00s04", "s01s04", "s02s04", "s00s05", "s01s05", "s02s05",
    *OTHER_SCORE_CODES.keys(),
}
HAD_OUTCOME_CODES = {
    "h": "主胜",
    "d": "平",
    "a": "客胜",
    "home": "主胜",
    "draw": "平",
    "away": "客胜",
}
HAD_CODE_ALIASES = {
    "h": "h",
    "home": "h",
    "胜": "h",
    "主胜": "h",
    "d": "d",
    "draw": "d",
    "平": "d",
    "a": "a",
    "away": "a",
    "负": "a",
    "客胜": "a",
    "主负": "a",
}


class ProviderError(RuntimeError):
    """A data source failed or returned an unexpected response."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= Decimal("1"):
        return None
    return number


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_datetime(value: Any, tz, *, fallback_date: str = "", fallback_time: str = "") -> datetime:
    raw = _str(value)
    candidates = [raw]
    if fallback_date or fallback_time:
        candidates.append(f"{fallback_date} {fallback_time}".strip())
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
        except ValueError:
            pass
        for fmt in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed.replace(tzinfo=tz)
            except ValueError:
                continue
    raise ProviderError(f"unrecognised match datetime: {raw or (fallback_date + ' ' + fallback_time)}")


def _parse_update_time(value: Any, start_at: datetime) -> datetime | None:
    raw = _str(value)
    if not raw:
        return None
    if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", raw):
        parts = [int(part) for part in raw.split(":")]
        while len(parts) < 3:
            parts.append(0)
        combined = datetime.combine(start_at.date(), time(*parts), tzinfo=start_at.tzinfo)
        # Odds commonly update on the previous day for after-midnight matches.
        if combined > start_at:
            combined -= timedelta(days=1)
        return combined
    try:
        return _parse_datetime(raw, start_at.tzinfo)
    except ProviderError:
        return None


def _is_truthy_flag(value: Any) -> bool:
    return _str(value).lower() not in {"", "0", "false", "no", "none", "null"}


def _crs_is_available_for_all_up(raw: dict[str, Any]) -> bool:
    """Use the CRS pool status when the calculator exposes it."""
    if _str(raw.get("matchStatus")).lower() != "selling":
        return False
    if _str(raw.get("sellStatus")) != "1":
        return False
    pool_list = raw.get("poolList")
    if not isinstance(pool_list, list):
        return False
    crs_pools = [
        pool
        for pool in pool_list
        if isinstance(pool, dict) and _str(pool.get("poolCode")).lower() == "crs"
    ]
    if len(crs_pools) != 1:
        return False
    pool = crs_pools[0]
    if _str(pool.get("poolStatus")).lower() != "selling":
        return False
    for key in ("bettingAllup", "bettingAllUp", "cbtAllUp"):
        if key in pool:
            return _is_truthy_flag(pool[key])
    return False


def _pool_is_available_for_all_up(raw: dict[str, Any], pool_code: str) -> bool:
    if _str(raw.get("matchStatus")).lower() != "selling":
        return False
    if _str(raw.get("sellStatus")) != "1":
        return False
    pool_list = raw.get("poolList")
    if not isinstance(pool_list, list):
        return False
    pools = [
        pool
        for pool in pool_list
        if isinstance(pool, dict) and _str(pool.get("poolCode")).lower() == pool_code.lower()
    ]
    if len(pools) != 1:
        return False
    pool = pools[0]
    if _str(pool.get("poolStatus")).lower() != "selling":
        return False
    for key in ("bettingAllup", "bettingAllUp", "cbtAllUp"):
        if key in pool:
            return _is_truthy_flag(pool[key])
    return False


def _score_options(raw: dict[str, Any]) -> tuple[ScoreOption, ...]:
    quoted: list[tuple[str, str, Decimal, bool]] = []
    for code, value in raw.items():
        exact = CRS_CODE.fullmatch(code)
        if exact:
            odds = _decimal(value)
            if odds is None:
                continue
            home, away = int(exact.group(1)), int(exact.group(2))
            quoted.append((code, f"{home}:{away}", odds, False))
        elif code in OTHER_SCORE_CODES:
            odds = _decimal(value)
            if odds is not None:
                quoted.append((code, OTHER_SCORE_CODES[code], odds, True))
    if not quoted:
        return ()
    overround = sum((Decimal("1") / item[2] for item in quoted), Decimal("0"))
    if overround <= 0:
        return ()
    return tuple(
        ScoreOption(
            code=code,
            label=label,
            odds=odds,
            probability=(Decimal("1") / odds) / overround,
            is_other=is_other,
        )
        for code, label, odds, is_other in quoted
    )


def _had_options(raw: dict[str, Any]) -> tuple[ScoreOption, ...]:
    quoted: dict[str, tuple[str, Decimal]] = {}
    for code, value in raw.items():
        normalized = HAD_CODE_ALIASES.get(_str(code).lower())
        if normalized is None:
            continue
        odds = _decimal(value)
        if odds is None:
            continue
        quoted[normalized] = (HAD_OUTCOME_CODES[normalized], odds)
    if set(quoted) != {"h", "d", "a"}:
        return ()
    overround = sum((Decimal("1") / item[1] for item in quoted.values()), Decimal("0"))
    if overround <= 0:
        return ()
    return tuple(
        ScoreOption(
            code=code,
            label=label,
            odds=odds,
            probability=(Decimal("1") / odds) / overround,
            is_other=False,
        )
        for code, (label, odds) in (("h", quoted["h"]), ("d", quoted["d"]), ("a", quoted["a"]))
    )


def _pass_size(formula: Any) -> int | None:
    normalized = _str(formula).lower().replace("×", "x").replace("*", "x").replace("串", "x")
    match = re.fullmatch(r"\s*([2-8])\s*x\s*1\s*", normalized)
    return int(match.group(1)) if match else None


def _all_up_pass_sizes(all_up_list: Any, pool_code: str, *, required: set[int]) -> frozenset[int]:
    if isinstance(all_up_list, dict):
        all_up_list = (
            all_up_list.get(pool_code.upper())
            or all_up_list.get(pool_code.lower())
            or all_up_list.get(pool_code)
        )
    if not isinstance(all_up_list, list) or not all_up_list:
        return frozenset(required)
    supported = frozenset(
        size
        for item in all_up_list
        if isinstance(item, dict)
        and (
            not _str(item.get("poolCode"))
            or _str(item.get("poolCode")).lower() == pool_code.lower()
        )
        if (size := _pass_size(item.get("formula"))) is not None
    )
    if not supported.intersection(required):
        raise ProviderError(
            f"official calculator does not currently expose {pool_code.upper()} "
            f"{'/'.join(f'{size}x1' for size in sorted(required))}"
        )
    return supported


def parse_sporttery_matches(payload: dict[str, Any], tz) -> list[Match]:
    """Parse the payload used by the official site's odds calculator."""
    value = payload.get("value", {})
    if not isinstance(value, dict):
        raise ProviderError("odds payload has no value object")
    supported_pass_sizes = _all_up_pass_sizes(value.get("allUpList"), "crs", required={2, 3, 4})
    groups = value.get("matchInfoList", [])
    if "matchInfoList" not in value:
        raise ProviderError("odds payload is missing value.matchInfoList")
    if not isinstance(groups, list):
        raise ProviderError("odds payload has no value.matchInfoList list")
    matches: list[Match] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for raw in group.get("subMatchList", []) or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("crs"), dict):
                continue
            start_at = _parse_datetime(
                raw.get("matchDateTime"),
                tz,
                fallback_date=_str(raw.get("matchDate")),
                fallback_time=_str(raw.get("matchTime")),
            )
            options = _score_options(raw["crs"])
            if not options:
                continue
            crs_update = raw["crs"].get("updateTime")
            update_date = _str(raw["crs"].get("updateDate"))
            if update_date and crs_update and re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", _str(crs_update)):
                crs_update = f"{update_date} {crs_update}"
            matches.append(
                Match(
                    match_id=_str(raw.get("matchId")),
                    match_num=_str(raw.get("matchNumStr") or raw.get("matchNum")),
                    business_date=_str(
                        raw.get("businessDate")
                        or group.get("businessDate")
                        or raw.get("matchNumDate")
                        or group.get("matchNumDate")
                        or start_at.date().isoformat()
                    ),
                    league=_str(raw.get("leagueAbbName") or raw.get("leagueAllName") or raw.get("leagueName")),
                    home=_str(raw.get("homeTeamAbbName") or raw.get("homeTeamAllName") or raw.get("homeTeam")),
                    away=_str(raw.get("awayTeamAbbName") or raw.get("awayTeamAllName") or raw.get("awayTeam")),
                    start_at=start_at,
                    odds_updated_at=_parse_update_time(crs_update, start_at),
                    score_options=options,
                    betting_all_up=_crs_is_available_for_all_up(raw),
                    supported_pass_sizes=supported_pass_sizes,
                    status=_str(raw.get("matchStatus")),
                )
            )
    return [match for match in matches if match.match_id and match.home and match.away]


def parse_sporttery_had_by_match_id(
    payload: dict[str, Any], tz
) -> tuple[dict[str, dict[str, Any]], frozenset[int]]:
    """Parse HAD markets keyed by match_id from an official calculator payload."""
    value = payload.get("value", {})
    if not isinstance(value, dict):
        raise ProviderError("HAD odds payload has no value object")
    supported_pass_sizes = _all_up_pass_sizes(value.get("allUpList"), "had", required={4, 5, 6})
    groups = value.get("matchInfoList", [])
    if not isinstance(groups, list):
        raise ProviderError("HAD odds payload has no value.matchInfoList list")
    by_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for raw in group.get("subMatchList", []) or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("had"), dict):
                continue
            match_id = _str(raw.get("matchId"))
            options = _had_options(raw["had"])
            if not match_id or not options:
                continue
            start_at = _parse_datetime(
                raw.get("matchDateTime"),
                tz,
                fallback_date=_str(raw.get("matchDate")),
                fallback_time=_str(raw.get("matchTime")),
            )
            had_update = raw["had"].get("updateTime")
            update_date = _str(raw["had"].get("updateDate"))
            if update_date and had_update and re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", _str(had_update)):
                had_update = f"{update_date} {had_update}"
            by_id[match_id] = {
                "options": options,
                "betting_all_up": _pool_is_available_for_all_up(raw, "had"),
                "odds_updated_at": _parse_update_time(had_update, start_at),
            }
    return by_id, supported_pass_sizes


def parse_normalized_matches(payload: dict[str, Any], tz) -> list[Match]:
    root = payload.get("data", payload)
    raw_matches = root.get("matches", []) if isinstance(root, dict) else []
    matches: list[Match] = []
    for raw in raw_matches:
        if not isinstance(raw, dict):
            continue
        markets = raw.get("markets", {})
        crs = markets.get("crs", {}) if isinstance(markets, dict) else {}
        had = markets.get("had", {}) if isinstance(markets, dict) else {}
        outcomes = crs.get("outcomes", raw.get("score_options", [])) if isinstance(crs, dict) else []
        options: list[ScoreOption] = []
        for outcome in outcomes or []:
            if not isinstance(outcome, dict):
                continue
            label = _str(outcome.get("labelZh") or outcome.get("label") or outcome.get("key"))
            odds = _decimal(outcome.get("odds"))
            if not label or odds is None:
                continue
            code = _str(outcome.get("code") or outcome.get("key") or label)
            is_other = "其它" in label or "other" in label.lower() or code in OTHER_SCORE_CODES
            exact = CRS_CODE.fullmatch(code)
            parsed_label = _parse_score_value(label)
            if not is_other and exact:
                label = f"{int(exact.group(1))}:{int(exact.group(2))}"
            elif not is_other and parsed_label is not None:
                label = f"{parsed_label[0]}:{parsed_label[1]}"
            probability_raw = outcome.get("noVigProb") or outcome.get("probability")
            probability = Decimal(str(probability_raw)) if probability_raw not in (None, "") else Decimal("0")
            options.append(
                ScoreOption(
                    code=code,
                    label=label,
                    odds=odds,
                    probability=probability,
                    is_other=is_other,
                )
            )
        if options and all(option.probability <= 0 for option in options):
            overround = sum((Decimal("1") / option.odds for option in options), Decimal("0"))
            options = [
                ScoreOption(
                    code=option.code,
                    label=option.label,
                    odds=option.odds,
                    probability=(Decimal("1") / option.odds) / overround,
                    is_other=option.is_other,
                )
                for option in options
            ]
        had_options: list[ScoreOption] = []
        had_outcomes = had.get("outcomes", []) if isinstance(had, dict) else []
        for outcome in had_outcomes or []:
            if not isinstance(outcome, dict):
                continue
            raw_code = _str(outcome.get("code") or outcome.get("key") or outcome.get("labelZh"))
            code = HAD_CODE_ALIASES.get(raw_code.lower())
            odds = _decimal(outcome.get("odds"))
            if code is None or odds is None:
                continue
            probability_raw = outcome.get("noVigProb") or outcome.get("probability")
            probability = Decimal(str(probability_raw)) if probability_raw not in (None, "") else Decimal("0")
            had_options.append(
                ScoreOption(
                    code=code,
                    label=HAD_OUTCOME_CODES[code],
                    odds=odds,
                    probability=probability,
                    is_other=False,
                )
            )
        if had_options and all(option.probability <= 0 for option in had_options):
            overround = sum((Decimal("1") / option.odds for option in had_options), Decimal("0"))
            had_options = [
                ScoreOption(
                    code=option.code,
                    label=option.label,
                    odds=option.odds,
                    probability=(Decimal("1") / option.odds) / overround,
                    is_other=False,
                )
                for option in had_options
            ]
        if len(had_options) != 3 or {option.code for option in had_options} != {"h", "d", "a"}:
            had_options = []
        else:
            order = {"h": 0, "d": 1, "a": 2}
            had_options = sorted(had_options, key=lambda item: order[item.code])
        start_raw = raw.get("start_at") or raw.get("matchDateTime")
        if not start_raw and raw.get("start_offset_minutes") is not None:
            start_raw = datetime.now(tz) + timedelta(minutes=int(raw["start_offset_minutes"]))
        start_at = _parse_datetime(
            start_raw,
            tz,
            fallback_date=_str(raw.get("matchDate")),
            fallback_time=_str(raw.get("matchTime")),
        )
        home_raw = raw.get("home", {})
        away_raw = raw.get("away", {})
        league_raw = raw.get("league", {})
        home = _str(home_raw.get("abbName") if isinstance(home_raw, dict) else home_raw)
        away = _str(away_raw.get("abbName") if isinstance(away_raw, dict) else away_raw)
        league = _str(league_raw.get("abbName") if isinstance(league_raw, dict) else league_raw)
        supported = frozenset(
            int(value)
            for value in raw.get("supported_pass_sizes", (2, 3, 4))
            if int(value) in {2, 3, 4}
        ) or frozenset({2, 3, 4})
        had_supported = frozenset(
            int(value)
            for value in raw.get("had_supported_pass_sizes", (4, 5, 6))
            if int(value) in {4, 5, 6}
        ) or frozenset({4, 5, 6})
        matches.append(
            Match(
                match_id=_str(raw.get("matchId") or raw.get("match_id")),
                match_num=_str(raw.get("matchNumStr") or raw.get("match_num")),
                business_date=_str(raw.get("businessDate") or raw.get("business_date") or start_at.date()),
                league=league,
                home=home,
                away=away,
                start_at=start_at,
                odds_updated_at=_parse_update_time(
                    crs.get("updateTime") if isinstance(crs, dict) else raw.get("odds_updated_at"),
                    start_at,
                ),
                score_options=tuple(options),
                snapshot_fetched_at=_parse_update_time(
                    (crs.get("fetchedAt") if isinstance(crs, dict) else None)
                    or raw.get("snapshot_fetched_at"),
                    start_at,
                ),
                betting_all_up=bool(raw.get("bettingAllUp", raw.get("betting_all_up", True))),
                supported_pass_sizes=supported,
                status=_str(raw.get("status")),
                had_options=tuple(had_options),
                had_betting_all_up=bool(
                    raw.get("hadBettingAllUp", raw.get("had_betting_all_up", bool(had_options)))
                ),
                had_supported_pass_sizes=had_supported,
                had_odds_updated_at=_parse_update_time(
                    had.get("updateTime") if isinstance(had, dict) else raw.get("had_odds_updated_at"),
                    start_at,
                ),
            )
        )
    return matches


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_score_value(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        home = value.get("home") if "home" in value else value.get("homeScore")
        away = value.get("away") if "away" in value else value.get("awayScore")
        if home is not None and away is not None:
            try:
                return int(home), int(away)
            except (TypeError, ValueError):
                return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    raw = _str(value)
    match = re.search(r"(?<!\d)(\d{1,2})\s*[:：\-]\s*(\d{1,2})(?!\d)", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _result_from_record(raw: dict[str, Any]) -> MatchResult | None:
    match_id = _str(raw.get("matchId") or raw.get("match_id") or raw.get("id"))
    if not match_id:
        return None
    home_score = raw.get("homeScore", raw.get("home_score"))
    away_score = raw.get("awayScore", raw.get("away_score"))
    score: tuple[int, int] | None = None
    if home_score is not None and away_score is not None:
        try:
            score = int(home_score), int(away_score)
        except (TypeError, ValueError):
            pass
    if score is None:
        for key in (
            "finalScore",
            "fullScore",
            "matchScore",
            "resultScore",
            "score",
            "sectionsNo999",
        ):
            score = _parse_score_value(raw.get(key))
            if score is not None:
                break
    status_text = " ".join(
        _str(raw.get(key))
        for key in ("status", "matchStatus", "matchResultStatus", "poolStatus", "resultStatus")
    ).strip()
    score_status_text = " ".join(
        _str(raw.get(key))
        for key in ("finalScore", "fullScore", "matchScore", "resultScore", "score", "sectionsNo999")
    ).strip()
    lowered = f"{status_text} {score_status_text}".lower()
    official_result_status = _str(raw.get("matchResultStatus"))
    official_pool_status = _str(raw.get("poolStatus")).lower()
    has_uniform_status = "matchResultStatus" in raw or "poolStatus" in raw
    if has_uniform_status and not (
        official_result_status == "2" and official_pool_status == "payout"
    ):
        # The uniform results feed may publish a live score before the game is
        # official. Only status=2 plus Payout is safe for final settlement.
        status = ResultStatus.PENDING
    elif any(token in lowered for token in ("cancel", "void", "invalid", "取消", "无效", "腰斩")):
        status = ResultStatus.VOID
    elif score is not None and (
        not status_text
        or any(token in lowered for token in ("final", "finished", "payout", "complete", "开奖", "结束"))
        or official_result_status == "2"
    ):
        status = ResultStatus.FINAL
    elif score is not None and not status_text:
        # Normalized offline fixtures may omit status once the final score is published.
        status = ResultStatus.FINAL
    else:
        status = ResultStatus.PENDING
    return MatchResult(
        match_id=match_id,
        status=status,
        home_score=score[0] if score else None,
        away_score=score[1] if score else None,
        official_status=status_text,
    )


def parse_results(payload: dict[str, Any]) -> dict[str, MatchResult]:
    results: dict[str, MatchResult] = {}
    rank = {ResultStatus.PENDING: 0, ResultStatus.FINAL: 1, ResultStatus.VOID: 2}
    for raw in _walk_dicts(payload):
        parsed = _result_from_record(raw)
        if parsed is None:
            continue
        current = results.get(parsed.match_id)
        if current is None or rank[parsed.status] > rank[current.status]:
            results[parsed.match_id] = parsed
    return results


class SportteryProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        import time
        import random

        parsed = urllib.parse.urlsplit(url)
        existing = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        existing.update({key: str(value) for key, value in params.items()})
        full_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(existing), parsed.fragment)
        )
        is_result = "MatchResult" in parsed.path
        referer = (
            "https://www.sporttery.cn/jc/zqsgkj/"
            if is_result
            else "https://m.sporttery.cn/mjc/jsq/zqspf/"
        )
        origin = "https://www.sporttery.cn" if is_result else "https://m.sporttery.cn"
        max_retries = 3
        last_error: Exception | None = None
        for attempt in range(max_retries):
            request = urllib.request.Request(
                full_url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": referer,
                    "Origin": origin,
                    "Connection": "keep-alive",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-site",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.http_timeout_seconds) as response:
                    body = response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (403, 429, 502, 503, 504) and attempt < max_retries - 1:
                    sleep_seconds = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(sleep_seconds)
                    continue
                raise ProviderError(f"data source returned HTTP {exc.code}; stop and verify access/authorization") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    sleep_seconds = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(sleep_seconds)
                    continue
                raise ProviderError(f"cannot reach data source: {exc.reason}") from exc
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderError("data source returned non-JSON content (possibly a WAF page)") from exc
            if not isinstance(payload, dict):
                raise ProviderError("data source returned a non-object JSON payload")
            error_code = _str(payload.get("errorCode"))
            if payload.get("success") is False or (
                error_code and error_code.upper() not in {"0", "E0000", "SUCCESS"}
            ):
                raise ProviderError(_str(payload.get("errorMessage") or payload.get("error") or "data source error"))
            return payload
        assert last_error is not None
        raise ProviderError(f"data source failed after {max_retries} attempts") from last_error

    def get_matches(self) -> list[Match]:
        fetched_at = datetime.now(self.settings.timezone)
        payload = self._get_json(self.settings.sporttery_odds_url, {"poolCode": "crs", "channel": "c"})
        if payload.get("success") is not True or _str(payload.get("errorCode")) != "0":
            raise ProviderError("official odds wrapper did not report success")
        value = payload.get("value")
        if not isinstance(value, dict):
            raise ProviderError("official odds payload has no value object")
        groups = value.get("matchInfoList")
        all_up_list = value.get("allUpList")
        if isinstance(all_up_list, dict):
            crs_all_up = all_up_list.get("CRS") or all_up_list.get("crs")
        else:
            crs_all_up = all_up_list
        if not isinstance(groups, list) or not isinstance(crs_all_up, list):
            raise ProviderError("official odds schema changed: matchInfoList/allUpList is invalid")
        supported_pass_sizes = {
            size
            for item in crs_all_up
            if isinstance(item, dict)
            and (not _str(item.get("poolCode")) or _str(item.get("poolCode")).lower() == "crs")
            if (size := _pass_size(item.get("formula"))) is not None
        }
        if not supported_pass_sizes.intersection({2, 3, 4}):
            raise ProviderError("official calculator does not expose CRS 2x1, 3x1 or 4x1")
        matches = parse_sporttery_matches(payload, self.settings.timezone)
        # The official CRS market contains exactly 31 mutually exclusive
        # outcomes. Incomplete markets inflate normalized probabilities.
        matches = [
            replace(match, snapshot_fetched_at=fetched_at)
            for match in matches
            if match.betting_all_up
            and {option.code for option in match.score_options} == EXPECTED_CRS_CODES
        ]
        if not self.settings.had_enabled:
            return matches
        try:
            had_payload = self._get_json(
                self.settings.sporttery_odds_url, {"poolCode": "had", "channel": "c"}
            )
            if had_payload.get("success") is not True or _str(had_payload.get("errorCode")) != "0":
                raise ProviderError("official HAD odds wrapper did not report success")
            had_by_id, had_pass_sizes = parse_sporttery_had_by_match_id(
                had_payload, self.settings.timezone
            )
        except ProviderError:
            # HAD failure must not block the exact-score ticket path.
            return matches
        merged: list[Match] = []
        for match in matches:
            had = had_by_id.get(match.match_id)
            if had is None:
                merged.append(match)
                continue
            merged.append(
                replace(
                    match,
                    had_options=had["options"],
                    had_betting_all_up=bool(had["betting_all_up"]),
                    had_supported_pass_sizes=had_pass_sizes,
                    had_odds_updated_at=had["odds_updated_at"],
                )
            )
        return merged

    def get_results(self, start_date: date, end_date: date) -> dict[str, MatchResult]:
        results: dict[str, MatchResult] = {}
        range_start = start_date
        while range_start <= end_date:
            range_end = min(range_start + timedelta(days=29), end_date)
            page_no = 1
            pages = 1
            while page_no <= pages:
                payload = self._get_json(
                    self.settings.sporttery_results_url,
                    {
                        "matchPage": 1,
                        "matchBeginDate": range_start.isoformat(),
                        "matchEndDate": range_end.isoformat(),
                        "leagueId": "",
                        "pageSize": 30,
                        "pageNo": page_no,
                        "isFix": 0,
                        "pcOrWap": 1,
                    },
                )
                value = payload.get("value", {})
                if payload.get("success") is not True or _str(payload.get("errorCode")) != "0":
                    raise ProviderError("official result wrapper did not report success")
                if not isinstance(value, dict) or not isinstance(value.get("matchResult"), list):
                    raise ProviderError("official result schema changed: value.matchResult is invalid")
                results.update(parse_results({"matchResult": value["matchResult"]}))
                try:
                    pages = max(1, int(value.get("pages", 1)))
                except (TypeError, ValueError) as exc:
                    raise ProviderError("official result page count is invalid") from exc
                if pages > 20:
                    raise ProviderError("official result page count exceeded the safety limit")
                page_no += 1
            range_start = range_end + timedelta(days=1)
        return results


class JsonProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _payload(self) -> dict[str, Any]:
        path = self.settings.json_data_file
        if not path.exists():
            raise ProviderError(f"JSON data file does not exist: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON data file: {path}") from exc
        if not isinstance(value, dict):
            raise ProviderError("JSON data file root must be an object")
        return value

    def get_matches(self) -> list[Match]:
        payload = self._payload()
        if isinstance(payload.get("value", {}).get("matchInfoList"), list):
            return parse_sporttery_matches(payload, self.settings.timezone)
        return parse_normalized_matches(payload, self.settings.timezone)

    def get_results(self, start_date: date, end_date: date) -> dict[str, MatchResult]:
        del start_date, end_date
        payload = self._payload()
        root = payload.get("data", payload)
        results_payload = {"results": root.get("results", [])} if isinstance(root, dict) else {"results": []}
        return parse_results(results_payload)


def build_provider(settings: Settings):
    if settings.data_provider == "json":
        return JsonProvider(settings)
    if settings.data_provider == "okooo":
        return OkoooProvider(settings)
    return SportteryProvider(settings)


_OKOOO_CRS_LABEL_MAP: dict[str, tuple[str, bool]] = {
    "1-0": ("s01s00", False),
    "2-0": ("s02s00", False),
    "2-1": ("s02s01", False),
    "3-0": ("s03s00", False),
    "3-1": ("s03s01", False),
    "3-2": ("s03s02", False),
    "4-0": ("s04s00", False),
    "4-1": ("s04s01", False),
    "4-2": ("s04s02", False),
    "5-0": ("s05s00", False),
    "5-1": ("s05s01", False),
    "5-2": ("s05s02", False),
    "胜其他": ("s1sh", True),
    "0-0": ("s00s00", False),
    "1-1": ("s01s01", False),
    "2-2": ("s02s02", False),
    "3-3": ("s03s03", False),
    "平其他": ("s1sd", True),
    "0-1": ("s00s01", False),
    "0-2": ("s00s02", False),
    "1-2": ("s01s02", False),
    "0-3": ("s00s03", False),
    "1-3": ("s01s03", False),
    "2-3": ("s02s03", False),
    "0-4": ("s00s04", False),
    "1-4": ("s01s04", False),
    "2-4": ("s02s04", False),
    "0-5": ("s00s05", False),
    "1-5": ("s01s05", False),
    "2-5": ("s02s05", False),
    "负其他": ("s1sa", True),
}


def _okooo_crs_options_from_html(html: str) -> tuple[ScoreOption, ...]:
    quoted: list[tuple[str, str, Decimal, bool]] = []
    # Attribute order and nesting on Okooo have changed more than once. Locate
    # the option start tag, then inspect a small bounded window for its label.
    start_pattern = re.compile(r"<(?P<tag>[a-z][\w:-]*)\b(?P<attrs>[^>]*)>", re.IGNORECASE)
    for match in start_pattern.finditer(html):
        attrs = _html_attributes(match.group("attrs"))
        if "ping" not in attrs.get("class", "").split() or "data-sp" not in attrs:
            continue
        label_match = re.search(
            r'<(?:div|span)\b[^>]*class=["\'][^"\']*\bpeilv\b[^"\']*["\'][^>]*>(.*?)</(?:div|span)>',
            html[match.end():match.end() + 800],
            re.IGNORECASE | re.DOTALL,
        )
        if label_match is None:
            continue
        sp_raw = _str(attrs["data-sp"])
        label_raw = html_lib.unescape(re.sub(r"<[^>]+>", "", label_match.group(1))).strip()
        odds = _decimal(sp_raw)
        if odds is None:
            continue
        mapped = _OKOOO_CRS_LABEL_MAP.get(label_raw)
        if mapped is None:
            continue
        code, is_other = mapped
        quoted.append((code, label_raw, odds, is_other))
    if not quoted:
        return ()
    overround = sum((Decimal("1") / item[2] for item in quoted), Decimal("0"))
    if overround <= 0:
        return ()
    return tuple(
        ScoreOption(
            code=code,
            label=label.replace("-", ":") if not is_other else label,
            odds=odds,
            probability=(Decimal("1") / odds) / overround,
            is_other=is_other,
        )
        for code, label, odds, is_other in quoted
    )


def _html_attributes(raw: str) -> dict[str, str]:
    """Return lower-cased HTML attributes with either quote style."""
    return {
        name.lower(): html_lib.unescape(value)
        for name, _quote, value in re.findall(
            r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", raw, re.DOTALL
        )
    }


def _okooo_parse_matches_from_html(html: str, tz) -> dict[str, dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    pattern = re.compile(
        r'<div\b(?P<attrs>[^>]*(?:id=["\']match_\d+["\']|class=["\'][^"\']*\btouzhu_\d+\b[^"\']*["\'])[^>]*)>',
        re.IGNORECASE | re.DOTALL,
    )
    league_pattern = re.compile(
        r'<a[^>]*class="saiming[^"]*"[^>]*>([^<]+)</a>',
        re.DOTALL,
    )
    time_pattern = re.compile(
        r'title="比赛时间:([^"]+)"',
        re.DOTALL,
    )
    idx = 0
    while True:
        m = pattern.search(html, idx)
        if not m:
            break
        attrs = _html_attributes(m.group("attrs"))
        id_match = re.fullmatch(r"match_(\d+)", attrs.get("id", ""), re.IGNORECASE)
        if id_match is None or not re.search(r"(?:^|\s)touzhu_\d+(?:\s|$)", attrs.get("class", "")):
            idx = m.end()
            continue
        match_id = id_match.group(1)
        order_cn = attrs.get("data-ordercn", "").strip()
        home = attrs.get("data-hname", "").strip()
        away = attrs.get("data-aname", "").strip()
        if not order_cn or not home or not away:
            idx = m.end()
            continue
        block_end = html.find('<div class="touzhu_', m.end())
        if block_end == -1:
            block_end = len(html)
        block = html[m.start():block_end]
        league_m = league_pattern.search(block)
        league = html_lib.unescape(league_m.group(1)).strip() if league_m else ""
        time_m = time_pattern.search(block)
        start_at: datetime | None = None
        if time_m:
            time_str = html_lib.unescape(time_m.group(1)).strip()
            try:
                start_at = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            except ValueError:
                try:
                    start_at = datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                except ValueError:
                    pass
        if start_at is None:
            idx = m.end()
            continue
        matches[match_id] = {
            "match_id": match_id,
            "match_num": order_cn,
            "league": league,
            "home": home,
            "away": away,
            "start_at": start_at,
        }
        idx = m.end()
    # Current (2026) desktop page uses table rows and puts the stable match id
    # in the match-detail URL instead of data attributes.
    row_pattern = re.compile(
        r'<tr\b(?P<attrs>[^>]*)>(?P<body>.*?)</tr>', re.IGNORECASE | re.DOTALL
    )
    for row in row_pattern.finditer(html):
        row_attrs = _html_attributes(row.group("attrs"))
        body = row.group("body")
        id_match = re.search(r'/soccer/match/(\d+)/', body, re.IGNORECASE)
        time_match = re.search(
            r'title=["\']比赛时间\s*[:：]\s*([^"\']+)["\']', body, re.IGNORECASE
        )
        if id_match is None or time_match is None:
            continue
        match_id = id_match.group(1)
        try:
            start_at = _parse_datetime(html_lib.unescape(time_match.group(1)), tz)
        except ProviderError:
            continue
        number_match = re.search(
            r'<span\b[^>]*class=["\'][^"\']*\bxh\b[^"\']*["\'][^>]*>\s*<i[^>]*>([^<]+)',
            body, re.IGNORECASE | re.DOTALL,
        )
        league_match = re.search(
            r'<a\b[^>]*class=["\'][^"\']*\bls\b[^"\']*["\'][^>]*>([^<]+)</a>',
            body, re.IGNORECASE | re.DOTALL,
        )
        teams = re.findall(
            r'<a\b[^>]*class=["\'][^"\']*\bduinameh\b[^"\']*["\'][^>]*>([^<]+)</a>',
            body, re.IGNORECASE | re.DOTALL,
        )
        if number_match is None or len(teams) < 2:
            continue
        inline_had: tuple[ScoreOption, ...] = ()
        had_box = re.search(
            r'<div\b[^>]*class=["\'][^"\']*\bfrqBetObj\b[^"\']*["\'][^>]*>(.*?)</div>',
            body, re.IGNORECASE | re.DOTALL,
        )
        if had_box:
            values = [
                _decimal(re.sub(r"<[^>]+>", "", value).strip())
                for value in re.findall(
                    r'<a\b[^>]*class=["\'][^"\']*\bbetObj\b[^"\']*["\'][^>]*>(.*?)</a>',
                    had_box.group(1), re.IGNORECASE | re.DOTALL,
                )
            ]
            if len(values) >= 3 and all(value is not None for value in values[:3]):
                h_odds, d_odds, a_odds = values[:3]
                assert h_odds is not None and d_odds is not None and a_odds is not None
                overround = Decimal("1") / h_odds + Decimal("1") / d_odds + Decimal("1") / a_odds
                inline_had = (
                    ScoreOption("h", "主胜", h_odds, (Decimal("1") / h_odds) / overround),
                    ScoreOption("d", "平", d_odds, (Decimal("1") / d_odds) / overround),
                    ScoreOption("a", "客胜", a_odds, (Decimal("1") / a_odds) / overround),
                )
        matches[match_id] = {
            "match_id": match_id,
            "match_num": html_lib.unescape(number_match.group(1)).strip(),
            "match_order": re.sub(r"^tr", "", row_attrs.get("id", ""), flags=re.IGNORECASE),
            "league": html_lib.unescape(league_match.group(1)).strip() if league_match else "",
            "home": html_lib.unescape(teams[0]).strip(),
            "away": html_lib.unescape(teams[1]).strip(),
            "start_at": start_at,
            "had_options": inline_had,
            "selling": row_attrs.get("isover", "1") == "1",
            "result_score": _okooo_score_from_row(body),
            "void_reason": _okooo_void_reason_from_row(body),
        }
    return matches


def _okooo_score_from_row(row_html: str) -> tuple[int, int] | None:
    score_match = re.search(
        r'<(?:b|span)\b[^>]*class=["\'][^"\']*\bbftext\b[^"\']*["\'][^>]*>\s*([^<]+)',
        row_html,
        re.IGNORECASE | re.DOTALL,
    )
    if score_match is None:
        return None
    return _parse_score_value(html_lib.unescape(score_match.group(1)).strip())


def _okooo_void_reason_from_row(row_html: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", row_html))
    for marker in ("取消", "延期", "腰斩", "中断", "推迟"):
        if marker in text:
            return marker
    return ""


def _okooo_business_date(start_at: datetime) -> str:
    if start_at.hour < 12:
        return (start_at - timedelta(days=1)).date().isoformat()
    return start_at.date().isoformat()


class OkoooProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _get(self, url: str, *, accept_html: bool = False) -> str:
        import time
        import random

        max_retries = 3
        last_error: Exception | None = None
        for attempt in range(max_retries):
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                        if accept_html
                        else "application/json, text/plain, */*"
                    ),
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": self.settings.okooo_base_url + "/jingcai/shengpingfu/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.http_timeout_seconds) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (403, 429, 502, 503, 504) and attempt < max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                raise ProviderError(f"data source returned HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                raise ProviderError(f"cannot reach data source: {exc.reason}") from exc
            charset_match = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.IGNORECASE)
            charset = charset_match.group(1) if charset_match else ""
            if not charset:
                head = body[:4096].decode("ascii", errors="ignore")
                meta_match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.IGNORECASE)
                charset = meta_match.group(1) if meta_match else "utf-8"
            if charset.lower() in {"gb2312", "gbk"}:
                charset = "gb18030"
            try:
                return body.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise ProviderError(f"failed to decode response body as {charset}") from exc
        assert last_error is not None
        raise ProviderError(f"data source failed after {max_retries} attempts") from last_error

    def _get_json(self, url: str) -> dict[str, Any]:
        body = self._get(url, accept_html=False)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("data source returned non-JSON content") from exc
        if not isinstance(payload, dict):
            raise ProviderError("data source returned a non-object JSON payload")
        return payload

    def get_matches(self) -> list[Match]:
        fetched_at = datetime.now(self.settings.timezone)
        page_url = self.settings.okooo_base_url + "/jingcai/shengpingfu/"
        page_html = self._get(page_url, accept_html=True)
        match_info = _okooo_parse_matches_from_html(page_html, self.settings.timezone)
        if not match_info:
            raise ProviderError("okooo page has no match data")

        today_str = fetched_at.date().isoformat()
        odds_url = (
            self.settings.okooo_base_url
            + "/ajax/?method=odds.sporttery.endodds&format=json"
            + "&LotteryNo=" + today_str
            + "&act=get_oupan&index=0&LotteryType=SportterySoccerMix&v="
        )
        try:
            odds_payload = self._get_json(odds_url)
            odds_root = odds_payload.get("sporttery_endodds_response")
        except ProviderError:
            odds_root = {}
        if not isinstance(odds_root, dict):
            odds_root = {}

        had_by_id: dict[str, tuple[ScoreOption, ...]] = {}
        for mid, odds_list in odds_root.items():
            if not isinstance(odds_list, list) or len(odds_list) < 3:
                continue
            h_odds = _decimal(odds_list[0])
            d_odds = _decimal(odds_list[1])
            a_odds = _decimal(odds_list[2])
            if h_odds is None or d_odds is None or a_odds is None:
                continue
            overround = (
                Decimal("1") / h_odds + Decimal("1") / d_odds + Decimal("1") / a_odds
            )
            if overround <= 0:
                continue
            had_by_id[mid] = (
                ScoreOption(code="h", label="主胜", odds=h_odds, probability=(Decimal("1") / h_odds) / overround),
                ScoreOption(code="d", label="平", odds=d_odds, probability=(Decimal("1") / d_odds) / overround),
                ScoreOption(code="a", label="客胜", odds=a_odds, probability=(Decimal("1") / a_odds) / overround),
            )

        matches: list[Match] = []
        crs_supported = frozenset({3, 4})
        had_supported = frozenset({4, 5, 6})
        for mid, info in match_info.items():
            if not info.get("selling", True):
                continue
            had_options = had_by_id.get(mid, info.get("had_options", ()))
            crs_options: tuple[ScoreOption, ...] = ()
            try:
                crs_url = (
                    self.settings.okooo_base_url
                    + "/jingcai/?"
                    + urllib.parse.urlencode(
                        {
                            "action": "more",
                            "LotteryNo": _okooo_business_date(info["start_at"]),
                            # The expand endpoint needs the internal row order
                            # (for example 1201), not the visible number (201).
                            "MatchOrder": info.get("match_order") or info["match_num"],
                        }
                    )
                )
                crs_html = self._get(crs_url, accept_html=True)
                crs_options = _okooo_crs_options_from_html(crs_html)
            except ProviderError:
                pass

            has_crs = bool(crs_options) and {opt.code for opt in crs_options} == EXPECTED_CRS_CODES
            has_had = bool(had_options) and len(had_options) == 3

            if not has_crs and not has_had:
                continue

            matches.append(
                Match(
                    match_id=mid,
                    match_num=info["match_num"],
                    business_date=_okooo_business_date(info["start_at"]),
                    league=info["league"],
                    home=info["home"],
                    away=info["away"],
                    start_at=info["start_at"],
                    odds_updated_at=fetched_at,
                    score_options=crs_options,
                    snapshot_fetched_at=fetched_at,
                    betting_all_up=has_crs,
                    supported_pass_sizes=crs_supported if has_crs else frozenset(),
                    had_options=had_options,
                    had_betting_all_up=has_had,
                    had_supported_pass_sizes=had_supported if has_had else frozenset(),
                    had_odds_updated_at=fetched_at if has_had else None,
                    status="selling",
                )
            )
        return matches

    def get_results(self, start_date: date, end_date: date) -> dict[str, MatchResult]:
        results: dict[str, MatchResult] = {}
        successful_pages = 0
        last_error: ProviderError | None = None
        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            url = (
                self.settings.okooo_base_url
                + "/jingcai/shengpingfu/?LotteryNo=" + date_str
            )
            try:
                html = self._get(url, accept_html=True)
            except ProviderError as exc:
                last_error = exc
                current += timedelta(days=1)
                continue
            successful_pages += 1
            match_info = _okooo_parse_matches_from_html(html, self.settings.timezone)
            for mid in match_info:
                if mid not in results:
                    results[mid] = MatchResult(
                        match_id=mid,
                        status=ResultStatus.PENDING,
                        official_status="",
                    )
            for mid, info in match_info.items():
                score_pair = info.get("result_score")
                if score_pair is not None:
                    score_text = f"{score_pair[0]}-{score_pair[1]}"
                    results[mid] = MatchResult(
                        match_id=mid,
                        status=ResultStatus.FINAL,
                        home_score=score_pair[0],
                        away_score=score_pair[1],
                        official_status=score_text,
                    )
                elif info.get("void_reason"):
                    results[mid] = MatchResult(
                        match_id=mid,
                        status=ResultStatus.VOID,
                        official_status=str(info["void_reason"]),
                    )
            current += timedelta(days=1)
        if successful_pages == 0:
            if last_error is not None:
                raise ProviderError("all okooo result page requests failed") from last_error
            raise ProviderError("no okooo result page could be loaded")
        return results
