from __future__ import annotations

from datetime import datetime, timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_now_iso() -> str:
    return beijing_now().isoformat()


def window_hours_to_gmail_newer_than(window_hours: int) -> str:
    if window_hours <= 0:
        window_hours = 72
    days = max(1, (window_hours + 23) // 24)
    return f"newer_than:{days}d"
