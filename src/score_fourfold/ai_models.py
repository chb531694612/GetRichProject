from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


LOGGER = logging.getLogger(__name__)


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
    thinking_enabled: bool = False
    # 该模型是否必须由自己完成联网搜索。置 False 时允许使用不联网的模型
    # （例如 DeepSeek 官方 flash 不执行 web_search），改由 reference_provider
    # 先检索资料、再把资料作为参考上下文喂给它。
    web_search_required: bool = True
    # 可选的「检索提供者」：在调用本模型之前，先用它联网检索并产出一份资料
    # 简报，拼进提示词。为空表示不做外部检索，行为与历史版本完全一致。
    reference_provider: "AIModelRuntime | None" = None


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "qwen",
        "阿里云百炼千问",
        "responses",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
        "qwen3.7-max",
        True,
    ),
    # 2026-09-11 实测：DeepSeek 官方合法名只剩 deepseek-flash（=V4.1-Flash）与
    # deepseek-v4-pro；旧名 deepseek-v4-flash 仍可调用但已路由到 V4.1-Flash。
    # 注意 deepseek-flash 实测不执行 web_search（开/关思考、带/不带 tool_choice
    # 均无 web_search_call），只有 deepseek-v4-pro 能通过强制联网校验，而官方
    # 公告 2026-09-14 12:00 起 v4-pro 也路由到 V4.1-Flash。默认值填官方长期
    # 合法名，避免用户被 400「模型名不存在」误导。
    ProviderSpec("deepseek", "DeepSeek", "responses", "https://api.deepseek.com/responses", "deepseek-flash", True),
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


# 2026-09-01 回退：恢复为 8/10 多源深度分析改造前的简洁版，控制输入 token 成本。
DEFAULT_SYSTEM_PROMPT = (
    "你是一名足球比赛信息分析师。必须先联网检索近期公开资料，并严格按用户要求输出。"
)

# 提示词覆盖注册表：空字符串表示使用内置默认。
# 由 SettingsRepository 在启动和设置更新时写入；放在模块级是为了让
# strategy/service 等无法穿透传参的调用链也能即时读到最新配置。
_PROMPT_OVERRIDES: dict[str, str] = {
    "system": "", "plan": "", "summary": "", "result": "", "retry": ""
}


def set_prompt_overrides(
    system_prompt: str = "",
    plan_requirements: str = "",
    summary_requirements: str = "",
    result_requirements: str = "",
    retry_requirements: str = "",
) -> None:
    _PROMPT_OVERRIDES["system"] = (system_prompt or "").strip()
    _PROMPT_OVERRIDES["plan"] = (plan_requirements or "").strip()
    _PROMPT_OVERRIDES["summary"] = (summary_requirements or "").strip()
    _PROMPT_OVERRIDES["result"] = (result_requirements or "").strip()
    _PROMPT_OVERRIDES["retry"] = (retry_requirements or "").strip()


def prompt_overrides() -> dict[str, str]:
    return dict(_PROMPT_OVERRIDES)


# 不要求模型自己联网时使用的默认系统提示词：此时资料由检索提供者供给，
# 再让模型「先联网检索」会自相矛盾。
DEFAULT_SYSTEM_PROMPT_WITH_REFERENCE = (
    "你是一名足球比赛信息分析师。请严格依据已提供的参考资料和用户要求输出，"
    "不得编造参考资料中没有的信息。"
)


def effective_system_prompt(web_search_required: bool = True) -> str:
    override = _PROMPT_OVERRIDES["system"]
    if override:
        return override
    return DEFAULT_SYSTEM_PROMPT if web_search_required else DEFAULT_SYSTEM_PROMPT_WITH_REFERENCE


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
    # 只有在该模型被要求「自己联网」时才校验供应商的联网能力；关掉该要求后
    # 允许启用不联网的模型，由检索提供者供给资料。
    if runtime.web_search_required and not spec.native_web_search:
        raise AIModelError(
            f"{spec.name} 当前配置的官方接口不支持本项目要求的强制联网搜索，不能启用"
        )
    if spec.protocol != "responses":
        raise AIModelError("该供应商的强制联网适配器尚不可用")
    return spec


def thinking_control_payload(base_url: str, runtime: AIModelRuntime) -> dict[str, Any]:
    """按接口实际宿主返回「关闭/开启思考模式」所需的请求参数。

    思考模式的开关参数在各家 Responses 接口上并不统一，而且写错参数不会报错，
    只会被静默忽略——表现为「以为关了思考、其实还在思考」，思考 token 会先吃掉
    max_output_tokens，业务路径只给 1024~1800，于是读到一半就 incomplete
    （2026-09-11 实测：DeepSeek 官方加 thinking.disabled / enable_thinking=false
    后 reasoning_tokens 仍有 113 / 57，只有 reasoning.effort=none 真正归零）。

    必须按 base_url 判定而不是按供应商：项目允许把 deepseek 供应商指向百炼地址，
    同一个参数名在两家并不通用（百炼不认 reasoning.effort，会直接 400）。
    """
    host = urlsplit(base_url).netloc.lower()
    if host.endswith("dashscope.aliyuncs.com"):
        # 百炼：沿用 Chat 兼容的 enable_thinking。
        return {"enable_thinking": runtime.thinking_enabled}
    if host.endswith("deepseek.com"):
        # DeepSeek 官方 Responses：reasoning.effort，none 表示关闭思考。
        return {"reasoning": {"effort": "high" if runtime.thinking_enabled else "none"}}
    # 其余供应商（含 OpenAI、自定义兼容接口）不擅自注入参数，避免未知字段被拒。
    return {}


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


# 外部检索用的提示词。只要求产出可核实的资料，不给预测，便于安全地作为
# 参考上下文拼进主模型的提示词。
RETRIEVAL_INSTRUCTION = (
    "你是足球资料检索员。请联网检索下面这些比赛的公开信息，输出一份客观的资料简报，"
    "供另一位分析师参考。\n\n"
    "严格要求：\n"
    "1. 逐场输出并标注比赛编号，与清单一一对应；\n"
    "2. 只写检索到的可核实事实：双方近期战绩、历史交锋、伤停与停赛、关键球员状态、"
    "赛程密度与主客场表现；\n"
    "3. 信息注明来源日期；确实检索不到就写「未检索到」，严禁编造；\n"
    "4. 只输出资料简报，不要给预测、推荐或投注建议；\n"
    "5. 不要讨论赔率、SP值、概率或系统策略。\n\n"
    "= = = 比赛清单 = = =\n\n"
)


def _post_responses(
    base_url: str, api_key: str, payload: dict[str, Any], timeout_seconds: int
) -> dict[str, Any]:
    """发送一次 Responses 请求，返回已校验状态与错误的 JSON 结果。"""
    # 0 或负数表示不限制超时：思考模型 + 强制联网搜索的完整分析
    # （例如 6 场比赛的 HAD 计划）可能远超 600 秒，由后台任务耐心等待。
    timeout = timeout_seconds if timeout_seconds > 0 else None
    request = urllib.request.Request(
        base_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ScoreFourfold/0.7.0 (required-web-search)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        if timeout is None:
            raise AIModelError("模型调用超时") from exc
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
    return result


def _retrieve_reference(
    search_runtime: AIModelRuntime, prompt: str, timeout_seconds: int
) -> str:
    """先用具备联网能力的模型做一次检索，返回资料简报；失败返回空字符串。

    检索只是给主模型补充外部事实，任何失败都不应该阻断分析主流程。
    """
    try:
        result = _post_responses(
            search_runtime.base_url,
            search_runtime.api_key,
            {
                "model": search_runtime.model_name,
                "input": [
                    {
                        "role": "system",
                        "content": "你是严谨的足球资料检索员，只输出可核实的事实。",
                    },
                    {"role": "user", "content": RETRIEVAL_INSTRUCTION + prompt},
                ],
                "tools": [{"type": "web_search"}],
                "tool_choice": {"type": "web_search"},
                # 检索本身不做思考模式开关：不同宿主对此参数的兼容性不一致，关错
                # 可能导致模型干脆不联网。这里给足额度让它带思考输出完整简报。
                "max_output_tokens": 8192,
            },
            timeout_seconds,
        )
        text, searched = _response_text_and_search(result)
        if not searched:
            LOGGER.warning(
                "检索模型 %s 没有执行联网搜索，本次不带外部资料继续分析",
                search_runtime.model_name,
            )
            return ""
        LOGGER.info(
            "检索模型 %s 已产出资料简报（%d 字）", search_runtime.model_name, len(text)
        )
        return text
    except Exception as exc:  # noqa: BLE001 - 检索失败必须降级而不是中断
        LOGGER.warning("检索模型 %s 调用失败：%s", search_runtime.model_name, exc)
        return ""


def _merge_reference(prompt: str, reference: str) -> str:
    """把资料简报拼进提示词，并明确要求模型不得越过资料编造事实。"""
    return (
        f"{prompt}\n\n"
        "============ 以下是检索员刚刚联网核实到的公开资料 ============\n"
        f"{reference}\n"
        "======================= 资料结束 =======================\n"
        "请以上述资料中可核实的信息作为比赛分析依据。资料未覆盖的部分请明确说明"
        "「资料未提供」，禁止凭记忆补充或编造具体战绩、比分、伤停信息。"
    )


def call_with_web_search(
    runtime: AIModelRuntime,
    prompt: str,
    *,
    timeout_seconds: int,
    max_output_tokens: int,
) -> str:
    spec = validate_runtime(runtime)

    # 外部检索：先用具备联网能力的模型产出资料简报，再拼进提示词。检索失败
    # 不影响主流程，只是让主模型在缺少外部资料的情况下作答。
    if runtime.reference_provider is not None:
        reference = _retrieve_reference(runtime.reference_provider, prompt, timeout_seconds)
        if reference:
            prompt = _merge_reference(prompt, reference)

    needs_own_search = runtime.web_search_required
    payload: dict[str, Any] = {
        "model": runtime.model_name,
        "input": [
            {"role": "system", "content": effective_system_prompt(needs_own_search)},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_output_tokens,
    }
    if needs_own_search:
        # 百炼思考模式不允许 tool_choice="required"；开启时省略该参数并在响应端校验
        # web_search_call。深度思考由每个模型配置明确控制，不能再根据输出额度自动开启。
        # 百炼 qwen3 系列 Normal 模式（enable_thinking=false）不支持 web_extractor，
        # 且不会主动调用 web_search，因此 Normal 模式仅保留 web_search 并显式指定
        # tool_choice 强制联网；思考模式则附加 web_extractor 且不带 tool_choice。
        payload["tools"] = [{"type": "web_search"}]
        if spec.code == "qwen":
            tools = [{"type": "web_search"}]
            if runtime.thinking_enabled:
                tools.append({"type": "web_extractor"})
            payload["tools"] = tools
            if not runtime.thinking_enabled:
                payload["tool_choice"] = {"type": "web_search"}
        else:
            payload["tool_choice"] = {"type": "web_search"}

    # 思考模式必须由请求显式控制。DeepSeek V4 系列默认开启思考，而业务路径只给
    # 1024~1800 个输出 token，思考还没结束就被 max_output_tokens 截断。
    payload.update(thinking_control_payload(runtime.base_url, runtime))

    def _attempt(attempt: int) -> str:
        started_at = time.monotonic()
        result = _post_responses(runtime.base_url, runtime.api_key, payload, timeout_seconds)
        text, searched = _response_text_and_search(result)
        if needs_own_search and not searched:
            raise AIModelError("模型连接正常，但没有执行项目要求的联网搜索")
        LOGGER.info(
            "AI model %s attempt %s completed in %.1f seconds",
            runtime.model_name,
            attempt,
            time.monotonic() - started_at,
        )
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
        return _attempt(1)
    except AIModelError as exc:
        message = str(exc)
        if not any(marker in message for marker in transient_markers):
            raise
        LOGGER.warning(
            "AI model %s attempt 1 failed with a transient response (%s); retrying once",
            runtime.model_name,
            message,
        )
        time.sleep(5)
        return _attempt(2)


def test_model(runtime: AIModelRuntime, timeout_seconds: int) -> str:
    # 要求模型自己联网时，用一条必须联网才能答对的提示词；否则只验证连通性，
    # 因为不联网的模型无法知道当前时间。
    prompt = (
        "请联网查询当前北京时间。完成搜索后只回复：AI连接正常"
        if runtime.web_search_required
        else "只回复：AI连接正常"
    )
    result = call_with_web_search(
        runtime,
        prompt,
        timeout_seconds=timeout_seconds,
        # 不能用 256：DeepSeek V4 系列默认开启思考模式，光 reasoning 就要吃掉
        # 200+ tokens，会把「没联网」误报成「输出达到长度上限」。现在请求会按
        # 接口宿主显式关闭思考（reasoning.effort=none / enable_thinking=false），
        # 但 1024 的余量仍是必要的保险，测试只回一句话，花费可忽略。
        max_output_tokens=1024,
    )
    if "AI连接正常" not in result.replace(" ", ""):
        raise AIModelError("模型已响应，但测试口令不正确")
    if runtime.web_search_required:
        return "API、模型和强制联网搜索测试通过"
    if runtime.reference_provider is not None:
        return "API、模型和联网检索资料测试通过"
    return "API、模型和基础连通性测试通过"


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
