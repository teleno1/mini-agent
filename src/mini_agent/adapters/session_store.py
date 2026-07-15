"""Durable append-only JSONL Session storage.

The event file is authoritative.  ``metadata.json`` is only a disposable
listing cache, and every writer holds an OS-level exclusive lock for the whole
writer lifetime.
"""

from __future__ import annotations

import json
import os
import socket
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from mini_agent.adapters.clocks import SystemClock
from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.application.ports import Clock, IDGenerator
from mini_agent.domain.messages import Message
from mini_agent.domain.sessions import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    InvalidSessionEvents,
    JSONValue,
    SessionEvent,
    SessionEventType,
    SessionProjection,
    SessionStatus,
    rebuild_projection,
)


class SessionStoreError(RuntimeError):
    """Base class for durable Session failures."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a requested Session does not exist."""


class SessionAlreadyExistsError(SessionStoreError):
    """Raised when a new Session would overwrite existing history."""


class SessionCorruptionError(SessionStoreError):
    """Raised when authoritative JSONL history cannot be safely rebuilt."""


class SessionReadOnlyError(SessionStoreError):
    """Raised when a newer Session schema prevents safe mutation or Resume."""


class SessionNotResumableError(SessionStoreError):
    """Raised when recovery finds an unfinished transient Turn."""


class SessionPersistenceError(SessionStoreError):
    """Raised when an event cannot be durably appended."""


class PartialTailWarning(UserWarning):
    """Visible warning emitted when a trailing partial JSON line is repaired."""


@dataclass(frozen=True, slots=True)
class LockEvidence:
    """Non-secret evidence about the process that owns a Session lock."""

    path: Path
    lock_id: str | None
    pid: int | None
    hostname: str | None
    created_at: str | None
    owner_alive: bool | None
    stale: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "lock_id": self.lock_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "created_at": self.created_at,
            "owner_alive": self.owner_alive,
            "stale": self.stale,
        }


class SessionLockError(SessionStoreError):
    """Raised when another writer owns the Session lock."""

    def __init__(self, evidence: LockEvidence) -> None:
        state = "stale" if evidence.stale else "active"
        super().__init__(f"Session lock is {state}: {evidence.path}")
        self.evidence = evidence


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """Rebuildable summary used by Session listing."""

    session_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_sequence: int
    turn_count: int
    message_count: int
    last_user_message: str | None
    last_assistant_message: str | None
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """A read of authoritative events and their disposable projection."""

    session_id: str
    events: tuple[SessionEvent, ...]
    projection: SessionProjection | None
    read_only: bool
    recovery_warnings: tuple[str, ...]

    @property
    def metadata(self) -> SessionMetadata:
        if self.projection is not None:
            projection = self.projection
            user_messages = [
                message.content
                for message in projection.messages
                if getattr(message, "role", None) == "user"
            ]
            assistant_messages = [
                message.content
                for message in projection.messages
                if getattr(message, "role", None) == "assistant"
            ]
            return SessionMetadata(
                session_id=self.session_id,
                status=projection.status.value,
                created_at=projection.created_at,
                updated_at=projection.updated_at,
                last_sequence=projection.last_sequence,
                turn_count=len(projection.turns),
                message_count=len(projection.messages),
                last_user_message=user_messages[-1] if user_messages else None,
                last_assistant_message=assistant_messages[-1] if assistant_messages else None,
                read_only=self.read_only,
            )

        first = self.events[0]
        return SessionMetadata(
            session_id=self.session_id,
            status="read-only",
            created_at=first.timestamp,
            updated_at=self.events[-1].timestamp,
            last_sequence=self.events[-1].sequence,
            turn_count=0,
            message_count=0,
            last_user_message=None,
            last_assistant_message=None,
            read_only=True,
        )

    @property
    def resumable(self) -> bool:
        return not self.read_only and self.projection is not None and self.projection.resumable


@dataclass(frozen=True, slots=True)
class ResumedSession:
    """The rebuilt state from which the next Turn may be started."""

    snapshot: SessionSnapshot

    @property
    def session_id(self) -> str:
        return self.snapshot.session_id

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return self.snapshot.events

    @property
    def projection(self) -> SessionProjection:
        if self.snapshot.projection is None:
            raise SessionReadOnlyError("a newer-schema Session has no resumable projection")
        return self.snapshot.projection

    @property
    def messages(self) -> tuple[Message, ...]:
        return self.projection.messages


class _SessionLock:
    """Small cross-platform lock based on atomic exclusive file creation."""

    def __init__(self, path: Path, *, force_stale: bool = False) -> None:
        self._path = path
        self._force_stale = force_stale
        self._lock_id = f"lock-{uuid4()}"
        self._acquired = False

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
        except FileExistsError:
            evidence = _read_lock_evidence(self._path)
            if not (self._force_stale and evidence.stale):
                raise SessionLockError(evidence) from None
            try:
                self._path.unlink()
            except FileNotFoundError:
                self._create()
            else:
                self._create()

    def _create(self) -> None:
        fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        evidence = {
            "lock_id": self._lock_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(evidence, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            raise
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            evidence = _read_lock_evidence(self._path)
            if evidence.lock_id == self._lock_id:
                self._path.unlink(missing_ok=True)
        finally:
            self._acquired = False


class SessionWriter:
    """One exclusive writer for one Session's append-only event stream."""

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        directory: Path,
        lock: _SessionLock,
        events: tuple[SessionEvent, ...],
    ) -> None:
        self._store = store
        self.session_id = session_id
        self.directory = directory
        self.events_path = directory / "events.jsonl"
        self._lock = lock
        self._events = list(events)
        self._projection: SessionProjection | None = (
            rebuild_projection(events) if events else None
        )
        self._closed = False

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def projection(self) -> SessionProjection:
        if self._projection is None:
            raise SessionStoreError("new Session has no projection before session.created")
        return self._projection

    @property
    def metadata(self) -> SessionMetadata:
        return SessionSnapshot(
            session_id=self.session_id,
            events=self.events,
            projection=self._projection,
            read_only=False,
            recovery_warnings=(),
        ).metadata

    def append(
        self,
        event_type: str | SessionEventType,
        payload: Mapping[str, JSONValue],
        *,
        turn_id: str | None = None,
        causation_id: str | None = None,
        timestamp: datetime | None = None,
        event_id: str | None = None,
    ) -> SessionEvent:
        """Validate, append, flush, and fsync one complete event line."""

        self._ensure_open()
        event = SessionEvent(
            schema_version=CURRENT_SCHEMA_VERSION,
            event_id=event_id or self._store._id_generator.new_id("event"),
            sequence=len(self._events) + 1,
            session_id=self.session_id,
            event_type=str(event_type),
            timestamp=timestamp or self._store._clock.now(),
            turn_id=turn_id,
            causation_id=causation_id,
            payload=dict(payload),
        )
        self.append_event(event)
        return event

    def append_event(self, event: SessionEvent) -> None:
        """Append a pre-built current-schema event after checking its position."""

        self._ensure_open()
        if event.schema_version != CURRENT_SCHEMA_VERSION:
            raise SessionReadOnlyError("writers can append only the current Session schema")
        if event.session_id != self.session_id:
            raise SessionPersistenceError("event belongs to a different Session")
        if event.sequence != len(self._events) + 1:
            raise SessionPersistenceError("event sequence is not contiguous")
        if any(existing.event_id == event.event_id for existing in self._events):
            raise SessionPersistenceError(f"duplicate event ID: {event.event_id}")

        candidate_events = (*self._events, event)
        try:
            candidate_projection = rebuild_projection(candidate_events)
            encoded = _encode_event(event)
        except (TypeError, ValueError, InvalidSessionEvents) as exc:
            raise SessionPersistenceError(str(exc)) from exc

        try:
            with self.events_path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise SessionPersistenceError(f"could not durably append Session event: {exc}") from exc

        self._events.append(event)
        self._projection = candidate_projection
        self._store._write_metadata(self.directory, self.metadata)

    def close(self) -> None:
        if not self._closed:
            self._lock.release()
            self._closed = True

    def __enter__(self) -> SessionWriter:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    async def __aenter__(self) -> SessionWriter:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.__exit__(exc_type, exc_value, traceback)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionStoreError("Session writer is closed")


class SessionStore:
    """Filesystem adapter for authoritative Session Events and projections."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        sessions_directory: Path | None = None,
        clock: Clock | None = None,
        id_generator: IDGenerator | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        if sessions_directory is not None:
            resolved_sessions_directory = sessions_directory
        elif self.workspace_root.name == "sessions":
            resolved_sessions_directory = self.workspace_root
        elif self.workspace_root.name == ".mini-agent":
            resolved_sessions_directory = self.workspace_root / "sessions"
        else:
            resolved_sessions_directory = self.workspace_root / ".mini-agent" / "sessions"
        self.sessions_directory = resolved_sessions_directory
        self._clock = clock or SystemClock()
        self._id_generator = id_generator or UUIDIdGenerator()

    def create(
        self,
        session_id: str | None = None,
        *,
        created_at: datetime | None = None,
        force_stale_lock: bool = False,
    ) -> SessionWriter:
        """Create a new Session and durably record its lifecycle root event."""

        resolved_id = session_id or self._id_generator.new_id("session")
        directory = self._session_directory(resolved_id)
        directory.mkdir(parents=True, exist_ok=True)
        events_path = directory / "events.jsonl"
        if events_path.exists() and events_path.stat().st_size:
            raise SessionAlreadyExistsError(f"Session already exists: {resolved_id}")
        events_path.touch(exist_ok=True)
        writer = self._open_writer(
            resolved_id,
            force_stale_lock=force_stale_lock,
            allow_empty=True,
        )
        try:
            writer.append(
                SessionEventType.SESSION_CREATED,
                {"status": SessionStatus.IDLE.value},
                timestamp=created_at,
            )
        except BaseException:
            writer.close()
            raise
        return writer

    create_session = create

    def open_writer(self, session_id: str, *, force_stale_lock: bool = False) -> SessionWriter:
        """Acquire the exclusive Session writer after validating its history."""

        return self._open_writer(session_id, force_stale_lock=force_stale_lock)

    def _open_writer(
        self,
        session_id: str,
        *,
        force_stale_lock: bool,
        allow_empty: bool = False,
    ) -> SessionWriter:
        directory = self._session_directory(session_id)
        events_path = directory / "events.jsonl"
        if not events_path.exists():
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        lock = _SessionLock(directory / "writer.lock", force_stale=force_stale_lock)
        try:
            lock.acquire()
            loaded = _load_events(events_path, session_id, repair=True, allow_empty=allow_empty)
            if loaded.newer_schema:
                raise SessionReadOnlyError(
                    f"Session {session_id} uses a newer schema and is read-only"
                )
            try:
                return SessionWriter(self, session_id, directory, lock, loaded.events)
            except InvalidSessionEvents as exc:
                raise SessionCorruptionError(str(exc)) from exc
        except BaseException:
            lock.release()
            raise

    def read(self, session_id: str) -> SessionSnapshot:
        """Read and rebuild a Session without creating a writer or coroutine."""

        directory = self._session_directory(session_id)
        events_path = directory / "events.jsonl"
        if not events_path.exists():
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        lock = _SessionLock(directory / "writer.lock")
        try:
            lock.acquire()
            loaded = _load_events(events_path, session_id, repair=True)
            projection: SessionProjection | None = None
            if not loaded.newer_schema:
                try:
                    projection = rebuild_projection(loaded.events)
                except InvalidSessionEvents as exc:
                    raise SessionCorruptionError(str(exc)) from exc
            return SessionSnapshot(
                session_id=session_id,
                events=loaded.events,
                projection=projection,
                read_only=loaded.newer_schema,
                recovery_warnings=loaded.warnings,
            )
        finally:
            lock.release()

    load = read
    open = read

    def resume(self, session_id: str) -> ResumedSession:
        """Rebuild a resumable Session from events, never serialized runtime state."""

        snapshot = self.read(session_id)
        if snapshot.read_only:
            raise SessionReadOnlyError(
                f"Session {session_id} uses a newer schema and cannot Resume"
            )
        if not snapshot.resumable:
            raise SessionNotResumableError(
                f"Session {session_id} contains an unfinished Turn and needs recovery"
            )
        return ResumedSession(snapshot)

    def list_sessions(self) -> tuple[SessionMetadata, ...]:
        """List Sessions by rebuilding each entry from events, not cache files."""

        if not self.sessions_directory.exists():
            return ()
        metadata: list[SessionMetadata] = []
        for directory in sorted(self.sessions_directory.iterdir(), key=lambda path: path.name):
            if not directory.is_dir() or not (directory / "events.jsonl").exists():
                continue
            metadata.append(self.read(directory.name).metadata)
        return tuple(sorted(metadata, key=lambda item: item.updated_at, reverse=True))

    list = list_sessions

    def append(
        self,
        session_id: str,
        event_type: str | SessionEventType,
        payload: Mapping[str, JSONValue],
        *,
        turn_id: str | None = None,
        causation_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> SessionEvent:
        """Convenience append that still holds exactly one lock during the write."""

        with self.open_writer(session_id) as writer:
            return writer.append(
                event_type,
                payload,
                turn_id=turn_id,
                causation_id=causation_id,
                timestamp=timestamp,
            )

    def _session_directory(self, session_id: str) -> Path:
        if not session_id.strip() or Path(session_id).name != session_id:
            raise ValueError("Session ID must be a non-blank single path component")
        if session_id in {".", ".."}:
            raise ValueError("invalid Session ID")
        return self.sessions_directory / session_id

    def _write_metadata(self, directory: Path, metadata: SessionMetadata) -> None:
        """Best-effort cache update; event durability has already completed."""

        record: dict[str, JSONValue] = {
            "session_id": metadata.session_id,
            "status": metadata.status,
            "created_at": metadata.created_at.isoformat(),
            "updated_at": metadata.updated_at.isoformat(),
            "last_sequence": metadata.last_sequence,
            "turn_count": metadata.turn_count,
            "message_count": metadata.message_count,
            "last_user_message": metadata.last_user_message,
            "last_assistant_message": metadata.last_assistant_message,
            "read_only": metadata.read_only,
        }
        temporary = directory / f"metadata.json.tmp-{uuid4()}"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / "metadata.json")
            _fsync_directory(directory)
        except OSError:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _LoadedEvents:
    events: tuple[SessionEvent, ...]
    newer_schema: bool
    warnings: tuple[str, ...]


def _load_events(
    path: Path,
    session_id: str,
    *,
    repair: bool,
    allow_empty: bool = False,
) -> _LoadedEvents:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SessionPersistenceError(f"could not read Session events: {exc}") from exc
    warnings_seen: list[str] = []

    if data and not data.endswith(b"\n"):
        boundary = data.rfind(b"\n") + 1
        tail = data[boundary:]
        try:
            tail_text = tail.decode("utf-8")
            json.loads(tail_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if not repair:
                raise SessionCorruptionError("Session ends with a partial JSON line")
            message = f"repaired trailing partial JSON line in {path}"
            _truncate(path, data[:boundary])
            warnings.warn(message, PartialTailWarning, stacklevel=3)
            warnings_seen.append(message)
            data = data[:boundary]
        else:
            # A valid final object without its framing newline is safe to keep,
            # but normalize it before a future append so events cannot merge.
            if repair:
                _truncate(path, data + b"\n")
                data += b"\n"

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionCorruptionError("Session events are not valid UTF-8") from exc

    raw_lines = text.splitlines()
    records: list[SessionEvent] = []
    newer_schema = False
    expected_sequence = 1
    event_ids: set[str] = set()
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            raise SessionCorruptionError(f"blank or malformed event line {line_number}")
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SessionCorruptionError(f"invalid JSON on event line {line_number}") from exc
        if not isinstance(decoded, dict):
            raise SessionCorruptionError(f"event line {line_number} must contain an object")
        try:
            event = _parse_event_record(cast(dict[str, object], decoded), session_id, line_number)
        except (TypeError, ValueError) as exc:
            raise SessionCorruptionError(f"invalid event line {line_number}: {exc}") from exc
        if event.session_id != session_id:
            raise SessionCorruptionError(f"event line {line_number} belongs to another Session")
        if event.sequence != expected_sequence:
            raise SessionCorruptionError(
                f"Session sequence gap at line {line_number}: expected {expected_sequence}, "
                f"found {event.sequence}"
            )
        if event.event_id in event_ids:
            raise SessionCorruptionError(f"duplicate event ID on line {line_number}")
        expected_sequence += 1
        event_ids.add(event.event_id)
        newer_schema = newer_schema or event.schema_version > CURRENT_SCHEMA_VERSION
        records.append(event)

    if not records and not allow_empty:
        raise SessionCorruptionError("Session events file is empty")
    return _LoadedEvents(tuple(records), newer_schema, tuple(warnings_seen))


def _parse_event_record(
    record: dict[str, object], session_id: str, line_number: int
) -> SessionEvent:
    version_value = record.get("schema_version", record.get("version", 0))
    if isinstance(version_value, bool) or not isinstance(version_value, int):
        raise ValueError("schema_version must be an integer")
    if version_value > CURRENT_SCHEMA_VERSION:
        return _parse_future_event(record, session_id, line_number, version_value)
    migrated = _migrate_record(record, session_id)
    return SessionEvent.from_record(migrated)


def _parse_future_event(
    record: dict[str, object], session_id: str, line_number: int, schema_version: int
) -> SessionEvent:
    """Keep a newer envelope inspectable without interpreting its semantics."""

    sequence_value = record.get("sequence", record.get("seq", line_number))
    sequence = (
        sequence_value
        if isinstance(sequence_value, int) and not isinstance(sequence_value, bool)
        else line_number
    )
    event_id_value = record.get("event_id", record.get("id"))
    event_id = (
        event_id_value
        if isinstance(event_id_value, str) and event_id_value.strip()
        else f"future-{session_id}-{line_number:08d}"
    )
    event_type_value = record.get("event_type", record.get("type", record.get("kind")))
    event_type = (
        event_type_value
        if isinstance(event_type_value, str) and event_type_value.strip()
        else "future.event"
    )
    timestamp_value = record.get("timestamp", record.get("time"))
    timestamp = _future_timestamp(timestamp_value)
    turn_id_value = record.get("turn_id")
    causation_value = record.get("causation_id", record.get("caused_by"))
    payload_value = record.get("payload", {})
    payload = cast(dict[str, JSONValue], payload_value) if isinstance(payload_value, dict) else {}
    session_value = record.get("session_id")
    future_session_id = session_value if isinstance(session_value, str) else session_id
    return SessionEvent(
        schema_version=schema_version,
        event_id=event_id,
        sequence=sequence,
        session_id=future_session_id,
        event_type=event_type,
        timestamp=timestamp,
        turn_id=turn_id_value if isinstance(turn_id_value, str) else None,
        causation_id=causation_value if isinstance(causation_value, str) else None,
        payload=payload,
    )


def _future_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return parsed
    return datetime.fromtimestamp(0, UTC)


def _migrate_record(record: dict[str, object], session_id: str) -> dict[str, object]:
    version_value = record.get("schema_version", record.get("version", 0))
    if isinstance(version_value, bool) or not isinstance(version_value, int):
        raise ValueError("schema_version must be an integer")
    if version_value not in SUPPORTED_SCHEMA_VERSIONS and version_value <= CURRENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported old schema version: {version_value}")
    if version_value > CURRENT_SCHEMA_VERSION:
        return record
    if version_value == CURRENT_SCHEMA_VERSION:
        return record

    # Schema 0 was the pre-versioned shape: its field names were shorter, and
    # migration is deliberately in-memory so the original file remains intact.
    sequence = record.get("sequence", record.get("seq"))
    event_id = record.get("event_id", record.get("id"))
    event_type = record.get("event_type", record.get("type"))
    timestamp = record.get("timestamp", record.get("time"))
    payload = record.get("payload", record.get("data", {}))
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise ValueError("legacy event sequence is missing")
    if not isinstance(event_id, str) or not event_id.strip():
        event_id = f"legacy-{session_id}-{sequence:08d}"
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("legacy event type is missing")
    if not isinstance(timestamp, str):
        raise ValueError("legacy event timestamp is missing")
    if not isinstance(payload, dict):
        raise ValueError("legacy event payload must be an object")
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "event_id": event_id,
        "sequence": sequence,
        "session_id": record.get("session_id", record.get("session", session_id)),
        "event_type": event_type,
        "timestamp": timestamp,
        "turn_id": record.get("turn_id"),
        "causation_id": record.get("causation_id", record.get("caused_by")),
        "payload": cast(dict[str, JSONValue], payload),
    }


def _encode_event(event: SessionEvent) -> bytes:
    try:
        line = json.dumps(
            event.to_record(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"event payload is not JSON serializable: {exc}") from exc
    return (line + "\n").encode("utf-8")


def _truncate(path: Path, data: bytes) -> None:
    temporary = path.with_name(f"{path.name}.repair-{uuid4()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SessionPersistenceError(f"could not repair Session events: {exc}") from exc


def _read_lock_evidence(path: Path) -> LockEvidence:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LockEvidence(path, None, None, None, None, None, True)
    if not isinstance(raw, dict):
        return LockEvidence(path, None, None, None, None, None, True)
    lock_id = raw.get("lock_id")
    pid = raw.get("pid")
    hostname = raw.get("hostname")
    created_at = raw.get("created_at")
    pid_value = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    owner_alive = _process_alive(pid_value)
    return LockEvidence(
        path=path,
        lock_id=lock_id if isinstance(lock_id, str) else None,
        pid=pid_value,
        hostname=hostname if isinstance(hostname, str) else None,
        created_at=created_at if isinstance(created_at, str) else None,
        owner_alive=owner_alive,
        stale=owner_alive is False or owner_alive is None,
    )


def _process_alive(pid: int | None) -> bool | None:
    if pid is None or pid < 1:
        return None
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# Descriptive aliases keep the adapter discoverable without coupling callers
# to one spelling of JSON Lines.
JSONLSessionStore = SessionStore
JsonlSessionStore = SessionStore
SessionEventStore = SessionStore
