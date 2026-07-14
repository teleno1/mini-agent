"""Identifier implementations for production and deterministic tests/smoke runs."""

from __future__ import annotations

from collections import defaultdict
from uuid import uuid4


class UUIDIdGenerator:
    """Generate opaque UUID identifiers with a readable namespace prefix."""

    def new_id(self, namespace: str) -> str:
        return f"{namespace}-{uuid4()}"


class DeterministicIdGenerator:
    """Generate stable, human-readable identifiers for repeatable journeys."""

    def __init__(self) -> None:
        self._counters: defaultdict[str, int] = defaultdict(int)

    def new_id(self, namespace: str) -> str:
        self._counters[namespace] += 1
        return f"{namespace}-{self._counters[namespace]:04d}"
