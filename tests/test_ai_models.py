from __future__ import annotations

import json
import unittest
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
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["tools"], [{"type": "web_search"}, {"type": "web_extractor"}])
        self.assertTrue(body["enable_thinking"])

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


if __name__ == "__main__":
    unittest.main()
