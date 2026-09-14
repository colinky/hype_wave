from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from hype_db_common import load_sync_config, reference_period_for_date
import sync_all


WEEKLY = {
    "job_name": "Weekly-Hot-100", "service": "ytmusic", "frequency": "weekly",
    "list_type": "chart", "schedule": "Monday", "enabled": True,
}


class YouTubeMondayScheduleTests(unittest.TestCase):
    def test_config_and_dispatch_allow_only_monday_not_retry_days(self) -> None:
        configured = next(task for task in load_sync_config() if task["job_name"] == "Weekly-Hot-100")
        self.assertEqual(configured["schedule"], "Monday")
        monday = datetime(2026, 9, 7, 12, tzinfo=sync_all.KST)
        for offset in range(14):
            now = monday + timedelta(days=offset)
            with self.subTest(day=now.date()):
                self.assertEqual(sync_all.task_enabled(configured, now), offset % 7 == 0)

    def test_kst_midnight_edges_use_korean_day_not_utc_day(self) -> None:
        for instant, expected in (
            ("2026-09-06T14:59:59+00:00", False),
            ("2026-09-06T15:00:00+00:00", True),
            ("2026-09-07T14:59:59+00:00", True),
            ("2026-09-07T15:00:00+00:00", False),
        ):
            with self.subTest(instant=instant), patch.object(sync_all, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(instant)
                self.assertEqual(sync_all.task_enabled(WEEKLY), expected)

    def test_other_services_and_disabled_tasks_keep_their_schedule(self) -> None:
        monday = datetime(2026, 9, 7, 12, tzinfo=sync_all.KST)
        friday = {"job_name": "Hot-Hits-Korea", "service": "spotify", "schedule": "Friday"}
        daily = {"job_name": "KR-Top-100", "service": "apple"}
        for offset in range(7):
            now = monday + timedelta(days=offset)
            with self.subTest(day=now.date()):
                self.assertEqual(sync_all.task_enabled(friday, now), offset == 4)
                self.assertTrue(sync_all.task_enabled(daily, now))
        self.assertFalse(sync_all.task_enabled({**WEEKLY, "enabled": False}, monday))

    def test_stored_period_still_comes_from_fetched_thursday_end(self) -> None:
        for end, period in (
            ("2026-08-27", "2026-W35"), ("2026-09-03", "2026-W36"),
            ("2026-12-31", "2026-W53"),
        ):
            with self.subTest(end=end):
                self.assertEqual(reference_period_for_date("Weekly-Hot-100", end), period)


if __name__ == "__main__":
    unittest.main()
