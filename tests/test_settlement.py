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
        """When some legs are still PENDING and no loss detected, don't settle."""
        results = [MatchResult(match.match_id, ResultStatus.FINAL, 1, 0) for match in self.matches[:3]]
        self.assertIsNone(self._refresh_after(results))

    def test_early_loss_settles_with_some_legs_still_pending(self):
        """When any leg already missed the prediction the whole ticket is lost,
        even if other legs are still PENDING."""
        # Leg 0 predicted 1:0, actual 0:0 → loss
        results = [
            MatchResult(self.matches[0].match_id, ResultStatus.FINAL, 0, 0),
        ]
        settlement = self._refresh_after(results)
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.status, PlanStatus.LOST)
        self.assertEqual(settlement.net_profit, Decimal("-2.00"))

    def test_early_loss_from_any_position_settles_immediately(self):
        """Loss on a middle leg (not the first) also triggers immediate LOST."""
        results = [
            MatchResult(self.matches[1].match_id, ResultStatus.FINAL, 0, 0),
        ]
        settlement = self._refresh_after(results)
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.status, PlanStatus.LOST)

    def test_no_early_loss_when_final_legs_all_hit_and_others_pending(self):
        """If every FINAL leg is a hit, wait for remaining PENDING legs."""
        results = [
            MatchResult(self.matches[0].match_id, ResultStatus.FINAL, 1, 0),  # hit
            MatchResult(self.matches[2].match_id, ResultStatus.FINAL, 1, 0),  # hit
        ]
        plan = self._refresh_after(results)
        # Legs 1 and 3 still PENDING, no loss detected → wait
        self.assertIsNone(plan)


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

    def test_colliding_match_id_from_other_fixture_is_ignored(self):
        """A result whose match_id collides with a *different* fixture must be rejected.

        The odds API and results API maintain independent match_id spaces, so
        the same stored match_id can resolve to a different match at settlement
        time (e.g. 周六016 vs 周日016 of another sales period).  Settlement
        must not consume that result.
        """
        victim = self.matches[0]
        # The stored plan leg refers to 主队1 vs 客队1, but the results feed
        # returns an unrelated fixture reusing the same match_id.
        results = {
            match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
            for match in self.matches[1:]
        }
        results[victim.match_id] = MatchResult(
            victim.match_id,
            ResultStatus.FINAL,
            3,
            0,
            home_team="完全无关主队",
            away_team="完全无关客队",
            match_num="周三888",
        )
        provider = _FakeResultProvider(results)
        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成0张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        # The colliding leg stays pending; the other legs are updated.
        collided = next(leg for leg in plan.legs if leg.match_id == victim.match_id)
        self.assertEqual(collided.result_status, ResultStatus.PENDING)
        self.assertIsNone(collided.result_home)

    def test_colliding_match_id_ignored_in_settle_plan(self):
        """settle_plan rejects a colliding match_id the same way."""
        victim = self.matches[0]
        results = {
            match.match_id: MatchResult(match.match_id, ResultStatus.FINAL, 1, 0)
            for match in self.matches[1:]
        }
        results[victim.match_id] = MatchResult(
            victim.match_id,
            ResultStatus.FINAL,
            0,
            0,
            home_team="无关主队A",
            away_team="无关客队B",
            match_num="周六999",
        )
        provider = _FakeResultProvider(results)
        settle_at = max(match.start_at for match in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle_plan(self.recommendation.plan_id, settle_at)
        # Three legitimate legs are updated; the colliding leg stays pending and
        # the plan remains pending until the real fixture finishes.
        self.assertEqual(outcome.status, "partial")
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        collided = next(leg for leg in plan.legs if leg.match_id == victim.match_id)
        self.assertEqual(collided.result_status, ResultStatus.PENDING)
        self.assertIsNone(collided.result_home)

    def test_match_num_digits_only_still_accepted_when_teams_absent(self):
        """Results without team names fall back to match-number validation."""
        results = {
            match.match_id: MatchResult(
                match.match_id,
                ResultStatus.FINAL,
                1,
                0,
                match_num=match.match_num,
            )
            for match in self.matches
        }
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

    def test_settled_plans_pending_legs_get_updated_when_results_come(self):
        """After a plan is settled early as LOST, remaining PENDING legs
        should still be filled in when results become available later."""
        settle_at = self.matches[-1].start_at + timedelta(hours=1)
        # First cycle: only leg 0 has a loss, triggering early LOST settlement.
        provider1 = _FakeResultProvider(
            {self.matches[0].match_id: MatchResult(self.matches[0].match_id, ResultStatus.FINAL, 0, 0)}
        )
        outcome1 = self._service(provider1).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome1.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.LOST)
        # Legs 1-3 should still be PENDING.
        self.assertEqual(plan.legs[0].result_status, ResultStatus.FINAL)
        for i in (1, 2, 3):
            self.assertEqual(plan.legs[i].result_status, ResultStatus.PENDING)

        # Second cycle: results for remaining legs appear.
        provider2 = _FakeResultProvider(
            {
                self.matches[i].match_id: MatchResult(self.matches[i].match_id, ResultStatus.FINAL, 1, 0)
                for i in (1, 2, 3)
            }
        )
        outcome2 = self._service(provider2).settle(settle_at + timedelta(minutes=5))
        self.assertIn("更新3条赛果", outcome2.detail)
        self.assertIn("完成0张计划结算", outcome2.detail)
        # All legs should now have results even though the plan was already settled.
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        for leg in plan.legs:
            self.assertEqual(leg.result_status, ResultStatus.FINAL)

    def test_backfill_includes_settled_plan_with_earlier_business_date(self):
        """A settled plan whose business_date predates the newest pending
        plan's first kickoff must still get its pending legs backfilled."""
        # Settle the original plan early as LOST, leaving legs 1-3 pending.
        provider1 = _FakeResultProvider(
            {self.matches[0].match_id: MatchResult(self.matches[0].match_id, ResultStatus.FINAL, 0, 0)}
        )
        settle_at = max(m.start_at for m in self.matches) + timedelta(hours=1)
        outcome1 = self._service(provider1).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome1.detail)
        plan_a = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan_a)
        self.assertEqual(plan_a.status, PlanStatus.LOST)

        # A newer pending plan whose legs kick off the next day, so its first
        # kickoff date (7/15) is after the settled plan's business_date (7/14).
        later = self.now + timedelta(days=1)
        later_matches = [
            make_match(i, later, business_date="2026-07-15", odds="2.00")
            for i in range(11, 15)
        ]
        second = replace(make_recommendation(later, later_matches), plan_id="BF4-TEST-LATER")
        subject, text_body, html_body = render_recommendation(second)
        self.assertTrue(
            self.database.create_plan_with_mail(
                second,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=later + timedelta(hours=5),
            )
        )
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE plans SET delivery_status = 'sent' WHERE plan_id = ?",
                (second.plan_id,),
            )

        results = {
            m.match_id: MatchResult(m.match_id, ResultStatus.FINAL, 1, 0)
            for m in self.matches[1:]
        }
        results.update(
            {
                m.match_id: MatchResult(m.match_id, ResultStatus.FINAL, 1, 0)
                for m in later_matches
            }
        )
        provider2 = _FakeResultProvider(results)
        settle_at2 = max(m.start_at for m in later_matches) + timedelta(hours=1)
        self._service(provider2).settle(settle_at2)

        # The settled plan's remaining legs must now be filled in.
        plan_a = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan_a)
        for leg in plan_a.legs[1:]:
            self.assertEqual(leg.result_status, ResultStatus.FINAL, f"leg {leg.position}")

    def test_stale_match_id_reconciled_by_match_num_and_date(self):
        """A stale match_id with matching team names is reconciled via the
        official match number + kickoff date (e.g. 001 + 8/13 vs 周三001)."""
        results: dict[str, MatchResult] = {}
        for match in self.matches:
            new_id = f"NEW-{match.match_id}"
            # Same team names so the match number + date can safely match.
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=match.home,
                away_team=match.away,
                match_num=match.match_num,
                match_date=match.start_at.date().isoformat(),
            )
        provider = _FakeResultProvider(results)
        settle_at = max(m.start_at for m in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)
        for leg in plan.legs:
            self.assertTrue(leg.match_id.startswith("NEW-"))

    def test_stale_match_id_with_unrelated_teams_is_not_reconciled(self):
        """A stale match_id whose result has unrelated team names must NOT be
        reconciled by match number + date — that is the collision case where
        the odds feed and results feed reused the same match_id for different
        fixtures (周六016 vs 周日016)."""
        results: dict[str, MatchResult] = {}
        for match in self.matches:
            new_id = f"NEW-{match.match_id}"
            # Unrelated names: only number + date would match, which is unsafe.
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=f"甲队{match.match_id}",
                away_team=f"乙队{match.match_id}",
                match_num=match.match_num,
                match_date=match.start_at.date().isoformat(),
            )
        provider = _FakeResultProvider(results)
        settle_at = max(m.start_at for m in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成0张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        for leg in plan.legs:
            self.assertEqual(leg.result_status, ResultStatus.PENDING)
            self.assertFalse(leg.match_id.startswith("NEW-"))

    def test_settle_plan_reconciles_stale_match_id_by_num_and_date(self):
        """settle_plan also reconciles a stale match_id via match number + date."""
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
                match_num=match.match_num,
                match_date=match.start_at.date().isoformat(),
            )
        provider = _FakeResultProvider(results)
        settle_at = max(m.start_at for m in self.matches) + timedelta(hours=1)
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
        for leg in plan.legs:
            self.assertTrue(leg.match_id.startswith("NEW-"))

    def test_team_name_containment_match_when_abbreviated(self):
        """Abbreviated stored names match the feed's full names by containment
        (e.g. 巴黎圣曼 vs 巴黎圣日尔曼, 维拉 vs 阿斯顿维拉)."""
        results: dict[str, MatchResult] = {}
        for match in self.matches:
            new_id = f"NEW-{match.match_id}"
            # Full names contain the stored short names; no match_date, so the
            # containment team-name path is the only one that can match.
            results[new_id] = MatchResult(
                new_id,
                ResultStatus.FINAL,
                1,
                0,
                home_team=f"全{match.home}队",
                away_team=f"全{match.away}队",
                match_num=match.match_num,
            )
        provider = _FakeResultProvider(results)
        settle_at = max(m.start_at for m in self.matches) + timedelta(hours=1)
        outcome = self._service(provider).settle(settle_at)
        self.assertIn("完成1张计划结算", outcome.detail)
        plan = self.database.get_plan(self.recommendation.plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.WON)
        for leg in plan.legs:
            self.assertTrue(leg.match_id.startswith("NEW-"))


if __name__ == "__main__":
    unittest.main()
