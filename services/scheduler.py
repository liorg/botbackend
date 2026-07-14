import json
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Optional


def _pg_day_to_python(day: int) -> int:
    # PostgreSQL: 0=Sunday
    # Python:     0=Monday
    return (day - 1) % 7


def compute_next_run(
    schedule_type: str,
    cron_expr: Optional[str],
    run_at: Optional[str] = None,
) -> Optional[str]:

    if schedule_type == "once":
        return run_at

    if not cron_expr:
        return None

    try:
        cfg = json.loads(cron_expr)
    except Exception:
        return None

    now = datetime.now(timezone.utc)

    hour = int(cfg.get("hour", 9))
    minute = int(cfg.get("minute", 0))

    if schedule_type == "hourly":
        interval = max(int(cfg.get("intervalHours", 1)), 1)

        next_run = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        while next_run <= now:
            next_run += timedelta(hours=interval)

        return next_run.isoformat()

    if schedule_type == "daily":
        next_run = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if next_run <= now:
            next_run += timedelta(days=1)

        return next_run.isoformat()

    if schedule_type == "weekly":

        days = cfg.get("days", [])

        if not days:
            return None

        wanted = {_pg_day_to_python(int(x)) for x in days}

        for i in range(8):

            candidate = (
                now.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
                + timedelta(days=i)
            )

            if candidate <= now:
                continue

            if candidate.weekday() in wanted:
                return candidate.isoformat()

        return None

    if schedule_type == "monthly":

        wanted_day = int(cfg.get("dayOfMonth", 1))

        year = now.year
        month = now.month

        for _ in range(2):

            day = min(wanted_day, monthrange(year, month)[1])

            candidate = datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=timezone.utc,
            )

            if candidate > now:
                return candidate.isoformat()

            if month == 12:
                year += 1
                month = 1
            else:
                month += 1

        return None

    return None
