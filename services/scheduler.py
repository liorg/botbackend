from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger


DEFAULT_TIMEZONE = "Asia/Jerusalem"


def compute_next_run(
    schedule_type: str,
    cron_expr: Optional[str],
    run_at: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> Optional[str]:
    """
    מחשב את זמן ההפעלה הבא.

    schedule_type:
        once — תזמון חד-פעמי לפי run_at.
        cron — תזמון חוזר לפי cron_expr.

    cron_expr הוא ביטוי Cron אמיתי בן חמישה שדות:

        minute hour day month day_of_week

    דוגמאות:
        30 20 * * *      — כל יום ב-20:30
        30 19 * * 0,3    — ראשון ורביעי ב-19:30
        15 */2 * * *     — כל שעתיים בדקה 15
        0 10 15 * *      — בכל 15 לחודש ב-10:00

    החישוב מתבצע באזור הזמן של המשתמש.
    התוצאה מוחזרת ב-UTC לשמירה בעמודת timestamptz.
    """

    normalized_type = (schedule_type or "").strip().lower()

    # תזמון חד-פעמי: בזמן יצירת התזמון next_run_at שווה ל-run_at.
    if normalized_type == "once":
        return normalize_run_at(run_at)

    if normalized_type != "cron":
        return None

    if not cron_expr or not cron_expr.strip():
        return None

    try:
        user_timezone = ZoneInfo(
            timezone_name or DEFAULT_TIMEZONE
        )
    except ZoneInfoNotFoundError:
        user_timezone = ZoneInfo(DEFAULT_TIMEZONE)

    try:
        trigger = CronTrigger.from_crontab(
            cron_expr.strip(),
            timezone=user_timezone,
        )

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(user_timezone)

        next_run = trigger.get_next_fire_time(
            previous_fire_time=None,
            now=now_local,
        )

        if next_run is None:
            return None

        return next_run.astimezone(timezone.utc).isoformat()

    except (TypeError, ValueError):
        # ביטוי Cron לא תקין.
        return None


def normalize_run_at(run_at: Optional[str]) -> Optional[str]:
    """
    בודק ומנרמל זמן חד-פעמי ל-UTC.

    אם run_at ללא timezone, הוא נחשב UTC.
    """

    if not run_at:
        return None

    try:
        value = datetime.fromisoformat(
            run_at.replace("Z", "+00:00")
        )

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc).isoformat()

    except (TypeError, ValueError):
        return None
