"""Redacted, correlated diagnostic records for the local CLI.

Diagnostics are deliberately an adapter concern.  A failed Turn remains a
failed Turn when the log directory is unavailable, and the log never becomes
an alternate source of prompts, credentials, or raw Tool output.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.configuration import ConfigurationError, redact_secrets
from mini_agent.domain.streams import Failure, FailureCategory
from mini_agent.domain.turns import InvalidStream, StreamFailed

if TYPE_CHECKING:
    from mini_agent.application.ports import IDGenerator


MAX_DIAGNOSTIC_FILES = 10
MAX_DIAGNOSTIC_BYTES = 10 * 1024 * 1024
_MAX_RECORD_BYTES = 128 * 1024


class DiagnosticLogger:
    """Append structured redacted failures to a small rotating JSONL set."""

    def __init__(
        self,
        workspace_or_log_directory: Path,
        *,
        id_generator: IDGenerator | None = None,
        max_files: int = MAX_DIAGNOSTIC_FILES,
        max_bytes: int = MAX_DIAGNOSTIC_BYTES,
    ) -> None:
        if max_files < 1 or max_bytes < 1:
            raise ValueError("diagnostic log limits must be positive")
        path = Path(workspace_or_log_directory)
        self.directory = path if path.name == "logs" else path / ".mini-agent" / "logs"
        self.path = self.directory / "diagnostic.jsonl"
        self._id_generator = id_generator or UUIDIdGenerator()
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.last_error_id: str | None = None
        self.last_write_error: str | None = None

    def record(
        self,
        failure: Failure,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        tool_call_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> Failure:
        """Correlate, redact, and best-effort persist one failure."""

        correlated = replace(
            failure,
            failure_id=failure.failure_id or self._id_generator.new_id("failure"),
            session_id=failure.session_id or session_id,
            turn_id=failure.turn_id or turn_id,
            request_id=failure.request_id or request_id,
            tool_call_id=failure.tool_call_id or tool_call_id,
        )
        correlated = replace(
            correlated,
            redacted_description=redact_secrets(correlated.redacted_description),
            required_user_action=redact_secrets(correlated.required_user_action),
            cause=(redact_secrets(correlated.cause) if correlated.cause is not None else None),
            details=cast(dict[str, object], _redacted_json(correlated.details)),
        )
        self.last_error_id = correlated.failure_id
        record = {
            "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
            "level": "INFO",
            "error_id": correlated.failure_id,
            "failure": _redacted_json(correlated.as_dict()),
        }
        try:
            encoded = _encode_record(record)
            self._append(encoded)
            self.last_write_error = None
        except Exception as exc:  # diagnostics must never hide the primary failure
            self.last_write_error = type(exc).__name__
        return correlated

    def record_exception(
        self,
        exc: BaseException,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        tool_call_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> Failure:
        return self.record(
            failure_from_exception(
                exc,
                session_id=session_id,
                turn_id=turn_id,
                request_id=request_id,
                tool_call_id=tool_call_id,
            ),
            timestamp=timestamp,
        )

    def find(self, error_id: str) -> dict[str, object] | None:
        """Resolve one error ID without returning unredacted log material."""

        if not error_id.strip():
            return None
        for path in self._paths():
            try:
                with path.open("rb") as handle:
                    for raw_line in handle:
                        if len(raw_line) > _MAX_RECORD_BYTES:
                            continue
                        try:
                            value = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(value, dict) or value.get("error_id") != error_id:
                            continue
                        return cast(dict[str, object], _redacted_json(value))
            except OSError:
                continue
        return None

    def _paths(self) -> tuple[Path, ...]:
        paths = [self.path]
        paths.extend(
            self.directory / f"diagnostic.{index}.jsonl" for index in range(1, self.max_files)
        )
        return tuple(path for path in paths if path.exists())

    def _append(self, encoded: bytes) -> None:
        if len(encoded) > self.max_bytes:
            encoded = _encode_record(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": "INFO",
                    "error_id": self.last_error_id,
                    "failure": {
                        "category": FailureCategory.INTERNAL.value,
                        "code": "diagnostic-record-too-large",
                        "description": "diagnostic record exceeded its storage bound",
                        "error_id": self.last_error_id,
                    },
                }
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size and current_size + len(encoded) > self.max_bytes:
            self._rotate()
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _rotate(self) -> None:
        oldest = self.directory / f"diagnostic.{self.max_files - 1}.jsonl"
        oldest.unlink(missing_ok=True)
        for index in range(self.max_files - 2, 0, -1):
            source = self.directory / f"diagnostic.{index}.jsonl"
            if source.exists():
                source.replace(self.directory / f"diagnostic.{index + 1}.jsonl")
        if self.path.exists():
            self.path.replace(self.directory / "diagnostic.1.jsonl")


def failure_from_exception(
    exc: BaseException,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    request_id: str | None = None,
    tool_call_id: str | None = None,
) -> Failure:
    """Map arbitrary host exceptions to the stable, redacted Failure taxonomy."""

    if isinstance(exc, StreamFailed):
        provider_failure = exc.event.failure
        return replace(
            provider_failure,
            redacted_description=redact_secrets(provider_failure.redacted_description),
            required_user_action=redact_secrets(provider_failure.required_user_action),
            cause=(redact_secrets(provider_failure.cause) if provider_failure.cause else None),
            details=cast(dict[str, object], _redacted_json(provider_failure.details)),
            session_id=provider_failure.session_id or session_id,
            turn_id=provider_failure.turn_id or turn_id,
            request_id=provider_failure.request_id or request_id,
            tool_call_id=provider_failure.tool_call_id or tool_call_id,
        )
    if isinstance(exc, InvalidStream):
        return _failure(
            FailureCategory.PROVIDER_PROTOCOL,
            "invalid-normalized-stream",
            "the Provider emitted an illegal normalized stream",
            source="application",
            action="inspect the Provider contract",
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )
    if isinstance(exc, asyncio.CancelledError):
        return _failure(
            FailureCategory.CANCELLATION,
            "turn-cancelled",
            "the active Turn was cancelled before it could complete",
            source="application",
            action="inspect the Session before starting another Turn",
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )
    if isinstance(exc, ConfigurationError):
        return _failure(
            FailureCategory.CONFIGURATION,
            "invalid-configuration",
            "Mini Agent configuration is invalid",
            source="configuration",
            action="correct the reported configuration source",
            details={"exception_type": type(exc).__name__},
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )
    exception_type = type(exc).__name__
    if exception_type in {
        "SessionPersistenceError",
        "ArtifactPersistenceError",
    }:
        return _failure(
            FailureCategory.PERSISTENCE,
            "persistence-failed",
            "required Session state could not be durably persisted",
            source="session-store",
            action="inspect the Session and retry as a new Turn",
            details={
                "exception_type": exception_type,
                "durability_uncertain": exception_type == "SessionPersistenceError",
            },
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            tool_call_id=tool_call_id,
        )
    return _failure(
        FailureCategory.INTERNAL,
        "internal-error",
        "Mini Agent could not complete the operation",
        source="application",
        action="inspect the diagnostic error ID",
        details={"exception_type": type(exc).__name__},
        session_id=session_id,
        turn_id=turn_id,
        request_id=request_id,
        tool_call_id=tool_call_id,
    )


def _failure(
    category: FailureCategory,
    code: str,
    description: str,
    *,
    source: str,
    action: str,
    retryable: bool = False,
    cause: str | None = None,
    details: Mapping[str, object] | None = None,
    session_id: str | None,
    turn_id: str | None,
    request_id: str | None,
    tool_call_id: str | None,
) -> Failure:
    return Failure(
        category=category.value,
        code=code,
        source=source,
        redacted_description=redact_secrets(description),
        retryable=retryable,
        required_user_action=redact_secrets(action),
        cause=redact_secrets(cause) if cause is not None else None,
        details=dict(details or {}),
        session_id=session_id,
        turn_id=turn_id,
        request_id=request_id,
        tool_call_id=tool_call_id,
    )


def _encode_record(record: Mapping[str, object]) -> bytes:
    return (json.dumps(_redacted_json(record), ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _redacted_json(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return "<truncated>"
    if isinstance(value, str):
        return redact_secrets(value[:4096])
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in list(value.items())[:100]:
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in {
                "prompt",
                "system_prompt",
                "messages",
                "raw_output",
                "tool_output",
                "stdout",
                "stderr",
            }:
                output[str(key)] = "<omitted>"
            else:
                output[str(key)] = _redacted_json(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_redacted_json(item, depth=depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(value)


__all__ = [
    "DiagnosticLogger",
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_DIAGNOSTIC_FILES",
    "failure_from_exception",
]
