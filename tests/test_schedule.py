from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.mail import Mailer, flush_outbox, render_recommendation
from score_fourfold.scheduler import due_recommendation_slot, slot_job_name
from score_fourfold.service import ScoreFourfoldService

from .helpers import make_match, make_recommendation, make_settings


TZ = ZoneInfo("Asia/Shanghai")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeProvider:
    def __init__(self, matches, after_fetch=None):
        self.matches = matches
        self.after_fetch = after_fetch
        self.match_calls = 0

    def get_matches(self):
        self.match_calls += 1
        if self.after_fetch is not None:
            self.after_fetch()
        return self.matches

    def get_results(self, *_):
        return {}


class ScheduleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("data") / f"test_schedule_{self._testMethodName}.db"
        self.preview = Path("data") / f"test_schedule_{self._testMethodName}_mail"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        if self.preview.exists():
            for child in self.preview.iterdir():
                child.unlink()
            self.preview.rmdir()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        if self.preview.exists():
            for child in self.preview.iterdir():
                child.unlink()
            self.preview.rmdir()

    def _service(self, clock: MutableClock, provider: FakeProvider):
        settings = make_settings(
            Path("data"), database_path=self.path, mail_preview_dir=self.preview
        )
        database = Database(self.path)
        database.initialize()
        mailer = Mailer(settings, clock=clock)
        return ScoreFourfoldService(
            settings, database, provider, mailer, clock=clock
        )

    def test_fixed_slots_are_restart_safe_and_stop_at_latest_start(self):
        slots = (time(10), time(14), time(17, 30))
        completed: set[str] = set()
        has_run = completed.__contains__
        at_0959 = datetime(2026, 7, 15, 9, 59, tzinfo=TZ)
        self.assertIsNone(due_recommendation_slot(at_0959, slots, time(17, 45), has_run))

        at_1000 = datetime(2026, 7, 15, 10, 0, tzinfo=TZ)
        self.assertEqual(due_recommendation_slot(at_1000, slots, time(17, 45), has_run), time(10))
        completed.add(slot_job_name(at_1000, time(10)))
        self.assertIsNone(due_recommendation_slot(at_1000, slots, time(17, 45), has_run))

        at_1400 = datetime(2026, 7, 15, 14, 0, tzinfo=TZ)
        completed.add(slot_job_name(at_1400, time(14)))
        at_1600 = datetime(2026, 7, 15, 16, 0, tzinfo=TZ)
        # Once the newest due slot is recorded, a restart must not walk back
        # through older missed slots and cause extra provider requests.
        self.assertIsNone(due_recommendation_slot(at_1600, slots, time(17, 45), has_run))

        at_1730 = datetime(2026, 7, 15, 17, 30, tzinfo=TZ)
        self.assertEqual(due_recommendation_slot(at_1730, slots, time(17, 45), has_run), time(17, 30))
        at_1745 = datetime(2026, 7, 15, 17, 45, tzinfo=TZ)
        self.assertIsNone(due_recommendation_slot(at_1745, slots, time(17, 45), has_run))
        at_1801 = datetime(2026, 7, 15, 18, 1, tzinfo=TZ)
        self.assertIsNone(due_recommendation_slot(at_1801, slots, time(17, 45), has_run))

    def test_service_does_not_call_provider_after_latest_start(self):
        now = datetime(2026, 7, 15, 17, 45, tzinfo=TZ)
        clock = MutableClock(now)
        provider = FakeProvider([make_match(i, now, business_date="2026-07-15") for i in range(1, 5)])
        service = self._service(clock, provider)
        outcome = service.recommend(now)
        self.assertEqual(outcome.status, "closed")
        self.assertEqual(provider.match_calls, 0)
        self.assertEqual(service.database.summary()["plans_total"], 0)

    def test_provider_returning_after_mail_cutoff_cannot_create_plan(self):
        start = datetime(2026, 7, 15, 17, 44, tzinfo=TZ)
        clock = MutableClock(start)
        matches = [make_match(i, start, business_date="2026-07-15") for i in range(1, 5)]
        provider = FakeProvider(
            matches,
            after_fetch=lambda: setattr(clock, "value", datetime(2026, 7, 15, 17, 50, tzinfo=TZ)),
        )
        service = self._service(clock, provider)
        outcome = service.recommend(start)
        self.assertEqual(outcome.status, "closed")
        self.assertEqual(provider.match_calls, 1)
        self.assertEqual(service.database.count_plans_for_recommendation_date("2026-07-15"), 0)

    def test_expired_recommendation_is_never_sent_or_settled(self):
        created_at = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        clock = MutableClock(datetime(2026, 7, 15, 18, 0, tzinfo=TZ))
        settings = make_settings(
            Path("data"), database_path=self.path, mail_preview_dir=self.preview
        )
        database = Database(self.path)
        database.initialize()
        recommendation = make_recommendation(
            created_at,
            [make_match(i, created_at, business_date="2026-07-15") for i in range(1, 5)],
        )
        subject, text_body, html_body = render_recommendation(recommendation)
        database.create_plan_with_mail(
            recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=datetime(2026, 7, 15, 17, 50, tzinfo=TZ),
        )
        sent, failed = flush_outbox(database, Mailer(settings, clock=clock), created_at)
        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(database.pending_plans(), [])
        summary = database.summary()
        self.assertEqual(summary["plans_total"], 0)
        self.assertEqual(summary["plans_undelivered"], 1)
        self.assertEqual(summary["emails_expired"], 1)
        self.assertEqual(summary["baseline_stake"], "0.00")

    def test_non_recommendation_mail_can_send_after_18(self):
        now = datetime(2026, 7, 15, 18, 5, tzinfo=TZ)
        clock = MutableClock(now)
        settings = make_settings(
            Path("data"), database_path=self.path, mail_preview_dir=self.preview
        )
        database = Database(self.path)
        database.initialize()
        database.enqueue_mail(
            dedupe_key="error:test",
            kind="error",
            subject="test",
            text_body="test",
            html_body="<p>test</p>",
            created_at=now,
        )
        self.assertEqual(flush_outbox(database, Mailer(settings, clock=clock), now), (1, 0))
        self.assertTrue((self.preview / "000001.txt").exists())

    def test_dry_run_preview_is_not_counted_or_settled(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        clock = MutableClock(now)
        settings = make_settings(
            Path("data"), database_path=self.path, mail_preview_dir=self.preview
        )
        database = Database(self.path)
        database.initialize()
        matches = [
            make_match(i, now, business_date="2026-07-15") for i in range(1, 5)
        ]
        recommendation = make_recommendation(now, matches)
        subject, text_body, html_body = render_recommendation(recommendation)
        self.assertTrue(
            database.create_plan_with_mail(
                recommendation,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=datetime(2026, 7, 15, 17, 50, tzinfo=TZ),
            )
        )

        self.assertEqual(
            flush_outbox(database, Mailer(settings, clock=clock), now),
            (1, 0),
        )
        stored = database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertEqual(stored.delivery_status, "previewed")
        self.assertEqual(database.pending_plans(), [])
        summary = database.summary()
        self.assertEqual(summary["plans_total"], 0)
        self.assertEqual(summary["plans_undelivered"], 1)
        self.assertEqual(summary["baseline_stake"], "0.00")
        self.assertEqual(summary["emails_previewed"], 1)

        service = ScoreFourfoldService(
            settings,
            database,
            FakeProvider(matches),
            Mailer(settings, clock=clock),
            clock=clock,
        )
        settlement = service.settle(now + timedelta(days=1))
        self.assertEqual(settlement.status, "idle")
        self.assertEqual(database.get_plan(recommendation.plan_id).status.value, "pending")

    def test_day_end_notice_is_idempotent(self):
        now = datetime(2026, 7, 15, 18, 0, tzinfo=TZ)
        clock = MutableClock(now)
        service = self._service(clock, FakeProvider([]))
        self.assertEqual(service.finalize_recommendation_day(now).status, "created")
        self.assertEqual(service.finalize_recommendation_day(now).status, "duplicate")
        self.assertEqual(service.database.summary()["emails_pending"], 1)


if __name__ == "__main__":
    unittest.main()
