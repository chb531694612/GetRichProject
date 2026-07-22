from __future__ import annotations

import sqlite3
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Sequence

from .domain import (
    Match,
    MarketType,
    MatchResult,
    PlanStatus,
    Recommendation,
    ResultStatus,
    ScoreOption,
    Settlement,
)
from .strategy import calculate_prize


def _cents(value: Decimal) -> int:
    return int(value * 100)


def _money(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.00"))


@dataclass(frozen=True, slots=True)
class StoredLeg:
    position: int
    match_id: str
    match_num: str
    business_date: str
    league: str
    home: str
    away: str
    start_at: datetime
    snapshot_fetched_at: datetime | None
    score_code: str
    score_label: str
    odds: Decimal
    probability: Decimal
    result_status: ResultStatus
    result_home: int | None
    result_away: int | None
    official_status: str
    options: tuple[ScoreOption, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredAISuggestion:
    match_id: str
    option_code: str
    option_label: str
    odds: Decimal
    probability: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class StoredPlan:
    plan_id: str
    business_date: str
    recommendation_date: str
    issue_date: str
    pass_size: int
    created_at: datetime
    status: PlanStatus
    delivery_status: str
    stake: Decimal
    combined_odds: Decimal
    joint_probability: Decimal
    gross_prize: Decimal
    tax: Decimal
    net_prize: Decimal
    settled_gross_prize: Decimal | None
    settled_tax: Decimal | None
    settled_net_prize: Decimal | None
    net_profit: Decimal | None
    strategy_version: str
    settled_at: datetime | None
    legs: tuple[StoredLeg, ...]
    market: MarketType = MarketType.CRS
    ai_summary: str = ""
    ai_suggestions: tuple[StoredAISuggestion, ...] = ()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    business_date TEXT NOT NULL,
                    recommendation_date TEXT NOT NULL,
                    issue_date TEXT NOT NULL,
                    pass_size INTEGER NOT NULL,
                    market TEXT NOT NULL DEFAULT 'crs',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivery_status TEXT NOT NULL DEFAULT 'queued',
                    stake_cents INTEGER NOT NULL,
                    combined_odds TEXT NOT NULL,
                    joint_probability TEXT NOT NULL,
                    gross_prize_cents INTEGER NOT NULL,
                    tax_cents INTEGER NOT NULL,
                    net_prize_cents INTEGER NOT NULL,
                    settled_gross_prize_cents INTEGER,
                    settled_tax_cents INTEGER,
                    settled_net_prize_cents INTEGER,
                    net_profit_cents INTEGER,
                    strategy_version TEXT NOT NULL,
                    settled_at TEXT,
                    ai_summary TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_plans_business_date ON plans(business_date);
                CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

                CREATE TABLE IF NOT EXISTS recommendation_days (
                    recommendation_date TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'crs',
                    plan_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (recommendation_date, market, plan_id)
                );

                CREATE TABLE IF NOT EXISTS plan_legs (
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    match_id TEXT NOT NULL,
                    match_num TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    league TEXT NOT NULL,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    odds_updated_at TEXT,
                    snapshot_fetched_at TEXT,
                    score_code TEXT NOT NULL,
                    score_label TEXT NOT NULL,
                    odds TEXT NOT NULL,
                    probability TEXT NOT NULL,
                    result_status TEXT NOT NULL DEFAULT 'pending',
                    result_home INTEGER,
                    result_away INTEGER,
                    official_status TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (plan_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_plan_legs_match_id ON plan_legs(match_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_legs_unique_match ON plan_legs(plan_id, match_id);

                CREATE TABLE IF NOT EXISTS plan_leg_options (
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
                    match_id TEXT NOT NULL,
                    option_code TEXT NOT NULL,
                    option_label TEXT NOT NULL,
                    odds TEXT NOT NULL,
                    probability TEXT NOT NULL,
                    is_other INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (plan_id, match_id, option_code)
                );

                CREATE TABLE IF NOT EXISTS plan_ai_suggestions (
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
                    match_id TEXT NOT NULL,
                    option_code TEXT NOT NULL,
                    option_label TEXT NOT NULL,
                    odds TEXT NOT NULL,
                    probability TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (plan_id, match_id)
                );

                CREATE TABLE IF NOT EXISTS email_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    text_body TEXT NOT NULL,
                    html_body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    claim_token TEXT,
                    claimed_until TEXT,
                    next_attempt_at TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    expired_at TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_email_outbox_status ON email_outbox(status, id);

                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS web_requests (
                    request_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'running',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Apply small, forward-only SQLite migrations for existing volumes."""
        plan_columns = {row["name"] for row in connection.execute("PRAGMA table_info(plans)")}
        for name in (
            "settled_gross_prize_cents",
            "settled_tax_cents",
            "settled_net_prize_cents",
        ):
            if name not in plan_columns:
                connection.execute(f"ALTER TABLE plans ADD COLUMN {name} INTEGER")
        # Older databases already recorded the final net profit but did not
        # keep a separate actual-return column. Reconstruct the only value that
        # is unambiguous (stake + final profit) so historical dashboard totals
        # do not incorrectly show a zero return after upgrading.
        connection.execute(
            """
            UPDATE plans
            SET settled_net_prize_cents = stake_cents + net_profit_cents
            WHERE status != 'pending'
              AND net_profit_cents IS NOT NULL
              AND settled_net_prize_cents IS NULL
            """
        )
        if "recommendation_date" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN recommendation_date TEXT")
        if "issue_date" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN issue_date TEXT")
        if "pass_size" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN pass_size INTEGER")
        if "delivery_status" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'queued'")
        if "market" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN market TEXT NOT NULL DEFAULT 'crs'")
        connection.execute(
            "UPDATE plans SET recommendation_date = substr(created_at, 1, 10) "
            "WHERE recommendation_date IS NULL OR recommendation_date = ''"
        )
        connection.execute(
            "UPDATE plans SET issue_date = business_date WHERE issue_date IS NULL OR issue_date = ''"
        )
        connection.execute(
            """
            UPDATE plans
            SET pass_size = (SELECT COUNT(*) FROM plan_legs WHERE plan_legs.plan_id = plans.plan_id)
            WHERE pass_size IS NULL OR pass_size = 0
            """
        )
        connection.execute(
            "UPDATE plans SET market = 'crs' WHERE market IS NULL OR market = ''"
        )

        if "ai_summary" not in plan_columns:
            connection.execute("ALTER TABLE plans ADD COLUMN ai_summary TEXT NOT NULL DEFAULT ''")

        outbox_columns = {row["name"] for row in connection.execute("PRAGMA table_info(email_outbox)")}
        for name in ("claim_token", "claimed_until", "next_attempt_at"):
            if name not in outbox_columns:
                connection.execute(f"ALTER TABLE email_outbox ADD COLUMN {name} TEXT")
        if "priority" not in outbox_columns:
            connection.execute("ALTER TABLE email_outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        for name in ("expires_at", "expired_at"):
            if name not in outbox_columns:
                connection.execute(f"ALTER TABLE email_outbox ADD COLUMN {name} TEXT")
        # Version 0.1 recommendation messages had no hard deadline. Any such
        # unsent message is stale at upgrade time and must never be delivered.
        connection.execute(
            """
            UPDATE email_outbox
            SET status = 'expired', expired_at = COALESCE(expired_at, created_at),
                last_error = 'legacy recommendation expired during v0.2 upgrade',
                claim_token = NULL, claimed_until = NULL, next_attempt_at = NULL
            WHERE kind = 'recommendation'
              AND expires_at IS NULL
              AND status IN ('pending', 'sending')
            """
        )

        leg_columns = {row["name"] for row in connection.execute("PRAGMA table_info(plan_legs)")}
        if "snapshot_fetched_at" not in leg_columns:
            connection.execute("ALTER TABLE plan_legs ADD COLUMN snapshot_fetched_at TEXT")
        # Existing plans did not keep the full option snapshot. Preserve at
        # least their selected option; a later manual AI run can refresh the
        # remaining choices from the configured provider.
        connection.execute(
            """
            INSERT OR IGNORE INTO plan_leg_options
                (plan_id, match_id, option_code, option_label, odds, probability, is_other)
            SELECT plan_id, match_id, score_code, score_label, odds, probability, 0
            FROM plan_legs
            """
        )

        connection.execute("DROP INDEX IF EXISTS idx_plans_one_per_business_date")
        connection.execute("DROP INDEX IF EXISTS idx_plans_one_per_recommendation_date")
        connection.execute("DROP INDEX IF EXISTS idx_plans_one_per_recommendation_market")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_recommendation_market "
            "ON plans(recommendation_date, market)"
        )

        day_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(recommendation_days)")
        }
        day_pk = [
            row["name"]
            for row in sorted(
                connection.execute("PRAGMA table_info(recommendation_days)"),
                key=lambda row: row["pk"],
            )
            if row["pk"]
        ]
        if "market" not in day_columns or day_pk != ["recommendation_date", "market", "plan_id"]:
            if "market" not in day_columns:
                connection.execute("ALTER TABLE recommendation_days ADD COLUMN market TEXT NOT NULL DEFAULT 'crs'")
            connection.execute("DROP TABLE IF EXISTS recommendation_days_v3")
            connection.execute(
                """
                CREATE TABLE recommendation_days_v3 (
                    recommendation_date TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'crs',
                    plan_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (recommendation_date, market)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO recommendation_days_v3
                    (recommendation_date, market, plan_id, created_at)
                SELECT recommendation_date, COALESCE(NULLIF(market, ''), 'crs'), plan_id, created_at
                FROM recommendation_days
                """
            )
            connection.execute("DROP TABLE recommendation_days")
            connection.execute("ALTER TABLE recommendation_days_v3 RENAME TO recommendation_days")

        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_legs_unique_match ON plan_legs(plan_id, match_id)"
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO recommendation_days
                (recommendation_date, market, plan_id, created_at)
            SELECT recommendation_date, COALESCE(NULLIF(market, ''), 'crs'), MIN(plan_id), MIN(created_at)
            FROM plans
            WHERE recommendation_date IS NOT NULL AND recommendation_date != ''
            GROUP BY recommendation_date, COALESCE(NULLIF(market, ''), 'crs'), plan_id
            """
        )
        # v0.7 migration: support multi-plan CRS by changing recommendation_days PK.
        pk_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='recommendation_days'"
        ).fetchone()
        if pk_row and ", plan_id)" not in (pk_row[0] or ""):
            connection.execute(
                """
                CREATE TABLE recommendation_days_v3 (
                    recommendation_date TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'crs',
                    plan_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (recommendation_date, market, plan_id)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO recommendation_days_v3
                    (recommendation_date, market, plan_id, created_at)
                SELECT recommendation_date, COALESCE(NULLIF(market, ''), 'crs'), plan_id, created_at
                FROM recommendation_days
                """
            )
            connection.execute("DROP TABLE recommendation_days")
            connection.execute("ALTER TABLE recommendation_days_v3 RENAME TO recommendation_days")
        connection.execute(
            """
            UPDATE plans
            SET delivery_status = 'sent'
            WHERE delivery_status = 'queued'
              AND EXISTS (
                SELECT 1 FROM email_outbox
                WHERE email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                  AND email_outbox.status = 'sent'
              )
            """
        )
        connection.execute(
            """
            UPDATE plans
            SET delivery_status = CASE
                WHEN EXISTS (
                    SELECT 1 FROM email_outbox
                    WHERE email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      AND email_outbox.status = 'expired'
                ) THEN 'expired'
                ELSE 'failed'
            END
            WHERE delivery_status = 'queued'
              AND EXISTS (
                SELECT 1 FROM email_outbox
                WHERE email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                  AND email_outbox.status IN ('expired', 'dead')
              )
            """
        )
        connection.execute("PRAGMA user_version = 8")

    def count_plans_for_business_date(self, business_date: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM plans WHERE business_date = ?", (business_date,)
            ).fetchone()
            return int(row["count"])

    def count_plans_for_recommendation_date(self, recommendation_date: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM plans WHERE recommendation_date = ?",
                (recommendation_date,),
            ).fetchone()
            return int(row["count"])

    def count_plans_for_recommendation_market(
        self, recommendation_date: str, market: MarketType | str
    ) -> int:
        market_value = market.value if isinstance(market, MarketType) else str(market)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM plans
                WHERE recommendation_date = ? AND market = ?
                """,
                (recommendation_date, market_value),
            ).fetchone()
            return int(row["count"])

    def has_plan_for_recommendation_market(
        self, recommendation_date: str, market: MarketType | str
    ) -> bool:
        return self.count_plans_for_recommendation_market(recommendation_date, market) > 0

    def has_job_run(self, job_name: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM job_runs WHERE job_name = ? LIMIT 1", (job_name,)
            ).fetchone()
            return row is not None

    def latest_job_run(self, job_name: str) -> tuple[str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT status, detail FROM job_runs
                WHERE job_name = ? ORDER BY id DESC LIMIT 1
                """,
                (job_name,),
            ).fetchone()
            return (str(row["status"]), str(row["detail"])) if row else None

    def claim_web_request(
        self,
        request_id: str,
        created_at: datetime,
        *,
        cooldown_seconds: int = 300,
    ) -> bool:
        cooldown_after = (created_at - timedelta(seconds=cooldown_seconds)).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO web_requests (request_id, status, created_at)
                SELECT ?, 'running', ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM web_requests
                    WHERE created_at > ? AND status != 'busy'
                )
                """,
                (request_id, created_at.isoformat(), cooldown_after),
            )
            return cursor.rowcount == 1

    def finish_web_request(
        self,
        request_id: str,
        status: str,
        detail: str,
        finished_at: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE web_requests
                SET status = ?, detail = ?, finished_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (status, detail[:2000], finished_at.isoformat(), request_id),
            )

    def get_web_request(self, request_id: str) -> tuple[str, str] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, detail FROM web_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return (str(row["status"]), str(row["detail"])) if row else None

    def has_sent_recommendation_on(self, recommendation_date: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM plans
                WHERE recommendation_date = ? AND delivery_status = 'sent'
                LIMIT 1
                """,
                (recommendation_date,),
            ).fetchone()
            return row is not None

    def unsettled_match_ids(self, market: MarketType | str | None = None) -> set[str]:
        market_value = (
            market.value
            if isinstance(market, MarketType)
            else (str(market) if market is not None else None)
        )
        with self.connect() as connection:
            if market_value is None:
                rows = connection.execute(
                    """
                    SELECT DISTINCT plan_legs.match_id
                    FROM plan_legs
                    JOIN plans ON plans.plan_id = plan_legs.plan_id
                    WHERE plans.status = 'pending'
                      AND plans.delivery_status IN ('queued', 'sent')
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT DISTINCT plan_legs.match_id
                    FROM plan_legs
                    JOIN plans ON plans.plan_id = plan_legs.plan_id
                    WHERE plans.status = 'pending'
                      AND plans.delivery_status IN ('queued', 'sent')
                      AND plans.market = ?
                    """,
                    (market_value,),
                ).fetchall()
            return {str(row["match_id"]) for row in rows}

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        dedupe_key: str,
        kind: str,
        subject: str,
        text_body: str,
        html_body: str,
        created_at: datetime,
        priority: int = 0,
        expires_at: datetime | None = None,
        not_before: datetime | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO email_outbox
                (dedupe_key, kind, subject, text_body, html_body, priority,
                 expires_at, next_attempt_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dedupe_key,
                kind,
                subject,
                text_body,
                html_body,
                priority,
                expires_at.isoformat() if expires_at else None,
                not_before.isoformat() if not_before else None,
                created_at.isoformat(),
            ),
        )

    def create_plan_with_mail(
        self,
        recommendation: Recommendation,
        *,
        subject: str,
        text_body: str,
        html_body: str,
        expires_at: datetime,
        not_before: datetime | None = None,
    ) -> bool:
        if expires_at.tzinfo is None:
            raise ValueError("recommendation expires_at must be timezone-aware")
        if expires_at <= recommendation.created_at:
            raise ValueError("recommendation expires_at must be after created_at")
        if not_before is not None:
            if not_before.tzinfo is None:
                raise ValueError("recommendation not_before must be timezone-aware")
            if not_before >= expires_at:
                raise ValueError("recommendation not_before must be before expires_at")
        with self.connect() as connection:
            # Serialize the count-and-insert gate across processes. CRS permits
            # three plans per day; every other market remains one-per-day.
            connection.execute("BEGIN IMMEDIATE")
            limit = 3 if recommendation.market is MarketType.CRS else 1
            existing = connection.execute(
                """
                SELECT COUNT(*) AS count FROM plans
                WHERE recommendation_date = ? AND market = ?
                """,
                (recommendation.recommendation_date, recommendation.market.value),
            ).fetchone()
            if int(existing["count"]) >= limit:
                return False
            existing_plan = connection.execute(
                "SELECT 1 FROM plans WHERE plan_id = ?", (recommendation.plan_id,)
            ).fetchone()
            if existing_plan is None:
                # A manually deleted deterministic plan can later be generated
                # with the same ID. Remove its orphaned mail dedupe records so
                # the replacement receives a fresh recommendation message.
                connection.execute(
                    """
                    DELETE FROM email_outbox
                    WHERE dedupe_key IN (?, ?)
                       OR dedupe_key LIKE ?
                    """,
                    (
                        f"recommendation:{recommendation.plan_id}",
                        f"settlement:{recommendation.plan_id}",
                        f"recommendation-update:{recommendation.plan_id}:%",
                    ),
                )
            # Insert into recommendation_days for historical tracking.
            connection.execute(
                """
                INSERT OR IGNORE INTO recommendation_days
                    (recommendation_date, market, plan_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    recommendation.recommendation_date,
                    recommendation.market.value,
                    recommendation.plan_id,
                    recommendation.created_at.isoformat(),
                ),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO plans
                    (plan_id, business_date, recommendation_date, issue_date, pass_size, market,
                     created_at, status, delivery_status, stake_cents,
                     combined_odds, joint_probability, gross_prize_cents, tax_cents,
                     net_prize_cents, strategy_version, ai_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation.plan_id,
                    recommendation.business_date,
                    recommendation.recommendation_date,
                    recommendation.issue_date,
                    recommendation.pass_size,
                    recommendation.market.value,
                    recommendation.created_at.isoformat(),
                    _cents(recommendation.stake),
                    str(recommendation.combined_odds),
                    str(recommendation.joint_probability),
                    _cents(recommendation.gross_prize),
                    _cents(recommendation.tax),
                    _cents(recommendation.net_prize),
                    recommendation.strategy_version,
                    getattr(recommendation, "ai_summary", ""),
                ),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    DELETE FROM recommendation_days
                    WHERE recommendation_date = ? AND market = ? AND plan_id = ?
                    """,
                    (
                        recommendation.recommendation_date,
                        recommendation.market.value,
                        recommendation.plan_id,
                    ),
                )
                return False
            for position, leg in enumerate(recommendation.legs, start=1):
                connection.execute(
                    """
                    INSERT INTO plan_legs
                        (plan_id, position, match_id, match_num, business_date, league,
                         home, away, start_at, odds_updated_at, score_code, score_label,
                         snapshot_fetched_at, odds, probability)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recommendation.plan_id,
                        position,
                        leg.match.match_id,
                        leg.match.match_num,
                        leg.match.business_date,
                        leg.match.league,
                        leg.match.home,
                        leg.match.away,
                        leg.match.start_at.isoformat(),
                        leg.match.odds_updated_at.isoformat() if leg.match.odds_updated_at else None,
                        leg.score.code,
                        leg.score.label,
                        leg.match.snapshot_fetched_at.isoformat() if leg.match.snapshot_fetched_at else None,
                        str(leg.score.odds),
                        str(leg.score.probability),
                    ),
                )
                market_options = (
                    leg.match.score_options
                    if recommendation.market is MarketType.CRS
                    else leg.match.had_options
                )
                options_by_code = {option.code: option for option in market_options}
                options_by_code.setdefault(leg.score.code, leg.score)
                for option in options_by_code.values():
                    connection.execute(
                        """
                        INSERT INTO plan_leg_options
                            (plan_id, match_id, option_code, option_label, odds,
                             probability, is_other)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            recommendation.plan_id,
                            leg.match.match_id,
                            option.code,
                            option.label,
                            str(option.odds),
                            str(option.probability),
                            int(option.is_other),
                        ),
                    )
            self._insert_outbox(
                connection,
                dedupe_key=f"recommendation:{recommendation.plan_id}",
                kind="recommendation",
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                created_at=recommendation.created_at,
                priority=100,
                expires_at=expires_at,
                not_before=not_before,
            )
            return True

    def enqueue_mail(
        self,
        *,
        dedupe_key: str,
        kind: str,
        subject: str,
        text_body: str,
        html_body: str,
        created_at: datetime,
        priority: int = 0,
        expires_at: datetime | None = None,
        not_before: datetime | None = None,
    ) -> bool:
        with self.connect() as connection:
            before = connection.total_changes
            self._insert_outbox(
                connection,
                dedupe_key=dedupe_key,
                kind=kind,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                created_at=created_at,
                priority=priority,
                expires_at=expires_at,
                not_before=not_before,
            )
            return connection.total_changes > before

    def refresh_recommendation_mail(
        self,
        plan_id: str,
        *,
        subject: str,
        text_body: str,
        html_body: str,
        changed_at: datetime,
        first_send_at: datetime,
        expires_at: datetime,
    ) -> str:
        """Refresh an unsent recommendation or queue a revision after delivery.

        Returns ``refreshed`` when the original pending message was replaced,
        ``queued`` when a new update message was added, ``expired`` after the
        recommendation cutoff, or ``missing`` when the plan no longer exists.
        """
        if any(value.tzinfo is None for value in (changed_at, first_send_at, expires_at)):
            raise ValueError("recommendation mail times must be timezone-aware")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if exists is None:
                return "missing"
            if changed_at >= expires_at:
                return "expired"
            original = connection.execute(
                "SELECT id, status FROM email_outbox WHERE dedupe_key = ?",
                (f"recommendation:{plan_id}",),
            ).fetchone()
            if original is not None and original["status"] == "pending":
                not_before = max(changed_at, first_send_at)
                connection.execute(
                    """
                    UPDATE email_outbox
                    SET subject = ?, text_body = ?, html_body = ?, attempts = 0,
                        last_error = '', claim_token = NULL, claimed_until = NULL,
                        next_attempt_at = ?, expires_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        subject,
                        text_body,
                        html_body,
                        not_before.isoformat(),
                        expires_at.isoformat(),
                        int(original["id"]),
                    ),
                )
                return "refreshed"
            pending_update = connection.execute(
                """
                SELECT id FROM email_outbox
                WHERE kind = 'recommendation-update'
                  AND dedupe_key LIKE ? AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                (f"recommendation-update:{plan_id}:%",),
            ).fetchone()
            if pending_update is not None:
                connection.execute(
                    """
                    UPDATE email_outbox
                    SET subject = ?, text_body = ?, html_body = ?, attempts = 0,
                        last_error = '', next_attempt_at = ?, expires_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        f"[更新] {subject}",
                        "本邮件为计划修改后的最新版，请以本邮件为准。\n\n" + text_body,
                        html_body,
                        changed_at.isoformat(),
                        expires_at.isoformat(),
                        int(pending_update["id"]),
                    ),
                )
                return "queued"
            self._insert_outbox(
                connection,
                dedupe_key=f"recommendation-update:{plan_id}:{uuid.uuid4().hex}",
                kind="recommendation-update",
                subject=f"[更新] {subject}",
                text_body="本邮件为计划修改后的最新版，请以本邮件为准。\n\n" + text_body,
                html_body=html_body,
                created_at=changed_at,
                priority=110,
                expires_at=expires_at,
                not_before=changed_at,
            )
            return "queued"

    def pending_plans(self) -> list[StoredPlan]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM plans
                WHERE status = 'pending' AND delivery_status = 'sent'
                ORDER BY created_at
                """
            ).fetchall()
            return [self._load_plan(connection, row) for row in rows]

    def get_plan(self, plan_id: str) -> StoredPlan | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            return self._load_plan(connection, row) if row else None

    def recent_plans(self, limit: int = 100) -> list[StoredPlan]:
        safe_limit = max(1, min(int(limit), 500))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
            return [self._load_plan(connection, row) for row in rows]

    def _load_plan(self, connection: sqlite3.Connection, row: sqlite3.Row) -> StoredPlan:
        leg_rows = connection.execute(
            "SELECT * FROM plan_legs WHERE plan_id = ? ORDER BY position", (row["plan_id"],)
        ).fetchall()
        option_rows = connection.execute(
            """
            SELECT * FROM plan_leg_options
            WHERE plan_id = ? ORDER BY match_id, rowid
            """,
            (row["plan_id"],),
        ).fetchall()
        options_by_match: dict[str, list[ScoreOption]] = {}
        for option in option_rows:
            options_by_match.setdefault(str(option["match_id"]), []).append(
                ScoreOption(
                    code=str(option["option_code"]),
                    label=str(option["option_label"]),
                    odds=Decimal(option["odds"]),
                    probability=Decimal(option["probability"]),
                    is_other=bool(option["is_other"]),
                )
            )
        legs = tuple(
            StoredLeg(
                position=int(leg["position"]),
                match_id=leg["match_id"],
                match_num=leg["match_num"],
                business_date=leg["business_date"],
                league=leg["league"],
                home=leg["home"],
                away=leg["away"],
                start_at=datetime.fromisoformat(leg["start_at"]),
                snapshot_fetched_at=(
                    datetime.fromisoformat(leg["snapshot_fetched_at"])
                    if leg["snapshot_fetched_at"]
                    else None
                ),
                score_code=leg["score_code"],
                score_label=leg["score_label"],
                odds=Decimal(leg["odds"]),
                probability=Decimal(leg["probability"]),
                result_status=ResultStatus(leg["result_status"]),
                result_home=leg["result_home"],
                result_away=leg["result_away"],
                official_status=leg["official_status"],
                options=tuple(options_by_match.get(str(leg["match_id"]), ())),
            )
            for leg in leg_rows
        )
        suggestion_rows = connection.execute(
            """
            SELECT * FROM plan_ai_suggestions
            WHERE plan_id = ? ORDER BY rowid
            """,
            (row["plan_id"],),
        ).fetchall()
        ai_suggestions = tuple(
            StoredAISuggestion(
                match_id=str(item["match_id"]),
                option_code=str(item["option_code"]),
                option_label=str(item["option_label"]),
                odds=Decimal(item["odds"]),
                probability=Decimal(item["probability"]),
                reason=str(item["reason"]),
            )
            for item in suggestion_rows
        )
        return StoredPlan(
            plan_id=row["plan_id"],
            business_date=row["business_date"],
            recommendation_date=row["recommendation_date"],
            issue_date=row["issue_date"],
            pass_size=int(row["pass_size"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            status=PlanStatus(row["status"]),
            delivery_status=row["delivery_status"],
            stake=_money(row["stake_cents"]),
            combined_odds=Decimal(row["combined_odds"]),
            joint_probability=Decimal(row["joint_probability"]),
            gross_prize=_money(row["gross_prize_cents"]),
            tax=_money(row["tax_cents"]),
            net_prize=_money(row["net_prize_cents"]),
            settled_gross_prize=(
                _money(row["settled_gross_prize_cents"])
                if row["settled_gross_prize_cents"] is not None
                else None
            ),
            settled_tax=(
                _money(row["settled_tax_cents"])
                if row["settled_tax_cents"] is not None
                else None
            ),
            settled_net_prize=(
                _money(row["settled_net_prize_cents"])
                if row["settled_net_prize_cents"] is not None
                else None
            ),
            net_profit=_money(row["net_profit_cents"]) if row["net_profit_cents"] is not None else None,
            strategy_version=row["strategy_version"],
            settled_at=(datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None),
            legs=legs,
            market=MarketType(row["market"] if "market" in row.keys() and row["market"] else "crs"),
            ai_summary=str(row["ai_summary"]) if "ai_summary" in row.keys() and row["ai_summary"] else "",
            ai_suggestions=ai_suggestions,
        )

    def update_ai_summary(self, plan_id: str, ai_summary: str) -> bool:
        """Update the AI analysis summary for a plan."""
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM plan_ai_suggestions WHERE plan_id = ?", (plan_id,)
            )
            cursor = connection.execute(
                "UPDATE plans SET ai_summary = ? WHERE plan_id = ?",
                (ai_summary, plan_id),
            )
            return cursor.rowcount > 0

    def replace_plan_leg_options(self, plan_id: str, matches: Sequence[Match]) -> int:
        """Refresh editable choices for plan legs from trusted provider data."""
        with self.connect() as connection:
            plan = connection.execute(
                "SELECT market FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                return 0
            market = MarketType(plan["market"])
            leg_rows = connection.execute(
                """
                SELECT match_id, score_code, score_label, odds, probability
                FROM plan_legs WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchall()
            legs = {str(item["match_id"]): item for item in leg_rows}
            refreshed = 0
            for match in matches:
                match_id = str(match.match_id)
                selected = legs.get(match_id)
                if selected is None:
                    continue
                market_options = (
                    match.score_options if market is MarketType.CRS else match.had_options
                )
                options_by_code = {option.code: option for option in market_options}
                if not options_by_code:
                    continue
                options_by_code.setdefault(
                    str(selected["score_code"]),
                    ScoreOption(
                        code=str(selected["score_code"]),
                        label=str(selected["score_label"]),
                        odds=Decimal(selected["odds"]),
                        probability=Decimal(selected["probability"]),
                    ),
                )
                connection.execute(
                    "DELETE FROM plan_leg_options WHERE plan_id = ? AND match_id = ?",
                    (plan_id, match_id),
                )
                for option in options_by_code.values():
                    connection.execute(
                        """
                        INSERT INTO plan_leg_options
                            (plan_id, match_id, option_code, option_label, odds,
                             probability, is_other)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plan_id,
                            match_id,
                            option.code,
                            option.label,
                            str(option.odds),
                            str(option.probability),
                            int(option.is_other),
                        ),
                    )
                refreshed += 1
            return refreshed

    def update_ai_analysis(
        self,
        plan_id: str,
        ai_summary: str,
        suggestions: Sequence[tuple[str, str, str]],
    ) -> bool:
        """Persist a validated suggestion for every leg using trusted option data."""
        with self.connect() as connection:
            leg_ids = {
                str(item["match_id"])
                for item in connection.execute(
                    "SELECT match_id FROM plan_legs WHERE plan_id = ?", (plan_id,)
                )
            }
            if not leg_ids:
                return False
            suggestion_map: dict[str, tuple[str, str]] = {}
            for match_id, option_code, reason in suggestions:
                match_key = str(match_id)
                if match_key in suggestion_map:
                    raise ValueError(f"duplicate AI suggestion for match {match_key}")
                suggestion_map[match_key] = (str(option_code), str(reason)[:500])
            if set(suggestion_map) != leg_ids:
                raise ValueError("AI suggestions must cover every plan leg")

            trusted: list[tuple[str, sqlite3.Row, str]] = []
            for match_id, (option_code, reason) in suggestion_map.items():
                option = connection.execute(
                    """
                    SELECT option_code, option_label, odds, probability
                    FROM plan_leg_options
                    WHERE plan_id = ? AND match_id = ? AND option_code = ?
                    """,
                    (plan_id, match_id, option_code),
                ).fetchone()
                if option is None:
                    raise ValueError(f"unavailable AI option for match {match_id}")
                trusted.append((match_id, option, reason))

            connection.execute(
                "DELETE FROM plan_ai_suggestions WHERE plan_id = ?", (plan_id,)
            )
            for match_id, option, reason in trusted:
                connection.execute(
                    """
                    INSERT INTO plan_ai_suggestions
                        (plan_id, match_id, option_code, option_label, odds,
                         probability, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id,
                        match_id,
                        option["option_code"],
                        option["option_label"],
                        option["odds"],
                        option["probability"],
                        reason,
                    ),
                )
            cursor = connection.execute(
                "UPDATE plans SET ai_summary = ? WHERE plan_id = ?",
                (str(ai_summary)[:4000], plan_id),
            )
            return cursor.rowcount > 0

    def delete_plan(self, plan_id: str) -> bool:
        """Delete a plan and all its legs. Returns True if deleted."""
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM email_outbox
                WHERE dedupe_key IN (?, ?) OR dedupe_key LIKE ?
                """,
                (
                    f"recommendation:{plan_id}",
                    f"settlement:{plan_id}",
                    f"recommendation-update:{plan_id}:%",
                ),
            )
            connection.execute("DELETE FROM recommendation_days WHERE plan_id = ?", (plan_id,))
            cursor = connection.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            return cursor.rowcount > 0

    def delete_plan_leg(self, plan_id: str, match_id: str) -> bool:
        """Delete a single leg from a plan. Returns True if deleted."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM plan_legs WHERE plan_id = ? AND match_id = ?",
                (plan_id, match_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "DELETE FROM plan_leg_options WHERE plan_id = ? AND match_id = ?",
                    (plan_id, match_id),
                )
                connection.execute(
                    "DELETE FROM plan_ai_suggestions WHERE plan_id = ? AND match_id = ?",
                    (plan_id, match_id),
                )
            return cursor.rowcount > 0

    def update_plan_leg_option(self, plan_id: str, match_id: str, option_code: str) -> bool:
        """Replace a stored leg pick with one trusted option and recalculate the plan."""
        with self.connect() as connection:
            option = connection.execute(
                """
                SELECT option_label, odds, probability
                FROM plan_leg_options
                WHERE plan_id = ? AND match_id = ? AND option_code = ?
                """,
                (plan_id, match_id, option_code),
            ).fetchone()
            if option is None:
                return False
            cursor = connection.execute(
                """
                UPDATE plan_legs
                SET score_code = ?, score_label = ?, odds = ?, probability = ?
                WHERE plan_id = ? AND match_id = ?
                """,
                (
                    option_code,
                    option["option_label"],
                    option["odds"],
                    option["probability"],
                    plan_id,
                    match_id,
                ),
            )
            if cursor.rowcount == 0:
                return False
        if not self.update_plan_after_leg_delete(plan_id, clear_ai=False):
            return False
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM plan_ai_suggestions WHERE plan_id = ? AND match_id = ?",
                (plan_id, match_id),
            )
        return True

    def update_plan_after_leg_delete(self, plan_id: str, *, clear_ai: bool = True) -> bool:
        """Recalculate plan combined odds, probability, and prize after a leg is deleted."""
        with self.connect() as connection:
            plan_row = connection.execute(
                "SELECT market, status, stake_cents FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan_row is None:
                return False
            if clear_ai:
                connection.execute(
                    "DELETE FROM plan_ai_suggestions WHERE plan_id = ?", (plan_id,)
                )
            leg_rows = connection.execute(
                """
                SELECT odds, probability, score_code, score_label, result_status,
                       result_home, result_away
                FROM plan_legs WHERE plan_id = ? ORDER BY position
                """,
                (plan_id,),
            ).fetchall()
            if not leg_rows:
                connection.execute("DELETE FROM recommendation_days WHERE plan_id = ?", (plan_id,))
                connection.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
                return True
            combined_odds = Decimal("1")
            joint_probability = Decimal("1")
            for leg in leg_rows:
                combined_odds *= Decimal(leg["odds"])
                joint_probability *= Decimal(leg["probability"])
            pass_size = len(leg_rows)
            gross, tax, net = calculate_prize(combined_odds, active_legs=pass_size)
            settlement_values: tuple[object, ...]
            if plan_row["status"] == PlanStatus.PENDING.value:
                settlement_values = (
                    PlanStatus.PENDING.value, None, None, None, None, None
                )
            elif any(row["result_status"] == ResultStatus.PENDING.value for row in leg_rows):
                settlement_values = (
                    PlanStatus.PENDING.value, None, None, None, None, None
                )
            else:
                market = MarketType(plan_row["market"])

                def leg_hit(row: sqlite3.Row) -> bool:
                    if row["result_home"] is None or row["result_away"] is None:
                        return False
                    home = int(row["result_home"])
                    away = int(row["result_away"])
                    if market is MarketType.HAD:
                        actual = "h" if home > away else ("a" if home < away else "d")
                        return str(row["score_code"]).lower() == actual
                    code_match = re.fullmatch(r"s(\d{2})s(\d{2})", str(row["score_code"]))
                    if code_match:
                        return (int(code_match.group(1)), int(code_match.group(2))) == (home, away)
                    label_match = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", str(row["score_label"]))
                    return bool(
                        label_match
                        and (int(label_match.group(1)), int(label_match.group(2))) == (home, away)
                    )

                final_rows = [
                    row for row in leg_rows if row["result_status"] == ResultStatus.FINAL.value
                ]
                lost = any(not leg_hit(row) for row in final_rows)
                if lost:
                    settled_status = PlanStatus.LOST
                    settled_gross = settled_tax = settled_net = Decimal("0.00")
                elif not final_rows:
                    settled_status = PlanStatus.VOID
                    settled_gross = settled_net = _money(int(plan_row["stake_cents"]))
                    settled_tax = Decimal("0.00")
                else:
                    settled_status = PlanStatus.WON
                    settled_odds = Decimal("1")
                    for row in final_rows:
                        settled_odds *= Decimal(row["odds"])
                    settled_gross, settled_tax, settled_net = calculate_prize(
                        settled_odds, active_legs=len(final_rows)
                    )
                stake = _money(int(plan_row["stake_cents"]))
                settlement_values = (
                    settled_status.value,
                    _cents(settled_gross),
                    _cents(settled_tax),
                    _cents(settled_net),
                    _cents((settled_net - stake).quantize(Decimal("0.00"))),
                    plan_id,
                )

            connection.execute(
                """
                UPDATE plans
                SET pass_size = ?, combined_odds = ?, joint_probability = ?,
                    gross_prize_cents = ?, tax_cents = ?, net_prize_cents = ?,
                    ai_summary = CASE WHEN ? THEN '' ELSE ai_summary END
                WHERE plan_id = ?
                """,
                (
                    pass_size,
                    str(combined_odds),
                    str(joint_probability),
                    _cents(gross),
                    _cents(tax),
                    _cents(net),
                    int(clear_ai),
                    plan_id,
                ),
            )
            if settlement_values[0] == PlanStatus.PENDING.value:
                connection.execute(
                    """
                    UPDATE plans SET status = ?, settled_at = NULL,
                        settled_gross_prize_cents = ?, settled_tax_cents = ?,
                        settled_net_prize_cents = ?, net_profit_cents = ?
                    WHERE plan_id = ?
                    """,
                    (*settlement_values[:5], plan_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE plans SET status = ?, settled_gross_prize_cents = ?,
                        settled_tax_cents = ?, settled_net_prize_cents = ?, net_profit_cents = ?
                    WHERE plan_id = ?
                    """,
                    settlement_values,
                )
            return True

    def update_leg_results(self, plan_id: str, results: dict[str, MatchResult]) -> None:
        with self.connect() as connection:
            for match_id, result in results.items():
                connection.execute(
                    """
                    UPDATE plan_legs
                    SET result_status = ?, result_home = ?, result_away = ?, official_status = ?
                    WHERE plan_id = ? AND match_id = ?
                    """,
                    (
                        result.status.value,
                        result.home_score,
                        result.away_score,
                        result.official_status,
                        plan_id,
                        match_id,
                    ),
                )

    def settle_plan_with_mail(
        self,
        settlement: Settlement,
        *,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE plans
                SET status = ?, settled_at = ?, settled_gross_prize_cents = ?, settled_tax_cents = ?,
                    settled_net_prize_cents = ?, net_profit_cents = ?
                WHERE plan_id = ? AND status = 'pending'
                """,
                (
                    settlement.status.value,
                    settlement.settled_at.isoformat(),
                    _cents(settlement.gross_prize),
                    _cents(settlement.tax),
                    _cents(settlement.net_prize),
                    _cents(settlement.net_profit),
                    settlement.plan_id,
                ),
            )
            if cursor.rowcount == 0:
                return False
            for result in settlement.leg_results:
                connection.execute(
                    """
                    UPDATE plan_legs
                    SET result_status = ?, result_home = ?, result_away = ?, official_status = ?
                    WHERE plan_id = ? AND match_id = ?
                    """,
                    (
                        result.status.value,
                        result.home_score,
                        result.away_score,
                        result.official_status,
                        settlement.plan_id,
                        result.match_id,
                    ),
                )
            self._insert_outbox(
                connection,
                dedupe_key=f"settlement:{settlement.plan_id}",
                kind="settlement",
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                created_at=settlement.settled_at,
            )
            return True

    def claim_due_emails(
        self,
        now: datetime,
        *,
        limit: int = 20,
        max_attempts: int = 5,
        lease_seconds: int = 300,
    ) -> list[sqlite3.Row]:
        """Atomically lease due messages so concurrent workers cannot both send them."""
        token = uuid.uuid4().hex
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE email_outbox
                SET status = 'expired', expired_at = ?, last_error = 'recommendation deadline passed',
                    claim_token = NULL, claimed_until = NULL, next_attempt_at = NULL
                WHERE kind = 'recommendation'
                  AND status IN ('pending', 'sending')
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            connection.execute(
                """
                UPDATE plans
                SET delivery_status = 'expired'
                WHERE delivery_status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM email_outbox
                    WHERE email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      AND email_outbox.status = 'expired'
                  )
                """
            )
            connection.execute(
                """
                UPDATE email_outbox
                SET status = 'dead', claim_token = NULL, claimed_until = NULL
                WHERE status = 'sending' AND claimed_until <= ? AND attempts >= ?
                """,
                (now.isoformat(), max_attempts),
            )
            connection.execute(
                """
                UPDATE plans
                SET delivery_status = 'failed'
                WHERE delivery_status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM email_outbox
                    WHERE email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      AND email_outbox.status = 'dead'
                  )
                """
            )
            rows = connection.execute(
                """
                SELECT id FROM email_outbox
                WHERE attempts < ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (
                    status = 'pending'
                    OR (status = 'sending' AND claimed_until <= ?)
                  )
                ORDER BY priority DESC, expires_at IS NULL, expires_at, id LIMIT ?
                """,
                (max_attempts, now.isoformat(), now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE email_outbox
                SET status = 'sending', claim_token = ?, claimed_until = ?,
                    attempts = attempts + 1, next_attempt_at = NULL
                WHERE id IN ({placeholders})
                """,
                (token, lease_until.isoformat(), *ids),
            )
            return connection.execute(
                "SELECT * FROM email_outbox WHERE claim_token = ? ORDER BY priority DESC, id", (token,)
            ).fetchall()

    def mark_email_sent(self, email_id: int, claim_token: str, sent_at: datetime) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE email_outbox
                SET status = 'sent', sent_at = ?, last_error = '', claim_token = NULL,
                    claimed_until = NULL, next_attempt_at = NULL
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (sent_at.isoformat(), email_id, claim_token),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE plans
                    SET delivery_status = 'sent'
                    WHERE delivery_status = 'queued'
                      AND EXISTS (
                        SELECT 1 FROM email_outbox
                        WHERE email_outbox.id = ?
                          AND email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      )
                    """,
                    (email_id,),
                )

    def mark_email_previewed(self, email_id: int, claim_token: str, previewed_at: datetime) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE email_outbox
                SET status = 'previewed', sent_at = ?, last_error = '', claim_token = NULL,
                    claimed_until = NULL, next_attempt_at = NULL
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (previewed_at.isoformat(), email_id, claim_token),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE plans
                    SET delivery_status = 'previewed'
                    WHERE delivery_status = 'queued'
                      AND EXISTS (
                        SELECT 1 FROM email_outbox
                        WHERE email_outbox.id = ?
                          AND email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      )
                    """,
                    (email_id,),
                )

    def mark_email_expired(
        self,
        email_id: int,
        claim_token: str,
        expired_at: datetime,
        error: str = "recommendation deadline passed",
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE email_outbox
                SET status = 'expired', expired_at = ?, last_error = ?, claim_token = NULL,
                    claimed_until = NULL, next_attempt_at = NULL
                WHERE id = ? AND status = 'sending' AND claim_token = ?
                """,
                (expired_at.isoformat(), error[:1000], email_id, claim_token),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE plans
                    SET delivery_status = 'expired'
                    WHERE delivery_status = 'queued'
                      AND EXISTS (
                        SELECT 1 FROM email_outbox
                        WHERE email_outbox.id = ?
                          AND email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      )
                    """,
                    (email_id,),
                )

    def mark_email_failed(
        self,
        email_id: int,
        claim_token: str,
        error: str,
        now: datetime,
        *,
        max_attempts: int = 5,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempts, kind, expires_at FROM email_outbox WHERE id = ? AND claim_token = ?",
                (email_id, claim_token),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"])
            dead = attempts >= max_attempts
            retry_at = now + timedelta(seconds=min(3600, 60 * (2 ** max(0, attempts - 1))))
            expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
            expired = row["kind"] == "recommendation" and expires_at is not None and retry_at >= expires_at
            connection.execute(
                """
                UPDATE email_outbox
                SET status = ?, last_error = ?, claim_token = NULL, claimed_until = NULL,
                    next_attempt_at = ?, expired_at = ?
                WHERE id = ? AND claim_token = ?
                """,
                (
                    "expired" if expired else ("dead" if dead else "pending"),
                    error[:1000],
                    None if dead or expired else retry_at.isoformat(),
                    now.isoformat() if expired else None,
                    email_id,
                    claim_token,
                ),
            )
            if row["kind"] == "recommendation" and (dead or expired):
                connection.execute(
                    """
                    UPDATE plans
                    SET delivery_status = ?
                    WHERE delivery_status = 'queued'
                      AND EXISTS (
                        SELECT 1 FROM email_outbox
                        WHERE email_outbox.id = ?
                          AND email_outbox.dedupe_key = 'recommendation:' || plans.plan_id
                      )
                    """,
                    ("expired" if expired else "failed", email_id),
                )

    def record_job(self, job_name: str, started_at: datetime, finished_at: datetime, status: str, detail: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_runs (job_name, started_at, finished_at, status, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_name, started_at.isoformat(), finished_at.isoformat(), status, detail[:4000]),
            )

    def summary(self) -> dict[str, int | str]:
        with self.connect() as connection:
            plan_row = connection.execute(
                """
                SELECT SUM(CASE WHEN delivery_status = 'sent' THEN 1 ELSE 0 END) AS total,
                       SUM(CASE WHEN delivery_status = 'sent' AND status = 'pending' THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN delivery_status = 'sent' AND status = 'won' THEN 1 ELSE 0 END) AS won,
                       SUM(CASE WHEN delivery_status = 'sent' AND status = 'lost' THEN 1 ELSE 0 END) AS lost,
                       SUM(CASE WHEN delivery_status = 'sent' AND status = 'void' THEN 1 ELSE 0 END) AS voided,
                       SUM(CASE WHEN delivery_status != 'sent' THEN 1 ELSE 0 END) AS undelivered,
                       SUM(CASE WHEN delivery_status = 'sent' AND pass_size = 2 THEN 1 ELSE 0 END) AS twofold,
                       SUM(CASE WHEN delivery_status = 'sent' AND pass_size = 3 THEN 1 ELSE 0 END) AS threefold,
                       SUM(CASE WHEN delivery_status = 'sent' AND pass_size = 4 THEN 1 ELSE 0 END) AS fourfold,
                       SUM(CASE WHEN delivery_status = 'sent' AND market = 'crs' THEN 1 ELSE 0 END) AS crs_total,
                       SUM(CASE WHEN delivery_status = 'sent' AND market = 'had' THEN 1 ELSE 0 END) AS had_total,
                       SUM(CASE WHEN delivery_status = 'sent' AND market = 'had' AND pass_size = 4 THEN 1 ELSE 0 END) AS had_fourfold,
                       SUM(CASE WHEN delivery_status = 'sent' AND market = 'had' AND pass_size = 5 THEN 1 ELSE 0 END) AS had_fivefold,
                       SUM(CASE WHEN delivery_status = 'sent' AND market = 'had' AND pass_size = 6 THEN 1 ELSE 0 END) AS had_sixfold,
                       COALESCE(SUM(CASE WHEN delivery_status = 'sent' THEN stake_cents ELSE 0 END), 0) AS stake_cents,
                       COALESCE(SUM(CASE WHEN delivery_status = 'sent' AND status != 'pending' THEN stake_cents ELSE 0 END), 0) AS settled_stake_cents,
                       COALESCE(SUM(CASE WHEN delivery_status = 'sent' AND status != 'pending' THEN settled_net_prize_cents ELSE 0 END), 0) AS return_cents,
                       COALESCE(SUM(CASE WHEN delivery_status = 'sent' AND status != 'pending' THEN net_profit_cents ELSE 0 END), 0) AS profit_cents
                FROM plans
                """
            ).fetchone()
            mail_row = connection.execute(
                """
                SELECT SUM(CASE WHEN status IN ('pending', 'sending') THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                       SUM(CASE WHEN status = 'previewed' THEN 1 ELSE 0 END) AS previewed,
                       SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead,
                       SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS expired
                FROM email_outbox
                """
            ).fetchone()
            return {
                "plans_total": int(plan_row["total"] or 0),
                "plans_pending": int(plan_row["pending"] or 0),
                "plans_won": int(plan_row["won"] or 0),
                "plans_lost": int(plan_row["lost"] or 0),
                "plans_void": int(plan_row["voided"] or 0),
                "plans_undelivered": int(plan_row["undelivered"] or 0),
                "plans_twofold": int(plan_row["twofold"] or 0),
                "plans_threefold": int(plan_row["threefold"] or 0),
                "plans_fourfold": int(plan_row["fourfold"] or 0),
                "plans_crs": int(plan_row["crs_total"] or 0),
                "plans_had": int(plan_row["had_total"] or 0),
                "plans_had_fourfold": int(plan_row["had_fourfold"] or 0),
                "plans_had_fivefold": int(plan_row["had_fivefold"] or 0),
                "plans_had_sixfold": int(plan_row["had_sixfold"] or 0),
                "baseline_stake": str(_money(plan_row["stake_cents"] or 0)),
                "settled_stake": str(_money(plan_row["settled_stake_cents"] or 0)),
                "baseline_return": str(_money(plan_row["return_cents"] or 0)),
                "baseline_profit": str(_money(plan_row["profit_cents"] or 0)),
                "emails_pending": int(mail_row["pending"] or 0),
                "emails_sent": int(mail_row["sent"] or 0),
                "emails_previewed": int(mail_row["previewed"] or 0),
                "emails_dead": int(mail_row["dead"] or 0),
                "emails_expired": int(mail_row["expired"] or 0),
            }
