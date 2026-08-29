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
from .domain import MarketType, ScoreOption

LOGGER = logging.getLogger(__name__)

SOURCE_REQUIREMENTS = [
    "- 每场至少核对 3 个相互独立的来源；同一稿件的转载只算 1 个来源；",
    "- 第一优先级：赛事官方、足协、俱乐部公告和赛前发布会；",
    "- 第二优先级：雷速体育、懂球帝、直播吧、腾讯体育等中文平台；",
    "- 第三优先级：Sofascore、Flashscore、Soccerway、Transfermarkt、BBC Sport、Opta 等国际数据或媒体；",
    "- 至少包含 1 个中文来源（确实检索不到时须说明），并核对伤停、首发预测等易变信息的发布时间；",
    "- 搜索摘要不足时读取原文；来源冲突时明确指出，不得擅自拼凑。",
]


# 总结分析（自动推荐/总体分析）的内置默认分析要求，可被面板设置覆盖。
DEFAULT_SUMMARY_REQUIREMENTS = "\n".join(
    [
        "1. 每场比赛必须引用至少 2 条具体搜索到的事实依据（如近期战绩、交锋数据、伤停信息），不得使用'双方实力接近''比赛难分胜负'等空泛套话；",
        "2. 明确指出影响结果的关键变量和信息时效风险；",
        "3. 必须客观评估冷门风险——强队是否存在伤停、疲劳或战术受克，弱队是否有状态回升、主场优势或阵容完整；不得为满足分析要求强行推荐冷门，也不能仅按信号数量决定方向，须按来源可靠性、证据强度、时效性及与结果的相关性加权判断；",
        "4. 当前玩法为进球数时，必须重点核对双方近 5–10 场总进球逐场分布、主客场进失球、预期首发与攻防核心伤停、赛程体能及战术节奏；区分持续趋势与单场大比分异常，不能仅由强弱或胜负倾向推导总进球；",
        "5. 给出一段不超过300字的中文总体判断，须体现实质性分析而非泛泛而谈。",
    ]
)

# 计划推荐（AI分析并推荐）的内置默认分析要求，可被面板设置覆盖。
DEFAULT_PLAN_REQUIREMENTS = "\n".join(
    [
        "爆冷分析要求（每场比赛都必须执行，但不得强行推荐冷门）：",
        "1. 如果纸面实力较强的队伍存在以下任一情况，必须审慎评估其输球或丢分的真实风险：",
        "   - 核心球员伤停、停赛或大规模轮换；",
        "   - 国内/洲际多线作战导致赛程密集、体能堪忧；",
        "   - 对手擅长低位防守反击或大巴战术、克制其进攻体系；",
        "   - 历史交锋处于劣势或主客场表现差异显著。",
        "2. 如果纸面实力较弱的一方存在以下信号，应提高冷门方向的评估权重：",
        "   - 近期状态持续回升（连胜、不败、进球稳定）；",
        "   - 主场作战而对手客场战绩平庸；",
        "   - 核心阵容整齐、战意明确（如保级、争冠关键节点）。",
        "3. 禁止仅凭实力差距排除冷门，也不得为了体现爆冷分析而强行推荐冷门。不能机械比较信号数量，必须按来源可靠性、证据强度、时效性及与比赛结果的相关性加权；只有多项相互独立且足以实质改变结果判断的证据一致指向冷门时，才推荐冷门方向。",
        "4. 如果冷门证据不充分或顺势证据更强，应顺势推荐；reason 中须简要说明冷门风险为何不足。",
        "",
        "进球数玩法专项要求（当前玩法为进球数时，每场必须执行）：",
        "1. 核对双方近 5–10 场正式比赛的总进球逐场分布、主客场进失球、零封与被零封情况，不能只引用胜负战绩或场均值；",
        "2. 核对预期首发、前锋/门将/中卫等攻防关键球员伤停、赛程间隔、轮换和战术节奏，并注明易变阵容信息的发布时间；",
        "3. 识别红牌、加时、弱旅杯赛或单场极端大比分等异常样本，不得把异常值直接当作稳定趋势；",
        "4. 推荐一个具体总进球数时，reason 必须说明该数字而非相邻数字的依据；资料不足时选择证据最一致的结果，不得用实力差距代替进球数分析。",
        "",
        "summary 须说明主要风险（含爆冷可能性）、关键信息依据和联网资料的时效性，禁止使用空泛套话。",
        "每场 reason 必须引用至少 2 条具体搜索到的事实（如近期战绩、交锋数据、伤停信息），不得使用'双方实力接近'等笼统表述。",
        "每场必须恰好返回一条建议；分析仅供辅助参考，不构成投注建议，不要建议增加投入。",
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


def _build_prompt(
    matches: list[tuple[Any, ScoreOption]], market: MarketType, history_context: str = ""
) -> str:
    overrides = prompt_overrides()
    lines: list[str] = [
        "你是一名严谨、专业的足球比赛分析师，绝不允许敷衍了事。",
        "足球比赛充满变数；实力悬殊不等于结果确定。你必须主动识别和评估爆冷可能，不得默认选择强队或热门方向。",
        "请先对每场比赛逐一联网搜索并读取关键原文，按以下规则交叉验证：",
        *SOURCE_REQUIREMENTS,
        "- 双方近 5–10 场正式比赛战绩、进球/失球数据；",
        "- 双方历史交锋记录（H2H）及主客场差异；",
        "- 球队伤停、停赛、轮换情况和关键球员 availability；",
        "- 近期赛程密度、体能状况及是否多线作战；",
        "- 主客场表现差异、主场优势程度；",
        "- 当前玩法为进球数时，额外核对近 5–10 场总进球逐场分布、主客场进失球、攻防关键伤停和战术节奏，识别单场极端比分，不能由胜负倾向直接推导总进球；",
        "- 足球讨论区、球迷社区只能用于发现线索或观点分歧，不能单独作为事实依据。",
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
    # 思考模式与 required 工具选择不兼容；调用后仍严格验证确实发生联网搜索。
    # 探测请求关闭思考，避免思考 token 提前耗尽 max_output_tokens 造成假失败。
    # 百炼 qwen3 系列 Normal 模式（enable_thinking=false）不支持 web_extractor，
    # 且不会主动调用 web_search，因此 Normal 模式仅保留 web_search 并显式指定
    # tool_choice 强制联网；思考模式则附加 web_extractor 且不带 tool_choice。
    thinking = max_tokens >= 2048
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
    return _qwen_response(_build_prompt(matches, market), settings, max_tokens=2048)


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
                max_output_tokens=2048,
            )
        return _qwen_response(_build_prompt(matches, market, history_context), settings, max_tokens=2048)
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
        "你是一名严谨、专业的足球比赛分析师，绝不允许敷衍了事。",
        "足球比赛充满变数；实力悬殊不等于结果确定。你必须主动识别和评估爆冷可能，不得默认选择强队或热门方向。",
        "必须先对每场比赛逐一联网搜索并读取关键原文，按以下规则交叉验证后再判断：",
        *SOURCE_REQUIREMENTS,
        "- 双方近 5–10 场正式比赛战绩、进球/失球数据；",
        "- 双方历史交锋记录（H2H）及主客场差异；",
        "- 球队伤停、停赛、轮换情况和关键球员 availability；",
        "- 近期赛程密度、体能状况及是否多线作战；",
        "- 主客场表现差异、主场优势程度；",
        "- 当前玩法为进球数时，额外核对近 5–10 场总进球逐场分布、主客场进失球、攻防关键伤停和战术节奏，识别单场极端比分，不能由胜负倾向直接推导总进球；",
        "- 足球讨论区、球迷社区只能用于发现线索或观点分歧，不能单独作为事实依据。",
        "",
        f"当前玩法：{pick_name}串关。你必须覆盖每一个 match_id。{pick_rule}",
        "系统不会向你提供赔率、概率、候选项或当前推荐；请不要讨论、猜测或反推这些信息。",
        "",
        "只输出一个 JSON 对象，不要使用 Markdown 代码块，不要添加 JSON 以外的文字。格式必须为：",
        '{"summary":"不超过300字的总体分析，须引用具体战绩和交锋数据","suggestions":['
        '{"match_id":"原样返回","pick":"该玩法的具体结果","reason":"不超过100字，须引用至少2条具体搜索到的事实依据"}]}',
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
    for attempt in (1, 2):
        if runtime is None:
            content = _qwen_response(prompt, settings, max_tokens=16384)
        else:
            try:
                content = call_with_web_search(
                    runtime,
                    prompt,
                    timeout_seconds=settings.ai_http_timeout_seconds,
                    max_output_tokens=16384,
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
