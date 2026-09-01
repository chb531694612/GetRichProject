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
    DEFAULT_PLAN_REQUIREMENTS,
    DEFAULT_SUMMARY_REQUIREMENTS,
    SOURCE_REQUIREMENTS,
    analyze_matches,
    analyze_plan_from_leg_data,
    probe_qwen,
    qwen_analyze,
    query_results_via_ai,
    _build_result_query_prompt,
    _parse_result_query,
)
from score_fourfold.ai_models import (
    DEFAULT_SYSTEM_PROMPT,
    set_prompt_overrides,
)
from score_fourfold.domain import MarketType, ResultStatus, ScoreOption

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
    def _run_plan_analysis_with_runtime(self, runtime):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "分析完成",
                "suggestions": [
                    {"match_id": leg.match_id, "pick": "1:0", "reason": "理由"}
                    for leg in self._legs()
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.call_with_web_search",
            return_value=content,
        ) as called:
            analyze_plan_from_leg_data(
                self._legs(), MarketType.CRS, settings, runtime=runtime
            )
        return called

    def test_plan_analysis_uses_compact_output_budget_without_thinking(self):
        # 提示词回退后输出需求大幅下降，非思考模式用精简预算控制成本。
        called = self._run_plan_analysis_with_runtime(
            SimpleNamespace(provider="qwen", thinking_enabled=False)
        )
        self.assertEqual(called.call_args.kwargs["max_output_tokens"], 1800)

    def test_plan_analysis_keeps_large_output_budget_for_thinking_models(self):
        # 回归保护：思考模式下思维链会计入输出预算，预算不足曾导致空/不完整响应，
        # 因此开启思考时必须沿用较大的上限。
        called = self._run_plan_analysis_with_runtime(
            SimpleNamespace(provider="qwen", thinking_enabled=True)
        )
        self.assertEqual(called.call_args.kwargs["max_output_tokens"], 16384)

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

    def test_zero_timeout_means_unlimited_wait(self):
        settings = make_settings(
            Path("data"),
            qwen_api_key="secret",
            ai_analysis_enabled=True,
            ai_http_timeout_seconds=0,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("AI连接正常")),
        ) as mocked:
            self.assertEqual(probe_qwen(settings), "AI连接正常")
        # 0 表示不限制超时，urlopen 收到 None 而不是 0。
        self.assertIsNone(mocked.call_args.kwargs["timeout"])

    def test_probe_uses_authenticated_qwen_responses_with_search(self):
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
        # 探测用小输出配额关闭思考模式（Normal 模式不支持 web_extractor），tools 仅保留 web_search，
        # 且必须显式指定 tool_choice 强制联网（qwen3 系列 Normal 模式不会主动调用 web_search）。
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["tool_choice"], {"type": "web_search"})
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
        # 回退后不再逐一点名来源平台，但仍要求独立来源交叉验证。
        self.assertIn("相互独立的来源", prompt)
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
        # 回退后不再逐一点名来源平台，但仍要求独立来源交叉验证。
        self.assertIn("相互独立的来源", prompt)
        self.assertNotIn("option_code", prompt)
        self.assertNotIn("SP", prompt)
        self.assertNotIn("5.00", prompt)
        self.assertNotIn("0.20", prompt)

    def test_plan_prompt_includes_source_check_and_anonymous_history(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps({"summary": "有依据。", "suggestions": [
            {"match_id": "1001", "pick": "1:0", "reason": "来源充分"},
            {"match_id": "1002", "pick": "1:1", "reason": "来源充分"},
        ]}, ensure_ascii=False)
        history = "最近120个已结算场次：主胜45.0%，常见比分1:1。"
        with patch("score_fourfold.ai_analyzer.urllib.request.urlopen",
                   return_value=_Response(_qwen_payload(content))) as mocked:
            analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings,
                                       history_context=history)
        prompt = json.loads(mocked.call_args.args[0].data.decode("utf-8"))["input"][1]["content"]
        # 回退后不再逐一点名来源平台、也不再要求每场核对 3 个来源，
        # 但仍保留独立来源交叉验证这一底线要求。
        self.assertIn("相互独立的来源", prompt)
        self.assertNotIn("雷速体育", prompt)
        self.assertNotIn("至少核对 3 个", prompt)
        # 匿名历史统计仍作为弱参考注入。
        self.assertIn(history, prompt)

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
            side_effect=[
                _Response(_qwen_payload(content)),
                _Response(_qwen_payload(content)),
            ],
        ) as mocked:
            with self.assertRaisesRegex(AIAnalysisError, "cannot map to a real option"):
                analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)
        self.assertEqual(mocked.call_count, 2)

    def test_plan_analysis_retries_non_json_once_with_correction(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        valid = json.dumps(
            {
                "summary": "已重新核对",
                "suggestions": [
                    {"match_id": "1001", "pick": "1:1", "reason": "状态稳定"},
                    {"match_id": "1002", "pick": "1:0", "reason": "主场占优"},
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            side_effect=[
                _Response(_qwen_payload("我先说明一下分析过程，稍后再给建议。")),
                _Response(_qwen_payload(valid)),
            ],
        ) as mocked:
            result = analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)

        self.assertEqual(result.summary, "已重新核对")
        self.assertEqual(mocked.call_count, 2)
        retry_payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        retry_prompt = retry_payload["input"][1]["content"]
        self.assertIn("上一次回答未通过系统的结构校验", retry_prompt)
        self.assertIn("只能输出一个完整", retry_prompt)
        self.assertNotIn("我先说明一下分析过程", retry_prompt)

    def test_plan_analysis_stops_after_two_invalid_responses(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        invalid = _qwen_payload("仍然没有 JSON")
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            side_effect=[_Response(invalid), _Response(invalid)],
        ) as mocked:
            with self.assertRaisesRegex(AIAnalysisError, "not a JSON object"):
                analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)
        self.assertEqual(mocked.call_count, 2)

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
        # 提示词已回退到上一版本：不再包含"大胆性"鼓励内容。
        self.assertNotIn("大胆", prompt)
        self.assertNotIn("胆量", prompt)
        self.assertIn("7+", prompt)
        # 成本回退后不再包含爆冷/进球数专项长段落。
        self.assertNotIn("爆冷分析要求", prompt)
        self.assertNotIn("进球数玩法专项要求", prompt)

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
        # 提示词已回退到上一版本：不再包含"大胆性要求"段落。
        self.assertNotIn("大胆性要求", prompt)
        self.assertIn("冷门", prompt)

    def test_default_prompts_stay_reverted_after_boldness_rollout(self):
        # 回归保护：默认提示词不得再出现"大胆/胆量"类鼓励措辞。
        self.assertNotIn("大胆", DEFAULT_PLAN_REQUIREMENTS)
        self.assertNotIn("胆量", DEFAULT_SUMMARY_REQUIREMENTS)
        self.assertNotIn("胆量", DEFAULT_SYSTEM_PROMPT)
        self.assertNotIn("爆冷信号多于顺势信号", DEFAULT_PLAN_REQUIREMENTS)

    def test_default_prompts_stay_compact_after_cost_rollback(self):
        # 回归保护：8/12 起的爆冷/进球数专项长段落与多来源逐场核对要求
        # 会成倍抬高 token 成本，回退后不得再出现。
        self.assertNotIn("爆冷分析要求", DEFAULT_PLAN_REQUIREMENTS)
        self.assertNotIn("进球数玩法专项要求", DEFAULT_PLAN_REQUIREMENTS)
        self.assertNotIn("总进球逐场分布", DEFAULT_SUMMARY_REQUIREMENTS)
        self.assertNotIn("每场至少核对 3 个相互独立的来源", "\n".join(SOURCE_REQUIREMENTS))
        # 总体判断字数已压回 120 字（提示词内声明为 160 字上限的 JSON 输出）。
        self.assertIn("不超过120字", DEFAULT_SUMMARY_REQUIREMENTS)
        # 回退仍保留极短的防幻觉约束，避免 AI 乱推冷门。
        self.assertIn("不得仅凭实力差距强行推荐冷门", DEFAULT_PLAN_REQUIREMENTS)
        self.assertIn("不得机械比较信号数量", DEFAULT_PLAN_REQUIREMENTS)
        self.assertIn(
            "不得仅凭实力差距或信号数量机械推导结论", DEFAULT_SUMMARY_REQUIREMENTS
        )

    def test_prompt_overrides_replace_default_requirements(self):
        self.addCleanup(set_prompt_overrides)
        set_prompt_overrides(
            system_prompt="自定义系统提示词ABC",
            plan_requirements="自定义计划要求XYZ",
            summary_requirements="自定义总结要求QRS",
        )
        settings = make_settings(Path("data"), qwen_api_key="secret")
        content = json.dumps(
            {
                "summary": "test",
                "suggestions": [
                    {"match_id": "1001", "pick": "1:1", "reason": "ok"},
                    {"match_id": "1002", "pick": "1:0", "reason": "ok"},
                ],
            }
        )
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload(content)),
        ) as mocked:
            analyze_plan_from_leg_data(self._legs(), MarketType.CRS, settings)

        payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["input"][0]["content"], "自定义系统提示词ABC")
        prompt = payload["input"][1]["content"]
        self.assertIn("自定义计划要求XYZ", prompt)
        self.assertNotIn("爆冷分析要求", prompt)

    def test_summary_prompt_uses_override_when_set(self):
        self.addCleanup(set_prompt_overrides)
        set_prompt_overrides(summary_requirements="自定义总结要求QRS")
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
        selected = ScoreOption("s01s00", "1:0", Decimal("9.99"), Decimal("0.12"))
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response(_qwen_payload("联网分析完成")),
        ) as mocked:
            qwen_analyze([(match, selected)], MarketType.CRS, settings)

        payload = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
        prompt = payload["input"][1]["content"]
        self.assertIn("自定义总结要求QRS", prompt)
        self.assertNotIn("空泛套话", prompt)

    # --- 联网查赛果（结算兜底） ---

    @staticmethod
    def _result_legs():
        return [
            SimpleNamespace(
                match_id="1001",
                match_num="周二001",
                business_date="2026-07-22",
                league="测试联赛",
                home="主队1",
                away="客队1",
                start_at=datetime(2026, 7, 22, 20, 0),
            ),
            SimpleNamespace(
                match_id="1002",
                match_num="周二002",
                business_date="2026-07-22",
                league="测试联赛",
                home="主队2",
                away="客队2",
                start_at=datetime(2026, 7, 22, 21, 30),
            ),
        ]

    def test_result_query_prompt_only_sends_basic_fixture_info(self):
        legs = self._result_legs()
        prompt = _build_result_query_prompt(legs)
        self.assertIn("match_id=1001", prompt)
        self.assertIn("周二002", prompt)
        self.assertIn("主队1 vs 客队1", prompt)
        self.assertIn("北京时间", prompt)
        # 只允许发送编号/日期/联赛/球队/开赛时间，绝不能带赔率或概率。
        self.assertNotIn("赔率", prompt)
        self.assertNotIn("odds", prompt)
        self.assertNotIn("probability", prompt)

    def test_parse_result_query_returns_final_and_skips_unknown(self):
        content = json.dumps(
            {
                "results": [
                    {"match_id": "1001", "status": "final", "home_score": 2, "away_score": 1},
                    {"match_id": "1002", "status": "unknown", "home_score": 0, "away_score": 0},
                ]
            },
            ensure_ascii=False,
        )
        results = _parse_result_query(content, self._result_legs())
        self.assertEqual(set(results), {"1001"})
        result = results["1001"]
        self.assertEqual(result.status, ResultStatus.FINAL)
        self.assertEqual(result.home_score, 2)
        self.assertEqual(result.away_score, 1)
        self.assertEqual(result.official_status, "AI联网确认")
        self.assertEqual(result.home_team, "主队1")

    def test_parse_result_query_rejects_bad_scores_and_unknown_ids(self):
        content = json.dumps(
            {
                "results": [
                    {"match_id": "1001", "status": "final", "home_score": 99, "away_score": 1},
                    {"match_id": "9999", "status": "final", "home_score": 1, "away_score": 0},
                    {"match_id": "1002", "status": "final", "home_score": -1, "away_score": 0},
                ]
            },
            ensure_ascii=False,
        )
        self.assertEqual(_parse_result_query(content, self._result_legs()), {})

    def test_query_results_via_ai_never_raises_on_ai_failure(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        with patch(
            "score_fourfold.ai_analyzer._qwen_response",
            side_effect=AIAnalysisError("boom"),
        ):
            self.assertEqual(
                query_results_via_ai(self._result_legs(), settings, runtime=None),
                {},
            )

    def test_query_results_via_ai_never_raises_on_invalid_json(self):
        settings = make_settings(Path("data"), qwen_api_key="secret")
        with patch(
            "score_fourfold.ai_analyzer._qwen_response",
            return_value="这不是 JSON",
        ):
            self.assertEqual(
                query_results_via_ai(self._result_legs(), settings, runtime=None),
                {},
            )


if __name__ == "__main__":
    unittest.main()
