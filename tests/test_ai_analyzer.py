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
    analyze_matches,
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
    def test_timeout_is_normalized_and_automatic_analysis_does_not_raise(self):
        settings = make_settings(
            Path("data"),
            qwen_api_key="secret",
            ai_analysis_enabled=True,
            ai_http_timeout_seconds=600,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            side_effect=TimeoutError("The read operation timed out"),
        ) as mocked:
            with self.assertRaisesRegex(AIAnalysisError, "timed out after 600 seconds"):
                probe_qwen(settings)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 600)

        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            side_effect=TimeoutError("The read operation timed out"),
        ):
            self.assertEqual(analyze_matches([], MarketType.CRS, settings), "")

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
        self.assertIn("历史交锋", prompt)
        self.assertIn("讨论区", prompt)
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
        self.assertIn("历史交锋", prompt)
        self.assertIn("讨论区", prompt)
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

    @staticmethod
    def _ttg_legs():
        options = tuple(
            ScoreOption(
                f"s{goals}",
                "7+" if goals == 7 else str(goals),
                Decimal(str(3 + goals)),
                Decimal("0.125"),
                False,
            )
            for goals in range(8)
        )
        return [
            SimpleNamespace(
                match_id=str(2000 + index),
                match_num=f"周三00{index}",
                business_date="2026-07-22",
                league="测试联赛",
                home=f"主队{index}",
                away=f"客队{index}",
                start_at=datetime(2026, 7, 22, 20, index),
                score_code=f"s{index}",
                score_label=str(index),
                odds=Decimal("5.00"),
                probability=Decimal("0.125"),
                options=options,
            )
            for index in (1, 2)
        ]

    def test_ttg_plan_analysis_accepts_high_goal_and_seven_plus(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "两队进攻火力较猛，存在大比分可能。",
                "suggestions": [
                    {"match_id": "2001", "pick": "6", "reason": "双方近期进攻火爆、防线漏洞多"},
                    {"match_id": "2002", "pick": "7+", "reason": "历史交锋多次打出大比分"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ) as mocked:
            result = analyze_plan_from_leg_data(self._ttg_legs(), MarketType.TTG, settings)

        self.assertEqual([item.option_code for item in result.suggestions], ["s6", "s7"])
        self.assertEqual([item.pick_label for item in result.suggestions], ["6", "7+"])
        prompt = json.loads(mocked.call_args.args[0].data.decode("utf-8"))["input"][1]["content"]
        self.assertIn("大胆", prompt)
        self.assertIn("7+", prompt)
        self.assertIn("爆冷", prompt)

    def test_ttg_plan_analysis_normalizes_goal_variants(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "test",
                "suggestions": [
                    {"match_id": "2001", "pick": "6球", "reason": "进攻强"},
                    {"match_id": "2002", "pick": "7球以上", "reason": "交锋大球多"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ):
            result = analyze_plan_from_leg_data(self._ttg_legs(), MarketType.TTG, settings)

        self.assertEqual([item.option_code for item in result.suggestions], ["s6", "s7"])
        self.assertEqual([item.pick_label for item in result.suggestions], ["6", "7+"])

    def test_had_plan_analysis_accepts_away_upset_pick(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        options = (
            ScoreOption("h", "主胜", Decimal("1.80"), Decimal("0.50"), False),
            ScoreOption("d", "平", Decimal("3.50"), Decimal("0.28"), False),
            ScoreOption("a", "客胜", Decimal("4.50"), Decimal("0.22"), False),
        )
        legs = [
            SimpleNamespace(
                match_id=str(3000 + index),
                match_num=f"周四00{index}",
                business_date="2026-07-22",
                league="测试联赛",
                home=f"主队{index}",
                away=f"客队{index}",
                start_at=datetime(2026, 7, 22, 20, index),
                score_code="a",
                score_label="客胜",
                odds=Decimal("4.50"),
                probability=Decimal("0.22"),
                options=options,
            )
            for index in (1, 2)
        ]
        content = json.dumps(
            {
                "summary": "客队状态回升且主队轮换，存在爆冷空间。",
                "suggestions": [
                    {"match_id": "3001", "pick": "客胜", "reason": "客队连胜、主队多名主力伤停"},
                    {"match_id": "3002", "pick": "主胜", "reason": "主队主场强势"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ) as mocked:
            result = analyze_plan_from_leg_data(legs, MarketType.HAD, settings)

        self.assertEqual([item.option_code for item in result.suggestions], ["a", "h"])
        self.assertEqual([item.pick_label for item in result.suggestions], ["客胜", "主胜"])
        prompt = json.loads(mocked.call_args.args[0].data.decode("utf-8"))["input"][1]["content"]
        self.assertIn("大胆性要求", prompt)
        self.assertIn("冷门", prompt)


if __name__ == "__main__":
    unittest.main()
