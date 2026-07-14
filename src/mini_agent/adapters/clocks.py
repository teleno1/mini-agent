"""Clock implementations for production and deterministic tests/smoke runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class SystemClock:
    """Read the host clock without making time a domain dependency."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class DeterministicClock:
    """Advance by a fixed interval on every read."""

    def __init__(self, start: datetime, step: timedelta = timedelta(microseconds=1)) -> None:
        if start.tzinfo is None:
            raise ValueError("deterministic clock start must be timezone-aware")
        if step < timedelta(0):
            raise ValueError("deterministic clock step cannot be negative")
        self._current = start
        self._step = step

    def now(self) -> datetime:
        current = self._current
        self._current += self._step
        return current
