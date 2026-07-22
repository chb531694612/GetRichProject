from __future__ import annotations

from datetime import datetime, time, timedelta


def slot_job_name(day: datetime, slot: time) -> str:
    return f"recommend-slot:{day:%Y%m%d}:{slot:%H%M}"


def due_recommendation_slot(
    now: datetime,
    slots: tuple[time, ...],
    latest_start: time,
    has_run,
) -> time | None:
    """Return the newest due slot that has not been recorded yet.

    A restart catches up the newest missed slot, but never starts a provider
    request at or after ``latest_start``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if now.timetz().replace(tzinfo=None) >= latest_start:
        return None
    due_slots = [
        slot
        for slot in slots
        if datetime.combine(now.date(), slot, tzinfo=now.tzinfo) <= now
    ]
    if not due_slots:
        return None
    latest = due_slots[-1]
    return None if has_run(slot_job_name(now, latest)) else latest


def seconds_until_next_event(
    now: datetime,
    slots: tuple[time, ...],
    deadline: time,
    poll_interval_seconds: int,
) -> float:
    """Wake on either the normal poll or the next recommendation/day-end edge."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidates: list[datetime] = []
    for slot in slots:
        candidate = datetime.combine(now.date(), slot, tzinfo=now.tzinfo)
        if candidate > now:
            candidates.append(candidate)
    day_end = datetime.combine(now.date(), deadline, tzinfo=now.tzinfo)
    if day_end > now:
        candidates.append(day_end)
    if not candidates:
        candidates.append(datetime.combine(now.date() + timedelta(days=1), slots[0], tzinfo=now.tzinfo))
    until_event = max(1.0, (min(candidates) - now).total_seconds())
    return min(float(poll_interval_seconds), until_event)
