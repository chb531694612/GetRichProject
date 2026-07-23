from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from .config import Settings
from .domain import MarketType, ScoreOption

LOGGER = logging.getLogger(__name__)


class AIAnalysisError(Exception):
    """AI analysis failed but should not block the recommendation pipeline."""


@dataclass(frozen=True, slots=True)
class AIOptionSuggestion:
    match_id: str
    option_code: str
    pick_label: str
    reason: str


@dataclass(frozen=True, slots=True)
class AIPlanAnalysis:
    summary: str
    suggestions: tuple[AIOptionSuggestion, ...]


def _build_prompt(matches: list[tuple[Any, ScoreOption]], market: MarketType) -> str:
    lines: list[str] = [
        "你是一名审慎的足球比赛分析师。请先联网搜索每场比赛双方球队的近期公开信息，再进行分析。",
        "",
        f"玩法：{'比分' if market is MarketType.CRS else '胜平负'}串关",
        "",
        "请从以下几个维度分析：",
        "1. 双方近期状态、伤停、赛程密度和主客场表现；",
        "2. 可能影响结果的不确定因素和信息时效风险；",
        "3. 给出一段不超过120字的中文总体判断。",
        "",
        "系统只提供以下基础赛程信息：",
    ]
    for idx, (match, _score) in enumerate(matches, start=1):
        business_date = getattr(match, "business_date", match.start_at.date().isoformat())
        match_num = getattr(match, "match_num", "")
        lines.append(
            f"{idx}. 比赛编号={match_num} | 比赛日期={business_date} | {match.league} | "
            f"{match.home} vs {match.away} | 开赛={match.start_at.strftime('%Y-%m-%d %H:%M')}"
        )
    lines.append("")
    lines.append("不要讨论或猜测任何赔率、SP值、概率，也不要声称系统已经选择了某个结果。")
    lines.append("请用中文输出分析结果，不要列出具体投注金额，不要建议用户加大投入。")
    return "\n".join(lines)


def _qwen_response(prompt: str, settings: Settings, *, max_tokens: int) -> str:
    if not settings.qwen_api_key:
        raise AIAnalysisError("QWEN_API_KEY is not configured")
    payload = {
        "model": settings.qwen_model,
        "input": [
            {
                "role": "system",
                "content": "你是一名足球比赛信息分析师。必须先联网检索近期公开资料，并严格按用户要求输出。",
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [{"type": "web_search"}],
        "tool_choice": "required",
        # DashScope does not allow tool_choice="required" while thinking mode
        # is enabled.  Web search is mandatory for this analyzer, so use the
        # non-thinking path and keep tool_choice required.
        "enable_thinking": False,
        "max_output_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        settings.qwen_api_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.qwen_api_key}",
            "Accept": "application/json",
            "User-Agent": "ScoreFourfold/0.7.0 (Qwen-web-search)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.ai_http_timeout_seconds
        ) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error_detail = exc.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            error_detail = ""
        raise AIAnalysisError(f"Qwen HTTP {exc.code}: {error_detail}") from exc
    except urllib.error.URLError as exc:
        raise AIAnalysisError(f"Qwen unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AIAnalysisError(
            f"Qwen timed out after {settings.ai_http_timeout_seconds} seconds"
        ) from exc

    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIAnalysisError("Qwen returned a non-JSON response") from exc

    if not isinstance(result, dict):
        raise AIAnalysisError("Qwen returned an unexpected response structure")

    error = result.get("error")
    if error:
        raise AIAnalysisError(f"Qwen API error: {error}")
    if result.get("status") not in {None, "completed"}:
        raise AIAnalysisError(f"Qwen response status is {result.get('status')}")

    output = result.get("output")
    if not isinstance(output, list):
        raise AIAnalysisError("Qwen response is missing output")
    search_performed = any(
        isinstance(item, dict) and item.get("type") == "web_search_call" for item in output
    )
    usage = result.get("usage")
    if isinstance(usage, dict):
        tools_usage = usage.get("x_tools")
        if isinstance(tools_usage, dict):
            web_usage = tools_usage.get("web_search")
            if isinstance(web_usage, dict) and int(web_usage.get("count", 0) or 0) > 0:
                search_performed = True
    if not search_performed:
        raise AIAnalysisError("Qwen did not perform the required web search")

    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text_parts.append(str(part.get("text", "")))
    content = "\n".join(text_parts).strip()
    if not content:
        raise AIAnalysisError("Qwen returned empty content")
    return content


def qwen_analyze(
    matches: list[tuple[Any, ScoreOption]],
    market: MarketType,
    settings: Settings,
) -> str:
    """Call Qwen with mandatory web search to analyze selected matches."""
    return _qwen_response(_build_prompt(matches, market), settings, max_tokens=1024)


def probe_qwen(settings: Settings) -> str:
    """Check Qwen authentication, Responses API compatibility, and web search."""
    return _qwen_response(
        "请联网搜索当前北京时间。完成搜索后只回复：AI连接正常",
        settings,
        max_tokens=64,
    )


def analyze_matches(
    matches: list[tuple[Any, ScoreOption]],
    market: MarketType,
    settings: Settings,
) -> str:
    """Safe wrapper that never raises."""
    try:
        return qwen_analyze(matches, market, settings)
    except AIAnalysisError as exc:
        LOGGER.warning("AI analysis skipped: %s", exc)
        return ""


def analyze_from_leg_data(
    legs: list,
    market: MarketType,
    settings: Settings,
) -> str:
    """Analyze a plan from database-stored leg data.

    Each leg should have: league, home, away, start_at (datetime),
    score_code, score_label, odds (Decimal), probability (Decimal).
    """
    proxy_combinations: list[tuple[Any, ScoreOption]] = []
    for leg in legs:
        m = type("_M", (), {})()
        m.league = leg.league
        m.home = leg.home
        m.away = leg.away
        m.start_at = leg.start_at
        s = ScoreOption(
            code=leg.score_code,
            label=leg.score_label,
            odds=leg.odds,
            probability=leg.probability,
        )
        proxy_combinations.append((m, s))
    return analyze_matches(proxy_combinations, market, settings)


def _leg_options(leg: Any) -> tuple[ScoreOption, ...]:
    options = tuple(getattr(leg, "options", ()) or ())
    if options:
        return options
    return (
        ScoreOption(
            code=leg.score_code,
            label=leg.score_label,
            odds=leg.odds,
            probability=leg.probability,
        ),
    )


def _build_plan_recommendation_prompt(legs: Sequence[Any], market: MarketType) -> str:
    pick_name = "比分" if market is MarketType.CRS else "胜平负结果"
    pick_rule = (
        "pick 必须是一个具体的全场比分，例如 1:0、1:1、0:2。"
        if market is MarketType.CRS
        else "pick 只能是 主胜、平、客胜 三者之一。"
    )
    lines = [
        "你是一名审慎的足球比赛分析师。必须先联网搜索每场双方球队的近期状态、伤停、赛程和主客场表现。",
        f"当前玩法：{pick_name}串关。你必须覆盖每一个 match_id。{pick_rule}",
        "系统不会向你提供赔率、概率、候选项或当前推荐；请不要讨论、猜测或反推这些信息。",
        "",
        "只输出一个 JSON 对象，不要使用 Markdown 代码块，不要添加 JSON 以外的文字。格式必须为：",
        '{"summary":"不超过160字的总体分析","suggestions":['
        '{"match_id":"原样返回","pick":"具体比分或胜平负结果","reason":"不超过60字"}]}',
        "",
        "基础赛程信息：",
    ]
    for index, leg in enumerate(legs, start=1):
        lines.append(
            f"{index}. match_id={leg.match_id} | 比赛编号={leg.match_num} | "
            f"比赛日期={leg.business_date} | {leg.league} | {leg.home} vs {leg.away} | "
            f"开赛={leg.start_at.strftime('%Y-%m-%d %H:%M')}"
        )
    lines.extend(
        [
            "",
            "summary 应说明主要风险和联网资料的时效性。每场必须恰好返回一条建议；reason 简述球队信息依据。",
            "分析仅供辅助参考，不构成投注建议，不要建议增加投入。",
        ]
    )
    return "\n".join(lines)


def _normalized_pick(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("：", ":")
        .replace(" ", "")
        .removeprefix("比分")
        .removeprefix("全场")
    )


def _option_for_pick(
    leg: Any, pick: str, market: MarketType
) -> tuple[ScoreOption, str] | None:
    normalized = _normalized_pick(pick)
    if market is MarketType.HAD:
        aliases = {
            "h": "主胜",
            "胜": "主胜",
            "主队胜": "主胜",
            "主胜": "主胜",
            "d": "平",
            "平局": "平",
            "平": "平",
            "a": "客胜",
            "负": "客胜",
            "客队胜": "客胜",
            "客胜": "客胜",
        }
        normalized = aliases.get(normalized, normalized)
    for option in _leg_options(leg):
        if _normalized_pick(option.label) == normalized:
            return option, option.label

    if market is MarketType.CRS:
        score = re.fullmatch(r"(\d{1,2}):(\d{1,2})", normalized)
        if score:
            home, away = int(score.group(1)), int(score.group(2))
            outcome = "胜" if home > away else ("负" if home < away else "平")
            for option in _leg_options(leg):
                if option.is_other and outcome in option.label:
                    return option, f"{home}:{away}"
    return None


def _parse_plan_analysis(
    content: str, legs: Sequence[Any], market: MarketType
) -> AIPlanAnalysis:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise AIAnalysisError("Qwen recommendation response is not a JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIAnalysisError("Qwen recommendation response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AIAnalysisError("Qwen recommendation response must be a JSON object")

    summary = str(payload.get("summary", "")).strip()
    raw_suggestions = payload.get("suggestions")
    if not summary:
        raise AIAnalysisError("Qwen recommendation response is missing summary")
    if not isinstance(raw_suggestions, list):
        raise AIAnalysisError("Qwen recommendation response is missing suggestions")

    legs_by_id = {str(leg.match_id): leg for leg in legs}
    parsed: list[AIOptionSuggestion] = []
    seen: set[str] = set()
    for item in raw_suggestions:
        if not isinstance(item, dict):
            raise AIAnalysisError("Qwen returned an invalid match suggestion")
        match_id = str(item.get("match_id", "")).strip()
        pick = str(item.get("pick", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if match_id not in legs_by_id or match_id in seen:
            raise AIAnalysisError("Qwen returned an unknown or duplicate match_id")
        matched_option = _option_for_pick(legs_by_id[match_id], pick, market)
        if matched_option is None:
            raise AIAnalysisError(
                f"Qwen recommended a result that cannot map to a real option for match {match_id}: {pick}"
            )
        option, pick_label = matched_option
        parsed.append(
            AIOptionSuggestion(match_id, option.code, pick_label, reason[:500])
        )
        seen.add(match_id)
    if seen != set(legs_by_id):
        missing = ", ".join(sorted(set(legs_by_id) - seen))
        raise AIAnalysisError(f"Qwen did not recommend every match: {missing}")
    return AIPlanAnalysis(summary=summary[:4000], suggestions=tuple(parsed))


def analyze_plan_from_leg_data(
    legs: Sequence[Any],
    market: MarketType,
    settings: Settings,
) -> AIPlanAnalysis:
    """Return a validated, structured recommendation for every stored plan leg."""
    if not legs:
        raise AIAnalysisError("plan has no legs to analyze")
    content = _qwen_response(
        _build_plan_recommendation_prompt(legs, market),
        settings,
        max_tokens=1800,
    )
    return _parse_plan_analysis(content, legs, market)
