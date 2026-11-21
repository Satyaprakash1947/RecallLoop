"""Pure commitment lifecycle rules and deterministic deadline helpers."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import CommitmentStatus


KOLKATA = ZoneInfo("Asia/Kolkata")

LIFECYCLE_TRANSITIONS: dict[CommitmentStatus, frozenset[CommitmentStatus]] = {
    CommitmentStatus.PROPOSED: frozenset({CommitmentStatus.CONFIRMED, CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED}),
    CommitmentStatus.CONFIRMED: frozenset({CommitmentStatus.IN_PROGRESS, CommitmentStatus.OVERDUE,
        CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED}),
    CommitmentStatus.IN_PROGRESS: frozenset({CommitmentStatus.OVERDUE, CommitmentStatus.RECOVERY,
        CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED}),
    CommitmentStatus.OVERDUE: frozenset({CommitmentStatus.RECOVERY, CommitmentStatus.IN_PROGRESS,
        CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED}),
    CommitmentStatus.RECOVERY: frozenset({CommitmentStatus.IN_PROGRESS, CommitmentStatus.OVERDUE,
        CommitmentStatus.COMPLETED_PENDING_VERIFICATION, CommitmentStatus.CANCELLED, CommitmentStatus.DISPUTED}),
    CommitmentStatus.COMPLETED_PENDING_VERIFICATION: frozenset({CommitmentStatus.VERIFIED,
        CommitmentStatus.IN_PROGRESS, CommitmentStatus.DISPUTED}),
    CommitmentStatus.VERIFIED: frozenset({CommitmentStatus.DISPUTED}),
    CommitmentStatus.DISPUTED: frozenset({CommitmentStatus.CONFIRMED, CommitmentStatus.CANCELLED}),
    CommitmentStatus.CANCELLED: frozenset(),
}


class LifecycleTransitionError(ValueError):
    """Raised for a transition that violates commitment lifecycle rules."""


def validate_transition(current: CommitmentStatus, target: CommitmentStatus) -> None:
    allowed = LIFECYCLE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        options = ", ".join(sorted(status.value for status in allowed)) or "none"
        raise LifecycleTransitionError(f"invalid commitment transition {current.value} -> {target.value}; allowed: {options}")


def require_aware(value: datetime, name: str = "reference_datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(KOLKATA)


def effective_deadline(value: datetime) -> datetime:
    """Interpret date-only midnight deadlines as end-of-day in Kolkata.

    The extraction resolver stores date-only expressions at local midnight; using
    that instant as a deadline would mark a task overdue at the start of its due day.
    """
    local_value = value.astimezone(KOLKATA)
    if local_value.timetz().replace(tzinfo=None) == time.min:
        return datetime.combine(local_value.date(), time.max, tzinfo=KOLKATA)
    return local_value
