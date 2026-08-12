from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from score_fourfold.config import Settings
from score_fourfold.database import Database
from score_fourfold.domain import MatchResult, PlanStatus, ResultStatus, Settlement
from score_fourfold.mail import Mailer, render_recommendation, render_settlement
from score_fourfold.service import ScoreFourfoldService

from .helpers import make_match, make_recommendation, make_settings


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path("data")
        self.database_path = self.tmp_path / f"test_settlement_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.matches = [make_match(i, self.now, odds="2.00") for i in range(1, 5)]
        self.recommendation = make_recommendation(self.now, self.matches)
        subject, text_body, html_body = render_recommendation(self.recommendation)
        self.database.create_plan_with_mail(
            self.recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=self.now + timedelta(hours=5),
        )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)

    def _refresh_after(self, results):
        self.database.update_leg_results(self.recommendation.plan_id, {r.match_id: r for r in results})
        plan = self.database.get_plan(self.recommendation.plan_id)
        assert plan is not None
        return ScoreFourfoldService._build_settlement(plan, self.now)

    def test_all_four_hit(self):
        settlement = self._refresh_after(
            [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        )
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.WON)
        self.assertEqual(settlement.gross_prize, Decimal("32.00"))
        self.assertEqual(settlement.net_profit, Decimal("30.00"))

    def test_one_loss_loses_ticket(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        results[2] = MatchResult(self.matches[2].match_id, ResultStatus.FINAL, 0, 0)
        settlement = self._refresh_after(results)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.LOST)
        self.assertEqual(settlement.net_profit, Decimal("-2.00"))

    def test_one_void_recalculates_as_threefold(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches]
        results[0] = MatchResult(self.matches[0].match_id, ResultStatus.VOID)
        settlement = self._refresh_after(results)
        assert settlement is not None
        self.assertEqual(settlement.status, PlanStatus.WON)
        self.assertEqual(settlement.gross_prize, Decimal("16.00"))
        plan = self.database.get_plan(self.recommendation.plan_id)
        assert plan is not None
        _, text_body, _ = render_settlement(plan, settlement, self.database.summary())
        self.assertIn("按3串1重算", text_body)
        self.assertIn("模拟净收益", text_body)

    def test_pending_leg_prevents_settlement(self):
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches[:3]]
        self.assertIsNone(self._refresh_after(results))


class _FakeResultProvider:
    def __init__(self, results: dict[str, MatchResult]):
        self.results = results

    def get_results(self, start_date, end_date):
        return self.results

    def get_matches(self):
        return []


class DelayedSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path("data")
        self.database_path = self.tmp_path / f"test_settlement_delay_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)
        self.database = Database(self.database_path)
        self.database.initialize()
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.matches = [make_match(i, self.now, odds="2.00") for i in range(1, 5)]
        self.recommendation = make_recommendation(self.now, self.matches)
        subject, text_body, html_body = render_recommendation(self.recommendation)
        self.database.create_plan_with_mail(
            self.recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=self.now + timedelta(hours=5),
        )
        # Settlement only processes plans whose recommendation mail has been sent.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE plans SET delivery_status = 'sent' WHERE plan_id = ?",
                (self.recommendation.plan_id,),
            )
        settings = make_settings(
            self.tmp_path,
            database_path=self.database_path,
            result_check_delay_minutes=0,
            mail_preview_dir=self.tmp_path / "mail-delay",
        )
        self.settings = settings
        self.mailer = Mailer(settings)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            (Path(f"{self.database_path}{suffix}")).unlink(missing_ok=True)

    def _service(self, provider):
        return ScoreFourfoldService(self.settings, self.database, provider, self.mailer)

    def test_settle_plan_updates_only_requested_plan(self):
        second_now = self.now + timedelta(days=1)
        second_matches = [
            make_match(
                i,
                second_now,
                business_date="2026-07-15",
                odds="2.00",
            )
            for i in range(11, 15)
        ]
        second = replace(
            make_recommendation(second_now, second_matches),
            plan_id="BF4-TEST-SECOND",
        )
        subject, text_body, html_body = render_recommendation(second)
        self.assertTrue(
            self.database.create_plan_with_mail(
                second,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=second_now + timedelta(hours=5),
            )
        )
        all_matches = [*self.matches, *second_matches]
        provider = _FakeResultProvider(
            {
                match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
                for match in all_matches
            }
        )

        settle_at = max(match.start_at for match in second_matches) + timedelta(hours=1)
        outcome = self._service(provider).settle_plan(
            self.recommendation.plan_id,
            settle_at,
        )

        self.assertEqual(outcome.status, "ok")
        first_plan = self.database.get_plan(self.recommendation.plan_id)
        second_plan = self.database.get_plan(second.plan_id)
        assert first_plan is not None and second_plan is not None
        self.assertEqual(first_plan.status, PlanStatus.WON)
        self.assertEqual(second_plan.status, PlanStatus.PENDING)
        self.assertTrue(
            all(leg.result_status is ResultStatus.PENDING for leg in second_plan.legs)
        )

    def test_settle_plan_uses_official_detail_fallback_for_missing_results(self):
        provider = _FakeResultProvider({})

        def page_result(leg):
            return MatchResult(
                leg.match_id,
                ResultStatus.FINAL,
                1,
                0,
                official_status="official-page:final",
            )

        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        with patch(
            "score_fourfold.service.SportteryPageResultProvider.get_result_for_leg",
            side_effect=page_result,
        ) as fallback:
            outcome = self._service(provider).settle_plan(
                self.recommendation.plan_id,
                settle_at,
            )

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(fallback.call_count, len(self.matches))
        self.assertIn("官网详情兜底 4 场", outcome.detail)

    def test_void_plan_can_be_refreshed_and_corrected(self):
        void_results = tuple(
            MatchResult(match.match_id, ResultStatus.VOID, official_status="invalid")
            for match in self.matches
        )
        self.assertTrue(
            self.database.settle_plan_with_mail(
                Settlement(
                    plan_id=self.recommendation.plan_id,
                    status=PlanStatus.VOID,
                    settled_at=self.now,
                    gross_prize=Decimal("2.00"),
                    tax=Decimal("0.00"),
                    net_prize=Decimal("2.00"),
                    net_profit=Decimal("0.00"),
                    leg_results=void_results,
                ),
                subject="void",
                text_body="void",
                html_body="void",
            )
        )
        provider = _FakeResultProvider(
            {
                match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
                for match in self.matches
            }
        )

        outcome = self._service(provider).settle_plan(
            self.recommendation.plan_id,
            max(match.start_at for match in self.matches) + timedelta(hours=1),
        )

        self.assertEqual(outcome.status, "ok")
        corrected = self.database.get_plan(self.recommendation.plan_id)
        assert corrected is not None
        self.assertEqual(corrected.status, PlanStatus.WON)
        self.assertTrue(all(leg.result_status is ResultStatus.FINAL for leg in corrected.legs))

    def test_postponed_match_stays_pending_within_one_day(self):
        """A delayed match stays pending — the ticket is still valid."""
        match_id = self.matches[0].match_id
        provider = _FakeResultProvider(
            {
                match_id: MatchResult(
                    match_id,
                    ResultStatus.PENDING,
                    official_status="比赛推迟",
                ),
            }
        )
        # Settle just after the match start but well within 24 hours.
        settle_at = self.matches[0].start_at + timedelta(hours=2)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("更新1条赛果", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        leg = plan.legs[0]
        self.assertEqual(leg.result_status, ResultStatus.PENDING)
        self.assertIn("推迟", leg.official_status)

    def test_postponed_match_stays_pending_after_one_day(self):
        """A delayed match is NOT voided after 24h — the ticket remains valid."""
        match_id = self.matches[0].match_id
        other_results = {
            match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
            for match in self.matches[1:]
        }
        other_results[match_id] = MatchResult(
            match_id,
            ResultStatus.PENDING,
            official_status="比赛推迟",
        )
        provider = _FakeResultProvider(other_results)
        # Wait until every leg in the plan is past the one-day window.
        settle_at = max(match.start_at for match in self.matches) + timedelta(days=1, minutes=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成0张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        leg = plan.legs[0]
        self.assertEqual(leg.result_status, ResultStatus.PENDING)

    def test_missing_match_stays_pending_after_one_day(self):
        """A match missing from the results feed is NOT voided — keep waiting."""
        provider = _FakeResultProvider({})
        settle_at = max(match.start_at for match in self.matches) + timedelta(days=1, minutes=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成0张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        for leg in plan.legs:
            self.assertEqual(leg.result_status, ResultStatus.PENDING)

    def test_final_match_settled_after_one_day(self):
        """A normal final result is settled and not treated as delayed."""
        provider = _FakeResultProvider(
            {
                match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
                for match in self.matches
            }
        )
        settle_at = self.matches[0].start_at + timedelta(days=1, minutes=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)

    def test_team_name_fallback_settles_when_match_id_format_differs(self):
        """When the data source changes match_id format, settle by team name."""
        # Results use completely different match_ids but same team names.
        results: dict[str, MatchResult] = {}
        for match in self.matches:
            new_id = f"NEW-{match.match_id}"
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=match.home,
                away_team=match.away,
            )
        provider = _FakeResultProvider(results)
        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)
        # Verify match_ids were migrated to the new format.
        for leg in plan.legs:
            self.assertTrue(leg.match_id.startswith("NEW-"))
        # Verify the team-name fallback was logged.
        with self.database.connect() as conn:
            logs = conn.execute(
                "SELECT message FROM activity_logs WHERE message LIKE '%队名兜底%'"
            ).fetchall()
        self.assertTrue(len(logs) > 0)

    def test_partial_team_name_fallback_with_match_num(self):
        """Match by one team name + match_num when transliterations differ."""
        results: dict[str, MatchResult] = {}
        for i, match in enumerate(self.matches):
            new_id = f"NEW-{match.match_id}"
            # Alter the home team name for one leg (like 沙巴巴库 vs 萨巴赫).
            home = f"不同队名{match.home}" if i == 1 else match.home
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=home,
                away_team=match.away,
                match_num=match.match_num,
            )
        provider = _FakeResultProvider(results)
        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)

    def test_settle_plan_team_name_fallback(self):
        """settle_plan also falls back to team-name matching."""
        results: dict[str, MatchResult] = {}
        for match in self.matches:
            new_id = f"NEW-{match.match_id}"
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=match.home,
                away_team=match.away,
            )
        provider = _FakeResultProvider(results)
        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        # Mock page provider to avoid real network calls; return None so team-name fallback kicks in.
        with patch(
            "score_fourfold.service.SportteryPageResultProvider.get_result_for_leg",
            return_value=None,
        ):
            outcome = self._service(provider).settle_plan(
                self.recommendation.plan_id,
                settle_at,
            )
        self.assertEqual(outcome.status, "ok")
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)

    def test_incremental_leg_update_when_plan_not_due(self):
        """Plans not yet due for full settlement still get individual leg
        results updated incrementally each settlement cycle."""
        provider = _FakeResultProvider(
            {
                match.match_id: MatchResult(
                    match.match_id, ResultStatus.FINAL, 1, 0,
                )
                for match in self.matches
            }
        )
        self.settings = replace(self.settings, result_check_delay_minutes=150)
        # Settle at a time *before* the plan becomes due.
        # max(start_at) = now + 3.4 h; delay = 2.5 h → due at now + 5.9 h.
        # settle_at = now + 4 h is after matches finish but before the
        # delay expires, so the plan is NOT due.
        settle_at = self.now + timedelta(hours=4)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("更新4条赛果", outcome.detail)
        self.assertIn("完成0张计划结算", outcome.detail)
        self.assertIn("尚未到整体结算时间", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        # Individual leg results should have been written.
        for leg in plan.legs:
            self.assertEqual(leg.result_status, ResultStatus.FINAL,
                             f"leg {leg.position} should be FINAL")
            self.assertEqual(leg.result_home, 1)
            self.assertEqual(leg.result_away, 0)


if __name__ == "__main__":
    unittest.main()
