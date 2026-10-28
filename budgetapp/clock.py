"""The app's notion of "today" and "now", with a test override.

Always use ``clock.today()`` / ``clock.now()`` instead of ``date.today()`` / ``datetime.now()`` so
tests can freeze time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time

_today: date | None = None
_now: datetime | None = None


def today() -> date:
    """Today's date in the Mac's local time zone (or the frozen date in tests)."""
    if _today is not None:
        return _today
    if _now is not None:
        return _now.astimezone().date()
    return date.today()


def now() -> datetime:
    """The current time as an aware UTC datetime (or the frozen time in tests)."""
    if _now is not None:
        return _now
    if _today is not None:
        return datetime.combine(_today, time(12, 0)).astimezone().astimezone(UTC)
    return datetime.now(UTC)


def set_override(*, today: date | None = None, now: datetime | None = None) -> None:
    """Freeze the clock. Pass a date, an aware datetime, or both.

    With only ``today`` set, ``now()`` is noon local time on that day. With only ``now`` set,
    ``today()`` is that instant's local date.
    """
    global _today, _now
    if now is not None and now.tzinfo is None:
        raise ValueError("clock override 'now' must be timezone-aware")
    _today = today
    _now = now.astimezone(UTC) if now is not None else None


def clear_override() -> None:
    """Return to the real clock."""
    set_override()


def is_overridden() -> bool:
    return _today is not None or _now is not None


@contextmanager
def frozen(*, today: date | None = None, now: datetime | None = None) -> Iterator[None]:
    """Context manager form of :func:`set_override`; restores the previous state on exit."""
    global _today, _now
    previous = (_today, _now)
    set_override(today=today, now=now)
    try:
        yield
    finally:
        _today, _now = previous
