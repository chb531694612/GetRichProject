from __future__ import annotations

import io
import json
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from score_fourfold.ai_analyzer import (
    AIAnalysisError,
    analyze_plan_from_leg_data,
    probe_qwen,
    qwen_analyze,
)
from score_fourfold.domain import MarketType, ScoreOption

from .helpers import make_settings


class _Response:
    def __init__(self, payload: dict):
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._stream.read()


def _qwen_payload(content: str, *, searched: bool = True) -> dict:
    output = []
    if searched:
        output.append({"type": "web_search_call", "status": "completed"})
    output.append(
        {
            "type": "message",
            "status": "completed",
            "content": [{"type": "output_text", "text": content}],
        }
    )
    return {"status": "completed", "output": output}


class AIAnalyzerTests(unittest.TestCase):
    def test_probe_uses_authenticated_qwen_responses_with_required_search(self):
        settings = make_settings(Path("data"), qwen_api_key="secret", ai_analysis_enabled=True)
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("AI连接正常")),
        ) as mocked:
            self.assertEqual(probe_qwen(settings), "AI连接正常")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen3.7-max")
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["tool_choice"], "required")
        self.assertFalse(payload["enable_thinking"])

    def test_probe_rejects_missing_key_and_empty_response(self):
        with self.assertRaisesRegex(AIAnalysisError, "not configured"):
            probe_qwen(make_settings(Path("data")))
        settings = make_settings(Path("data"), qwen_api_key="secret")
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("")),
        ):
            with self.assertRaisesRegex(AIAnalysisError, "empty content"):
                probe_qwen(settings)

    def test_probe_rejects_response_that_did_not_search(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("AI连接正常", searched=False)),
        ):
            with self.assertRaisesRegex(AIAnalysisError, "required web search"):
                probe_qwen(settings)

    def test_automatic_analysis_prompt_does_not_send_pick_odds_or_probability(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        match = SimpleNamespace(
            match_id="1001",
            match_num="周二001",
            business_date="2026-07-22",
            league="测试联赛",
            home="主队",
            away="客队",
            start_at=datetime(2026, 7, 22, 20, 0),
        )
        selected = ScoreOption(
            "s01s00", "SECRET-PICK-1:0", Decimal("9.99"), Decimal("0.12345")
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("联网分析完成")),
        ) as mocked:
            qwen_analyze([(match, selected)], MarketType.CRS, settings)

        payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        prompt = payload["input"][1]["content"]
        self.assertIn("周二001", prompt)
        self.assertIn("主队 vs 客队", prompt)
        self.assertNotIn("SECRET-PICK", prompt)
        self.assertNotIn("9.99", prompt)
        self.assertNotIn("0.12345", prompt)

    @staticmethod
    def _legs():
        options = (
            ScoreOption("s01s00", "1:0", Decimal("5.00"), Decimal("0.20")),
            ScoreOption("s01s01", "1:1", Decimal("6.00"), Decimal("0.16")),
        )
        return [
            SimpleNamespace(
                match_id=str(1000 + index),
                match_num=f"周二00{index}",
                business_date="2026-07-22",
                league="测试联赛",
                home=f"主队{index}",
                away=f"客队{index}",
                start_at=datetime(2026, 7, 22, 20, index),
                score_code="s01s00",
                score_label="1:0",
                odds=Decimal("5.00"),
                probability=Decimal("0.20"),
                options=options,
            )
            for index in (1, 2)
        ]

    def test_plan_analysis_returns_validated_recommendation_for_every_leg(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "总体风险适中。",
                "suggestions": [
                    {"match_id": "1001", "pick": "1:1", "reason": "双方近期状态接近"},
                    {"match_id": "1002", "pick": "1:0", "reason": "主队近期表现更稳"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ) as mocked:
            result = analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)

        self.assertEqual(result.summary, "总体风险适中。")
        self.assertEqual([item.option_code for item in result.suggestions], ["s01s01", "s01s00"])
        self.assertEqual([item.pick_label for item in result.suggestions], ["1:1", "1:0"])
        request_payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        prompt = request_payload["input"][1]["content"]
        self.assertIn("match_id=1001", prompt)
        self.assertIn("比赛日期=2026-07-22", prompt)
        self.assertNotIn("option_code", prompt)
        self.assertNotIn("SP", prompt)
        self.assertNotIn("5.00", prompt)
        self.assertNotIn("0.20", prompt)

    def test_plan_analysis_rejects_invented_or_missing_options(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "test",
                "suggestions": [
                    {"match_id": "1001", "pick": "9:9", "reason": "bad"},
                    {"match_id": "1002", "pick": "1:0", "reason": "ok"},
                ],
            }
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ):
            with self.assertRaisesRegex(AIAnalysisError, "cannot map to a real option"):
                analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)

    def test_exact_ai_score_can_map_to_trusted_other_score_option(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        legs = self._legs()
        legs[0].options += (
            ScoreOption("s1sh", "胜其它", Decimal("4.00"), Decimal("0.30"), True),
        )
        content = json.dumps(
            {
                "summary": "test",
                "suggestions": [
                    {"match_id": "1001", "pick": "4:2", "reason": "进攻状态较好"},
                    {"match_id": "1002", "pick": "1:0", "reason": "主场表现较稳"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ):
            result = analyze_plan_from_leg_data(legs, MarketType.CRS, settings)

        self.assertEqual(result.suggestions[0].option_code, "s1sh")
        self.assertEqual(result.suggestions[0].pick_label, "4:2")


if __name__ == "__main__":
    unittest.main()
