import json
import warnings
from datetime import UTC, datetime

import pytest

from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.adapters.session_store import (
    PartialTailWarning,
    SessionCorruptionError,
    SessionLockError,
    SessionNotResumableError,
    SessionReadOnlyError,
    SessionStore,
)
from mini_agent.application.turns import TextTurnApplication
from mini_agent.domain.sessions import SessionEventType
from mini_agent.domain.streams import Failure, ResponseFailed, ResponseStarted, TextDelta
from mini_agent.domain.turns import StreamFailed
from mini_agent.providers.fake import ScriptedFakeModelProvider


class FailingProvider:
    def stream(self, messages):
        return self._stream()

    async def _stream(self):
        yield ResponseStarted(request_id="failed-request")
        yield TextDelta(text="incomplete")
        yield ResponseFailed(
            failure=Failure(
                category="network",
                source="fake-provider",
                redacted_description="connection closed",
                retryable=True,
                required_user_action="retry",
                cause="fake",
            )
        )


@pytest.mark.asyncio
async def test_failed_stream_persists_failure_without_an_assistant_message(tmp_path) -> None:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    application = TextTurnApplication(
        provider=FailingProvider(),
        clock=clock,
        id_generator=ids,
        session_store=store,
    )

    with pytest.raises(StreamFailed):
        await application.run("will fail")

    session = store.list_sessions()[0]
    snapshot = store.read(session.session_id)
    assert [event.event_type for event in snapshot.events] == [
        SessionEventType.SESSION_CREATED,
        SessionEventType.TURN_STARTED,
        SessionEventType.USER_MESSAGE,
        SessionEventType.MODEL_REQUEST_STARTED,
        SessionEventType.MODEL_REQUEST_FAILED,
        SessionEventType.TURN_FAILED,
    ]
    assert snapshot.projection is not None
    assert [message.content for message in snapshot.projection.messages] == ["will fail"]


def _application(
    tmp_path, *, chunks=("answer",)
) -> tuple[TextTurnApplication, SessionStore, ScriptedFakeModelProvider]:
    clock = DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC))
    ids = DeterministicIdGenerator()
    store = SessionStore(tmp_path, clock=clock, id_generator=ids)
    provider = ScriptedFakeModelProvider(chunks=chunks)
    return (
        TextTurnApplication(
            provider=provider,
            clock=clock,
            id_generator=ids,
            session_store=store,
        ),
        store,
        provider,
    )


@pytest.mark.asyncio
async def test_text_turn_is_durable_and_resumes_from_rebuilt_messages(tmp_path) -> None:
    application, store, provider = _application(tmp_path, chunks=("first", " answer"))

    result = await application.run("remember this")
    snapshot = store.read(result.session_id)

    assert [event.event_type for event in snapshot.events] == [
        "session.created",
        "turn.started",
        "user.message",
        "model.request.started",
        "model.request.completed",
        "assistant.message",
        "turn.completed",
    ]
    assert [event.sequence for event in snapshot.events] == list(range(1, 8))
    assert snapshot.projection is not None
    assert [message.content for message in snapshot.projection.messages] == [
        "remember this",
        "first answer",
    ]
    assert store.list_sessions()[0].last_assistant_message == "first answer"

    resumed = store.resume(result.session_id)
    assert [message.content for message in resumed.messages] == ["remember this", "first answer"]

    second = await application.run("continue", session_id=result.session_id)

    assert second.session_id == result.session_id
    assert len(provider.requests) == 2
    assert [message.content for message in provider.requests[1]] == [
        "remember this",
        "first answer",
        "continue",
    ]
    assert store.read(result.session_id).projection.last_sequence == 13  # type: ignore[union-attr]


def test_one_exclusive_writer_reports_active_and_stale_lock_evidence(tmp_path) -> None:
    store = SessionStore(tmp_path)
    writer = store.create("session-lock")
    try:
        with pytest.raises(SessionLockError) as error:
            store.open_writer("session-lock")
        assert error.value.evidence.owner_alive is True
        assert error.value.evidence.stale is False
    finally:
        writer.close()

    lock_path = tmp_path / ".mini-agent" / "sessions" / "session-lock" / "writer.lock"
    lock_path.write_text(
        json.dumps(
            {
                "lock_id": "stale-lock",
                "pid": 2_147_483_647,
                "hostname": "test",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SessionLockError) as error:
        store.open_writer("session-lock")
    assert error.value.evidence.stale is True

    forced = store.open_writer("session-lock", force_stale_lock=True)
    forced.close()


def test_intermediate_request_failure_is_not_a_resumable_turn(tmp_path) -> None:
    store = SessionStore(tmp_path)
    writer = store.create("session-intermediate-failure")
    turn = writer.append(SessionEventType.TURN_STARTED, {}, turn_id="turn-1")
    user = writer.append(
        SessionEventType.USER_MESSAGE,
        {"role": "user", "content": "unfinished"},
        turn_id="turn-1",
        causation_id=turn.event_id,
    )
    request = writer.append(
        SessionEventType.MODEL_REQUEST_STARTED,
        {"request_id": "request-1", "message_count": 1},
        turn_id="turn-1",
        causation_id=user.event_id,
    )
    writer.append(
        SessionEventType.MODEL_REQUEST_FAILED,
        {"category": "network", "description": "stopped"},
        turn_id="turn-1",
        causation_id=request.event_id,
    )
    writer.close()

    with pytest.raises(SessionNotResumableError):
        store.resume("session-intermediate-failure")


@pytest.mark.asyncio
async def test_listing_rebuilds_when_disposable_metadata_is_missing(tmp_path) -> None:
    application, store, _ = _application(tmp_path)
    result = await application.run("list me")
    metadata_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "metadata.json"
    metadata_path.unlink()

    listing = store.list_sessions()

    assert len(listing) == 1
    assert listing[0].session_id == result.session_id
    assert listing[0].last_user_message == "list me"


@pytest.mark.asyncio
async def test_only_a_trailing_partial_json_line_is_repaired(tmp_path) -> None:
    application, store, _ = _application(tmp_path)
    result = await application.run("repair me")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    with events_path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"event_id":"partial"')

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        snapshot = store.read(result.session_id)

    assert snapshot.events[-1].event_type == "turn.completed"
    assert any(item.category is PartialTailWarning for item in caught)
    assert events_path.read_bytes().endswith(b"\n")


@pytest.mark.asyncio
async def test_mid_file_corruption_and_sequence_gaps_refuse_recovery(tmp_path) -> None:
    application, store, _ = _application(tmp_path)
    result = await application.run("corrupt me")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    lines = events_path.read_bytes().splitlines(keepends=True)
    lines[2] = b"not-json\n"
    events_path.write_bytes(b"".join(lines))
    with pytest.raises(SessionCorruptionError):
        store.read(result.session_id)

    result = await application.run("gap me")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    lines = events_path.read_bytes().splitlines()
    record = json.loads(lines[2])
    record["sequence"] = 99
    lines[2] = json.dumps(record, separators=(",", ":")).encode()
    events_path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(SessionCorruptionError, match="sequence gap"):
        store.read(result.session_id)


@pytest.mark.asyncio
async def test_newer_schema_is_read_only_and_old_schema_migrates_in_memory(tmp_path) -> None:
    application, store, _ = _application(tmp_path)
    result = await application.run("schema me")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    lines = events_path.read_bytes().splitlines()
    newer = json.loads(lines[0])
    newer["schema_version"] = 99
    lines[0] = json.dumps(newer, separators=(",", ":")).encode()
    events_path.write_bytes(b"\n".join(lines) + b"\n")

    snapshot = store.read(result.session_id)
    assert snapshot.read_only is True
    assert snapshot.resumable is False
    with pytest.raises(SessionReadOnlyError):
        store.resume(result.session_id)
    with pytest.raises(SessionReadOnlyError):
        store.open_writer(result.session_id)

    result = await application.run("future envelope")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    future_lines = events_path.read_bytes().splitlines()
    future_lines[0] = json.dumps(
        {
            "schema_version": 99,
            "id": "future-root",
            "seq": 1,
            "kind": "future.session.created",
            "time": "2026-01-01T00:00:00+00:00",
            "data": {"status": "future"},
        },
        separators=(",", ":"),
    ).encode()
    events_path.write_bytes(b"\n".join(future_lines) + b"\n")

    future_snapshot = store.read(result.session_id)
    assert future_snapshot.read_only is True
    assert future_snapshot.events[0].event_id == "future-root"

    result = await application.run("old schema")
    events_path = tmp_path / ".mini-agent" / "sessions" / result.session_id / "events.jsonl"
    old_lines = []
    for line in events_path.read_bytes().splitlines():
        record = json.loads(line)
        record["schema_version"] = 0
        old_lines.append(json.dumps(record, separators=(",", ":")).encode())
    original = b"\n".join(old_lines) + b"\n"
    events_path.write_bytes(original)

    migrated = store.read(result.session_id)
    assert migrated.read_only is False
    assert migrated.projection is not None
    assert migrated.projection.messages[-1].content == "answer"
    assert events_path.read_bytes() == original
