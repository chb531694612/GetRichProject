from __future__ import annotations

import json
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import patch

from score_fourfold.ai_models import (
    AIModelError,
    AIModelRuntime,
    call_with_web_search,
    public_provider_catalog,
    test_model as probe_model,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class AIModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.runtime = AIModelRuntime(
            config_id="qwen-test",
            provider="qwen",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
            model_name="qwen3.7-max",
            api_key="secret",
        )

    def test_provider_catalog_contains_main_domestic_models(self):
        codes = {item["code"] for item in public_provider_catalog()}
        self.assertTrue(
            {"qwen", "deepseek", "zhipu", "moonshot", "doubao", "hunyuan", "qianfan", "minimax", "siliconflow"}.issubset(codes)
        )

    def test_responses_adapter_requires_and_verifies_web_search(self):
        payload = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "AI连接正常"}],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as opened:
            self.assertIn("测试通过", probe_model(self.runtime, 10))
        request = opened.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["tool_choice"], {"type": "web_search"})
        # 小输出配额的连接性探测关闭思考模式（Normal 模式不支持 web_extractor），tools 仅保留 web_search；
        # Normal 模式必须显式指定 tool_choice 强制联网（qwen3 系列不会主动调用 web_search）。
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertFalse(body["enable_thinking"])

    def test_qwen_thinking_mode_attaches_web_extractor(self):
        # 分析请求输出配额充足时开启思考模式，此时才允许附带 web_extractor；
        # 思考模式不允许 tool_choice，依赖模型自动搜索 + 响应端校验。
        payload = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "AI连接正常"}],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as opened:
            call_with_web_search(
                replace(self.runtime, thinking_enabled=True),
                "test",
                timeout_seconds=10,
                max_output_tokens=2048,
            )
        request = opened.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertTrue(body["enable_thinking"])
        self.assertNotIn("tool_choice", body)
        self.assertEqual(
            body["tools"],
            [{"type": "web_search"}, {"type": "web_extractor"}],
        )

    def test_qwen_analysis_defaults_to_normal_mode(self):
        payload = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {"type": "message", "content": [{"type": "output_text", "text": "完成"}]},
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as opened:
            call_with_web_search(
                self.runtime, "test", timeout_seconds=10, max_output_tokens=16384
            )
        body = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["tool_choice"], {"type": "web_search"})
        self.assertEqual(body["tools"], [{"type": "web_search"}])

    def test_zero_timeout_disables_urlopen_deadline(self):
        # 0 表示不限制超时：思考模型的完整分析可能远超 600 秒，
        # 后台任务应等待模型自然返回而不是中途掐断。
        payload = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "分析完成"}],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as opened:
            result = call_with_web_search(
                self.runtime,
                "test",
                timeout_seconds=0,
                max_output_tokens=2048,
            )
        self.assertEqual(result, "分析完成")
        self.assertIsNone(opened.call_args.kwargs["timeout"])

    def test_responses_adapter_rejects_answer_without_actual_search(self):
        payload = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "AI连接正常"}],
                }
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)):
            with self.assertRaisesRegex(AIModelError, "没有执行"):
                call_with_web_search(
                    self.runtime,
                    "test",
                    timeout_seconds=10,
                    max_output_tokens=64,
                )

    def test_responses_adapter_explains_output_limit(self):
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)):
            with patch("time.sleep"):
                with self.assertRaisesRegex(AIModelError, "达到长度上限"):
                    call_with_web_search(
                        self.runtime,
                        "test",
                        timeout_seconds=10,
                        max_output_tokens=64,
                    )

    def test_deepseek_responses_endpoint_supports_native_web_search(self):
        runtime = AIModelRuntime(
            config_id="deepseek-test",
            provider="deepseek",
            base_url="https://api.deepseek.com/responses",
            model_name="deepseek-v4-flash",
            api_key="secret",
        )
        payload = {
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"type": "search", "queries": ["当前北京时间"]},
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "AI连接正常"}],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as opened:
            self.assertIn("测试通过", probe_model(runtime, 10))
        request = opened.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        # DeepSeek Responses 兼容接口推荐对象形式强制 web_search。
        self.assertEqual(body["tool_choice"], {"type": "web_search"})
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        # qwen 专属字段不应出现在 DeepSeek 请求中。
        self.assertNotIn("enable_thinking", body)


class _ErrorResponse:
    def __init__(self, code, body=""):
        self.code = code
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def _http_error(code):
    error = urllib.error.HTTPError("https://example.invalid", code, "err", {}, None)
    return error


class RetryBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = AIModelRuntime(
            config_id="deepseek-retry",
            provider="deepseek",
            base_url="https://api.deepseek.com/responses",
            model_name="deepseek-v4-flash",
            api_key="secret",
        )

    def test_transient_500_is_retried_once_then_succeeds(self):
        payload = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "AI连接正常"}],
                },
            ],
        }
        # HTTPError must be raised; patch urlopen to raise it on first call.
        def flaky(*_args, **_kwargs):
            if flaky.calls == 0:
                flaky.calls += 1
                raise _http_error(500)
            flaky.calls += 1
            return _Response(payload)

        flaky.calls = 0
        with patch("urllib.request.urlopen", side_effect=flaky) as opened:
            with patch("time.sleep") as slept:
                result = call_with_web_search(
                    self.runtime,
                    "test",
                    timeout_seconds=10,
                    max_output_tokens=64,
                )
        self.assertEqual(result, "AI连接正常")
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(5)

    def test_empty_content_is_retried_once_then_succeeds(self):
        empty = {
            "status": "completed",
            "output": [{"type": "web_search_call"}],
        }
        completed = {
            "status": "completed",
            "output": [
                {"type": "web_search_call"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "分析完成"}],
                },
            ],
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[_Response(empty), _Response(completed)],
        ) as opened:
            with patch("time.sleep"):
                result = call_with_web_search(
                    self.runtime,
                    "test",
                    timeout_seconds=10,
                    max_output_tokens=2048,
                )
        self.assertEqual(result, "分析完成")
        self.assertEqual(opened.call_count, 2)

    def test_transient_500_is_not_retried_when_second_attempt_fails(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda *a, **k: (_ for _ in ()).throw(_http_error(500)),
        ) as opened:
            with patch("time.sleep"):
                with self.assertRaisesRegex(AIModelError, "HTTP 500"):
                    call_with_web_search(
                        self.runtime,
                        "test",
                        timeout_seconds=10,
                        max_output_tokens=64,
                    )
        self.assertEqual(opened.call_count, 2)

    def test_incomplete_status_is_retried_once(self):
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }

        def flaky(*_args, **_kwargs):
            if flaky.calls == 0:
                flaky.calls += 1
                return _Response(payload)
            flaky.calls += 1
            return _Response(
                {
                    "status": "completed",
                    "output": [
                        {"type": "web_search_call"},
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "AI连接正常"}],
                        },
                    ],
                }
            )

        flaky.calls = 0
        with patch("urllib.request.urlopen", side_effect=flaky) as opened:
            with patch("time.sleep"):
                result = call_with_web_search(
                    self.runtime,
                    "test",
                    timeout_seconds=10,
                    max_output_tokens=64,
                )
        self.assertEqual(result, "AI连接正常")
        self.assertEqual(opened.call_count, 2)

    def test_deterministic_error_is_not_retried(self):
        # 4xx 属于确定性错误，直接抛出不重试。
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda *a, **k: (_ for _ in ()).throw(_http_error(401)),
        ) as opened:
            with self.assertRaisesRegex(AIModelError, "HTTP 401"):
                call_with_web_search(
                    self.runtime,
                    "test",
                    timeout_seconds=10,
                    max_output_tokens=64,
                )
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
