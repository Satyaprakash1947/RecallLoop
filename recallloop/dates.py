"""Small deterministic date resolver; unsupported language stays unresolved."""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = ZoneInfo("Asia/Kolkata")
WEEKDAYS = {name.lower(): index for index, name in enumerate(
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
)}


def resolve_date(expression: str, reference: datetime, timezone: str = "Asia/Kolkata") -> datetime | None:
    """Resolve supported explicit expressions relative to an injected aware datetime.

    Supported: today, tomorrow, weekdays (next/this optional), and ISO dates.
    The resolver does not infer a clock time. Resolved dates use local midnight.
    """
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("reference must be timezone-aware")
    zone = ZoneInfo(timezone)
    local_ref = reference.astimezone(zone)
    text = expression.strip().lower()
    target: date | None = None
    if text == "today":
        target = local_ref.date()
    elif text == "tomorrow":
        target = local_ref.date() + timedelta(days=1)
    elif text in WEEKDAYS:
        delta = (WEEKDAYS[text] - local_ref.weekday()) % 7
        target = local_ref.date() + timedelta(days=delta)
    else:
        match = re.fullmatch(r"(?:(?:next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", text)
        if match:
            weekday = WEEKDAYS[match.group(1)]
            delta = (weekday - local_ref.weekday()) % 7
            if delta == 0 and (text.startswith("next ") or text.startswith("this ")):
                delta = 7
            target = local_ref.date() + timedelta(days=delta)
        else:
            try:
                target = date.fromisoformat(text)
            except ValueError:
                return None
    return datetime.combine(target, time.min, tzinfo=zone)
