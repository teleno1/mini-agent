from datetime import UTC, datetime

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator


def test_deterministic_substitutes_make_time_and_ids_repeatable() -> None:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()

    assert ids.new_id("session") == "session-0001"
    assert ids.new_id("turn") == "turn-0001"
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
