"""Phase 5B: scheduled-search automation service.

Provides deterministic next-run calculation, due-vacation selection,
and schedule-state helpers for the periodic search runner.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlmodel import text


def _env_int(name: str, default: int) -> int:
    """Read an integer env var with a fallback."""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str = "") -> str:
    """Read a string env var with a fallback."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value


# ---------------------------------------------------------------------------
# Default run slots keyed by searches_per_day
# ---------------------------------------------------------------------------

_DEFAULT_SLOTS: dict[int, list[str]] = {
    1: ["08:00"],
    2: ["08:00", "20:00"],
    3: ["07:00", "14:00", "21:00"],
}

# Clamp invalid values to default (2)
_MIN_SEARCHES = 1
_MAX_SEARCHES = 3


def _clamp_searches_per_day(value: int) -> int:
    if value < _MIN_SEARCHES:
        return _MIN_SEARCHES
    if value > _MAX_SEARCHES:
        return _MAX_SEARCHES
    return value


# ---------------------------------------------------------------------------
# Deterministic jitter (testable, bounded)
# ---------------------------------------------------------------------------

def _compute_deterministic_jitter(
    vacation_id: int | None,
    slot_index: int,
    jitter_minutes: int,
    seed: str = "",
) -> timedelta:
    """Return a deterministic timedelta in [-jitter_minutes, +jitter_minutes].

    Uses SHA-256 of (vacation_id, date, slot_index, seed) so the same inputs
    always produce the same jitter.  In tests the caller can pass ``seed="test"``
    to get reproducible results.
    """
    if jitter_minutes <= 0:
        return timedelta(minutes=0)

    raw = f"{vacation_id}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:{slot_index}:{seed}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    # Use first 8 hex chars -> integer in [0, 2^32)
    value = int(digest[:8], 16)
    range_size = 2 * jitter_minutes + 1  # inclusive on both sides
    offset = (value % range_size) - jitter_minutes  # [-jitter_minutes, +jitter_minutes]
    return timedelta(minutes=offset)


# ---------------------------------------------------------------------------
# calculate_next_scheduled_run
# ---------------------------------------------------------------------------

def calculate_next_scheduled_run(
    vacation_id: int | None,
    searches_per_day: int,
    last_run_at: datetime | str | None = None,
    jitter_minutes: int = 20,
    seed: str = "",
) -> Optional[datetime]:
    """Calculate the next scheduled run time.

    Returns a timezone-aware UTC datetime that is always in the future relative
    to *last_run_at* (or now if last_run_at is None).  The result accounts for
    deterministic jitter bounded by ``jitter_minutes``.

    If searches_per_day is invalid it is clamped to [1, 3].
    """
    sp = _clamp_searches_per_day(searches_per_day)
    slots = _DEFAULT_SLOTS.get(sp, _DEFAULT_SLOTS[2])

    now = datetime.now(timezone.utc)

    if last_run_at is None:
        # First run ever — pick the next slot today (or tomorrow).
        current_time = now.time()
        for i, slot_str in enumerate(slots):
            slot_dt = _slot_datetime(slot_str, now)
            jittered = slot_dt + _compute_deterministic_jitter(vacation_id, i, jitter_minutes, seed)
            if jittered > now:
                return jittered
        # All slots today have passed — use first slot tomorrow.
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        base = _slot_datetime(slots[0], next_day)
        return base + _compute_deterministic_jitter(vacation_id, 0, jitter_minutes, seed)

    # Normalise last_run_at to datetime
    if isinstance(last_run_at, str):
        try:
            parsed = datetime.fromisoformat(last_run_at)
            last_run_at = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except (ValueError, TypeError):
            last_run_at = now

    # Find which slot we were due for and move to the next one.
    last_dt = last_run_at if isinstance(last_run_at, datetime) else now
    current_time = last_dt.time()

    # Try today's remaining slots first
    for i, slot_str in enumerate(slots):
        candidate = _slot_datetime(slot_str, last_dt.date())
        jittered = candidate + _compute_deterministic_jitter(vacation_id, i, jitter_minutes, seed)
        if jittered > now and jittered > last_dt:
            return jittered

    # All today's slots passed — use first slot of tomorrow.
    next_day = (last_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    base = _slot_datetime(slots[0], next_day)
    return base + _compute_deterministic_jitter(vacation_id, 0, jitter_minutes, seed)


def _slot_datetime(slot_str: str, day: datetime | None = None) -> datetime:
    """Parse 'HH:MM' into a timezone-aware UTC datetime for *day*."""
    parts = slot_str.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    d = day or datetime.now(timezone.utc)
    # Convert to datetime if we got a date, then set time components
    if not isinstance(d, datetime):
        d = datetime.combine(d, datetime.min.time())
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Due-vacation selection (used by the runner script)
# ---------------------------------------------------------------------------

def _due_vacations_query() -> str:
    """Return raw SQL for selecting vacations due for a scheduled search."""
    return """
        SELECT id, slug, title, status, schedule_enabled, searches_per_day,
               last_scheduled_run_at, next_scheduled_run_at,
               schedule_jitter_minutes, schedule_paused_reason
        FROM vacation
        WHERE status = 'active'
          AND schedule_enabled = 1
          AND (next_scheduled_run_at IS NULL OR next_scheduled_run_at <= :now)
          AND (schedule_paused_reason IS NULL OR schedule_paused_reason = '')
    """


# ---------------------------------------------------------------------------
# Schedule state helpers
# ---------------------------------------------------------------------------

def update_schedule_state(
    session,  # noqa: ANN001 — sqlmodel Session
    vacation_id: int,
    *,
    last_run_at: datetime | None = None,
    next_run_at: datetime | None = None,
    status: str = "completed",
    message: str = "",
) -> None:
    """Update schedule-related fields on a Vacation row."""
    now_str = (last_run_at or datetime.now(timezone.utc)).isoformat()
    next_str = next_run_at.isoformat() if next_run_at else "NULL"

    session.execute(
        text(
            "UPDATE vacation SET "
            "last_scheduled_run_at = :now, "
            "next_scheduled_run_at = :next, "
            "schedule_last_status = :status, "
            "schedule_last_message = :message "
            "WHERE id = :vid"
        ).bindparams(
            now=now_str,
            next=next_str if next_str != "NULL" else None,
            status=status,
            message=message,
            vid=vacation_id,
        )
    )
    session.commit()
