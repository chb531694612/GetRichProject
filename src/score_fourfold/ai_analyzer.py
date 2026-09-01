from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from .ai_models import (
    AIModelError,
    AIModelRuntime,
    call_with_web_search,
    effective_system_prompt,
    prompt_overrides,
)
from .config import Settings
from .domain import MarketType, MatchResult, ResultStatus, ScoreOption

LOGGER = logging.getLogger(__name__)

# 2026-09-01 回退：恢复为 8/10 多源深度分析改造前的轻量要求。
# 原先"每场核对 3 个独立来源 + 读取原文"会成倍放大 web_search 次数与输入 token，
# 是 AI 费用的主要来源；这里只保留最低限度的交叉验证约束。
SOURCE_REQUIREMENTS = [
    "- 用至少 2 个相互独立的来源交叉验证关键事实，避免依赖单一媒体观点；",
]


# 总结分析（自动推荐/总体分析）的内置默认分析要求，可被面板设置覆盖。
# 2026-09-01 回退：恢复为 8/10 改造前的 3 条维度，并把总体判断从 300 字压回 120 字。
# 仅保留一句极短的防幻觉约束（不得机械比较信号数量），成本近似为零但可避免乱推冷门。
DEFAULT_SUMMARY_REQUIREMENTS = "\n".join(
    [
        "1. 双方近期状态、伤停、赛程密度和主客场表现；",
        "2. 可能影响结果的不确定因素和信息时效风险；",
        "3. 不得仅凭实力差距或信号数量机械推导结论，须按证据强度与时效性判断；",
        "4. 给出一段不超过120字的中文总体判断。",
    ]
)

# 计划推荐（AI分析并推荐）的内置默认分析要求，可被面板设置覆盖。
# 2026-09-01 回退：删除 8/12 起的"爆冷分析要求"与"进球数玩法专项要求"两大段明细
# （合计约 700 字、每场强制逐条自检，是输入与输出 token 的大头），
# 恢复为 8/10 前的简短收尾要求，仅保留一句防乱推冷门的约束。
DEFAULT_PLAN_REQUIREMENTS = "\n".join(
    [
        "summary 应说明主要风险和联网资料的时效性；每场必须恰好返回一条建议，reason 简述球队信息依据。",
        "不得仅凭实力差距强行推荐冷门，也不得机械比较信号数量，须按证据强度与时效性加权判断。",
        "分析仅供辅助参考，不构成投注建议，不要建议增加投入。",
    ]
)


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


def _output_token_budget(
    runtime: AIModelRuntime | None, *, compact: int, thinking: int
) -> int:
    """按是否开启思考模式选择输出 token 上限。

    思考模式下思维链会计入输出预算：历史上并发分析多个计划时曾因预算不足
    产生空响应或不完整响应（见 web.py 的 _ai_analysis_semaphore 注释），
    因此开启思考时沿用较大的上限；关闭思考时按精简后的提示词给较小上限，
    以控制调用成本。旧环境变量路径不传 runtime，一律按非思考模式计。
    """
    if runtime is not None and getattr(runtime, "thinking_enabled", False):
        return thinking
    return compact


def _build_prompt(
    matches: list[tuple[Any, ScoreOption]], market: MarketType, history_context: str = ""
) -> str:
    overrides = prompt_overrides()
    lines: list[str] = [
        # 2026-09-01 回退：恢复 8/10 前的简洁开头，信息源清单由 7 条压回 4 条。
        "你是一名审慎的足球比赛分析师。请先联网搜索每场比赛双方球队的近期公开信息，再进行分析。",
        *SOURCE_REQUIREMENTS,
        "- 双方近期战绩、进球/失球数据；",
        "- 双方历史交锋记录（H2H）；",
        "- 球队伤停、停赛和关键球员情况；",
        "- 近期赛程密度与主客场表现差异。",
        "",
        f"玩法：{market.label_zh}串关",
        "",
        "分析要求：",
        overrides["summary"] or DEFAULT_SUMMARY_REQUIREMENTS,
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
    if history_context:
        lines.extend(["", "本系统已结算历史赛果的匿名聚合统计（仅作弱参考，不代表未来规律）：", history_context,
                      "不得因历史样本而忽略当前球队实时信息，也不得虚构因果关系。"])
    lines.append("")
    lines.append("不要讨论或猜测任何赔率、SP值、概率，也不要声称系统已经选择了某个结果。")
    lines.append("请用中文输出分析结果，不要列出具体投注金额，不要建议用户加大投入。")
    lines.append("分析必须基于联网搜索到的具体信息，禁止仅凭常识或猜测给出笼统结论。")
    return "\n".join(lines)


def _qwen_response(prompt: str, settings: Settings, *, max_tokens: int) -> str:
    if not settings.qwen_api_key:
        raise AIAnalysisError("QWEN_API_KEY is not configured")
    # 旧环境变量兼容路径固定使用 Normal 模式；可配置的深度思考只由数据库模型配置控制。
    # 百炼 qwen3 系列 Normal 模式（enable_thinking=false）不支持 web_extractor，
    # 且不会主动调用 web_search，因此 Normal 模式仅保留 web_search 并显式指定
    # tool_choice 强制联网；思考模式则附加 web_extractor 且不带 tool_choice。
    thinking = False
    tools = [{"type": "web_search"}]
    if thinking:
        tools.append({"type": "web_extractor"})
    payload = {
        "model": settings.qwen_model,
        "input": [
            {
                "role": "system",
                "content": effective_system_prompt(),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": tools,
        "enable_thinking": thinking,
        "max_output_tokens": max_tokens,
    }
    if not thinking:
        payload["tool_choice"] = {"type": "web_search"}
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
        # 0 表示不限制超时（思考模型的完整生成时间不受固定上限约束）。
        with urllib.request.urlopen(
            request, timeout=settings.ai_http_timeout_seconds or None
        ) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error_detail = exc.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            error_detail = ""
        if exc.code >= 500:
            raise AIAnalysisError(
                f"Qwen 上游服务暂时不可用（HTTP {exc.code}）：{error_detail}"
            ) from exc
        raise AIAnalysisError(f"Qwen HTTP {exc.code}: {error_detail}") from exc
    except urllib.error.URLError as exc:
        raise AIAnalysisError(f"Qwen unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        if settings.ai_http_timeout_seconds > 0:
            raise AIAnalysisError(
                f"Qwen timed out after {settings.ai_http_timeout_seconds} seconds"
            ) from exc
        raise AIAnalysisError("Qwen timed out") from exc

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
        max_tokens=128,
    )


def analyze_matches(
    matches: list[tuple[Any, ScoreOption]],
    market: MarketType,
    settings: Settings,
    runtime: AIModelRuntime | None = None,
    history_context: str = "",
) -> str:
    """Safe wrapper that never raises."""
    try:
        if runtime is not None:
            return call_with_web_search(
                runtime,
                _build_prompt(matches, market, history_context),
                timeout_seconds=settings.ai_http_timeout_seconds,
                max_output_tokens=_output_token_budget(runtime, compact=1024, thinking=2048),
            )
        return _qwen_response(_build_prompt(matches, market, history_context), settings, max_tokens=1024)
    except (AIAnalysisError, AIModelError) as exc:
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


def _build_plan_recommendation_prompt(
    legs: Sequence[Any], market: MarketType, history_context: str = ""
) -> str:
    pick_name = market.label_zh
    pick_rule = {
        MarketType.CRS: "pick 必须是一个具体的全场比分，例如 1:0、1:1、0:2。",
        MarketType.HAD: "pick 只能是 主胜、平、客胜 三者之一。",
        MarketType.TTG: "pick 只能是 0、1、2、3、4、5、6、7+ 八者之一。",
    }[market]
    lines = [
        # 2026-09-01 回退：恢复 8/10 前的简洁开头，信息源清单由 7 条压回 4 条，
        # 输出字数上限由 300/100 字压回 160/60 字。
        "你是一名审慎的足球比赛分析师。请先联网搜索每场比赛双方球队的近期公开信息，再进行分析。",
        *SOURCE_REQUIREMENTS,
        "- 双方近期战绩、进球/失球数据；",
        "- 双方历史交锋记录（H2H）；",
        "- 球队伤停、停赛和关键球员情况；",
        "- 近期赛程密度与主客场表现差异。",
        "",
        f"当前玩法：{pick_name}串关。你必须覆盖每一个 match_id。{pick_rule}",
        "系统不会向你提供赔率、概率、候选项或当前推荐；请不要讨论、猜测或反推这些信息。",
        "",
        "只输出一个 JSON 对象，不要使用 Markdown 代码块，不要添加 JSON 以外的文字。格式必须为：",
        '{"summary":"不超过160字的总体分析","suggestions":['
        '{"match_id":"原样返回","pick":"该玩法的具体结果","reason":"不超过60字"}]}',
        "",
        "基础赛程信息：",
    ]
    for index, leg in enumerate(legs, start=1):
        lines.append(
            f"{index}. match_id={leg.match_id} | 比赛编号={leg.match_num} | "
            f"比赛日期={leg.business_date} | {leg.league} | {leg.home} vs {leg.away} | "
            f"开赛={leg.start_at.strftime('%Y-%m-%d %H:%M')}"
        )
    if history_context:
        lines.extend(["", "本系统已结算历史赛果的匿名聚合统计（仅作弱参考，不代表未来规律）：", history_context,
                      "不得因历史样本而忽略当前球队实时信息，也不得虚构因果关系。"])
    overrides = prompt_overrides()
    lines.extend(
        [
            "",
            overrides["plan"] or DEFAULT_PLAN_REQUIREMENTS,
        ]
    )
    return "\n".join(lines)


def _normalized_pick(value: str) -> str:
    normalized = (
        value.strip()
        .lower()
        .replace("：", ":")
        .replace(" ", "")
        .removeprefix("比分")
        .removeprefix("全场")
    )
    # 进球数常见变体归一化："6球"→"6"、"7+球"→"7+"、"7球以上"→"7+"、"7以上"→"7+"。
    if normalized.endswith("以上"):
        normalized = normalized[:-2] + "+"
    if normalized.endswith("球"):
        normalized = normalized[:-1]
    if normalized.endswith("球+"):
        normalized = normalized[:-2] + "+"
    return normalized


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


def _plan_format_retry_prompt(prompt: str, error: AIAnalysisError) -> str:
    """Ask for one fresh, complete response without echoing the invalid output."""
    return "\n".join(
        [
            prompt,
            "",
            "上一次回答未通过系统的结构校验。请重新联网核对并从头生成完整答案。",
            f"需要修正的问题：{error}",
            "这次只能输出一个完整、可被严格解析的 JSON 对象；不要输出解释、前后缀或 Markdown 代码块。",
            "suggestions 必须覆盖上面列出的每一个 match_id，不能遗漏、重复或增加比赛。",
        ]
    )


def analyze_plan_from_leg_data(
    legs: Sequence[Any],
    market: MarketType,
    settings: Settings,
    runtime: AIModelRuntime | None = None,
    history_context: str = "",
) -> AIPlanAnalysis:
    """Return a validated, structured recommendation for every stored plan leg."""
    if not legs:
        raise AIAnalysisError("plan has no legs to analyze")
    prompt = _build_plan_recommendation_prompt(legs, market, history_context)
    # 思考模式下思维链会占用输出预算，需沿用较大上限；非思考模式用精简上限控成本。
    output_budget = _output_token_budget(runtime, compact=1800, thinking=16384)
    for attempt in (1, 2):
        if runtime is None:
            content = _qwen_response(prompt, settings, max_tokens=output_budget)
        else:
            try:
                content = call_with_web_search(
                    runtime,
                    prompt,
                    timeout_seconds=settings.ai_http_timeout_seconds,
                    max_output_tokens=output_budget,
                )
            except AIModelError as exc:
                raise AIAnalysisError(str(exc)) from exc
        try:
            return _parse_plan_analysis(content, legs, market)
        except AIAnalysisError as exc:
            if attempt == 2:
                raise
            LOGGER.warning(
                "AI plan response failed structural validation (%s); retrying once",
                exc,
            )
            prompt = _plan_format_retry_prompt(prompt, exc)
    raise AssertionError("unreachable")


def _build_result_query_prompt(legs: Sequence[Any]) -> str:
    """Build the prompt for an AI lookup of already-finished match results."""
    lines = [
        "你是一名足球赛果核查员。请对下面每场比赛联网搜索，确认其官方全场最终比分。",
        "只允许报告已经结束并已官方确认的全场最终比分（含伤停补时，不含加时赛和点球大战）。",
        "对每场比赛：",
        "- 优先核对赛事官方、足协、俱乐部，以及 Sofascore、Flashscore、Soccerway、雷速体育、懂球帝、直播吧等权威数据平台；",
        "- 用多个相互独立的来源交叉验证后再确认；",
        "- 如果比赛尚未开始、正在进行、延期、取消或搜索不到可靠赛果，status 必须填 unknown，home_score 与 away_score 填 0，严禁猜测比分。",
        "",
        "只输出一个 JSON 对象，不要使用 Markdown 代码块，不要输出 JSON 以外的任何文字。格式必须为：",
        '{"results":[{"match_id":"原样返回","status":"final 或 unknown","home_score":0,"away_score":0,"note":"简短说明"}]}',
        "",
        "status 取值：final 表示已确认官方全场最终比分；unknown 表示无法确定（未开始/进行中/延期/取消/查不到）。",
        "home_score 与 away_score 仅在 status=final 时填写非负整数，否则一律填 0。",
        "",
        "比赛列表（时间均为北京时间）：",
    ]
    for leg in legs:
        lines.append(
            f"- match_id={leg.match_id} | 比赛编号={leg.match_num} | 比赛日期={leg.business_date} | "
            f"{leg.league} | {leg.home} vs {leg.away} | 开赛={leg.start_at.strftime('%Y-%m-%d %H:%M')}"
        )
    return "\n".join(lines)


def _parse_result_query(content: str, legs: Sequence[Any]) -> dict[str, MatchResult]:
    """Parse the AI result-query response into validated ``MatchResult`` objects.

    Only legs the AI confirms as ``final`` with plausible scores are returned;
    unknown, malformed, out-of-range and unknown-match entries are skipped.
    """
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise AIAnalysisError("AI result query response is not a JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIAnalysisError("AI result query response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AIAnalysisError("AI result query response must be a JSON object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise AIAnalysisError("AI result query response is missing results")

    legs_by_id = {str(leg.match_id): leg for leg in legs}
    results: dict[str, MatchResult] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        match_id = str(item.get("match_id", "")).strip()
        if match_id not in legs_by_id:
            continue
        status = str(item.get("status", "")).strip().lower()
        if status != "final":
            continue
        home = item.get("home_score")
        away = item.get("away_score")
        if isinstance(home, bool) or isinstance(away, bool):
            continue
        if not isinstance(home, int) or not isinstance(away, int):
            try:
                home = int(home)
                away = int(away)
            except (TypeError, ValueError):
                continue
        if home < 0 or away < 0 or home > 30 or away > 30:
            continue
        leg = legs_by_id[match_id]
        results[match_id] = MatchResult(
            match_id=match_id,
            status=ResultStatus.FINAL,
            home_score=home,
            away_score=away,
            official_status="AI联网确认",
            home_team=leg.home,
            away_team=leg.away,
            match_num=getattr(leg, "match_num", ""),
            match_date=getattr(leg, "business_date", ""),
        )
    return results


def query_results_via_ai(
    legs: Sequence[Any],
    settings: Settings,
    runtime: AIModelRuntime | None = None,
) -> dict[str, MatchResult]:
    """Query the AI (with mandatory web search) for final match results.

    Returns a mapping of ``match_id -> MatchResult`` for every leg the AI could
    confirm as FINAL; legs the AI could not confirm are omitted.  Never raises:
    a failed query simply yields no results, so the caller falls back to its
    existing "still pending" reporting.
    """
    if not legs:
        return {}
    prompt = _build_result_query_prompt(legs)
    try:
        if runtime is not None:
            content = call_with_web_search(
                runtime,
                prompt,
                timeout_seconds=settings.ai_http_timeout_seconds,
                max_output_tokens=_output_token_budget(runtime, compact=1024, thinking=4096),
            )
        else:
            content = _qwen_response(prompt, settings, max_tokens=1024)
    except (AIAnalysisError, AIModelError) as exc:
        LOGGER.warning("AI result query failed: %s", exc)
        return {}
    try:
        return _parse_result_query(content, legs)
    except AIAnalysisError as exc:
        LOGGER.warning("AI result query response could not be parsed: %s", exc)
        return {}
