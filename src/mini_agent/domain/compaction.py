"""Context budgeting, observable summaries, and loss-aware compaction rules.

Compaction is deliberately a domain operation.  It produces a smaller derived
view of a Session; it never edits the authoritative event history.  Summary
content is limited to facts that can be traced to messages, events, or known
Artifact references.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any, Protocol, cast

from mini_agent.domain.artifacts import ArtifactReference
from mini_agent.domain.messages import (
    AssistantMessage,
    Message,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from mini_agent.domain.plans import PlanSnapshot

SUMMARY_SCHEMA_VERSION = 1
MIN_RESPONSE_RESERVE_TOKENS = 16_000
RESPONSE_RESERVE_FRACTION = 0.20
SMALL_WINDOW_RESERVE_FRACTION = 0.30
DEFAULT_RECENT_MESSAGE_COUNT = 8
DEFAULT_SUMMARY_TEXT_LIMIT = 1_024

SUMMARY_FIELDS = (
    "objective",
    "constraints",
    "decisions",
    "plan",
    "files",
    "commands_results",
    "failures",
    "unresolved_work",
    "next_actions",
    "references",
    "summary_boundary",
)


class ContextCompactionError(RuntimeError):
    """Raised when the host cannot produce a context that fits safely."""


class SummaryValidationError(ValueError):
    """Raised when a Context Summary is not a trusted observable state."""


def response_reserve_tokens(
    context_window_tokens: int,
    configured_reserve_tokens: int | None = None,
) -> int:
    """Return a conservative response reserve for a context window.

    The normal reserve is the larger of 16,000 tokens and 20% of the window.
    For small windows the reserve is capped at 30% so the input side retains a
    usable budget.  An explicit configuration is respected, subject to that
    small-window cap.
    """

    if isinstance(context_window_tokens, bool) or context_window_tokens < 1:
        raise ValueError("context_window_tokens must be positive")
    normal = max(
        MIN_RESPONSE_RESERVE_TOKENS,
        ceil(context_window_tokens * RESPONSE_RESERVE_FRACTION),
    )
    cap = ceil(context_window_tokens * SMALL_WINDOW_RESERVE_FRACTION)
    reserve = normal if configured_reserve_tokens is None else configured_reserve_tokens
    if isinstance(reserve, bool) or reserve < 1:
        raise ValueError("response reserve must be positive")
    return min(reserve, cap) if cap < normal else reserve


@dataclass(slots=True)
class TokenEstimator:
    """Conservative character estimator calibrated by Provider usage."""

    chars_per_token: float = 4.0
    calibration_factor: float = 1.0
    calibration_samples: int = 0

    def __post_init__(self) -> None:
        if self.chars_per_token <= 0 or self.calibration_factor <= 0:
            raise ValueError("token estimator factors must be positive")

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0
        return max(1, ceil(len(text) / self.chars_per_token * self.calibration_factor))

    def estimate_value(self, value: object) -> int:
        if isinstance(value, str):
            return self.estimate_text(value)
        return self.estimate_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def estimate_messages(self, messages: Sequence[Message]) -> int:
        return sum(self.estimate_text(_message_text(message)) for message in messages)

    def estimate_context(self, frame: object) -> int:
        """Estimate a derived Context Frame using its current layer contents."""

        layers = getattr(frame, "layers", ())
        return sum(
            self.estimate_text(content)
            for layer in layers
            if isinstance((content := getattr(layer, "content", None)), str)
        )

    def calibrate(self, estimated_input_tokens: int, actual_input_tokens: int) -> float:
        """Raise the estimator when Provider usage proves it was optimistic."""

        if estimated_input_tokens < 1 or actual_input_tokens < 0:
            raise ValueError("token calibration values are out of range")
        observed_factor = max(1.0, actual_input_tokens / estimated_input_tokens)
        self.calibration_factor = max(self.calibration_factor, observed_factor)
        self.calibration_samples += 1
        return self.calibration_factor

    def calibrate_with_usage(self, estimated_input_tokens: int, usage: object) -> float:
        actual = getattr(usage, "input_tokens", None)
        if isinstance(actual, bool) or not isinstance(actual, int):
            raise TypeError("Provider usage must expose integer input_tokens")
        return self.calibrate(estimated_input_tokens, actual)


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """Validated, structured replacement for older observable Session state."""

    objective: str
    constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    plan: dict[str, object]
    files: tuple[dict[str, object], ...]
    commands_results: tuple[dict[str, object], ...]
    failures: tuple[str, ...]
    unresolved_work: tuple[str, ...]
    next_actions: tuple[str, ...]
    references: tuple[dict[str, object], ...]
    summary_boundary: int
    schema_version: int = SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SUMMARY_SCHEMA_VERSION:
            raise SummaryValidationError("unsupported Context Summary schema")
        if not self.objective.strip():
            raise SummaryValidationError("summary objective cannot be blank")
        if self.summary_boundary < 0:
            raise SummaryValidationError("summary boundary cannot be negative")
        for name in ("constraints", "decisions", "failures", "unresolved_work", "next_actions"):
            values = getattr(self, name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise SummaryValidationError(f"summary {name} must contain non-blank strings")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        events: Sequence[object] = (),
        artifacts: Mapping[str, ArtifactReference] | Sequence[ArtifactReference] = (),
        previous_boundary: int = 0,
    ) -> ContextSummary:
        if not isinstance(value, Mapping):
            raise SummaryValidationError("Context Summary must be an object")
        missing = [key for key in SUMMARY_FIELDS if key not in value]
        if missing:
            raise SummaryValidationError(f"Context Summary is missing fields: {', '.join(missing)}")
        schema_version = value.get("schema_version", SUMMARY_SCHEMA_VERSION)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise SummaryValidationError("summary schema_version must be an integer")
        boundary = value["summary_boundary"]
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            raise SummaryValidationError("summary_boundary must be an integer")
        if boundary < previous_boundary:
            raise SummaryValidationError("summary_boundary must be monotonic")
        maximum_sequence = _max_sequence(events)
        if boundary > maximum_sequence:
            raise SummaryValidationError(
                f"summary_boundary {boundary} exceeds the latest event {maximum_sequence}"
            )
        summary = cls(
            objective=_required_string(value, "objective"),
            constraints=_string_tuple(value, "constraints"),
            decisions=_string_tuple(value, "decisions"),
            plan=_object(value, "plan"),
            files=_object_tuple(value, "files"),
            commands_results=_object_tuple(value, "commands_results"),
            failures=_string_tuple(value, "failures"),
            unresolved_work=_string_tuple(value, "unresolved_work"),
            next_actions=_string_tuple(value, "next_actions"),
            references=_object_tuple(value, "references"),
            summary_boundary=boundary,
            schema_version=schema_version,
        )
        _validate_references(summary.references, events, artifacts, boundary)
        return summary

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "plan": dict(self.plan),
            "files": [dict(item) for item in self.files],
            "commands_results": [dict(item) for item in self.commands_results],
            "failures": list(self.failures),
            "unresolved_work": list(self.unresolved_work),
            "next_actions": list(self.next_actions),
            "references": [dict(item) for item in self.references],
            "summary_boundary": self.summary_boundary,
        }


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """A smaller derived view and an optional validated new summary."""

    history: tuple[Message, ...]
    selected_events: tuple[dict[str, object], ...]
    summary: ContextSummary | None
    summary_boundary: int
    changed: bool
    reason: str


class SummarySource(Protocol):
    def __call__(
        self,
        objective: str,
        history: Sequence[Message],
        events: Sequence[object],
        plan: PlanSnapshot | None,
        artifacts: Sequence[ArtifactReference],
        boundary: int,
    ) -> Mapping[str, object]:
        """Produce observable candidate summary data."""


class ContextCompactor:
    """Perform bounded micro-compaction and validate structured summaries."""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        *,
        recent_message_count: int = DEFAULT_RECENT_MESSAGE_COUNT,
        summary_text_limit: int = DEFAULT_SUMMARY_TEXT_LIMIT,
        summary_source: SummarySource | None = None,
    ) -> None:
        if recent_message_count < 2 or summary_text_limit < 64:
            raise ValueError("compaction limits are too small")
        self.estimator = estimator or TokenEstimator()
        self.recent_message_count = recent_message_count
        self.summary_text_limit = summary_text_limit
        self.summary_source = summary_source or self._fact_summary

    def compact(
        self,
        objective: str,
        history: Sequence[Message],
        *,
        events: Sequence[object] = (),
        selected_events: Sequence[Mapping[str, object]] = (),
        plan: PlanSnapshot | None = None,
        artifacts: Sequence[ArtifactReference] = (),
        existing_summary: ContextSummary | None = None,
        summary_boundary: int = 0,
    ) -> CompactionResult:
        source_events = tuple(events)
        boundary = max(summary_boundary, _max_sequence(source_events))
        compacted_history = self.micro_compact_history(history)
        summary_history = ContextCompactor(
            self.estimator,
            recent_message_count=min(4, self.recent_message_count),
            summary_text_limit=self.summary_text_limit,
        ).micro_compact_history(compacted_history)
        compacted_events = self.micro_compact_events(selected_events, source_events, boundary)
        candidate_data = self.summary_source(
            objective,
            summary_history,
            source_events,
            plan,
            artifacts,
            boundary,
        )
        if existing_summary is not None:
            candidate_data = _recompress_candidate(existing_summary.as_dict(), candidate_data)
        candidate_data = dict(candidate_data)
        candidate_data["summary_boundary"] = boundary
        summary = ContextSummary.from_dict(
            candidate_data,
            events=source_events,
            artifacts=artifacts,
            previous_boundary=summary_boundary,
        )
        changed = compacted_history != tuple(history) or compacted_events != tuple(selected_events)
        if existing_summary is None or summary.as_dict() != existing_summary.as_dict():
            changed = True
        return CompactionResult(
            history=summary_history,
            selected_events=compacted_events,
            summary=summary,
            summary_boundary=boundary,
            changed=changed,
            reason="structured-summary",
        )

    def micro_compact_history(self, history: Sequence[Message]) -> tuple[Message, ...]:
        """Keep recent conversation plus complete structured Tool protocol groups."""

        messages = tuple(history)
        if len(messages) <= self.recent_message_count:
            return messages
        first_recent = max(0, len(messages) - self.recent_message_count)
        keep: set[int] = set(range(first_recent, len(messages)))
        for index, message in enumerate(messages):
            if not isinstance(message, AssistantMessage) or not message.tool_calls:
                continue
            call_ids = {call.tool_call_id for call in message.tool_calls}
            result_ids: set[str] = set()
            cursor = index + 1
            while cursor < len(messages) and isinstance(messages[cursor], ToolResultMessage):
                result = cast(ToolResultMessage, messages[cursor])
                if result.tool_call_id in call_ids:
                    keep.add(cursor)
                    result_ids.add(result.tool_call_id)
                cursor += 1
            # Never retain an orphaned Tool Result without its assistant call.
            if result_ids != call_ids or cursor == index + 1:
                keep.add(index)
            if result_ids != call_ids:
                keep.update(
                    cursor_index
                    for cursor_index in range(index + 1, cursor)
                    if isinstance(messages[cursor_index], ToolResultMessage)
                )
        for index in tuple(keep):
            message = messages[index]
            if not isinstance(message, ToolResultMessage):
                continue
            for previous_index in range(index - 1, -1, -1):
                previous = messages[previous_index]
                if isinstance(previous, AssistantMessage) and any(
                    call.tool_call_id == message.tool_call_id for call in previous.tool_calls
                ):
                    keep.add(previous_index)
                    break
        ordered = tuple(messages[index] for index in sorted(keep))
        return tuple(_compact_message(message, self.summary_text_limit) for message in ordered)

    def micro_compact_events(
        self,
        selected_events: Sequence[Mapping[str, object]],
        events: Sequence[object],
        boundary: int,
    ) -> tuple[dict[str, object], ...]:
        """Drop re-derivable lifecycle noise while preserving unfinished calls."""

        del events
        terminal: dict[str, dict[str, object]] = {}
        active: dict[str, list[dict[str, object]]] = {}
        for raw in selected_events:
            event = dict(raw)
            sequence = event.get("sequence")
            if isinstance(sequence, int) and sequence <= boundary:
                continue
            call_id = event.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            event_type = str(event.get("type", ""))
            if event_type in {"tool.completed", "tool.failed", "tool.interrupted"}:
                terminal[call_id] = event
                active.pop(call_id, None)
            else:
                active.setdefault(call_id, []).append(event)
        flattened: list[dict[str, object]] = []
        for group in active.values():
            flattened.extend(group)
        flattened.extend(terminal.values())
        return tuple(sorted(flattened, key=_dict_sequence))

    def _fact_summary(
        self,
        objective: str,
        history: Sequence[Message],
        events: Sequence[object],
        plan: PlanSnapshot | None,
        artifacts: Sequence[ArtifactReference],
        boundary: int,
    ) -> Mapping[str, object]:
        decisions = [
            _clip(message.content, self.summary_text_limit)
            for message in history
            if isinstance(message, AssistantMessage) and message.content
        ][-8:]
        files: dict[str, dict[str, object]] = {}
        commands: list[dict[str, object]] = []
        commands_by_call: dict[str, dict[str, object]] = {}
        failures: list[str] = []
        unresolved: list[str] = []
        references: list[dict[str, object]] = []
        terminal_ids: set[str] = set()
        for event in events:
            record = _event_record(event)
            sequence = record.get("sequence")
            if not isinstance(sequence, int) or sequence > boundary:
                continue
            event_type = str(record.get("event_type", record.get("type", "")))
            payload = record.get("payload", record)
            if not isinstance(payload, Mapping):
                continue
            call_id = payload.get("tool_call_id")
            if event_type.startswith("tool.") and isinstance(call_id, str):
                references.append(
                    {
                        "kind": "event",
                        "sequence": sequence,
                        "event_id": record.get("event_id"),
                    }
                )
            if event_type == "tool.proposed":
                arguments = payload.get("arguments")
                if isinstance(arguments, Mapping):
                    path = arguments.get("path")
                    if isinstance(path, str):
                        files.setdefault(path, {"path": path, "status": "observed"})
                    command = arguments.get("command")
                    if isinstance(command, str):
                        command_record: dict[str, object] = {
                            "command": _clip(command, self.summary_text_limit),
                            "result": "started",
                        }
                        commands.append(command_record)
                        if isinstance(call_id, str):
                            commands_by_call[call_id] = command_record
            if event_type in {"tool.completed", "tool.failed", "tool.interrupted"}:
                if isinstance(call_id, str):
                    terminal_ids.add(call_id)
                outcome = payload.get("outcome")
                result_text = payload.get("result_text", "")
                result_data = payload.get("result")
                if isinstance(result_data, Mapping):
                    data = result_data.get("data")
                    if isinstance(data, Mapping):
                        changed_files = data.get("changed_files")
                        if isinstance(changed_files, list):
                            for changed_file in changed_files:
                                if isinstance(changed_file, str):
                                    files[changed_file] = {
                                        "path": changed_file,
                                        "status": "changed",
                                    }
                if event_type != "tool.completed" or outcome != "success":
                    failures.append(
                        _clip(
                            (
                                f"Tool {call_id or '<unknown>'} ended with "
                                f"{outcome or 'unknown'}: {result_text}"
                            ),
                            self.summary_text_limit,
                        )
                    )
                if event_type == "tool.completed" and isinstance(result_text, str):
                    name = payload.get("name")
                    if name == "shell" and isinstance(call_id, str):
                        recorded_command = commands_by_call.get(call_id)
                        if recorded_command is not None:
                            recorded_command["result"] = _clip(result_text, self.summary_text_limit)
                        else:
                            commands.append(
                                {
                                    "command": "<recorded shell command>",
                                    "result": _clip(result_text, self.summary_text_limit),
                                }
                            )
        for event in events:
            record = _event_record(event)
            payload = record.get("payload", record)
            if not isinstance(payload, Mapping):
                continue
            if record.get("event_type", record.get("type")) == "tool.proposed":
                call_id = payload.get("tool_call_id")
                if isinstance(call_id, str) and call_id not in terminal_ids:
                    unresolved.append(f"Tool Call {call_id} has no terminal result yet.")
        artifact_refs = [
            {
                "kind": "artifact",
                "artifact_id": artifact.artifact_id,
                "path": artifact.path,
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
            }
            for artifact in artifacts
        ]
        references.extend(artifact_refs)
        return {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "objective": objective.strip(),
            "constraints": [
                "Safety Policy and Permission Policy remain active.",
                "Original Session Events remain authoritative; hidden reasoning is not retained.",
            ],
            "decisions": decisions,
            "plan": plan.as_dict() if plan is not None else {},
            "files": list(files.values()),
            "commands_results": commands[-16:],
            "failures": failures[-16:],
            "unresolved_work": unresolved[-16:],
            "next_actions": [
                "Continue from the latest observable Session state.",
            ],
            "references": references[-64:],
            "summary_boundary": boundary,
        }


def messages_after_boundary(events: Sequence[object], boundary: int) -> tuple[Message, ...]:
    """Rebuild only message observations newer than a Summary Boundary."""

    messages: list[Message] = []
    for event in events:
        record = _event_record(event)
        sequence = record.get("sequence")
        if not isinstance(sequence, int) or sequence <= boundary:
            continue
        event_type = str(record.get("event_type", record.get("type", "")))
        payload = record.get("payload", record)
        if not isinstance(payload, Mapping):
            continue
        if event_type == "user.message" and isinstance(payload.get("content"), str):
            messages.append(UserMessage(payload["content"]))
        elif event_type == "assistant.message" and isinstance(payload.get("content"), str):
            raw_calls = payload.get("tool_calls", [])
            calls: list[ToolCallBlock] = []
            if isinstance(raw_calls, list):
                for raw_call in raw_calls:
                    if not isinstance(raw_call, Mapping):
                        continue
                    call_id = raw_call.get("tool_call_id")
                    name = raw_call.get("name")
                    arguments = raw_call.get("arguments", {})
                    if (
                        isinstance(call_id, str)
                        and isinstance(name, str)
                        and isinstance(arguments, dict)
                    ):
                        calls.append(ToolCallBlock(call_id, name, cast(dict[str, Any], arguments)))
            if payload["content"] or calls:
                messages.append(AssistantMessage(payload["content"], tuple(calls)))
        elif event_type in {"tool.completed", "tool.failed", "tool.interrupted"}:
            call_id = payload.get("tool_call_id")
            result_text = payload.get("result_text", "")
            outcome = payload.get("outcome", "failed")
            if (
                isinstance(call_id, str)
                and isinstance(result_text, str)
                and isinstance(outcome, str)
            ):
                messages.append(ToolResultMessage(call_id, result_text, outcome))
    return _paired_message_history(messages)


def _paired_message_history(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Admit terminal Tool Results only when their assistant call is present."""

    call_ids = {
        call.tool_call_id
        for message in messages
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
    }
    seen_results: set[str] = set()
    paired: list[Message] = []
    for message in messages:
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id not in call_ids or message.tool_call_id in seen_results:
                continue
            seen_results.add(message.tool_call_id)
        paired.append(message)
    return tuple(paired)


def _validate_references(
    references: Sequence[Mapping[str, object]],
    events: Sequence[object],
    artifacts: Mapping[str, ArtifactReference] | Sequence[ArtifactReference],
    boundary: int,
) -> None:
    event_by_sequence = {
        sequence: event
        for event in events
        if isinstance((sequence := _event_record(event).get("sequence")), int)
    }
    if isinstance(artifacts, Mapping):
        artifact_by_id = dict(artifacts)
    else:
        artifact_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    for reference in references:
        kind = reference.get("kind")
        if kind == "event":
            sequence = reference.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise SummaryValidationError("event references require a positive sequence")
            if sequence > boundary or sequence not in event_by_sequence:
                raise SummaryValidationError(
                    f"summary references unknown event sequence {sequence}"
                )
            event_id = reference.get("event_id")
            actual_id = _event_record(event_by_sequence[sequence]).get("event_id")
            if event_id is not None and event_id != actual_id:
                raise SummaryValidationError(
                    f"summary event reference {sequence} has a wrong event ID"
                )
        elif kind == "artifact":
            artifact_id = reference.get("artifact_id")
            if not isinstance(artifact_id, str) or artifact_id not in artifact_by_id:
                raise SummaryValidationError("summary references an unknown Artifact")
            artifact = artifact_by_id[artifact_id]
            for key, actual in {
                "path": artifact.path,
                "media_type": artifact.media_type,
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
            }.items():
                if key in reference and reference[key] != actual:
                    raise SummaryValidationError(f"summary Artifact reference has a wrong {key}")
        else:
            raise SummaryValidationError("summary references must identify an event or Artifact")


def _recompress_candidate(
    previous: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    """Keep the factual fields of an old summary while bounding repeated growth."""

    result = dict(candidate)
    old_objective = previous.get("objective")
    new_objective = result.get("objective")
    if (
        isinstance(old_objective, str)
        and isinstance(new_objective, str)
        and old_objective.strip()
        and old_objective != new_objective
    ):
        result["objective"] = _clip(
            f"{new_objective}\nEarlier durable objective: {old_objective}",
            2_048,
        )
    for field_name in SUMMARY_FIELDS:
        if field_name in {"summary_boundary", "plan"}:
            continue
        old = previous.get(field_name)
        new = result.get(field_name)
        if isinstance(old, list) and isinstance(new, list):
            merged: list[object] = []
            for item in [*old, *new]:
                if item not in merged:
                    merged.append(item)
            result[field_name] = merged[-64:]
        elif isinstance(old, list) and field_name not in result:
            result[field_name] = old[-64:]
    old_references = previous.get("references")
    new_references = result.get("references")
    if isinstance(old_references, list) and isinstance(new_references, list):
        merged_references: list[object] = []
        for reference in [*old_references, *new_references]:
            if reference not in merged_references:
                merged_references.append(reference)
        result["references"] = merged_references[-64:]
    old_plan = previous.get("plan")
    if isinstance(old_plan, dict) and not result.get("plan"):
        result["plan"] = old_plan
    return result


def _compact_message(message: Message, limit: int) -> Message:
    if isinstance(message, ToolResultMessage) and len(message.content) > limit:
        try:
            decoded = json.loads(message.content)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("artifact"), dict):
            artifact = dict(decoded["artifact"])
            if isinstance(artifact.get("preview"), str):
                artifact["preview"] = _clip(artifact["preview"], max(64, limit // 4))
            compacted = json.dumps(
                {"artifact": artifact}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return ToolResultMessage(message.tool_call_id, compacted, message.outcome)
        return ToolResultMessage(
            message.tool_call_id, _clip(message.content, limit), message.outcome
        )
    return message


def _message_text(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        calls = [
            {"tool_call_id": call.tool_call_id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
        return message.content + (
            json.dumps(calls, ensure_ascii=False, sort_keys=True) if calls else ""
        )
    return message.content


def _event_record(event: object) -> dict[str, object]:
    if isinstance(event, Mapping):
        return dict(event)
    to_record = getattr(event, "to_record", None)
    if callable(to_record):
        value = to_record()
        if isinstance(value, dict):
            return cast(dict[str, object], value)
    return {
        "sequence": getattr(event, "sequence", 0),
        "event_id": getattr(event, "event_id", None),
        "event_type": getattr(event, "event_type", ""),
        "payload": getattr(event, "payload", {}),
    }


def _dict_sequence(value: Mapping[str, object]) -> int:
    sequence = value.get("sequence", 0)
    return sequence if isinstance(sequence, int) else 0


def _max_sequence(events: Sequence[object]) -> int:
    return max(
        (
            sequence
            for event in events
            if isinstance((sequence := _event_record(event).get("sequence")), int)
        ),
        default=0,
    )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise SummaryValidationError(f"summary {key} must be a non-blank string")
    return item


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or any(not isinstance(entry, str) for entry in item):
        raise SummaryValidationError(f"summary {key} must be a list of strings")
    return tuple(cast(str, entry) for entry in item)


def _object(value: Mapping[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise SummaryValidationError(f"summary {key} must be an object")
    return dict(item)


def _object_tuple(value: Mapping[str, object], key: str) -> tuple[dict[str, object], ...]:
    item = value.get(key)
    if not isinstance(item, list) or any(not isinstance(entry, dict) for entry in item):
        raise SummaryValidationError(f"summary {key} must be a list of objects")
    return tuple(dict(cast(dict[str, object], entry)) for entry in item)


def _clip(value: object, limit: int) -> str:
    text = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 14)] + "…[truncated]"


__all__ = [
    "SUMMARY_FIELDS",
    "SUMMARY_SCHEMA_VERSION",
    "ContextCompactionError",
    "ContextCompactor",
    "ContextSummary",
    "CompactionResult",
    "MIN_RESPONSE_RESERVE_TOKENS",
    "SummaryValidationError",
    "TokenEstimator",
    "messages_after_boundary",
    "response_reserve_tokens",
]
