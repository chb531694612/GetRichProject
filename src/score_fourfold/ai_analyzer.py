from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .domain import MarketType, ScoreOption

LOGGER = logging.getLogger(__name__)


class AIAnalysisError(Exception):
    """AI analysis failed but should not block the recommendation pipeline."""


def _build_prompt(matches: list[tuple[Any, ScoreOption]], market: MarketType) -> str:
    lines: list[str] = [
        "你是一名专业的足球彩票数据分析师。请基于提供的实时赔率数据，对以下选中的串关比赛进行深度分析。",
        "",
        f"玩法：{'比分' if market is MarketType.CRS else '胜平负'}串关",
        "",
        "请从以下几个维度分析：",
        "1. 赔率结构：观察各场比赛推荐选项的赔率是否合理，是否存在市场倾向；",
        "2. 概率与价值：结合隐含概率和推荐选项的SP值，评估选项的价值；",
        "3. 风险提示：指出可能影响结果的不确定因素（如低概率高赔率、实力接近的对阵等）；",
        "4. 综合结论：给出一段简洁的总体判断（不超过80字）。",
        "",
        "比赛数据如下：",
    ]
    for idx, (match, score) in enumerate(matches, start=1):
        lines.append(
            f"{idx}. {match.league} | {match.home} vs {match.away} | "
            f"开赛：{match.start_at.strftime('%Y-%m-%d %H:%M')} | "
            f"推荐：{score.label} | SP：{score.odds} | 隐含概率：{float(score.probability) * 100:.2f}%"
        )
    lines.append("")
    lines.append("注意：以上赔率数据由系统配置的数据源提供，分析仅供辅助参考，不构成投注建议。")
    lines.append("请用中文输出分析结果，不要列出具体投注金额，不要建议用户加大投入。")
    return "\n".join(lines)


def _chat_completion(prompt: str, settings: Settings, *, max_tokens: int) -> str:
    if not settings.deepseek_api_key:
        raise AIAnalysisError("DEEPSEEK_API_KEY is not configured")
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": "你是一名专业的足球彩票数据分析师，擅长基于赔率结构进行理性分析。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        settings.deepseek_api_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Accept": "application/json",
            "User-Agent": "ScoreFourfold/0.7.0 (AI-analysis)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.http_timeout_seconds) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error_detail = exc.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            error_detail = ""
        raise AIAnalysisError(f"DeepSeek HTTP {exc.code}: {error_detail}") from exc
    except urllib.error.URLError as exc:
        raise AIAnalysisError(f"DeepSeek unreachable: {exc.reason}") from exc

    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIAnalysisError("DeepSeek returned non-JSON response") from exc

    if not isinstance(result, dict):
        raise AIAnalysisError("DeepSeek returned unexpected response structure")

    error = result.get("error")
    if error:
        raise AIAnalysisError(f"DeepSeek API error: {error}")

    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIAnalysisError("DeepSeek response missing choices")

    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    content = str(content).strip()
    if not content:
        raise AIAnalysisError("DeepSeek returned empty content")
    return content


def deepseek_analyze(
    matches: list[tuple[Any, ScoreOption]],
    market: MarketType,
    settings: Settings,
) -> str:
    """Call DeepSeek API to analyze selected matches."""
    return _chat_completion(_build_prompt(matches, market), settings, max_tokens=1024)


def probe_deepseek(settings: Settings) -> str:
    """Perform a small authenticated request used by deployment checks."""
    return _chat_completion(
        "这是连通性测试。请只回复：AI连接正常",
        settings,
        max_tokens=16,
    )


def analyze_matches(
    matches: list[tuple[Any, ScoreOption]],
    market: MarketType,
    settings: Settings,
) -> str:
    """Safe wrapper that never raises."""
    try:
        return deepseek_analyze(matches, market, settings)
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
