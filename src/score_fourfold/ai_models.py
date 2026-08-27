from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class AIModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    code: str
    name: str
    protocol: str
    default_base_url: str
    default_model: str
    native_web_search: bool


@dataclass(frozen=True, slots=True)
class AIModelRuntime:
    config_id: str
    provider: str
    base_url: str
    model_name: str
    api_key: str


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "qwen",
        "阿里云百炼千问",
        "responses",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
        "qwen3.7-max",
        True,
    ),
    ProviderSpec("deepseek", "DeepSeek", "responses", "https://api.deepseek.com/responses", "deepseek-v4-flash", True),
    ProviderSpec("zhipu", "智谱 GLM", "chat", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.5", False),
    ProviderSpec("moonshot", "Moonshot Kimi", "chat", "https://api.moonshot.cn/v1/chat/completions", "kimi-k2", False),
    ProviderSpec("doubao", "火山方舟豆包", "chat", "https://ark.cn-beijing.volces.com/api/v3/chat/completions", "doubao", False),
    ProviderSpec("hunyuan", "腾讯混元", "chat", "https://api.hunyuan.cloud.tencent.com/v1/chat/completions", "hunyuan", False),
    ProviderSpec("qianfan", "百度千帆文心", "chat", "https://qianfan.baidubce.com/v2/chat/completions", "ernie-4.5", False),
    ProviderSpec("minimax", "MiniMax", "chat", "https://api.minimaxi.com/v1/text/chatcompletion_v2", "MiniMax-M2", False),
    ProviderSpec("siliconflow", "硅基流动", "chat", "https://api.siliconflow.cn/v1/chat/completions", "deepseek-ai/DeepSeek-V3", False),
    ProviderSpec("openai", "OpenAI", "responses", "https://api.openai.com/v1/responses", "gpt-5-mini", True),
    ProviderSpec("custom", "自定义 OpenAI 兼容接口", "responses", "https://example.invalid/v1/responses", "", True),
)
PROVIDER_BY_CODE = {provider.code: provider for provider in PROVIDERS}


DEFAULT_SYSTEM_PROMPT = (
    "你是一名严谨的足球比赛信息分析师。先在内部完成充分推理，再输出简洁结论；不得展示思维链。"
    "必须联网检索公开资料，并用多个相互独立的数据源交叉验证战绩、交锋、伤停、阵容和赛程。"
    "兼顾权威赛事/俱乐部来源、国际数据平台与雷速体育等中文体育平台，标明信息时效，"
    "不得把单一媒体观点当成事实。严禁敷衍或给出空泛结论，严格按用户要求输出。"
)

# 提示词覆盖注册表：空字符串表示使用内置默认。
# 由 SettingsRepository 在启动和设置更新时写入；放在模块级是为了让
# strategy/service 等无法穿透传参的调用链也能即时读到最新配置。
_PROMPT_OVERRIDES: dict[str, str] = {"system": "", "plan": "", "summary": ""}


def set_prompt_overrides(
    system_prompt: str = "",
    plan_requirements: str = "",
    summary_requirements: str = "",
) -> None:
    _PROMPT_OVERRIDES["system"] = (system_prompt or "").strip()
    _PROMPT_OVERRIDES["plan"] = (plan_requirements or "").strip()
    _PROMPT_OVERRIDES["summary"] = (summary_requirements or "").strip()


def prompt_overrides() -> dict[str, str]:
    return dict(_PROMPT_OVERRIDES)


def effective_system_prompt() -> str:
    return _PROMPT_OVERRIDES["system"] or DEFAULT_SYSTEM_PROMPT


def validate_runtime(runtime: AIModelRuntime) -> ProviderSpec:
    spec = PROVIDER_BY_CODE.get(runtime.provider)
    if spec is None:
        raise AIModelError("不支持的大模型供应商")
    parsed = urlsplit(runtime.base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise AIModelError("模型 API 地址必须是有效的 HTTPS 地址")
    if not runtime.model_name.strip():
        raise AIModelError("调用模型不能为空")
    if not runtime.api_key:
        raise AIModelError("API Key 未配置")
    if not spec.native_web_search:
        raise AIModelError(
            f"{spec.name} 当前配置的官方接口不支持本项目要求的强制联网搜索，不能启用"
        )
    if spec.protocol != "responses":
        raise AIModelError("该供应商的强制联网适配器尚不可用")
    return spec


def _response_text_and_search(payload: dict[str, Any]) -> tuple[str, bool]:
    output = payload.get("output")
    if not isinstance(output, list):
        raise AIModelError("模型响应缺少 output")
    searched = any(
        isinstance(item, dict) and item.get("type") == "web_search_call"
        for item in output
    )
    usage = payload.get("usage")
    if isinstance(usage, dict):
        tools_usage = usage.get("x_tools")
        if isinstance(tools_usage, dict):
            web_usage = tools_usage.get("web_search")
            if isinstance(web_usage, dict) and int(web_usage.get("count", 0) or 0) > 0:
                searched = True
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text_parts.append(str(part.get("text", "")))
    text = "\n".join(text_parts).strip()
    if not text:
        raise AIModelError("模型返回了空内容")
    return text, searched


def call_with_web_search(
    runtime: AIModelRuntime,
    prompt: str,
    *,
    timeout_seconds: int,
    max_output_tokens: int,
) -> str:
    spec = validate_runtime(runtime)
    payload: dict[str, Any] = {
        "model": runtime.model_name,
        "input": [
            {
                "role": "system",
                "content": effective_system_prompt(),
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [{"type": "web_search"}],
        "max_output_tokens": max_output_tokens,
    }
    # 百炼思考模式不允许 tool_choice="required"；省略该参数并在响应端校验
    # web_search_call，既保留思考能力，又保证没有实际联网时整次调用失败。
    # 小输出配额（连接性探测）关闭思考，避免思考 token 耗尽输出预算造成假失败。
    if spec.code == "qwen":
        payload["tools"] = [{"type": "web_search"}, {"type": "web_extractor"}]
        payload["enable_thinking"] = max_output_tokens >= 2048
    else:
        payload["tool_choice"] = {"type": "web_search"}

    def _attempt() -> str:
        request = urllib.request.Request(
            runtime.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {runtime.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ScoreFourfold/0.7.0 (required-web-search)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = ""
            raise AIModelError(f"模型接口返回 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise AIModelError(f"无法连接模型接口：{exc.reason}") from exc
        except TimeoutError as exc:
            raise AIModelError(f"模型调用超过 {timeout_seconds} 秒") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIModelError("模型返回的不是 JSON") from exc
        if not isinstance(result, dict):
            raise AIModelError("模型返回结构无效")
        if result.get("error"):
            raise AIModelError(f"模型接口错误：{result['error']}")
        status = result.get("status")
        if status not in {None, "completed"}:
            details = result.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else ""
            if status == "incomplete" and reason == "max_output_tokens":
                raise AIModelError("模型输出达到长度上限，未能生成完整推荐，请重试")
            suffix = f"（原因：{reason}）" if reason else ""
            raise AIModelError(f"模型任务状态异常：{status}{suffix}")
        text, searched = _response_text_and_search(result)
        if not searched:
            raise AIModelError("模型连接正常，但没有执行项目要求的联网搜索")
        return text

    # 暂时性失败（上游 5xx、incomplete、空内容、输出超限）重试一次，短暂退避后
    # 原样重放同一个请求；配置/鉴权等确定性错误立即抛出，不浪费重试。思考模型
    # 偶发把输出配额耗光（incomplete/max_output_tokens）时，重试往往能成功。
    transient_markers = (
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
        "任务状态异常",
        "达到长度上限",
        "空内容",
    )
    try:
        return _attempt()
    except AIModelError as exc:
        message = str(exc)
        if not any(marker in message for marker in transient_markers):
            raise
        time.sleep(5)
        return _attempt()


def test_model(runtime: AIModelRuntime, timeout_seconds: int) -> str:
    result = call_with_web_search(
        runtime,
        "请联网查询当前北京时间。完成搜索后只回复：AI连接正常",
        timeout_seconds=timeout_seconds,
        max_output_tokens=256,
    )
    if "AI连接正常" not in result.replace(" ", ""):
        raise AIModelError("模型已响应，但测试口令不正确")
    return "API、模型和强制联网搜索测试通过"


def public_provider_catalog() -> list[dict[str, Any]]:
    return [
        {
            "code": provider.code,
            "name": provider.name,
            "default_base_url": provider.default_base_url,
            "default_model": provider.default_model,
            "native_web_search": provider.native_web_search,
        }
        for provider in PROVIDERS
    ]
