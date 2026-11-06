"""Build timezone-stable monthly calendars from retained live sessions."""

import calendar
from datetime import datetime, timedelta

from .live import CHINA, duration


def build_calendars(rows, now):
    """Split sessions at Beijing midnight and clip totals to the retention window.

    Args:
        rows: History records with numeric start and observed-end timestamps.
        now: Current Unix timestamp.

    Returns:
        Newest-first monthly calendars, including empty months in the last 30 days.
    """
    cutoff = now - 30 * 86400
    today = datetime.fromtimestamp(now, CHINA).date()
    first_day = datetime.fromtimestamp(cutoff, CHINA).date()
    months = []
    year, month = today.year, today.month
    while (year, month) >= (first_day.year, first_day.month):
        days = []
        session_ids = set()
        total = 0
        for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
            for number in week:
                if not number:
                    days.append({"day": 0})
                    continue
                date = datetime(year, month, number, tzinfo=CHINA)
                start = date.timestamp()
                stop = (date + timedelta(days=1)).timestamp()
                events = []
                seconds = 0
                for row in rows:
                    a = max(row["start_ts"], start, cutoff)
                    b = min(row["observed_end_ts"], stop, now)
                    # A just-started session is visible even before its first full second.
                    just_started = (
                        a == b == row["start_ts"]
                        and start <= a < stop
                        and cutoff <= a <= now
                    )
                    if b <= a and not just_started:
                        continue
                    value = max(0, b - a)
                    seconds += value
                    session_ids.add(row["id"])
                    end_label = (
                        "24:00"
                        if b == stop
                        else datetime.fromtimestamp(b, CHINA).strftime("%H:%M")
                    )
                    events.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "time": datetime.fromtimestamp(a, CHINA).strftime("%H:%M")
                            + "–"
                            + end_label,
                            "seconds": value,
                            "ongoing": row["end_ts"] is None,
                        }
                    )
                events.sort(key=lambda event: event["time"])
                total += seconds
                minutes = int(seconds) // 60
                short = (
                    f"{minutes // 60}时{minutes % 60:02d}分" if minutes else "不足1分"
                )
                days.append(
                    {
                        "day": number,
                        "date": date.strftime("%Y-%m-%d"),
                        "today": date.date() == today,
                        "available": first_day <= date.date() <= today,
                        "partial": date.date() == first_day,
                        "seconds": seconds,
                        "duration": short,
                        "events": events,
                        "level": 0
                        if not events
                        else 1
                        if seconds < 7200
                        else 2
                        if seconds < 14400
                        else 3,
                    }
                )
        months.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "label": f"{year} 年 {month:02d} 月",
                "days": days,
                "weeks": len(days) // 7,
                "active_days": sum(bool(d.get("events")) for d in days),
                "count": len(session_ids),
                "seconds": total,
                "total": duration(total),
                "window": datetime.fromtimestamp(cutoff, CHINA).strftime("%m-%d %H:%M")
                + " 至 "
                + datetime.fromtimestamp(now, CHINA).strftime("%m-%d %H:%M"),
            }
        )
        month -= 1
        if not month:
            year, month = year - 1, 12
    return months
