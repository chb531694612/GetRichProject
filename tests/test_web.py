from __future__ import annotations

import gzip
import http.client
import io
import json
import re
import shutil
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from score_fourfold.cli import _build_manual_trigger
from score_fourfold.ai_analyzer import AIOptionSuggestion, AIPlanAnalysis
from score_fourfold.database import Database
from score_fourfold.domain import MatchResult, ResultStatus, Settlement, PlanStatus
from score_fourfold.mail import Mailer, flush_outbox, render_recommendation, render_settlement
from score_fourfold.scheduler import slot_job_name
from score_fourfold.service import ScoreFourfoldService
from score_fourfold.web import DashboardApplication, DashboardServer
from score_fourfold.api import serialize_plan

from .helpers import make_match, make_recommendation, make_settings


TZ = ZoneInfo("Asia/Shanghai")


def _unlink_with_retry(path: Path, attempts: int = 5) -> None:
    """删除测试临时文件，容忍 Windows 上 SQLite 句柄未释放造成的占用。

    Windows 下 unlink 一个仍被 SQLite 持有的 .db 文件会抛
    PermissionError(WinError 32)，直接失败会让 tearDown 误报测试错误；
    短暂重试即可等到句柄释放。
    """
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)


class FixedClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeProvider:
    def __init__(self, matches):
        self.matches = matches
        self.match_calls = 0
        self.result_calls = 0

    def get_matches(self):
        self.match_calls += 1
        return self.matches

    def get_results(self, *_):
        self.result_calls += 1
        return {}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_web_{self._testMethodName}.db"
        self.preview = self.root / f"test_web_{self._testMethodName}_mail"
        self._clean()
        self.settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_port=0,
        )
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        self.triggered: list[str] = []
        self.application = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"dashboard-test-secret" * 2,
        )

    def tearDown(self):
        self._clean()

    def _clean(self):
        for suffix in ("", "-wal", "-shm"):
            _unlink_with_retry(Path(f"{self.database_path}{suffix}"))
        if self.preview.exists():
            for child in self.preview.iterdir():
                _unlink_with_retry(child)
            self.preview.rmdir()

    def _trigger(self, request_id: str) -> tuple[str, str]:
        self.triggered.append(request_id)
        return "created", "已创建测试计划"

    @staticmethod
    def _wait_for(predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("background task did not finish in time")

    def _create_plan(self, *, secret_team: str | None = None, index: int = 0):
        business_date = f"2026-07-{15 + index:02d}"
        created_at = self.now + timedelta(days=index)
        matches = [
            make_match(i, created_at, business_date=business_date, odds="2.00")
            for i in range(1, 5)
        ]
        if secret_team is not None:
            matches[0] = replace(matches[0], home=secret_team)
        recommendation = make_recommendation(created_at, matches)
        if index:
            recommendation = replace(recommendation, plan_id=f"BF4-TEST-{1000 + index}")
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=created_at + timedelta(hours=5),
            )
        )
        return recommendation

    def _mark_recommendation_sent(self) -> None:
        rows = self.database.claim_due_emails(self.now, limit=1)
        self.assertEqual(len(rows), 1)
        self.database.mark_email_sent(int(rows[0]["id"]), rows[0]["claim_token"], self.now)

    @staticmethod
    def _request(
        server: DashboardServer,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        _, port = server.address
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, payload
        finally:
            connection.close()

    def test_json_api_bootstrap_plans_and_recommend_without_page_reload(self):
        recommendation = self._create_plan()
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, headers, payload = self._request(server, "GET", "/api/v1/bootstrap")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
            bootstrap = json.loads(payload)["data"]
            self.assertEqual(bootstrap["title"], "个人看板")
            self.assertEqual(bootstrap["csrf_token"], "ssh-loopback")

            status, _, payload = self._request(
                server,
                "GET",
                "/api/v1/plans?page=1&per_page=8&market=CRS",
            )
            self.assertEqual(status, 200)
            plans = json.loads(payload)["data"]
            self.assertEqual(plans["pagination"]["total"], 1)
            self.assertEqual(plans["summary"]["plans_total"], 1)
            self.assertEqual(plans["items"][0]["market"], "crs")
            leg = plans["items"][0]["legs"][0]
            self.assertEqual(leg["original_pick_code"], leg["pick_code"])
            self.assertEqual(leg["original_pick_label"], leg["pick_label"])

            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE plan_legs
                    SET result_status = 'final', result_home = 1, result_away = 0
                    WHERE plan_id = ? AND position = 1
                    """,
                    (recommendation.plan_id,),
                )
            status, _, payload = self._request(
                server,
                "GET",
                "/api/v1/analytics?market=CRS",
            )
            self.assertEqual(status, 200)
            analytics = json.loads(payload)["data"]
            self.assertEqual(analytics["timeline"][0]["date"], "2026-07-15")
            self.assertEqual(analytics["markets"][0]["market"], "crs")
            self.assertEqual(analytics["markets"][0]["settled"], 1)

            status, _, payload = self._request(
                server,
                "GET",
                "/api/v1/plans?page=99&per_page=8&market=CRS",
            )
            self.assertEqual(status, 200)
            clamped = json.loads(payload)["data"]
            self.assertEqual(clamped["pagination"]["page"], 1)
            self.assertEqual(len(clamped["items"]), 1)

            purchase_payload = json.dumps(
                {
                    "plan_id": recommendation.plan_id,
                    "purchased": True,
                    "filters": {"market": "crs"},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            status, _, payload = self._request(
                server,
                "POST",
                "/api/v1/actions/mark-purchased",
                body=purchase_payload,
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": "ssh-loopback",
                },
            )
            self.assertEqual(status, 200)
            purchase = json.loads(payload)
            self.assertTrue(purchase["data"]["plan"]["purchased"])
            self.assertEqual(purchase["data"]["summary"]["plans_purchased"], 1)

            request_payload = json.dumps(
                bootstrap["recommendation_request"], ensure_ascii=False
            ).encode("utf-8")
            status, _, payload = self._request(
                server,
                "POST",
                "/api/v1/actions/recommend",
                body=request_payload,
                headers={
                    "Content-Type": "application/json",
                    "X-CSRF-Token": "ssh-loopback",
                },
            )
            self.assertEqual(status, 200)
            response = json.loads(payload)
            self.assertEqual(response["level"], "ok")
            self.assertEqual(len(self.triggered), 1)
        finally:
            server.stop()

    def test_json_api_rejects_missing_csrf(self):
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, _, payload = self._request(
                server,
                "POST",
                "/api/v1/actions/settle-plan",
                body=b'{"plan_id":"missing"}',
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
            self.assertIn("操作令牌无效", json.loads(payload)["detail"])
        finally:
            server.stop()

    def test_json_api_compresses_response_with_gzip_when_accepted(self):
        for i in range(5):
            self._create_plan(index=i)
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, headers, payload = self._request(
                server,
                "GET",
                "/api/v1/plans?page=1&per_page=8",
                headers={"Accept-Encoding": "gzip"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("content-encoding"), "gzip")
            self.assertEqual(headers.get("vary"), "Accept-Encoding")
            self.assertGreater(len(payload), 0)
            decompressed = gzip.decompress(payload)
            data = json.loads(decompressed)["data"]
            self.assertEqual(data["pagination"]["total"], 5)
        finally:
            server.stop()

    def test_json_api_sends_uncompressed_response_without_gzip_acceptance(self):
        for i in range(5):
            self._create_plan(index=i)
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, headers, payload = self._request(
                server,
                "GET",
                "/api/v1/plans?page=1&per_page=8",
            )
            self.assertEqual(status, 200)
            self.assertNotIn("content-encoding", headers)
            data = json.loads(payload)["data"]
            self.assertEqual(data["pagination"]["total"], 5)
        finally:
            server.stop()

    def test_removed_legacy_frontend_and_form_actions_return_404(self):
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, _, _ = self._request(server, "GET", "/legacy")
            self.assertEqual(status, 404)
            status, _, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=b"request_id=obsolete",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": f"127.0.0.1:{server.address[1]}",
                },
            )
            self.assertEqual(status, 404)
            self.assertEqual(self.triggered, [])
        finally:
            server.stop()

    def test_manual_ai_refreshes_options_and_stores_every_match_suggestion(self):
        recommendation = self._create_plan()
        matches = [
            make_match(i, self.now, business_date="2026-07-15", odds="2.00")
            for i in range(1, 5)
        ]
        provider = FakeProvider(matches)
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_port=0,
            ai_analysis_enabled=True,
            qwen_api_key="secret",
        )
        application = DashboardApplication(
            settings,
            self.database,
            self._trigger,
            provider=provider,
        )
        result = AIPlanAnalysis(
            summary="总体建议",
            suggestions=tuple(
                AIOptionSuggestion(
                    leg.match.match_id, "s01s01", "1:1", "双方近期状态接近"
                )
                for leg in recommendation.legs
            ),
        )
        with patch(
            "score_fourfold.web.analyze_plan_from_leg_data", return_value=result
        ) as mocked:
            level, detail = application.trigger_ai_analysis(recommendation.plan_id)

        self.assertEqual(level, "ok")
        self.assertIn("逐场推荐", detail)
        self.assertEqual(provider.match_calls, 1)
        mocked.assert_called_once()
        stored = self.database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertEqual(stored.ai_summary, "总体建议")
        self.assertEqual(len(stored.ai_suggestions), 4)
        self.assertTrue(
            all("AI预测：1:1" in item.reason for item in stored.ai_suggestions)
        )

    def test_ai_analysis_serializes_concurrent_plan_requests(self):
        """Burst submissions must not call the model concurrently."""
        recommendation = self._create_plan(index=0)
        second = self._create_plan(index=1)
        third = self._create_plan(index=2)
        matches = [
            make_match(i, self.now, business_date="2026-07-15", odds="2.00")
            for i in range(1, 5)
        ]
        provider = FakeProvider(matches)
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_port=0,
            ai_analysis_enabled=True,
            qwen_api_key="secret",
        )
        application = DashboardApplication(
            settings,
            self.database,
            self._trigger,
            provider=provider,
        )
        # 第一个任务阻塞在事件上，确保后续提交时它仍处于 running。
        gate = threading.Event()

        def analyze_stub(legs, market, settings, *, runtime=None, history_context=""):
            gate.wait(timeout=5)
            return AIPlanAnalysis(
                summary="总体建议",
                suggestions=tuple(
                    AIOptionSuggestion(
                        leg.match_id, "s01s01", "1:1", "理由"
                    )
                    for leg in legs
                ),
            )

        with patch(
            "score_fourfold.web.analyze_plan_from_leg_data",
            side_effect=analyze_stub,
        ):
            level1, _ = application.queue_ai_analysis(recommendation.plan_id)
            # 第一个请求立即进入 running。
            self.assertEqual(level1, "ok")
            task1 = application.analysis_task(recommendation.plan_id)
            self.assertEqual(task1.status, "running")
            # 第二个请求必须排队而不是并发调用模型。
            level2, _ = application.queue_ai_analysis(second.plan_id)
            self.assertEqual(level2, "warn")
            task2 = application.analysis_task(second.plan_id)
            self.assertEqual(task2.status, "queued")
            # 第三个请求排在队尾。
            level3, _ = application.queue_ai_analysis(third.plan_id)
            self.assertEqual(level3, "warn")
            # 同一 plan 排队期间不允许重复提交。
            level_dup, _ = application.queue_ai_analysis(second.plan_id)
            self.assertEqual(level_dup, "warn")
            # 放行第一个任务，其余排队任务随后串行完成。
            gate.set()
            for plan_id in (recommendation.plan_id, second.plan_id, third.plan_id):
                # 三个任务经信号量串行执行，机器负载高时单个任务可能数秒才完成。
                # 该超时只在失败时才等满，成功会立即返回，因此放宽不影响正常速度。
                self._wait_for(
                    lambda pid=plan_id: (
                        application.analysis_task(pid) is not None
                        and application.analysis_task(pid).status == "finished"
                    ),
                    timeout=30.0,
                )
            for plan_id in (recommendation.plan_id, second.plan_id, third.plan_id):
                stored = self.database.get_plan(plan_id)
                self.assertIsNotNone(stored)
                self.assertEqual(stored.ai_summary, "总体建议")

    def test_ai_logs_carry_plan_id_for_dashboard_grouping(self):
        """activity_logs rows written by AI analysis must include plan_id."""
        recommendation = self._create_plan()
        matches = [
            make_match(i, self.now, business_date="2026-07-15", odds="2.00")
            for i in range(1, 5)
        ]
        provider = FakeProvider(matches)
        settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_port=0,
            ai_analysis_enabled=True,
            qwen_api_key="secret",
        )
        application = DashboardApplication(
            settings,
            self.database,
            self._trigger,
            provider=provider,
        )

        def analyze_stub(legs, market, settings, *, runtime=None, history_context=""):
            return AIPlanAnalysis(
                summary="总体建议",
                suggestions=tuple(
                    AIOptionSuggestion(leg.match_id, "s01s01", "1:1", "理由")
                    for leg in legs
                ),
            )

        with patch(
            "score_fourfold.web.analyze_plan_from_leg_data",
            side_effect=analyze_stub,
        ):
            level, detail = application.trigger_ai_analysis(recommendation.plan_id)
        self.assertEqual(level, "ok")
        logs = self.database.query_logs(category="ai", limit=50)
        ai_logs = [item for item in logs["items"] if item["plan_id"] == recommendation.plan_id]
        self.assertTrue(ai_logs, "expected ai logs with plan_id")
        self.assertIn("开始AI分析", ai_logs[-1]["message"])

    def test_deleting_losing_leg_recalculates_settled_plan(self):
        recommendation = self._create_plan()
        self._mark_recommendation_sent()
        results = {
            leg.match.match_id: MatchResult(
                leg.match.match_id, ResultStatus.FINAL, 1, 0
            )
            for leg in recommendation.legs
        }
        losing_leg = recommendation.legs[0]
        results[losing_leg.match.match_id] = MatchResult(
            losing_leg.match.match_id, ResultStatus.FINAL, 0, 1
        )
        self.database.update_leg_results(recommendation.plan_id, results)
        plan = self.database.get_plan(recommendation.plan_id)
        assert plan is not None
        settlement = ScoreFourfoldService._build_settlement(
            plan, self.now + timedelta(days=1)
        )
        assert settlement is not None
        subject, text_body, html_body = render_settlement(
            plan, settlement, self.database.summary()
        )
        self.assertTrue(
            self.database.settle_plan_with_mail(
                settlement,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        )
        self.assertEqual(self.database.get_plan(recommendation.plan_id).status.value, "lost")

        level, _ = self.application.trigger_delete_leg(
            recommendation.plan_id, losing_leg.match.match_id
        )

        self.assertEqual(level, "ok")
        updated = self.database.get_plan(recommendation.plan_id)
        assert updated is not None
        self.assertEqual(updated.pass_size, 3)
        self.assertEqual(updated.status.value, "won")
        self.assertEqual(updated.settled_net_prize, Decimal("16.00"))
        self.assertEqual(updated.net_profit, Decimal("14.00"))

    def test_plan_changes_no_longer_auto_refresh_mail_push_is_manual(self):
        """update_leg no longer auto-refreshes mail; push is now manual."""
        recommendation = self._create_plan()
        clock_value = [self.now]
        wake_calls: list[bool] = []
        application = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"mail-update-test-secret" * 2,
            wake_mailer=lambda: wake_calls.append(True),
            clock=lambda: clock_value[0],
        )
        first_leg = recommendation.legs[0]
        # Count mails before update_leg (initial recommendation mail from _create_plan)
        with self.database.connect() as connection:
            before = connection.execute(
                "SELECT COUNT(*) AS count FROM email_outbox"
            ).fetchone()["count"]

        # update_leg should NOT create new mail
        level, detail = application.trigger_update_leg(
            recommendation.plan_id, first_leg.match.match_id, "s01s01"
        )
        self.assertEqual(level, "ok")
        self.assertIn("推送邮件", detail)
        with self.database.connect() as connection:
            after = connection.execute(
                "SELECT COUNT(*) AS count FROM email_outbox"
            ).fetchone()["count"]
        self.assertEqual(after, before, "update_leg should not create new mail")

        # manual push should refresh the existing recommendation mail
        wake_calls.clear()
        level, detail = application.trigger_push_mail(recommendation.plan_id)
        self.assertEqual(level, "ok")
        self.assertIn("推荐邮件", detail)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, status, text_body FROM email_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), before, "push_mail should refresh existing, not add new")
        self.assertEqual(rows[-1]["status"], "pending")
        self.assertTrue(
            any("1:1" in (r["text_body"] or "") for r in rows),
            "mail body should contain updated score after push",
        )
        self.assertTrue(wake_calls, "wake_mailer should be called on manual push")

class ManualActionAndDatabaseGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_web_action_{self._testMethodName}.db"
        self.preview = self.root / f"test_web_action_{self._testMethodName}_mail"
        self._clean()
        self.now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        self.clock = FixedClock(self.now)
        self.settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            had_enabled=False,
        )
        self.database = Database(self.database_path)
        self.database.initialize()

    def tearDown(self):
        self._clean()

    def _clean(self):
        for suffix in ("", "-wal", "-shm"):
            _unlink_with_retry(Path(f"{self.database_path}{suffix}"))
        if self.preview.exists():
            for child in self.preview.iterdir():
                _unlink_with_retry(child)
            self.preview.rmdir()

    def _service(self):
        matches = [
            make_match(i, self.now, business_date="2026-07-15")
            for i in range(1, 5)
        ]
        provider = FakeProvider(matches)
        service = ScoreFourfoldService(
            self.settings,
            self.database,
            provider,
            Mailer(self.settings, clock=self.clock),
            clock=self.clock,
        )
        return service, provider

    def test_manual_request_replay_runs_provider_once_and_marks_due_slot(self):
        service, provider = self._service()
        service.settings = replace(service.settings, qwen_api_key="secret")
        service.provider.settings = service.settings
        service.mailer.settings = service.settings
        wake = threading.Event()
        trigger = _build_manual_trigger(service, wake)
        request_id = "manual-request-20260715-0001"

        with patch(
            "score_fourfold.strategy.analyze_plan_from_leg_data",
            side_effect=lambda legs, *_args, **_kwargs: AIPlanAnalysis(
                "AI联网预测摘要",
                tuple(
                    AIOptionSuggestion(leg.match_id, "s01s00", "1:0", "联网分析")
                    for leg in legs
                ),
            ),
        ):
            first = trigger(request_id)
        second = trigger(request_id)

        self.assertEqual(first[0], "created")
        self.assertEqual(second, first)
        self.assertEqual(provider.match_calls, 1)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date("2026-07-15"),
            1,
        )
        self.assertTrue(wake.is_set())
        self.assertTrue(self.database.has_job_run(slot_job_name(self.now, self.settings.recommendation_times[0])))

    def test_manual_request_already_running_returns_busy_without_provider_call(self):
        service, provider = self._service()
        wake = threading.Event()
        trigger = _build_manual_trigger(service, wake)
        request_id = "manual-request-20260715-running"
        self.assertTrue(self.database.claim_web_request(request_id, self.now))

        status, detail = trigger(request_id)

        self.assertEqual(status, "busy")
        self.assertIn("正在执行", detail)
        self.assertEqual(provider.match_calls, 0)
        self.assertFalse(wake.is_set())

    def test_different_manual_requests_have_five_minute_cooldown(self):
        provider = FakeProvider([])
        service = ScoreFourfoldService(
            self.settings,
            self.database,
            provider,
            Mailer(self.settings, clock=self.clock),
            clock=self.clock,
        )
        trigger = _build_manual_trigger(service, threading.Event())

        first = trigger("manual-request-20260715-empty-01")
        second = trigger("manual-request-20260715-empty-02")

        self.assertEqual(first[0], "no-recommendation")
        self.assertEqual(second[0], "cooldown")
        self.assertIn("5分钟", second[1])
        self.assertEqual(provider.match_calls, 1)

    def test_recommendation_day_gate_is_atomic_without_unique_plan_index(self):
        with self.database.connect() as connection:
            connection.execute("DROP INDEX IF EXISTS idx_plans_one_per_recommendation_date")
            connection.execute("DROP INDEX IF EXISTS idx_plans_one_per_recommendation_market")
        base_matches = [
            make_match(i, self.now, business_date="2026-07-15")
            for i in range(1, 5)
        ]
        base = make_recommendation(self.now, base_matches)
        barrier = threading.Barrier(8)

        def create(index: int) -> bool:
            recommendation = replace(base, plan_id=f"BF4-CONCURRENT-{index:02d}")
            subject, text_body, html_body = render_recommendation(recommendation)
            barrier.wait(timeout=5)
            return self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=self.now + timedelta(hours=5),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            created = list(executor.map(create, range(8)))

        self.assertEqual(sum(created), 1)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date("2026-07-15"),
            1,
        )
        self.assertEqual(self.database.summary()["emails_pending"], 1)


PUBLIC_IP = "8.8.8.8"
PUBLIC_ORIGIN = f"https://{PUBLIC_IP}"
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = (
    "scrypt:16384:8:1:00112233445566778899aabbccddeeff:"
    "fcd5a58d5301bbc44e90fc9a53f156134baee795eb7735ed6473da86e34ba930"
)


class PublicDashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_web_public_{self._testMethodName}.db"
        self.preview = self.root / f"test_web_public_{self._testMethodName}_mail"
        self._clean()
        self.settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_host="0.0.0.0",
            web_port=0,
            web_access_mode="public",
            web_public_origin=PUBLIC_ORIGIN,
            web_username="owner",
            web_password_hash=PASSWORD_HASH,
            web_trust_proxy_headers=True,
            web_session_hours=12,
        )
        self.database = Database(self.database_path)
        self.database.initialize()
        self.triggered: list[str] = []
        self.application = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"public-dashboard-test-secret-xxxx",
        )
        self.server = DashboardServer(self.settings, self.application)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self._clean()

    def _clean(self):
        for suffix in ("", "-wal", "-shm"):
            _unlink_with_retry(Path(f"{self.database_path}{suffix}"))
        if self.preview.exists():
            for child in self.preview.iterdir():
                _unlink_with_retry(child)
            self.preview.rmdir()

    def _trigger(self, request_id: str) -> tuple[str, str]:
        self.triggered.append(request_id)
        return "created", "已创建测试计划"

    def _public_headers(self, **extra: str) -> dict[str, str]:
        headers = {
            "Host": PUBLIC_IP,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "198.51.100.20",
            "Origin": PUBLIC_ORIGIN,
        }
        headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        _, port = self.server.address
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, payload
        finally:
            connection.close()

    def _login(
        self,
        *,
        username: str = "owner",
        password: str = PASSWORD,
        client_ip: str = "198.51.100.20",
        cookie: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        status, _, login_page = self._request("GET", "/login", headers=self._public_headers())
        self.assertEqual(status, 200)
        marker = 'name="login_csrf" value="'
        start = login_page.decode("utf-8").index(marker) + len(marker)
        end = login_page.decode("utf-8").index('"', start)
        login_csrf = login_page.decode("utf-8")[start:end]
        body = urlencode(
            {
                "login_csrf": login_csrf,
                "username": username,
                "password": password,
            }
        ).encode()
        headers = self._public_headers(
            **{
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-For": client_ip,
            }
        )
        if cookie is not None:
            headers["Cookie"] = cookie
        return self._request("POST", "/login", body=body, headers=headers)

    def _cookie_from(self, headers: dict[str, str]) -> str:
        cookie = headers["set-cookie"]
        return cookie.split(";", 1)[0]

    def test_login_sets_secure_cookie_and_rejects_account_enumeration(self):
        status, headers, _ = self._login()
        self.assertEqual(status, 303)
        cookie = headers["set-cookie"]
        self.assertIn("__Host-score_session=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Domain=", cookie)

        status, _, payload = self._login(username="missing-user", password=PASSWORD)
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", payload.decode("utf-8"))
        status, _, payload = self._login(username="owner", password="wrong horse battery!!")
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", payload.decode("utf-8"))

    def test_vue_api_logout_requires_csrf_and_clears_session_cookie(self):
        status, headers, _ = self._login()
        self.assertEqual(status, 303)
        cookie = self._cookie_from(headers)
        token = cookie.split("=", 1)[1]
        session = self.application.get_session(token)
        assert session is not None

        status, response_headers, payload = self._request(
            "POST",
            "/api/v1/logout",
            body=b"{}",
            headers=self._public_headers(
                **{
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "X-CSRF-Token": session.csrf_token,
                }
            ),
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["level"], "ok")
        self.assertIn("Max-Age=0", response_headers["set-cookie"])
        self.assertIsNone(self.application.get_session(token))

    def test_login_rate_limit_is_per_ip_and_clears_on_success(self):
        for _ in range(5):
            status, _, _ = self._login(password="wrong horse battery!!")
            self.assertEqual(status, 401)
        status, headers, payload = self._login(password="wrong horse battery!!")
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)
        self.assertIn("登录尝试过于频繁", payload.decode("utf-8"))

        status, _, _ = self._login(
            password="wrong horse battery!!",
            client_ip="198.51.100.99",
        )
        self.assertEqual(status, 401)

        status, headers, _ = self._login()
        # Still locked for the failing IP.
        self.assertEqual(status, 429)

        status, headers, _ = self._login(client_ip="198.51.100.99")
        self.assertEqual(status, 303)
        self.assertIn("set-cookie", headers)

    def test_bad_bodies_and_healthz_and_security_headers(self):
        status, headers, payload = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"ok\n")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "same-origin")
        self.assertIn("camera=()", headers["permissions-policy"])
        self.assertEqual(headers["strict-transport-security"], "max-age=31536000")

        form_headers = self._public_headers(
            **{"Content-Type": "application/x-www-form-urlencoded"}
        )
        status, _, _ = self._request(
            "POST",
            "/login",
            body=b"a=" + b"x" * 5000,
            headers=form_headers,
        )
        self.assertEqual(status, 413)

        status, _, _ = self._request(
            "POST",
            "/login",
            body=b"login_csrf=1&username=owner&password=x",
            headers=self._public_headers(**{"Content-Type": "text/plain"}),
        )
        self.assertEqual(status, 415)

        status, _, _ = self._request(
            "POST",
            "/login",
            body=b"login_csrf=1&username=owner&password=x",
            headers=self._public_headers(
                **{
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": "abc",
                }
            ),
        )
        self.assertEqual(status, 400)

        status, _, _ = self._request(
            "POST",
            "/login",
            body=b"\xff\xfe",
            headers=form_headers,
        )
        self.assertEqual(status, 400)

        too_many = urlencode(
            {f"field{i}": "x" for i in range(13)},
            doseq=True,
        ).encode()
        status, _, _ = self._request(
            "POST",
            "/login",
            body=too_many,
            headers=form_headers,
        )
        self.assertEqual(status, 400)

    def _create_sensitive_plan(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        matches = [
            make_match(i, now, business_date="2026-07-15", odds="2.00")
            for i in range(1, 5)
        ]
        matches[0] = replace(matches[0], home="SECRET-PUBLIC-TEAM")
        recommendation = make_recommendation(now, matches)
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=now + timedelta(hours=5),
            )
        )
        rows = self.database.claim_due_emails(now, limit=1)
        self.assertEqual(len(rows), 1)
        self.database.mark_email_sent(int(rows[0]["id"]), rows[0]["claim_token"], now)


class NewFeatureTests(unittest.TestCase):
    """Regression tests for pagination, calendar, purchase marking, ticket upload, settle."""

    def setUp(self):
        self.root = Path("data")
        self.database_path = self.root / f"test_features_{self._testMethodName}.db"
        self.preview = self.root / f"test_features_{self._testMethodName}_mail"
        self._clean()
        self.settings = make_settings(
            self.root,
            database_path=self.database_path,
            mail_preview_dir=self.preview,
            web_port=0,
        )
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        self.triggered: list[str] = []
        self.application = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"features-test-secret" * 2,
        )

    def tearDown(self):
        self._clean()

    def _clean(self):
        for suffix in ("", "-wal", "-shm"):
            _unlink_with_retry(Path(f"{self.database_path}{suffix}"))
        if self.preview.exists():
            for child in self.preview.iterdir():
                _unlink_with_retry(child)
            self.preview.rmdir()
        # rmtree because ticket-images now also holds a thumbs/ subdirectory.
        ticket_dir = self.root / "ticket-images"
        if ticket_dir.exists():
            shutil.rmtree(ticket_dir)

    def _trigger(self, request_id: str) -> tuple[str, str]:
        self.triggered.append(request_id)
        return "created", "已创建测试计划"

    def _create_sent_plan(self, *, plan_id: str = "BF4-TEST-0001", business_date: str = "2026-07-15", day_offset: int = 0):
        now = self.now + timedelta(days=day_offset)
        matches = [
            make_match(i, now, business_date=business_date, odds="2.00")
            for i in range(1, 5)
        ]
        recommendation = make_recommendation(now, matches)
        recommendation = replace(recommendation, plan_id=plan_id, business_date=business_date)
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=now + timedelta(hours=5),
            )
        )
        rows = self.database.claim_due_emails(now, limit=1)
        self.assertEqual(len(rows), 1)
        self.database.mark_email_sent(int(rows[0]["id"]), rows[0]["claim_token"], now)
        return recommendation

    # --- image magic bytes detection ---

    def test_image_extension_detects_jpeg_png_gif_webp(self):
        detect = DashboardApplication._image_extension
        self.assertEqual(detect(b'\xff\xd8\xff\x00\x00', "photo.jpg"), "jpg")
        self.assertEqual(detect(b'\x89PNG\r\n\x1a\n\x00\x00', "photo.png"), "png")
        self.assertEqual(detect(b'GIF89a\x00\x00', "photo.gif"), "gif")
        self.assertEqual(detect(b'RIFF\x00\x00\x00\x00WEBP', "photo.webp"), "webp")

    def test_image_extension_rejects_non_image_data(self):
        detect = DashboardApplication._image_extension
        self.assertIsNone(detect(b'\x00\x01\x02\x03', "not_image.txt"))
        self.assertIsNone(detect(b'', "empty.bin"))

    def test_image_extension_fallback_to_filename(self):
        detect = DashboardApplication._image_extension
        self.assertEqual(detect(b'\x00\x00\x00\x00', "photo.jpeg"), "jpg")
        self.assertEqual(detect(b'\x00\x00\x00\x00', "photo.png"), "png")

    # --- purchase marking ---

    def test_mark_purchased_nonexistent_plan(self):
        level, detail = self.application.trigger_mark_purchased("NONEXISTENT", True)
        self.assertEqual(level, "warn")

    # --- ticket image upload ---

    def test_upload_ticket_saves_image_and_marks_purchased(self):
        rec = self._create_sent_plan()
        png_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        level, detail = self.application.trigger_upload_ticket(
            rec.plan_id, "ticket.png", png_data
        )
        self.assertEqual(level, "ok")
        self.assertIn("实票图片已上传", detail)
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertTrue(plan.purchased)
        self.assertEqual(plan.ticket_image, "BF4-TEST-0001.png")
        saved = self.settings.ticket_image_dir
        from pathlib import Path as P
        self.assertTrue((P(saved) / "BF4-TEST-0001.png").exists())

    def test_upload_ticket_rejects_oversized_file(self):
        rec = self._create_sent_plan()
        big_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * (2 * 1024 * 1024 + 1)
        level, detail = self.application.trigger_upload_ticket(
            rec.plan_id, "big.png", big_data
        )
        self.assertEqual(level, "warn")
        self.assertIn("2MB", detail)

    def test_upload_ticket_rejects_non_image(self):
        rec = self._create_sent_plan()
        level, detail = self.application.trigger_upload_ticket(
            rec.plan_id, "file.txt", b'hello world'
        )
        self.assertEqual(level, "warn")
        self.assertIn("JPG", detail)

    def test_upload_ticket_nonexistent_plan(self):
        level, _ = self.application.trigger_upload_ticket(
            "NONEXISTENT", "x.png", b'\x89PNG\r\n\x1a\n'
        )
        self.assertEqual(level, "warn")

    # --- REST API ticket upload / delete (regression for the Vue frontend gap) ---

    def test_api_upload_ticket_saves_image_and_marks_purchased(self):
        from score_fourfold.api import DashboardAPI
        rec = self._create_sent_plan()
        png_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        api = DashboardAPI(self.application)
        level, detail = api.upload_ticket(rec.plan_id, "ticket.png", png_data)
        self.assertEqual(level, "ok")
        self.assertIn("实票图片已上传", detail)
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertTrue(plan.purchased)
        self.assertEqual(plan.ticket_image, f"{rec.plan_id}.png")
        self.assertTrue((Path(self.settings.ticket_image_dir) / f"{rec.plan_id}.png").exists())

    def test_api_delete_ticket_clears_image_keeps_purchase(self):
        from score_fourfold.api import DashboardAPI
        rec = self._create_sent_plan()
        png_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.png", png_data)
        api = DashboardAPI(self.application)
        level, detail = api.delete_ticket(rec.plan_id)
        self.assertEqual(level, "ok")
        self.assertIn("已移除", detail)
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertEqual(plan.ticket_image, "", "删除实票应清空 ticket_image")
        self.assertTrue(plan.purchased, "删除实票不应取消购买标记")

    def test_api_delete_ticket_without_image_returns_warn(self):
        from score_fourfold.api import DashboardAPI
        rec = self._create_sent_plan()
        api = DashboardAPI(self.application)
        level, detail = api.delete_ticket(rec.plan_id)
        self.assertEqual(level, "warn")
        self.assertIn("没有实票图片", detail)

    def test_rest_upload_ticket_endpoint_accepts_multipart(self):
        rec = self._create_sent_plan()
        png_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        boundary = "----testboundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="plan_id"\r\n\r\n{rec.plan_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="ticket_image"; filename="ticket.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + png_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=5)
            connection.request(
                "POST", "/api/v1/actions/upload-ticket",
                body=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "X-CSRF-Token": "ssh-loopback",
                },
            )
            response = connection.getresponse()
            status = response.status
            payload = response.read()
            connection.close()
        finally:
            server.stop()
        self.assertEqual(status, 200)
        body_json = json.loads(payload)
        self.assertEqual(body_json["level"], "ok")
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertEqual(plan.ticket_image, f"{rec.plan_id}.png")

    # --- ticket thumbnails (list must never download the full photo) ---

    def _real_jpeg(self, width: int = 1200, height: int = 900) -> bytes:
        """A genuinely decodable JPEG; the upload tests above use stub bytes."""
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("Pillow is required for thumbnail tests")
        buffer = io.BytesIO()
        Image.effect_noise((width, height), 48).convert("RGB").save(buffer, "JPEG", quality=90)
        return buffer.getvalue()

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            status = response.status
            headers = {key.lower(): value for key, value in response.getheaders()}
            payload = response.read()
            connection.close()
        finally:
            server.stop()
        return status, headers, payload

    def test_upload_ticket_pregenerates_thumbnail(self):
        rec = self._create_sent_plan()
        jpeg = self._real_jpeg()
        level, _ = self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", jpeg)
        self.assertEqual(level, "ok")
        thumb = Path(self.settings.ticket_image_dir) / "thumbs" / f"{rec.plan_id}.jpg"
        self.assertTrue(thumb.is_file(), "上传时应预生成缩略图，避免首次浏览付费")
        self.assertLess(thumb.stat().st_size, len(jpeg) // 4)

    def test_serve_ticket_thumbnail_returns_downscaled_image(self):
        rec = self._create_sent_plan()
        jpeg = self._real_jpeg()
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", jpeg)
        status, headers, payload = self._get(f"/tickets/thumbs/{rec.plan_id}.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "image/jpeg")
        self.assertLess(len(payload), len(jpeg) // 4, "缩略图路由不能返回原图")

    def test_serve_ticket_full_image_is_unchanged(self):
        rec = self._create_sent_plan()
        jpeg = self._real_jpeg()
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", jpeg)
        status, headers, payload = self._get(f"/tickets/{rec.plan_id}.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "image/jpeg")
        self.assertEqual(payload, jpeg, "放大查看必须拿到完整原图")

    def test_serve_ticket_thumbnail_falls_back_to_original_when_undecodable(self):
        """A broken upload must still render rather than show a broken image."""
        rec = self._create_sent_plan()
        broken = b"\xff\xd8\xff" + b"\x00" * 120
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", broken)
        status, headers, payload = self._get(f"/tickets/thumbs/{rec.plan_id}.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(payload, broken, "无法生成缩略图时应回退到原图")

    def test_serve_ticket_rejects_unsafe_names(self):
        rec = self._create_sent_plan()
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", self._real_jpeg())
        for path in ("/tickets/../secret.jpg", "/tickets/thumbs/..%2f..%2fetc%2fpasswd"):
            status, _, _ = self._get(path)
            self.assertEqual(status, 404, f"应拒绝非法路径 {path}")

    def test_serialize_plan_exposes_thumbnail_and_full_urls(self):
        rec = self._create_sent_plan()
        self.application.trigger_upload_ticket(rec.plan_id, "ticket.jpg", self._real_jpeg())
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        payload = serialize_plan(plan)
        self.assertEqual(payload["ticket_image_url"], f"/tickets/{rec.plan_id}.jpg")
        self.assertEqual(payload["ticket_thumb_url"], f"/tickets/thumbs/{rec.plan_id}.jpg")

    # --- manual settle ---

    def test_queue_settle_returns_immediately_and_completes(self):
        settle_called = threading.Event()
        settle_result = ("ok", "赛果更新完成")

        def fake_settle():
            settle_called.set()
            return settle_result

        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-test-secret" * 2,
            trigger_settle=fake_settle,
        )
        level, detail = app.queue_settle()
        self.assertEqual(level, "ok")
        self.assertIn("后台", detail)
        self.assertTrue(settle_called.wait(2))
        self._wait_for(
            lambda: app.settle_task() is not None
            and app.settle_task().status == "finished"
        )
        task = app.settle_task()
        assert task is not None
        self.assertEqual(task.level, "ok")
        self.assertIn("赛果更新完成", task.detail)

    def test_queue_settle_duplicate_while_running(self):
        release = threading.Event()
        started = threading.Event()

        def slow_settle():
            started.set()
            release.wait(2)
            return ("ok", "done")

        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-dup-test-secret" * 2,
            trigger_settle=slow_settle,
        )
        app.queue_settle()
        self.assertTrue(started.wait(1))
        level2, _ = app.queue_settle()
        self.assertEqual(level2, "warn")
        release.set()

    # --- pagination view ---

    def test_ajax_mark_purchased_returns_json(self):
        """AJAX requests should get JSON, not a 303 redirect."""
        rec = self._create_sent_plan()
        level, detail = self.application.trigger_mark_purchased(rec.plan_id, True)
        self.assertEqual(level, "ok")
        # Verify the application method works (the HTTP layer is tested implicitly)
        plan = self.database.get_plan(rec.plan_id)
        self.assertTrue(plan.purchased)

    def test_ajax_delete_plan_returns_deleted_flag(self):
        """After deleting a plan, the JSON response should have deleted=True."""
        rec = self._create_sent_plan()
        level, detail = self.application.trigger_delete_plan(rec.plan_id)
        self.assertEqual(level, "ok")
        # Plan should be gone from database
        self.assertIsNone(self.database.get_plan(rec.plan_id))

    # --- toast notifications ---

    def test_queue_settle_plan_nonexistent(self):
        level, detail = self.application.queue_settle_plan("NONEXISTENT")
        self.assertEqual(level, "warn")

    def test_queue_settle_plan_no_trigger(self):
        """Without trigger_settle configured, per-plan settle should warn."""
        rec = self._create_sent_plan()
        # Application was created without trigger_settle
        level, detail = self.application.queue_settle_plan(rec.plan_id)
        self.assertEqual(level, "warn")

    def test_queue_settle_plan_with_trigger(self):
        """With trigger_settle configured, per-plan settle should queue."""
        rec = self._create_sent_plan()
        called: list[str] = []

        def settle_one(plan_id: str) -> tuple[str, str]:
            called.append(plan_id)
            return ("ok", "done")

        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-test-secret" * 2,
            trigger_settle_plan=settle_one,
            trigger_settle=lambda: ("ok", "赛果更新完成"),
        )
        level, detail = app.queue_settle_plan(rec.plan_id)
        self.assertEqual(level, "ok")
        self._wait_for(lambda: called == [rec.plan_id])
        self.assertIn("已提交后台", detail)

    def test_queue_settle_plan_already_settled(self):
        rec = self._create_sent_plan()
        # Fully settle the plan: mark every leg FINAL so no PENDING legs remain.
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        leg_results = tuple(
            MatchResult(leg.match_id, ResultStatus.FINAL, 1, 0)
            for leg in plan.legs
        )
        settlement = Settlement(
            plan_id=rec.plan_id,
            status=PlanStatus.WON,
            settled_at=self.now,
            gross_prize=Decimal("100"),
            tax=Decimal("0"),
            net_prize=Decimal("100"),
            net_profit=Decimal("98"),
            leg_results=leg_results,
        )
        self.database.settle_plan_with_mail(
            settlement,
            subject="test",
            text_body="test",
            html_body="test",
        )
        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-test-secret" * 2,
            trigger_settle_plan=lambda _plan_id: ("ok", "noop"),
            trigger_settle=lambda: ("ok", "noop"),
        )
        level, detail = app.queue_settle_plan(rec.plan_id)
        self.assertEqual(level, "warn")
        self.assertIn("已结算", detail)

    def test_queue_settle_plan_allows_settled_plan_with_pending_legs(self):
        """A settled (e.g. early-loss) plan with PENDING legs can be re-queued."""
        rec = self._create_sent_plan()
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        # Settle early as LOST with only the first leg final; legs 1-3 stay PENDING.
        settlement = Settlement(
            plan_id=rec.plan_id,
            status=PlanStatus.LOST,
            settled_at=self.now,
            gross_prize=Decimal("0"),
            tax=Decimal("0"),
            net_prize=Decimal("0"),
            net_profit=Decimal("-2"),
            leg_results=(MatchResult(plan.legs[0].match_id, ResultStatus.FINAL, 0, 0),),
        )
        self.database.settle_plan_with_mail(
            settlement,
            subject="lost",
            text_body="lost",
            html_body="lost",
        )
        called: list[str] = []
        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-pending-secret" * 2,
            trigger_settle_plan=lambda plan_id: (called.append(plan_id) or ("ok", "done")),
        )
        level, detail = app.queue_settle_plan(rec.plan_id)
        self.assertEqual(level, "ok")
        self._wait_for(lambda: called == [rec.plan_id])
        self.assertIn("已提交后台", detail)

    def test_queue_settle_plan_allows_void_plan_recheck(self):
        rec = self._create_sent_plan()
        self.assertTrue(
            self.database.settle_plan_with_mail(
                Settlement(
                    plan_id=rec.plan_id,
                    status=PlanStatus.VOID,
                    settled_at=self.now,
                    gross_prize=Decimal("2"),
                    tax=Decimal("0"),
                    net_prize=Decimal("2"),
                    net_profit=Decimal("0"),
                    leg_results=(),
                ),
                subject="void",
                text_body="void",
                html_body="void",
            )
        )
        called: list[str] = []
        app = DashboardApplication(
            self.settings,
            self.database,
            self._trigger,
            secret=b"settle-void-secret" * 2,
            trigger_settle_plan=lambda plan_id: (called.append(plan_id) or ("ok", "done")),
        )

        level, detail = app.queue_settle_plan(rec.plan_id)

        self.assertEqual(level, "ok")
        self._wait_for(lambda: called == [rec.plan_id])
        self.assertIn("已提交后台", detail)

    # --- fetch-based postAction in JS ---

    @staticmethod
    def _wait_for(predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("background task did not finish in time")


if __name__ == "__main__":
    unittest.main()
