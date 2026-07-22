from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.domain import MatchResult, ResultStatus
from score_fourfold.mail import render_recommendation, render_settlement
from score_fourfold.service import ScoreFourfoldService

from .helpers import make_match, make_recommendation


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("data") / f"test_database_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self.database = Database(self.path)
        self.database.initialize()
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def _create_plan(self):
        matches = [make_match(index, self.now, odds="2.00") for index in range(1, 5)]
        recommendation = make_recommendation(self.now, matches)
        subject, text_body, html_body = render_recommendation(recommendation)
        created = self.database.create_plan_with_mail(
            recommendation,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            expires_at=self.now + timedelta(hours=5),
        )
        self.assertTrue(created)
        return recommendation

    def test_database_enforces_three_crs_plans_per_recommendation_date(self):
        recommendation = self._create_plan()
        for number in (2, 3, 4):
            candidate = replace(
                recommendation,
                plan_id=f"BF4-TEST-{number:04d}",
                business_date="2026-07-15",
            )
            subject, text_body, html_body = render_recommendation(candidate)
            created = self.database.create_plan_with_mail(
                candidate,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=self.now + timedelta(hours=5),
            )
            self.assertEqual(created, number <= 3)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date(recommendation.recommendation_date), 3
        )

    def test_same_issue_date_is_allowed_on_next_recommendation_date(self):
        matches = [make_match(index, self.now, odds="2.00") for index in range(1, 5)]
        first = replace(make_recommendation(self.now, matches), business_date="2026-07-15")
        subject, text_body, html_body = render_recommendation(first)
        self.assertTrue(
            self.database.create_plan_with_mail(
                first,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=first.created_at + timedelta(hours=5),
            )
        )
        second = replace(
            first,
            plan_id="BF4-TEST-NEXT-DAY",
            created_at=first.created_at + timedelta(days=1),
        )
        subject, text_body, html_body = render_recommendation(second)
        self.assertTrue(
            self.database.create_plan_with_mail(
                second,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                expires_at=second.created_at + timedelta(hours=5),
            )
        )
        self.assertEqual(
            self.database.count_plans_for_recommendation_date("2026-07-15"), 1
        )

    def test_outbox_lease_prevents_parallel_claim(self):
        self._create_plan()
        first = self.database.claim_due_emails(self.now)
        self.assertEqual(len(first), 1)
        self.assertEqual(self.database.claim_due_emails(self.now), [])

        row = first[0]
        self.database.mark_email_failed(
            int(row["id"]),
            row["claim_token"],
            "temporary SMTP failure",
            self.now,
        )
        self.assertEqual(self.database.claim_due_emails(self.now + timedelta(seconds=59)), [])
        retry = self.database.claim_due_emails(self.now + timedelta(seconds=60))
        self.assertEqual(len(retry), 1)
        self.database.mark_email_sent(int(retry[0]["id"]), retry[0]["claim_token"], self.now)

    def test_deleted_plan_can_be_recreated_with_fresh_mail(self):
        recommendation = self._create_plan()
        claimed = self.database.claim_due_emails(self.now, limit=1)
        self.assertEqual(len(claimed), 1)
        self.database.mark_email_sent(
            int(claimed[0]["id"]), claimed[0]["claim_token"], self.now
        )
        self.assertTrue(self.database.delete_plan(recommendation.plan_id))

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
        recreated = self.database.get_plan(recommendation.plan_id)
        assert recreated is not None
        self.assertEqual(recreated.delivery_status, "queued")
        self.assertEqual(len(self.database.claim_due_emails(self.now, limit=1)), 1)

    def test_fifth_mail_failure_becomes_dead_letter(self):
        self.database.enqueue_mail(
            dedupe_key="dead-letter-test",
            kind="error",
            subject="test",
            text_body="test",
            html_body="<p>test</p>",
            created_at=self.now,
        )
        attempt_at = self.now
        for _ in range(5):
            rows = self.database.claim_due_emails(attempt_at)
            self.assertEqual(len(rows), 1)
            self.database.mark_email_failed(
                int(rows[0]["id"]),
                rows[0]["claim_token"],
                "permanent SMTP failure",
                attempt_at,
            )
            attempt_at += timedelta(hours=2)
        self.assertEqual(self.database.summary()["emails_dead"], 1)

    def test_settlement_keeps_original_quoted_prize(self):
        recommendation = self._create_plan()
        results = {
            leg.match.match_id: MatchResult(leg.match.match_id, ResultStatus.FINAL, 1, 0)
            for leg in recommendation.legs
        }
        first_match = recommendation.legs[0].match.match_id
        results[first_match] = MatchResult(first_match, ResultStatus.VOID)
        self.database.update_leg_results(recommendation.plan_id, results)
        plan = self.database.get_plan(recommendation.plan_id)
        assert plan is not None
        settlement = ScoreFourfoldService._build_settlement(plan, self.now)
        assert settlement is not None
        subject, text_body, html_body = render_settlement(plan, settlement, self.database.summary())
        self.database.settle_plan_with_mail(
            settlement,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        stored = self.database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertEqual(stored.gross_prize, recommendation.gross_prize)
        self.assertNotEqual(stored.gross_prize, settlement.gross_prize)

    def test_migrates_version_one_database_without_losing_plan(self):
        legacy_path = Path("data") / f"legacy_{self._testMethodName}.db"
        legacy_path.unlink(missing_ok=True)
        try:
            connection = sqlite3.connect(legacy_path)
            connection.executescript(
                """
                CREATE TABLE plans (
                    plan_id TEXT PRIMARY KEY, business_date TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', stake_cents INTEGER NOT NULL,
                    combined_odds TEXT NOT NULL, joint_probability TEXT NOT NULL,
                    gross_prize_cents INTEGER NOT NULL, tax_cents INTEGER NOT NULL,
                    net_prize_cents INTEGER NOT NULL, net_profit_cents INTEGER,
                    strategy_version TEXT NOT NULL, settled_at TEXT
                );
                CREATE UNIQUE INDEX idx_plans_one_per_business_date ON plans(business_date);
                CREATE TABLE plan_legs (
                    plan_id TEXT NOT NULL, position INTEGER NOT NULL, match_id TEXT NOT NULL,
                    match_num TEXT NOT NULL, business_date TEXT NOT NULL, league TEXT NOT NULL,
                    home TEXT NOT NULL, away TEXT NOT NULL, start_at TEXT NOT NULL,
                    odds_updated_at TEXT, score_code TEXT NOT NULL, score_label TEXT NOT NULL,
                    odds TEXT NOT NULL, probability TEXT NOT NULL,
                    result_status TEXT NOT NULL DEFAULT 'pending', result_home INTEGER,
                    result_away INTEGER, official_status TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (plan_id, position)
                );
                CREATE TABLE email_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, subject TEXT NOT NULL, text_body TEXT NOT NULL,
                    html_body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, sent_at TEXT
                );
                CREATE TABLE job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO plans VALUES (
                    'BF4-LEGACY', '2026-07-14', '2026-07-14T12:00:00+08:00', 'won',
                    200, '16', '0.001', 3200, 0, 3200, 3000, 'legacy',
                    '2026-07-14T23:00:00+08:00'
                );
                INSERT INTO plans VALUES (
                    'BF4-LEGACY-DEAD', '2026-07-13', '2026-07-13T12:00:00+08:00', 'lost',
                    200, '16', '0.001', 3200, 0, 3200, -200, 'legacy',
                    '2026-07-14T08:00:00+08:00'
                );
                INSERT INTO email_outbox
                    (dedupe_key, kind, subject, text_body, html_body, status, created_at, sent_at)
                VALUES (
                    'recommendation:BF4-LEGACY', 'recommendation', 'legacy', 'legacy',
                    '<p>legacy</p>', 'sent', '2026-07-14T12:00:00+08:00',
                    '2026-07-14T12:01:00+08:00'
                );
                INSERT INTO email_outbox
                    (dedupe_key, kind, subject, text_body, html_body, status, created_at)
                VALUES (
                    'recommendation:STALE', 'recommendation', 'stale', 'stale',
                    '<p>stale</p>', 'pending', '2026-07-13T12:00:00+08:00'
                );
                INSERT INTO email_outbox
                    (dedupe_key, kind, subject, text_body, html_body, status, attempts, created_at)
                VALUES (
                    'recommendation:BF4-LEGACY-DEAD', 'recommendation', 'dead', 'dead',
                    '<p>dead</p>', 'dead', 5, '2026-07-13T12:00:00+08:00'
                );
                """
            )
            for plan_id, business_date in (
                ("BF4-LEGACY", "2026-07-14"),
                ("BF4-LEGACY-DEAD", "2026-07-13"),
            ):
                for position in range(1, 5):
                    connection.execute(
                        """
                        INSERT INTO plan_legs
                            (plan_id, position, match_id, match_num, business_date, league,
                             home, away, start_at, score_code, score_label, odds, probability)
                        VALUES (?, ?, ?, ?, ?, '联赛', '主队', '客队',
                                '2026-07-14T20:00:00+08:00', 's01s00', '1:0', '2.00', '0.10')
                        """,
                        (
                            plan_id,
                            position,
                            f"{plan_id}-{position}",
                            f"周二{position:03d}",
                            business_date,
                        ),
                    )
            connection.commit()
            connection.close()

            legacy = Database(legacy_path)
            legacy.initialize()
            plan = legacy.get_plan("BF4-LEGACY")
            assert plan is not None
            self.assertEqual(plan.recommendation_date, "2026-07-14")
            self.assertEqual(plan.issue_date, "2026-07-14")
            self.assertEqual(plan.pass_size, 4)
            self.assertEqual(plan.delivery_status, "sent")
            self.assertEqual(plan.settled_net_prize, Decimal("32.00"))
            dead_plan = legacy.get_plan("BF4-LEGACY-DEAD")
            assert dead_plan is not None
            self.assertEqual(dead_plan.delivery_status, "failed")
            self.assertEqual(legacy.summary()["baseline_stake"], "2.00")
            self.assertEqual(legacy.summary()["baseline_return"], "32.00")
            self.assertEqual(legacy.summary()["baseline_profit"], "30.00")
            with legacy.connect() as migrated:
                stale = migrated.execute(
                    "SELECT status FROM email_outbox WHERE dedupe_key = 'recommendation:STALE'"
                ).fetchone()
                self.assertEqual(stale["status"], "expired")
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{legacy_path}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
