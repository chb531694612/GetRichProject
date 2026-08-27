from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.domain import MarketType, MatchResult, ResultStatus
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

    def test_database_enforces_one_crs_plan_per_recommendation_date(self):
        recommendation = self._create_plan()
        for number in (2, 3):
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
            self.assertEqual(created, number <= 1)
        self.assertEqual(
            self.database.count_plans_for_recommendation_date(recommendation.recommendation_date), 1
        )

    def test_database_atomically_honors_configured_market_limit(self):
        matches = [make_match(index, self.now, odds="2.00") for index in range(1, 5)]
        base = make_recommendation(self.now, matches)
        outcomes: list[bool] = []
        for number in range(1, 4):
            candidate = replace(base, plan_id=f"BF4-LIMIT-{number:04d}")
            subject, text_body, html_body = render_recommendation(candidate)
            outcomes.append(
                self.database.create_plan_with_mail(
                    candidate,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    expires_at=self.now + timedelta(hours=5),
                    market_limit=2,
                )
            )
        self.assertEqual(outcomes, [True, True, False])
        self.assertEqual(
            self.database.count_plans_for_recommendation_market(
                base.recommendation_date,
                MarketType.CRS,
            ),
            2,
        )

    def test_ai_history_context_deduplicates_matches_across_plans(self):
        matches = [make_match(index, self.now, odds="2.00") for index in range(1, 5)]
        base = make_recommendation(self.now, matches)
        for number in (1, 2):
            candidate = replace(base, plan_id=f"BF4-HISTORY-{number:04d}")
            subject, text_body, html_body = render_recommendation(candidate)
            self.assertTrue(
                self.database.create_plan_with_mail(
                    candidate,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    expires_at=self.now + timedelta(hours=5),
                    market_limit=2,
                )
            )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE plan_legs
                SET result_status = 'final', result_home = 1, result_away = 0
                WHERE plan_id LIKE 'BF4-HISTORY-%'
                """
            )
        context = self.database.ai_history_context(MarketType.CRS)
        self.assertIn("最近4个已结算场次", context)
        self.assertIn("主胜4场(100.0%)", context)

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

    def test_recommendation_dates_and_plans_for_date(self):
        first = self._create_plan()
        second_date = self.now + timedelta(days=1)
        second_matches = [make_match(index, second_date, odds="2.00") for index in range(1, 5)]
        second = replace(
            make_recommendation(second_date, second_matches),
            plan_id="BF4-TEST-SECOND-DAY",
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
        dates = self.database.recommendation_dates()
        self.assertEqual(len(dates), 2)
        self.assertEqual(dates[0], "2026-07-15")
        self.assertEqual(dates[1], "2026-07-14")
        day1_plans = self.database.plans_for_recommendation_date("2026-07-14")
        self.assertEqual(len(day1_plans), 1)
        self.assertEqual(day1_plans[0].plan_id, first.plan_id)
        day2_plans = self.database.plans_for_recommendation_date("2026-07-15")
        self.assertEqual(len(day2_plans), 1)
        self.assertEqual(day2_plans[0].plan_id, second.plan_id)

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

    def test_plan_options_ai_suggestions_and_manual_replacement_are_persisted(self):
        recommendation = self._create_plan()
        stored = self.database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertTrue(all(len(leg.options) == 3 for leg in stored.legs))

        suggestions = [
            (
                leg.match_id,
                "s01s01" if index == 0 else "s01s00",
                f"第{index + 1}场理由",
            )
            for index, leg in enumerate(stored.legs)
        ]
        self.assertTrue(
            self.database.update_ai_analysis(
                recommendation.plan_id, "结构化总体分析", suggestions
            )
        )
        analyzed = self.database.get_plan(recommendation.plan_id)
        assert analyzed is not None
        self.assertEqual(len(analyzed.ai_suggestions), 4)
        first = analyzed.legs[0]
        self.assertEqual(first.original_score_code, first.score_code)
        self.assertEqual(first.original_score_label, first.score_label)
        original_label = first.score_label

        self.assertTrue(
            self.database.update_plan_leg_option(
                recommendation.plan_id, first.match_id, "s01s01"
            )
        )
        updated = self.database.get_plan(recommendation.plan_id)
        assert updated is not None
        self.assertEqual(updated.legs[0].score_label, "1:1")
        self.assertEqual(updated.legs[0].odds, Decimal("7.50"))
        self.assertEqual(updated.combined_odds, Decimal("60"))
        self.assertEqual(updated.gross_prize, Decimal("120.00"))
        self.assertEqual(updated.ai_summary, "结构化总体分析")
        self.assertEqual(len(updated.ai_suggestions), 3)
        self.assertNotIn(first.match_id, {item.match_id for item in updated.ai_suggestions})
        self.assertEqual(updated.legs[0].original_score_code, first.score_code)
        self.assertEqual(updated.legs[0].original_score_label, original_label)
        self.assertFalse(
            self.database.update_plan_leg_option(
                recommendation.plan_id, first.match_id, "invented-option"
            )
        )

    def test_had_pick_can_be_manually_changed_to_another_real_outcome(self):
        matches = [make_match(index, self.now) for index in range(1, 7)]
        recommendation = make_recommendation(
            self.now, matches, market=MarketType.HAD
        )
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
        stored = self.database.get_plan(recommendation.plan_id)
        assert stored is not None
        self.assertTrue(all(len(leg.options) == 3 for leg in stored.legs))
        first = stored.legs[0]

        self.assertTrue(
            self.database.update_plan_leg_option(
                recommendation.plan_id, first.match_id, "a"
            )
        )
        updated = self.database.get_plan(recommendation.plan_id)
        assert updated is not None
        self.assertEqual(updated.legs[0].score_label, "客胜")
        self.assertEqual(updated.legs[0].odds, Decimal("4.50"))
        self.assertEqual(
            updated.combined_odds,
            Decimal("4.50") * (Decimal("1.80") ** 5),
        )

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
            self.assertTrue(all(len(leg.options) == 1 for leg in plan.legs))
            dead_plan = legacy.get_plan("BF4-LEGACY-DEAD")
            assert dead_plan is not None
            self.assertEqual(dead_plan.delivery_status, "failed")
            self.assertEqual(legacy.summary()["baseline_stake"], "2.00")
            self.assertEqual(legacy.summary()["baseline_return"], "32.00")
            self.assertEqual(legacy.summary()["baseline_profit"], "30.00")
            with legacy.connect() as migrated:
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 9)
                stale = migrated.execute(
                    "SELECT status FROM email_outbox WHERE dedupe_key = 'recommendation:STALE'"
                ).fetchone()
                self.assertEqual(stale["status"], "expired")
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{legacy_path}{suffix}").unlink(missing_ok=True)

    def test_migrates_legacy_activity_logs_adds_plan_id_column(self):
        """Old databases without activity_logs.plan_id must upgrade idempotently."""
        legacy_path = Path("data") / f"legacy_logs_{self._testMethodName}.db"
        legacy_path.unlink(missing_ok=True)
        try:
            connection = sqlite3.connect(legacy_path)
            connection.executescript(
                """
                CREATE TABLE activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO activity_logs (created_at, category, message, detail)
                VALUES ('2026-08-27T10:00:00', 'ai', '旧日志', '无 plan_id');
                """
            )
            connection.commit()
            connection.close()

            legacy = Database(legacy_path)
            legacy.initialize()
            # 迁移后 plan_id 列存在且旧行保留。
            with legacy.connect() as migrated:
                columns = {
                    row["name"]
                    for row in migrated.execute("PRAGMA table_info(activity_logs)")
                }
                self.assertIn("plan_id", columns)
                row = migrated.execute(
                    "SELECT message, plan_id FROM activity_logs WHERE message = '旧日志'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertIsNone(row["plan_id"])
            # 新写入带 plan_id 的日志。
            legacy.add_log("ai", "新日志", "带计划", "BF4-TEST-0001")
            logs = legacy.query_logs(category="ai", limit=10)
            self.assertTrue(
                any(item["plan_id"] == "BF4-TEST-0001" for item in logs["items"])
            )
            # 幂等：再次初始化不报错。
            legacy.initialize()
            with legacy.connect() as migrated:
                self.assertEqual(
                    migrated.execute("PRAGMA user_version").fetchone()[0], 9
                )
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{legacy_path}{suffix}").unlink(missing_ok=True)


class PaginationAndCalendarTests(unittest.TestCase):
    """Regression tests for paginated_plans, calendar_stats, set_purchased, set_ticket_image."""

    def setUp(self):
        self.path = Path("data") / f"test_pagination_{self._testMethodName}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self.database = Database(self.path)
        self.database.initialize()
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def _create_sent_plan(self, *, plan_id: str = "BF4-TEST-0001", business_date: str = "2026-07-14", day_offset: int = 0):
        now = self.now + timedelta(days=day_offset)
        matches = [make_match(i, now, odds="2.00", business_date=business_date) for i in range(1, 5)]
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

    def test_paginated_plans_returns_newest_first_with_total(self):
        for n in range(1, 13):
            self._create_sent_plan(
                plan_id=f"BF4-TEST-{n:04d}",
                business_date=f"2026-07-{n:02d}",
                day_offset=n - 1,
            )
        plans, total = self.database.paginated_plans(page=1, per_page=10)
        self.assertEqual(total, 12)
        self.assertEqual(len(plans), 10)
        self.assertEqual(plans[0].plan_id, "BF4-TEST-0012")
        self.assertEqual(plans[9].plan_id, "BF4-TEST-0003")
        plans2, total2 = self.database.paginated_plans(page=2, per_page=10)
        self.assertEqual(total2, 12)
        self.assertEqual(len(plans2), 2)
        self.assertEqual(plans2[0].plan_id, "BF4-TEST-0002")

    def test_paginated_plans_prefetches_details_for_the_whole_page(self):
        for n in range(1, 4):
            self._create_sent_plan(
                plan_id=f"BF4-BATCH-{n:04d}",
                business_date=f"2026-07-{n:02d}",
                day_offset=n - 1,
            )

        with patch.object(
            self.database,
            "_load_plan",
            wraps=self.database._load_plan,
        ) as load_plan:
            plans, total = self.database.paginated_plans(page=1, per_page=10)

        self.assertEqual((len(plans), total), (3, 3))
        self.assertEqual(load_plan.call_count, 3)
        self.assertTrue(
            all(call.kwargs.get("detail_rows") is not None for call in load_plan.call_args_list)
        )

    def test_paginated_plans_empty_database(self):
        plans, total = self.database.paginated_plans(page=1)
        self.assertEqual(total, 0)
        self.assertEqual(plans, [])

    def test_paginated_plans_clamps_per_page(self):
        self._create_sent_plan()
        plans, total = self.database.paginated_plans(page=1, per_page=999)
        self.assertEqual(len(plans), 1)
        self.assertEqual(total, 1)

    def test_calendar_stats_groups_by_date_and_status(self):
        self._create_sent_plan(plan_id="BF4-A-0001", business_date="2026-07-14", day_offset=0)
        self._create_sent_plan(plan_id="BF4-B-0002", business_date="2026-07-15", day_offset=1)
        self._create_sent_plan(plan_id="BF4-C-0003", business_date="2026-07-16", day_offset=2)
        stats = self.database.calendar_stats(2026, 7)
        self.assertIn("2026-07-14", stats)
        self.assertIn("2026-07-15", stats)
        self.assertIn("2026-07-16", stats)
        self.assertEqual(stats["2026-07-14"]["total"], 1)
        self.assertEqual(stats["2026-07-14"]["pending"], 1)
        self.assertEqual(stats["2026-07-15"]["total"], 1)
        self.assertEqual(stats["2026-07-16"]["total"], 1)

    def test_calendar_stats_empty_month(self):
        stats = self.database.calendar_stats(2025, 1)
        self.assertEqual(stats, {})

    def test_filtered_plans_summary_and_calendar_share_filters(self):
        first = self._create_sent_plan(
            plan_id="BF4-ALPHA-0001", business_date="2026-07-14", day_offset=0
        )
        second = self._create_sent_plan(
            plan_id="BF4-BETA-0002", business_date="2026-07-15", day_offset=1
        )
        self._create_sent_plan(
            plan_id="BF4-GAMMA-0003", business_date="2026-07-16", day_offset=2
        )
        self.database.set_purchased(second.plan_id, True)

        plans, total = self.database.filtered_plans(
            {"purchased": True}, page=1, per_page=10
        )
        summary = self.database.filtered_summary({"purchased": True})
        calendar = self.database.filtered_calendar_stats(
            2026, 7, {"purchased": True}
        )
        self.assertEqual(total, 1)
        self.assertEqual([plan.plan_id for plan in plans], [second.plan_id])
        self.assertEqual(summary["plans_total"], 1)
        self.assertEqual(summary["plans_purchased"], 1)
        self.assertEqual(set(calendar), {"2026-07-15"})

        searched, searched_total = self.database.filtered_plans(
            {"q": "ALPHA"}, page=1, per_page=10
        )
        self.assertEqual(searched_total, 1)
        self.assertEqual(searched[0].plan_id, first.plan_id)
        dated = self.database.filtered_summary({"date": "2026-07-16"})
        self.assertEqual(dated["plans_total"], 1)

    def test_set_purchased_and_ticket_image_roundtrip(self):
        rec = self._create_sent_plan()
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertFalse(plan.purchased)
        self.assertEqual(plan.ticket_image, "")

        self.assertTrue(self.database.set_purchased(rec.plan_id, True))
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertTrue(plan.purchased)

        self.assertTrue(self.database.set_ticket_image(rec.plan_id, "BF4-TEST-0001.png"))
        plan = self.database.get_plan(rec.plan_id)
        assert plan is not None
        self.assertEqual(plan.ticket_image, "BF4-TEST-0001.png")
        self.assertTrue(plan.purchased, "set_ticket_image should also mark purchased")

    def test_set_purchased_nonexistent_plan(self):
        self.assertFalse(self.database.set_purchased("NONEXISTENT", True))

    def test_set_ticket_image_nonexistent_plan(self):
        self.assertFalse(self.database.set_ticket_image("NONEXISTENT", "x.png"))


if __name__ == "__main__":
    unittest.main()
