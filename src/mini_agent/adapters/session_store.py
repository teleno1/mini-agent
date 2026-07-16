"""Durable append-only JSONL Session storage.

The event file is authoritative.  ``metadata.json`` is only a disposable
listing cache, and every writer holds an OS-level exclusive lock for the whole
writer lifetime.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mini_agent.adapters.artifacts import ArtifactNotFoundError, ArtifactStore
from mini_agent.adapters.clocks import SystemClock
from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.application.ports import Clock, IDGenerator
from mini_agent.domain.artifacts import ArtifactReference
from mini_agent.domain.compaction import ContextSummary
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
from mini_agent.instructions import InstructionLoader
from mini_agent.tools.workspace import Workspace


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


class ResumeChoice(StrEnum):
    """The only choices that may resolve an interrupted Tool call."""

    INSPECT = "inspect"
    ABANDON = "abandon"
    RETRY = "retry"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class ResumeToolEvidence:
    """Bounded evidence collected without claiming an uncertain effect succeeded."""

    tool_call_id: str
    name: str
    arguments: dict[str, JSONValue]
    kind: str
    evidence: dict[str, JSONValue]

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "arguments": self.arguments,
            "kind": self.kind,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ResumeInspection:
    """Validated, read-only recovery facts for a Session Resume attempt."""

    snapshot: SessionSnapshot
    interrupted_tools: tuple[ResumeToolEvidence, ...]
    open_request_ids: tuple[str, ...]
    previous_instruction_hashes: tuple[tuple[str, str], ...]
    current_instruction_hashes: tuple[tuple[str, str], ...]
    instruction_change: bool
    blocked_reason: str | None = None

    @property
    def requires_recovery(self) -> bool:
        return bool(self.snapshot.projection and self.snapshot.projection.current_turn)

    @property
    def safe_to_continue(self) -> bool:
        return not self.requires_recovery and self.blocked_reason is None

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "session_id": self.snapshot.session_id,
            "interrupted_tools": cast(
                list[JSONValue], [item.as_dict() for item in self.interrupted_tools]
            ),
            "open_request_ids": list(self.open_request_ids),
            "previous_instruction_hashes": cast(
                list[JSONValue], _hash_records(self.previous_instruction_hashes)
            ),
            "current_instruction_hashes": cast(
                list[JSONValue], _hash_records(self.current_instruction_hashes)
            ),
            "instruction_change": self.instruction_change,
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """Result of one explicit Resume recovery choice."""

    inspection: ResumeInspection
    choice: ResumeChoice
    resumed: ResumedSession | None


class SessionRecoveryRequired(SessionNotResumableError):
    """Raised with validated evidence when a Session needs a user choice."""

    def __init__(self, inspection: ResumeInspection) -> None:
        self.inspection = inspection
        super().__init__(
            f"Session {inspection.snapshot.session_id} has interrupted work requiring "
            "inspect, abandon, retry, or exit"
        )


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

    @property
    def configuration_overrides(self) -> Mapping[str, JSONValue]:
        return self.projection.configuration_overrides

    @property
    def context_manifests(self) -> tuple[dict[str, JSONValue], ...]:
        return self.projection.context_manifests

    @property
    def context_summary(self) -> ContextSummary | None:
        return self.projection.context_summary

    @property
    def summary_boundary(self) -> int:
        return self.projection.summary_boundary


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
        except OSError as exc:
            raise SessionPersistenceError("could not durably acquire Session lock") from exc

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
        self._projection: SessionProjection | None = rebuild_projection(events) if events else None
        self._artifact_store = ArtifactStore(directory, id_generator=store._id_generator)
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

    def write_artifact(
        self,
        content: bytes,
        *,
        media_type: str,
        preview_bytes: int = 4 * 1024,
    ) -> ArtifactReference:
        """Write an Artifact while this writer's exclusive Session lock is held."""

        self._ensure_open()
        return self._artifact_store.write(
            content,
            media_type=media_type,
            preview_bytes=preview_bytes,
        )

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

        original_size = self.events_path.stat().st_size
        try:
            with self.events_path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # A failed fsync makes the append's durability unknowable.  Roll
            # back the visible bytes best-effort and let the caller stop; it
            # must not append a compensating event after this boundary.
            try:
                with self.events_path.open("r+b") as handle:
                    handle.truncate(original_size)
                    handle.flush()
            except OSError:
                pass
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

        inspection = self.inspect_resume(session_id)
        if inspection.snapshot.read_only:
            raise SessionReadOnlyError(
                f"Session {session_id} uses a newer schema and cannot Resume"
            )
        if inspection.blocked_reason is not None:
            raise SessionNotResumableError(inspection.blocked_reason)
        if inspection.requires_recovery:
            raise SessionRecoveryRequired(inspection)
        return ResumedSession(inspection.snapshot)

    def inspect_resume(self, session_id: str) -> ResumeInspection:
        """Validate a Resume and collect evidence without replaying any Tool."""

        snapshot = self.read(session_id)
        return _build_resume_inspection(
            snapshot,
            self.workspace_root,
            self._session_directory(session_id),
        )

    def reconcile_resume(
        self,
        session_id: str,
        choice: ResumeChoice | str,
    ) -> ResumeOutcome:
        """Apply one explicit recovery choice while holding the Session lock.

        ``inspect`` records only the evidence review.  ``exit`` performs no
        append at all.  ``abandon`` and ``retry`` first close every uncertain
        Tool with exactly one ``tool.interrupted`` result and fail the old Turn;
        the caller may then start a fresh Turn, which revalidates and
        reauthorizes any new Tool call.
        """

        selected = ResumeChoice(choice)
        inspection = self.inspect_resume(session_id)
        if inspection.snapshot.read_only:
            raise SessionReadOnlyError(
                f"Session {session_id} uses a newer schema and cannot Resume"
            )
        if selected is ResumeChoice.EXIT:
            return ResumeOutcome(inspection, selected, None)
        if inspection.blocked_reason is not None:
            raise SessionNotResumableError(inspection.blocked_reason)
        if not inspection.requires_recovery:
            return ResumeOutcome(inspection, selected, ResumedSession(inspection.snapshot))

        with self._open_writer(session_id, force_stale_lock=False) as writer:
            current_snapshot = SessionSnapshot(
                session_id=session_id,
                events=writer.events,
                projection=writer.projection,
                read_only=False,
                recovery_warnings=inspection.snapshot.recovery_warnings,
            )
            current = _build_resume_inspection(
                current_snapshot,
                self.workspace_root,
                self._session_directory(session_id),
            )
            if current.blocked_reason is not None:
                raise SessionNotResumableError(current.blocked_reason)
            if not current.requires_recovery:
                return ResumeOutcome(current, selected, ResumedSession(current.snapshot))
            if selected is ResumeChoice.INSPECT:
                writer.append(
                    SessionEventType.RESUME_RECOVERY_INSPECTED,
                    {"inspection": current.as_dict()},
                    turn_id=_active_turn_id(current.snapshot),
                )
                return ResumeOutcome(current, selected, None)

            turn_id = _active_turn_id(current.snapshot)
            if turn_id is None:
                raise SessionNotResumableError("Resume lost its active Turn during validation")
            if current.instruction_change:
                writer.append(
                    SessionEventType.INSTRUCTION_CHANGED,
                    {
                        "previous_hashes": cast(
                            list[JSONValue], _hash_records(current.previous_instruction_hashes)
                        ),
                        "current_hashes": cast(
                            list[JSONValue], _hash_records(current.current_instruction_hashes)
                        ),
                        "source": "validated-resume",
                    },
                    turn_id=turn_id,
                )
            recovery_event = (
                SessionEventType.RESUME_RECOVERY_RETRIED
                if selected is ResumeChoice.RETRY
                else SessionEventType.RESUME_RECOVERY_ABANDONED
            )
            writer.append(
                recovery_event,
                {
                    "choice": selected.value,
                    "tools": [item.as_dict() for item in current.interrupted_tools],
                    "open_request_ids": list(current.open_request_ids),
                },
                turn_id=turn_id,
            )
            for item in current.interrupted_tools:
                _append_interrupted_tool(writer, item, turn_id=turn_id)
                _clear_recovery_sidecar(self._session_directory(session_id), item.tool_call_id)
            for request_id in current.open_request_ids:
                writer.append(
                    SessionEventType.MODEL_REQUEST_FAILED,
                    {
                        "request_id": request_id,
                        "category": "cancellation",
                        "code": "resume-interrupted",
                        "description": "the Provider request was not closed before process exit",
                        "retryable": False,
                    },
                    turn_id=turn_id,
                )
            if current.snapshot.projection is not None and current.snapshot.projection.current_plan:
                plan = current.snapshot.projection.current_plan
                writer.append(
                    SessionEventType.PLAN_RESET,
                    {
                        "plan_id": plan.plan_id,
                        "reason": "interrupted Turn cannot be resumed as a coroutine",
                    },
                    turn_id=turn_id,
                )
            writer.append(
                SessionEventType.TURN_FAILED,
                {
                    "outcome": "interrupted-recovery",
                    "choice": selected.value,
                    "uncertain_tool_count": len(current.interrupted_tools),
                },
                turn_id=turn_id,
            )
            final_snapshot = SessionSnapshot(
                session_id=session_id,
                events=writer.events,
                projection=writer.projection,
                read_only=False,
                recovery_warnings=inspection.snapshot.recovery_warnings,
            )
            return ResumeOutcome(current, selected, ResumedSession(final_snapshot))

    def record_instruction_change(self, session_id: str) -> bool:
        """Persist a validated instruction change for an otherwise idle Session."""

        inspection = self.inspect_resume(session_id)
        if inspection.snapshot.read_only:
            raise SessionReadOnlyError(
                f"Session {session_id} uses a newer schema and cannot Resume"
            )
        if inspection.blocked_reason is not None:
            raise SessionNotResumableError(inspection.blocked_reason)
        if inspection.requires_recovery:
            raise SessionRecoveryRequired(inspection)
        if not inspection.instruction_change:
            return False
        with self._open_writer(session_id, force_stale_lock=False) as writer:
            current_snapshot = SessionSnapshot(
                session_id=session_id,
                events=writer.events,
                projection=writer.projection,
                read_only=False,
                recovery_warnings=inspection.snapshot.recovery_warnings,
            )
            current = _build_resume_inspection(
                current_snapshot,
                self.workspace_root,
                self._session_directory(session_id),
            )
            if current.blocked_reason is not None:
                raise SessionNotResumableError(current.blocked_reason)
            if current.requires_recovery:
                raise SessionRecoveryRequired(current)
            if not current.instruction_change:
                return False
            writer.append(
                SessionEventType.INSTRUCTION_CHANGED,
                {
                    "previous_hashes": cast(
                        list[JSONValue], _hash_records(current.previous_instruction_hashes)
                    ),
                    "current_hashes": cast(
                        list[JSONValue], _hash_records(current.current_instruction_hashes)
                    ),
                    "source": "validated-resume",
                },
            )
        return True

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

    def read_artifact(
        self,
        session_id: str,
        artifact_id: str,
        *,
        start_byte: int,
        max_bytes: int,
    ) -> tuple[ArtifactReference, bytes, bool]:
        """Resolve a model-visible identity from durable events and read a range."""

        directory = self._session_directory(session_id)
        events_path = directory / "events.jsonl"
        lock_path = directory / "writer.lock"
        lock_evidence = _read_lock_evidence(lock_path) if lock_path.exists() else None
        if lock_evidence is not None and lock_evidence.pid == os.getpid():
            loaded = _load_events(events_path, session_id, repair=False)
            projection = rebuild_projection(loaded.events) if not loaded.newer_schema else None
            snapshot = SessionSnapshot(
                session_id=session_id,
                events=loaded.events,
                projection=projection,
                read_only=loaded.newer_schema,
                recovery_warnings=loaded.warnings,
            )
        else:
            snapshot = self.read(session_id)
        if snapshot.projection is None:
            raise ArtifactNotFoundError(
                "Artifact references are unavailable in a newer Session schema"
            )
        try:
            reference = next(
                artifact
                for artifact in snapshot.projection.artifacts
                if artifact.artifact_id == artifact_id
            )
        except StopIteration as exc:
            raise ArtifactNotFoundError("Artifact identity is not known to this Session") from exc
        content, truncated = ArtifactStore(directory, id_generator=self._id_generator).read(
            reference, start_byte=start_byte, max_bytes=max_bytes
        )
        return reference, content, truncated

    def list_artifact_orphans(self, session_id: str) -> tuple[Path, ...]:
        """Return Artifact files not referenced by any terminal Tool event."""

        snapshot = self.read(session_id)
        committed: set[str] = set()
        terminal_types = {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
        for event in snapshot.events:
            if event.event_type not in terminal_types:
                continue
            value = event.payload.get("artifact")
            if isinstance(value, dict):
                artifact_id = value.get("artifact_id")
                if isinstance(artifact_id, str):
                    committed.add(artifact_id)
        return ArtifactStore(self._session_directory(session_id)).list_orphans(committed)

    find_artifact_orphans = list_artifact_orphans

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


def _build_resume_inspection(
    snapshot: SessionSnapshot,
    workspace_root: Path,
    session_directory: Path,
) -> ResumeInspection:
    previous_hashes = _manifest_instruction_hashes(snapshot)
    current_hashes: tuple[tuple[str, str], ...] = ()
    blocked_reason: str | None = None
    if snapshot.read_only or snapshot.projection is None:
        blocked_reason = "a newer Session schema is read-only; Resume is blocked"
    else:
        try:
            instructions = InstructionLoader(workspace_root).load(
                _resume_instruction_targets(snapshot.events, previous_hashes)
            )
            current_hashes = instructions.hashes
            if instructions.conflicts or instructions.issues:
                blocked_reason = (
                    "current AGENTS.md instructions are conflicting, unreadable, or oversized; "
                    "unsafe continuation is blocked"
                )
        except Exception as exc:
            blocked_reason = f"current AGENTS.md instructions could not be validated: {exc}"

    sidecars, sidecar_error = _load_recovery_sidecars(session_directory)
    if sidecar_error is not None:
        blocked_reason = sidecar_error

    interrupted: list[ResumeToolEvidence] = []
    open_requests: tuple[str, ...] = ()
    projection = snapshot.projection
    if projection is not None and projection.current_turn is not None:
        turn_id = projection.current_turn.turn_id
        open_requests = _open_request_ids(snapshot.events, turn_id)
        for call in projection.current_turn.tool_calls:
            if call.status.value != "started":
                continue
            start_record = _started_tool_record(snapshot.events, call.tool_call_id)
            sidecar = sidecars.get(call.tool_call_id, {})
            interrupted.append(
                _tool_evidence(
                    call.tool_call_id,
                    call.name,
                    call.arguments,
                    start_record,
                    sidecar,
                    workspace_root,
                    snapshot.session_id,
                    session_directory,
                )
            )

    started_ids = {
        event.payload.get("tool_call_id")
        for event in snapshot.events
        if event.event_type == SessionEventType.TOOL_STARTED
    }
    terminal_ids = {
        event.payload.get("tool_call_id")
        for event in snapshot.events
        if event.event_type
        in {
            SessionEventType.TOOL_COMPLETED,
            SessionEventType.TOOL_FAILED,
            SessionEventType.TOOL_INTERRUPTED,
        }
    }
    orphaned = [
        call_id
        for call_id in sidecars
        if call_id not in started_ids and call_id not in terminal_ids
    ]
    if orphaned:
        blocked_reason = (
            "unresolved Tool persistence evidence exists without a durable tool.started "
            f"event: {', '.join(sorted(orphaned))}"
        )

    return ResumeInspection(
        snapshot=snapshot,
        interrupted_tools=tuple(interrupted),
        open_request_ids=open_requests,
        previous_instruction_hashes=previous_hashes,
        current_instruction_hashes=current_hashes,
        instruction_change=previous_hashes != current_hashes,
        blocked_reason=blocked_reason,
    )


def _manifest_instruction_hashes(snapshot: SessionSnapshot) -> tuple[tuple[str, str], ...]:
    if snapshot.projection is None or not snapshot.projection.context_manifests:
        return ()
    raw = snapshot.projection.context_manifests[-1].get("instruction_hashes", [])
    if not isinstance(raw, list):
        return ()
    result: list[tuple[str, str]] = []
    for item in raw:
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
        ):
            path_value = item.get("path")
            digest_value = item.get("sha256")
            if isinstance(path_value, str) and isinstance(digest_value, str):
                result.append((path_value, digest_value))
    return tuple(result)


def _resume_instruction_targets(
    events: tuple[SessionEvent, ...],
    previous_hashes: tuple[tuple[str, str], ...] = (),
) -> tuple[str, ...]:
    targets: list[str] = [path for path, _ in previous_hashes]
    for event in events:
        if event.event_type != SessionEventType.TOOL_VALIDATED:
            continue
        arguments = event.payload.get("arguments")
        if not isinstance(arguments, dict):
            continue
        name = event.payload.get("name")
        if name == "read_file" and isinstance(arguments.get("path"), str):
            targets.append(cast(str, arguments["path"]))
        elif name == "search_files" and isinstance(arguments.get("directory"), str):
            targets.append(cast(str, arguments["directory"]))
        elif name == "shell" and isinstance(arguments.get("working_directory"), str):
            targets.append(cast(str, arguments["working_directory"]))
        elif name in {"apply_patch", "create_file"}:
            operations = arguments.get("operations")
            if name == "create_file":
                operations = [arguments]
            if isinstance(operations, list):
                for operation in operations:
                    if isinstance(operation, dict) and isinstance(operation.get("path"), str):
                        targets.append(cast(str, operation["path"]))
    return tuple(dict.fromkeys(targets))


def _open_request_ids(events: tuple[SessionEvent, ...], turn_id: str) -> tuple[str, ...]:
    started: list[str] = []
    closed: set[str] = set()
    for event in events:
        if event.turn_id != turn_id:
            continue
        if event.event_type == SessionEventType.MODEL_REQUEST_STARTED:
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                started.append(request_id)
        elif event.event_type in {
            SessionEventType.MODEL_REQUEST_COMPLETED,
            SessionEventType.MODEL_REQUEST_FAILED,
        }:
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                closed.add(request_id)
    return tuple(request_id for request_id in started if request_id not in closed)


def _started_tool_record(
    events: tuple[SessionEvent, ...], tool_call_id: str
) -> dict[str, JSONValue]:
    for event in reversed(events):
        if (
            event.event_type == SessionEventType.TOOL_STARTED
            and event.payload.get("tool_call_id") == tool_call_id
        ):
            return dict(event.payload)
    return {}


def _load_recovery_sidecars(
    session_directory: Path,
) -> tuple[dict[str, dict[str, JSONValue]], str | None]:
    directory = session_directory / "recovery"
    if not directory.exists():
        return {}, None
    records: dict[str, dict[str, JSONValue]] = {}
    try:
        paths = sorted(directory.glob("*.json"))
        for path in paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(record.get("tool_call_id"), str):
                return {}, f"recovery evidence is malformed: {path.name}"
            records[record["tool_call_id"]] = cast(dict[str, JSONValue], record)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"recovery evidence cannot be read safely: {exc}"
    return records, None


def _clear_recovery_sidecar(session_directory: Path, tool_call_id: str) -> None:
    path = (
        session_directory
        / "recovery"
        / (f"{hashlib.sha256(tool_call_id.encode('utf-8')).hexdigest()}.json")
    )
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # The terminal event is authoritative; an orphaned evidence file is
        # retained for the next inspection if cleanup cannot be proven.
        pass


def _tool_evidence(
    tool_call_id: str,
    name: str,
    arguments: dict[str, JSONValue],
    started_record: dict[str, JSONValue],
    sidecar: dict[str, JSONValue],
    workspace_root: Path,
    session_id: str,
    session_directory: Path,
) -> ResumeToolEvidence:
    merged_sidecar = dict(sidecar)
    started_recovery = started_record.get("recovery")
    if isinstance(started_recovery, dict):
        for key, value in started_recovery.items():
            merged_sidecar.setdefault(key, value)
    workspace = Workspace(
        workspace_root,
        checkpoint_directory=session_directory / "checkpoints",
    )
    if name in {"read_file", "search_files"}:
        evidence = _read_tool_evidence(workspace, name, arguments)
        kind = "read"
    elif name in {"apply_patch", "create_file"}:
        evidence = _patch_tool_evidence(
            workspace,
            name,
            arguments,
            merged_sidecar,
            session_directory,
        )
        kind = "patch"
    elif name == "shell":
        evidence = _shell_tool_evidence(merged_sidecar)
        kind = "shell"
    else:
        evidence = {"state": "unknown", "sidecar": merged_sidecar}
        kind = "unknown"
    return ResumeToolEvidence(tool_call_id, name, arguments, kind, evidence)


def _read_tool_evidence(
    workspace: Workspace,
    name: str,
    arguments: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    target_value = arguments.get("path") if name == "read_file" else arguments.get("directory", ".")
    target = target_value if isinstance(target_value, str) else "."
    try:
        resolved = workspace.resolve_read(target, directory=name == "search_files")
        raw = resolved.path.read_bytes() if name == "read_file" else b""
        if name == "search_files":
            return {
                "state": "directory-available",
                "target": resolved.relative_path,
                "query": arguments.get("query", arguments.get("pattern", "")),
                "glob": arguments.get("glob", arguments.get("file_glob")),
                "regex": arguments.get("regex", False),
                "recommendation": "retry as a new validated call if the observation is needed",
            }
        return {
            "state": "available",
            "target": resolved.relative_path,
            "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "preview": raw[:4096].decode("utf-8", errors="replace") if raw else "",
            "recommendation": "retry as a new validated call if the observation is needed",
        }
    except Exception as exc:
        return {
            "state": "changed-or-unavailable",
            "target": target,
            "error": type(exc).__name__,
            "recommendation": "retry as a new validated call if the observation is needed",
        }


def _patch_tool_evidence(
    workspace: Workspace,
    name: str,
    arguments: dict[str, JSONValue],
    sidecar: dict[str, JSONValue],
    session_directory: Path,
) -> dict[str, JSONValue]:
    operations = arguments.get("operations")
    if name == "create_file":
        operations = [
            {
                "operation": "add",
                "path": arguments.get("path"),
                "new_text": arguments.get("content"),
            }
        ]
    if not isinstance(operations, list):
        return {"state": "unknown", "reason": "Patch arguments were not durable"}

    checkpoint_id = sidecar.get("checkpoint_id")
    checkpoint_directory = workspace.checkpoint_directory
    if not isinstance(checkpoint_id, str):
        candidates = sorted(
            checkpoint_directory.glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        checkpoint_id = candidates[0].parent.name if candidates else None
    manifest: dict[str, Any] = {}
    if isinstance(checkpoint_id, str):
        manifest_path = checkpoint_directory / checkpoint_id / "manifest.json"
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw_manifest, dict):
                manifest = raw_manifest
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            manifest = {}
    before_by_path: dict[str, tuple[str | None, bytes | None]] = {}
    raw_files = manifest.get("files", [])
    if isinstance(raw_files, list) and isinstance(checkpoint_id, str):
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            before: bytes | None = None
            if item.get("existed") is True:
                before_path = checkpoint_directory / checkpoint_id / "before" / f"{index:04d}.bin"
                try:
                    before = before_path.read_bytes()
                except OSError:
                    before = None
            digest = item.get("sha256") if isinstance(item.get("sha256"), str) else None
            before_by_path[item["path"]] = (digest, before)

    files: list[dict[str, JSONValue]] = []
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("path"), str):
            continue
        relative = cast(str, operation["path"])
        before_hash, before_bytes = before_by_path.get(relative, (None, None))
        current_hash: str | None = None
        try:
            current = workspace.resolve_write(relative, allow_missing=True)
            if current.existed:
                current_hash = hashlib.sha256(current.path.read_bytes()).hexdigest()
        except Exception:
            current_hash = None
        expected_new: str | None = None
        operation_name = operation.get("operation", operation.get("op"))
        new_text = operation.get("new_text", operation.get("new"))
        if operation_name == "add" and isinstance(new_text, str):
            expected_new = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
        elif operation_name == "update" and isinstance(new_text, str) and before_bytes is not None:
            old_text = operation.get("old_text", operation.get("old"))
            if isinstance(old_text, str):
                try:
                    updated = before_bytes.decode("utf-8").replace(old_text, new_text, 1)
                    expected_new = hashlib.sha256(updated.encode("utf-8")).hexdigest()
                except UnicodeDecodeError:
                    expected_new = None
        state = "unknown"
        if current_hash == expected_new and expected_new is not None:
            state = "present-as-expected"
        elif current_hash == before_hash:
            state = "still-at-before-image"
        elif current_hash is None and operation_name == "delete" and before_hash is not None:
            state = "absent-as-expected"
        elif current_hash is not None or before_hash is not None:
            state = "changed-or-partial"
        files.append(
            {
                "path": relative,
                "operation": operation_name if isinstance(operation_name, str) else "unknown",
                "before_sha256": before_hash,
                "current_sha256": current_hash,
                "expected_new_sha256": expected_new,
                "state": state,
            }
        )
    states = {cast(str, item["state"]) for item in files if isinstance(item.get("state"), str)}
    if states and states <= {"still-at-before-image", "absent-as-expected"}:
        overall = "unchanged"
    elif states and states <= {"present-as-expected", "absent-as-expected"}:
        overall = "all-expected-bytes-present-but-not-proven"
    elif "changed-or-partial" in states:
        overall = "partial-or-raced"
    else:
        overall = "unknown"
    return {
        "state": overall,
        "checkpoint_id": checkpoint_id,
        "files": cast(list[JSONValue], files),
        "sidecar": sidecar,
        "note": "Hashes are evidence only; Resume never converts them into success.",
    }


def _shell_tool_evidence(sidecar: dict[str, JSONValue]) -> dict[str, JSONValue]:
    evidence = dict(sidecar)
    raw_process = evidence.get("process_evidence")
    process = dict(raw_process) if isinstance(raw_process, dict) else {}
    pid = process.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        process["alive"] = _process_alive(pid)
    evidence["process_evidence"] = process
    evidence.setdefault("state", "unknown")
    evidence.setdefault(
        "note",
        "Process and output evidence cannot prove that an uncertain Shell call succeeded.",
    )
    return evidence


def _append_interrupted_tool(
    writer: SessionWriter,
    evidence: ResumeToolEvidence,
    *,
    turn_id: str,
) -> SessionEvent:
    result: dict[str, JSONValue] = {
        "tool_call_id": evidence.tool_call_id,
        "tool_name": evidence.name,
        "outcome": "interrupted",
        "data": {
            "recovery": evidence.as_dict(),
            "confirmed_effect": False,
        },
        "error": {
            "category": "recovery",
            "code": "uncertain-interruption",
            "message": (
                "Tool side effects were uncertain after process exit; no success was assumed"
            ),
        },
    }
    result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return writer.append(
        SessionEventType.TOOL_INTERRUPTED,
        {
            "tool_call_id": evidence.tool_call_id,
            "name": evidence.name,
            "outcome": "interrupted",
            "result": result,
            "result_text": result_text,
            "recovery": evidence.as_dict(),
        },
        turn_id=turn_id,
        causation_id=evidence.tool_call_id,
    )


def _active_turn_id(snapshot: SessionSnapshot) -> str | None:
    if snapshot.projection is None or snapshot.projection.current_turn is None:
        return None
    return snapshot.projection.current_turn.turn_id


def _hash_records(values: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"path": path, "sha256": digest} for path, digest in values]


def _looks_like_partial_json(text: str, error: json.JSONDecodeError) -> bool:
    """Recognize an incomplete tail without forgiving a complete bad record."""

    stripped = text.rstrip()
    if not stripped:
        return True
    if error.msg.startswith("Unterminated"):
        return True
    return stripped.count("{") > stripped.count("}") or stripped.count("[") > stripped.count("]")


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
            tail_record = json.loads(tail_text)
        except UnicodeDecodeError:
            if not repair:
                raise SessionCorruptionError("Session ends with a partial JSON line")
            message = f"repaired trailing partial JSON line in {path}"
            _truncate(path, data[:boundary])
            warnings.warn(message, PartialTailWarning, stacklevel=3)
            warnings_seen.append(message)
            data = data[:boundary]
        except json.JSONDecodeError as exc:
            if not repair or not _looks_like_partial_json(tail_text, exc):
                raise SessionCorruptionError("Session ends with malformed JSON") from exc
            message = f"repaired trailing partial JSON line in {path}"
            _truncate(path, data[:boundary])
            warnings.warn(message, PartialTailWarning, stacklevel=3)
            warnings_seen.append(message)
            data = data[:boundary]
        else:
            # A valid final object without its framing newline is safe to keep,
            # but normalize it before a future append so events cannot merge.
            schema_value = tail_record.get("schema_version", tail_record.get("version", 0))
            if repair and not (
                isinstance(schema_value, int)
                and not isinstance(schema_value, bool)
                and schema_value > CURRENT_SCHEMA_VERSION
            ):
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
