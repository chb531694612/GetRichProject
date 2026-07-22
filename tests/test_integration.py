from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from score_fourfold.database import Database
from score_fourfold.mail import Mailer
from score_fourfold.provider import JsonProvider
from score_fourfold.service import ScoreFourfoldService

from .helpers import make_settings


class AcceptedPreviewMailer(Mailer):
    """Write the preview artifacts while simulating an SMTP-accepted message."""

    def send(self, **kwargs) -> str:
        super().send(**kwargs)
        return "sent"


class FullFlowTests(unittest.TestCase):
    def test_recommend_mail_and_settle_flow(self):
        tmp_path = Path("data")
        data_file = tmp_path / "test_integration_data.json"
        database_path = tmp_path / "test_integration.db"
        preview_files = [tmp_path / f"{email_id:06d}.{extension}" for email_id in (1, 2) for extension in ("html", "txt")]
        for path in [data_file, database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"), *preview_files]:
            path.unlink(missing_ok=True)

        now = datetime(2026, 7, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        matches = []
        results = []
        for index in range(1, 6):
            match_id = str(2000 + index)
            matches.append(
                {
                    "match_id": match_id,
                    "match_num": f"周二{index:03d}",
                    "business_date": "2026-07-14",
                    "league": {"abbName": f"联赛{index % 3}"},
                    "home": {"abbName": f"主队{index}"},
                    "away": {"abbName": f"客队{index}"},
                    "start_at": f"2026-07-14T{18 + index:02d}:00:00+08:00",
                    "markets": {
                        "crs": {
                            "updateTime": "2026-07-14 11:55:00",
                            "outcomes": [
                                {"code": "s01s00", "labelZh": "1:0", "odds": 2, "noVigProb": 0.2},
                                {"code": "s01s01", "labelZh": "1:1", "odds": 3, "noVigProb": 0.15},
                                {"code": "s1sh", "labelZh": "胜其它", "odds": 4, "noVigProb": 0.25},
                            ],
                        }
                    },
                }
            )
            results.append({"match_id": match_id, "status": "finished", "finalScore": "1:0"})
        data_file.write_text(json.dumps({"matches": matches, "results": results}, ensure_ascii=False), encoding="utf-8")
        settings = make_settings(
            tmp_path,
            json_data_file=data_file,
            database_path=database_path,
            mail_preview_dir=tmp_path,
            had_enabled=False,
        )
        database = Database(settings.database_path)
        database.initialize()
        clock = lambda: now
        service = ScoreFourfoldService(
            settings,
            database,
            JsonProvider(settings),
            AcceptedPreviewMailer(settings, clock=clock),
            clock=clock,
        )

        recommendation_outcome = service.recommend(now)
        self.assertEqual(recommendation_outcome.status, "created")
        sent = service.send_mail(now)
        self.assertIn("发送3封", sent.detail)
        self.assertTrue((tmp_path / "000001.html").exists())

        settlement_at = now + timedelta(days=1)
        settlement_outcome = service.settle(settlement_at)
        self.assertIn("完成3张", settlement_outcome.detail)
        service.send_mail(settlement_at)
        summary = database.summary()
        self.assertEqual(summary["plans_won"], 3)
        self.assertEqual(summary["baseline_profit"], "50.00")
        self.assertTrue((tmp_path / "000002.html").exists())

        for path in [data_file, database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"), *preview_files]:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
