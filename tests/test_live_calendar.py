"""Verify date boundaries and retained-time totals in live calendars."""

import unittest
from datetime import datetime

from astrbot_plugin_bilibili_parser.live import CHINA
from astrbot_plugin_bilibili_parser.live_calendar import build_calendars


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=CHINA).timestamp()


def row(start, end, identity=1, ongoing=False):
    return {
        "id": identity,
        "title": "测试直播",
        "start_ts": ts(start),
        "end_ts": None if ongoing else ts(end),
        "observed_end_ts": ts(end),
    }


class CalendarTests(unittest.TestCase):
    def test_midnight_and_month_boundary_split_without_double_count(self):
        months = build_calendars(
            [row("2026-08-31 23:00", "2026-09-01 02:00")], ts("2026-09-02 12:00")
        )
        self.assertEqual([m["month"] for m in months], ["2026-09", "2026-08"])
        self.assertEqual([m["seconds"] for m in months], [7200, 3600])
        self.assertEqual([m["count"] for m in months], [1, 1])
        event = next(d for d in months[1]["days"] if d["day"] == 31)["events"][0]
        self.assertEqual(event["time"], "23:00–24:00")

    def test_retention_boundary_and_ongoing_observed_end(self):
        records = [row("2026-08-01 00:00", "2026-09-26 10:00", ongoing=True)]
        months = build_calendars(records, ts("2026-09-26 12:00"))
        self.assertEqual(sum(m["seconds"] for m in months), 30 * 86400 - 7200)
        first = next(d for d in months[-1]["days"] if d["day"] == 27)
        self.assertTrue(first["partial"])
        self.assertEqual(first["events"][0]["time"], "12:00–24:00")
        last = next(d for d in months[0]["days"] if d["day"] == 26)
        self.assertEqual(last["seconds"], 10 * 3600)

    def test_leap_year_week_alignment_and_three_month_window(self):
        months = build_calendars([], ts("2025-03-01 12:00"))
        self.assertEqual(
            [m["month"] for m in months], ["2025-03", "2025-02", "2025-01"]
        )
        leap = build_calendars([], ts("2024-02-29 12:00"))[0]
        self.assertEqual(sum(bool(d["day"]) for d in leap["days"]), 29)
        self.assertEqual([d["day"] for d in leap["days"][:4]], [0, 0, 0, 1])

    def test_multiple_sessions_same_day_and_zero_duration(self):
        records = [
            row("2026-09-25 10:00", "2026-09-25 11:00", 1),
            row("2026-09-25 12:00", "2026-09-25 14:00", 2),
            row("2026-09-26 12:00", "2026-09-26 12:00", 3, ongoing=True),
        ]
        month = build_calendars(records, ts("2026-09-26 12:00"))[0]
        self.assertEqual(month["count"], 3)
        self.assertEqual(month["active_days"], 2)
        self.assertEqual(month["seconds"], 10800)
        day = next(d for d in month["days"] if d["day"] == 25)
        self.assertEqual(day["level"], 2)
        self.assertEqual(day["duration"], "3时00分")

    def test_midnight_end_does_not_create_next_day_entry(self):
        month = build_calendars(
            [row("2026-09-24 23:00", "2026-09-25 00:00")], ts("2026-09-26 12:00")
        )[0]
        self.assertEqual(month["active_days"], 1)
        day = next(d for d in month["days"] if d["day"] == 25)
        self.assertEqual(day["events"], [])

    def test_empty_future_and_expired_days_are_distinct(self):
        month = build_calendars([], ts("2026-09-26 12:00"))[0]
        self.assertEqual(month["count"], 0)
        today = next(d for d in month["days"] if d["day"] == 26)
        future = next(d for d in month["days"] if d["day"] == 27)
        self.assertTrue(today["today"] and today["available"])
        self.assertFalse(future["available"])
