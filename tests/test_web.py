from __future__ import annotations

import http.client
import re
import threading
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
from score_fourfold.domain import MatchResult, ResultStatus
from score_fourfold.mail import Mailer, flush_outbox, render_recommendation, render_settlement
from score_fourfold.scheduler import slot_job_name
from score_fourfold.service import ScoreFourfoldService
from score_fourfold.web import DashboardApplication, DashboardServer

from .helpers import make_match, make_recommendation, make_settings


TZ = ZoneInfo("Asia/Shanghai")


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
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        if self.preview.exists():
            for child in self.preview.iterdir():
                child.unlink()
            self.preview.rmdir()

    def _trigger(self, request_id: str) -> tuple[str, str]:
        self.triggered.append(request_id)
        return "created", "已创建测试计划"

    def _create_plan(self, *, secret_team: str | None = None):
        matches = [
            make_match(i, self.now, business_date="2026-07-15", odds="2.00")
            for i in range(1, 5)
        ]
        if secret_team is not None:
            matches[0] = replace(matches[0], home=secret_team)
        recommendation = make_recommendation(self.now, matches)
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            self.database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=self.now + timedelta(hours=5),
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

    def test_previewed_plan_is_not_counted_and_hides_purchase_details(self):
        recommendation = self._create_plan(secret_team="PRIVATE-TEAM-NAME")
        clock = FixedClock(self.now)
        self.assertEqual(
            flush_outbox(self.database, Mailer(self.settings, clock=clock), self.now),
            (1, 0),
        )

        page = self.application.render()
        self.assertIn("仅生成本地预览，不计账", page)
        self.assertIn("购买内容已隐藏", page)
        self.assertNotIn("PRIVATE-TEAM-NAME", page)
        self.assertNotIn("SP 2.00", page)
        self.assertNotIn("<strong>1:0</strong>", page)
        stored = self.database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertEqual(stored.delivery_status, "previewed")
        self.assertEqual(self.database.summary()["plans_total"], 0)
        self.assertEqual(self.database.summary()["baseline_stake"], "0.00")

    def test_sent_plan_escapes_database_and_flash_content(self):
        attack = '<script>alert("database-xss")</script>'
        self._create_plan(secret_team=attack)
        self._mark_recommendation_sent()

        page = self.application.render(
            message='<img src=x onerror="alert(1)">',
            level="not-a-real-level",
        )
        self.assertNotIn(attack, page)
        self.assertIn("&lt;script&gt;alert(&quot;database-xss&quot;)&lt;/script&gt;", page)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertIn('class="flash warn"', page)

    def test_ai_summary_is_rendered_in_modal_with_per_match_ai_cells(self):
        recommendation = self._create_plan()
        self._mark_recommendation_sent()
        self.assertTrue(
            self.database.update_ai_summary(
                recommendation.plan_id,
                "这是一段已持久化的AI分析内容。",
            )
        )

        page = self.application.render(csrf_token="csrf-test-token")

        self.assertIn("这是一段已持久化的AI分析内容。", page)
        self.assertEqual(page.count('class="ai-cell"'), 4)
        self.assertNotIn("rowspan=", page)
        self.assertIn("查看总体分析", page)

    def test_dashboard_marks_recommendation_and_each_match_during_ai_work(self):
        self._create_plan()
        self._mark_recommendation_sent()

        page = self.application.render(csrf_token="csrf-test-token")

        self.assertIn('id="recommend-form"', page)
        self.assertIn("推荐生成中（含 AI 分析）", page)
        self.assertIn("本场比赛 AI 分析中，请稍候", page)
        self.assertIn("AI 正在联网分析，最长可能需要约 10 分钟", page)
        self.assertIn("target.disabled=true", page)

    def test_dashboard_renders_ai_replacements_and_manual_pick_editor(self):
        recommendation = self._create_plan()
        self._mark_recommendation_sent()
        plan = self.database.get_plan(recommendation.plan_id)
        assert plan is not None
        suggestions = [
            (
                leg.match_id,
                "s01s01" if index == 0 else leg.score_code,
                "这是逐场AI推荐理由",
            )
            for index, leg in enumerate(plan.legs)
        ]
        self.assertTrue(
            self.database.update_ai_analysis(
                plan.plan_id, "总体AI分析", suggestions
            )
        )

        page = self.application.render(csrf_token="csrf-test-token")

        self.assertIn("AI逐场推荐", page)
        self.assertEqual(page.count("AI建议："), 4)
        self.assertEqual(page.count("替换为此推荐"), 1)
        self.assertEqual(page.count("与当前推荐一致"), 3)
        self.assertEqual(page.count("手动修改推荐"), 4)
        self.assertIn("width:min(96vw,1680px)", page)

        first = plan.legs[0]
        level, _ = self.application.trigger_update_leg(
            plan.plan_id, first.match_id, "s01s01"
        )
        self.assertEqual(level, "ok")
        updated = self.database.get_plan(plan.plan_id)
        assert updated is not None
        self.assertEqual(updated.legs[0].score_label, "1:1")
        self.assertEqual(updated.combined_odds, Decimal("60"))

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

    def test_dashboard_distinguishes_quoted_and_actual_settlement_values(self):
        recommendation = self._create_plan()
        self._mark_recommendation_sent()
        results = {
            leg.match.match_id: MatchResult(
                leg.match.match_id,
                ResultStatus.FINAL,
                1,
                0,
            )
            for leg in recommendation.legs
        }
        results[recommendation.legs[0].match.match_id] = MatchResult(
            recommendation.legs[0].match.match_id,
            ResultStatus.VOID,
        )
        self.database.update_leg_results(recommendation.plan_id, results)
        plan = self.database.get_plan(recommendation.plan_id)
        assert plan is not None
        settlement = ScoreFourfoldService._build_settlement(plan, self.now + timedelta(days=1))
        assert settlement is not None
        subject, text_body, html_body = render_settlement(
            plan,
            settlement,
            self.database.summary(),
        )
        self.assertTrue(
            self.database.settle_plan_with_mail(
                settlement,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        )

        page = self.application.render()
        self.assertIn("2元理论税前返还", page)
        self.assertIn("32.00 元", page)
        self.assertIn("实际税后返还", page)
        self.assertIn("16.00 元", page)
        self.assertIn("本期净盈亏", page)
        self.assertIn("14.00 元", page)
        summary = self.database.summary()
        self.assertEqual(summary["baseline_return"], "16.00")
        self.assertEqual(summary["baseline_profit"], "14.00")

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

    def test_get_has_security_headers_and_action_is_post_only(self):
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, headers, payload = self._request(server, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["content-type"])
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertIn("default-src 'none'", headers["content-security-policy"])
            self.assertEqual(headers["x-frame-options"], "DENY")
            self.assertIn("比分串关个人看板", payload.decode("utf-8"))

            status, _, _ = self._request(server, "GET", "/actions/recommend")
            self.assertEqual(status, 404)
            self.assertEqual(self.triggered, [])
        finally:
            server.stop()

    def test_update_leg_route_is_post_only_and_recalculates_plan(self):
        recommendation = self._create_plan()
        self._mark_recommendation_sent()
        first = recommendation.legs[0]
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            status, _, _ = self._request(server, "GET", "/actions/update-leg")
            self.assertEqual(status, 404)
            body = urlencode(
                {
                    "plan_id": recommendation.plan_id,
                    "match_id": first.match.match_id,
                    "option_code": "s01s01",
                    "csrf_token": "",
                }
            ).encode()
            status, _, _ = self._request(
                server,
                "POST",
                "/actions/update-leg",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": f"127.0.0.1:{server.address[1]}",
                },
            )
            self.assertEqual(status, 303)
            updated = self.database.get_plan(recommendation.plan_id)
            assert updated is not None
            self.assertEqual(updated.legs[0].score_code, "s01s01")
            self.assertEqual(updated.combined_odds, Decimal("60"))
        finally:
            server.stop()

    def test_plan_changes_refresh_first_mail_then_queue_revision_after_send(self):
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

        level, detail = application.trigger_update_leg(
            recommendation.plan_id, first_leg.match.match_id, "s01s01"
        )
        self.assertEqual(level, "ok")
        self.assertIn("首封推荐邮件已同步更新", detail)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, status, next_attempt_at, text_body FROM email_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(
            datetime.fromisoformat(rows[0]["next_attempt_at"]),
            datetime(2026, 7, 15, 15, 0, tzinfo=TZ),
        )
        self.assertIn("1:1", rows[0]["text_body"])

        sent_at = datetime(2026, 7, 15, 15, 0, tzinfo=TZ)
        claimed = self.database.claim_due_emails(sent_at, limit=1)
        self.assertEqual(len(claimed), 1)
        self.database.mark_email_sent(
            int(claimed[0]["id"]), claimed[0]["claim_token"], sent_at
        )
        clock_value[0] = sent_at + timedelta(minutes=5)
        level, detail = application.trigger_update_leg(
            recommendation.plan_id, first_leg.match.match_id, "s01s00"
        )
        self.assertEqual(level, "ok")
        self.assertIn("最新版推荐邮件已重新排队", detail)
        with self.database.connect() as connection:
            update = connection.execute(
                """
                SELECT kind, status, subject FROM email_outbox
                WHERE kind = 'recommendation-update'
                """
            ).fetchone()
        self.assertIsNotNone(update)
        self.assertEqual(update["status"], "pending")
        self.assertTrue(update["subject"].startswith("[更新]"))

        level, _ = application.trigger_update_leg(
            recommendation.plan_id, first_leg.match.match_id, "s01s01"
        )
        self.assertEqual(level, "ok")
        with self.database.connect() as connection:
            update_count = connection.execute(
                "SELECT COUNT(*) AS count FROM email_outbox WHERE kind = 'recommendation-update'"
            ).fetchone()["count"]
        self.assertEqual(update_count, 1)
        self.assertEqual(len(wake_calls), 3)

    def test_post_requires_loopback_host_same_origin_and_valid_signature(self):
        server = DashboardServer(self.settings, self.application)
        server.start()
        try:
            request_id, signature = self.application.new_request()
            body = urlencode({"request_id": request_id, "signature": signature}).encode()
            base_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": f"127.0.0.1:{server.address[1]}",
            }

            status, _, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=body,
                headers={**base_headers, "Host": "dashboard.example.com"},
            )
            self.assertEqual(status, 403)
            status, _, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=body,
                headers={**base_headers, "Origin": "https://attacker.example"},
            )
            self.assertEqual(status, 403)
            invalid = urlencode({"request_id": request_id, "signature": "0" * 64}).encode()
            status, _, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=invalid,
                headers=base_headers,
            )
            self.assertEqual(status, 403)
            self.assertEqual(self.triggered, [])

            status, _, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=b"\xff",
                headers=base_headers,
            )
            self.assertEqual(status, 400)
            self.assertEqual(self.triggered, [])

            status, headers, _ = self._request(
                server,
                "POST",
                "/actions/recommend",
                body=body,
                headers={**base_headers, "Origin": f"http://127.0.0.1:{server.address[1]}"},
            )
            self.assertEqual(status, 303)
            self.assertTrue(headers["location"].startswith("/?message="))
            self.assertEqual(self.triggered, [request_id])
        finally:
            server.stop()


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
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        if self.preview.exists():
            for child in self.preview.iterdir():
                child.unlink()
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
        wake = threading.Event()
        trigger = _build_manual_trigger(service, wake)
        request_id = "manual-request-20260715-0001"

        first = trigger(request_id)
        second = trigger(request_id)

        self.assertEqual(first[0], "created")
        self.assertEqual(second, first)
        self.assertEqual(provider.match_calls, 1)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date("2026-07-15"),
            3,
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

        self.assertEqual(sum(created), 3)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date("2026-07-15"),
            3,
        )
        self.assertEqual(self.database.summary()["emails_pending"], 3)


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
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        if self.preview.exists():
            for child in self.preview.iterdir():
                child.unlink()
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

    def test_unauthenticated_home_redirects_and_recommend_is_blocked(self):
        self._create_sensitive_plan()
        status, headers, payload = self._request("GET", "/", headers=self._public_headers())
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertNotIn(b"SECRET-PUBLIC-TEAM", payload)

        request_id, signature = self.application.new_request()
        body = urlencode(
            {
                "request_id": request_id,
                "signature": signature,
                "csrf_token": "forged",
            }
        ).encode()
        status, headers, _ = self._request(
            "POST",
            "/actions/recommend",
            body=body,
            headers=self._public_headers(
                **{"Content-Type": "application/x-www-form-urlencoded"}
            ),
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertEqual(self.triggered, [])

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

    def test_forged_cookie_is_ignored_and_successful_login_rotates_token(self):
        forged = f"__Host-score_session={'A' * 48}"
        status, headers, _ = self._login(cookie=forged)
        self.assertEqual(status, 303)
        new_cookie = self._cookie_from(headers)
        self.assertNotEqual(new_cookie, forged)
        status, page_headers, payload = self._request(
            "GET",
            "/",
            headers=self._public_headers(Cookie=new_cookie),
        )
        self.assertEqual(status, 200)
        self.assertIn("退出登录", payload.decode("utf-8"))
        page = payload.decode("utf-8")
        nonce = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', page)
        self.assertIsNotNone(nonce)
        assert nonce is not None
        self.assertIn(
            f"script-src 'nonce-{nonce.group(1)}'",
            page_headers["content-security-policy"],
        )
        self.assertNotIn('href="javascript:', page)

    def test_logout_requires_csrf_and_invalidates_old_cookie(self):
        status, headers, _ = self._login()
        cookie = self._cookie_from(headers)
        token = cookie.split("=", 1)[1]
        session = self.application.get_session(token)
        assert session is not None

        body = urlencode({"csrf_token": "wrong-csrf"}).encode()
        status, _, _ = self._request(
            "POST",
            "/logout",
            body=body,
            headers=self._public_headers(
                **{
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                }
            ),
        )
        self.assertEqual(status, 403)
        self.assertIsNotNone(self.application.get_session(token))

        body = urlencode({"csrf_token": session.csrf_token}).encode()
        status, headers, _ = self._request(
            "POST",
            "/logout",
            body=body,
            headers=self._public_headers(
                **{
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                }
            ),
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assertIsNone(self.application.get_session(token))
        status, headers, _ = self._request(
            "GET",
            "/",
            headers=self._public_headers(Cookie=cookie),
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")

    def test_recommend_requires_session_csrf_host_and_origin(self):
        status, headers, home = self._login()
        cookie = self._cookie_from(headers)
        token = cookie.split("=", 1)[1]
        session = self.application.get_session(token)
        assert session is not None
        page = home.decode("utf-8") if home else ""
        if "csrf_token" not in page:
            status, _, payload = self._request(
                "GET",
                "/",
                headers=self._public_headers(Cookie=cookie),
            )
            self.assertEqual(status, 200)
            page = payload.decode("utf-8")
        request_id, signature = self.application.new_request()
        other_token, other_session = self.application.create_session()

        valid_body = urlencode(
            {
                "request_id": request_id,
                "signature": signature,
                "csrf_token": session.csrf_token,
            }
        ).encode()
        cases = [
            (
                urlencode(
                    {
                        "request_id": request_id,
                        "signature": signature,
                        "csrf_token": other_session.csrf_token,
                    }
                ).encode(),
                self._public_headers(
                    **{
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": cookie,
                    }
                ),
                403,
            ),
            (
                valid_body,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": PUBLIC_IP,
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "198.51.100.20",
                    "Cookie": cookie,
                },
                403,
            ),
            (
                valid_body,
                self._public_headers(
                    **{
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": cookie,
                        "Origin": "https://evil.example",
                    }
                ),
                403,
            ),
            (
                valid_body,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": f"{PUBLIC_IP},{PUBLIC_IP}",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "198.51.100.20",
                    "Origin": PUBLIC_ORIGIN,
                    "Cookie": cookie,
                },
                403,
            ),
            (
                valid_body,
                self._public_headers(
                    **{
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": cookie,
                        "X-Forwarded-For": "198.51.100.20, 198.51.100.21",
                    }
                ),
                403,
            ),
            (
                valid_body,
                self._public_headers(
                    **{
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": cookie,
                        "X-Forwarded-Proto": "http",
                    }
                ),
                403,
            ),
        ]
        for body, headers, expected in cases:
            with self.subTest(expected=expected, headers=headers):
                before = list(self.triggered)
                status, _, _ = self._request(
                    "POST",
                    "/actions/recommend",
                    body=body,
                    headers=headers,
                )
                self.assertEqual(status, expected)
                self.assertEqual(self.triggered, before)

        # Referer with a path should still be accepted when Origin is absent.
        no_origin = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": PUBLIC_IP,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "198.51.100.20",
            "Referer": f"{PUBLIC_ORIGIN}/",
            "Cookie": cookie,
        }
        status, headers, _ = self._request(
            "POST",
            "/actions/recommend",
            body=valid_body,
            headers=no_origin,
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.triggered, [request_id])
        _ = other_token

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


if __name__ == "__main__":
    unittest.main()
