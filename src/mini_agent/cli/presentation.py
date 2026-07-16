"""Production-facing conversational terminal presentation.

The Agent Loop remains UI-independent.  This module translates its normalized
stream and durable lifecycle observations into a small transcript that is
useful in a terminal and safe to pipe into another process.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping

import typer
from typer import Abort

from mini_agent.application.permissions import (
    ConfirmationChoice,
    PermissionPreview,
    UserInteraction,
)
from mini_agent.application.rendering import BoundedStreamRenderer
from mini_agent.domain.reports import CompletionReport
from mini_agent.domain.sessions import JSONValue
from mini_agent.domain.streams import ResponseCompleted, ResponseFailed, StreamEvent, TextDelta

type Output = Callable[[str], object]


class ConversationPresenter:
    """Render one conversational Turn without diagnostic dashboard concepts."""

    def __init__(
        self,
        *,
        output: Output | None = None,
        interactive: bool = False,
    ) -> None:
        self._output = output or (lambda text: typer.echo(text, nl=False))
        self.interactive = interactive
        self._line_open = False
        self._response_has_text = False
        self._status_active = False
        self._status_text = ""
        self._plan_line_length = 0
        self._current_calls: dict[str, tuple[str, Mapping[str, object]]] = {}
        self._last_plan_signature: str | None = None
        self._stream = BoundedStreamRenderer(
            plain_sink=self._write_fragment,
            max_queue_size=64,
        )

    @property
    def aggregate_text(self) -> str:
        return self._stream.aggregate_text

    @property
    def incomplete(self) -> bool:
        return self._stream.incomplete

    def user(self, task: str) -> None:
        self._line()
        self._emit(f"You: {task}")

    async def observe(self, event: StreamEvent) -> None:
        if isinstance(event, ResponseCompleted):
            self._clear_status()
            await self._stream.observe(event)
            await self._stream.wait_idle()
            if self._response_has_text:
                self._line()
            self._response_has_text = False
            return
        if isinstance(event, ResponseFailed):
            self._clear_status()
            if not self._response_has_text:
                self._write_fragment("Agent: ")
                self._response_has_text = True
            await self._stream.observe(event)
            await self._stream.wait_idle()
            self._line()
            self._response_has_text = False
            return
        if isinstance(event, TextDelta):
            self._clear_status()
            if not self._response_has_text:
                self._write_fragment("Agent: ")
                self._response_has_text = True
        await self._stream.observe(event)

    async def mark_incomplete(self) -> None:
        """Close a cancelled response with an explicit partial-output marker."""

        if not self._response_has_text:
            return
        self._clear_status()
        await self._stream.wait_idle()
        self._write_fragment(" [stream incomplete]")
        self._line()
        self._response_has_text = False

    def on_lifecycle(self, event_type: str, payload: Mapping[str, JSONValue]) -> None:
        """Render a durable lifecycle fact as one concise audit/update line."""

        if event_type == "tool.proposed":
            call_id = _string(payload.get("tool_call_id"))
            arguments = payload.get("arguments")
            self._current_calls[call_id] = (
                _string(payload.get("name")),
                arguments if isinstance(arguments, Mapping) else {},
            )
            return
        if event_type == "tool.completed" or event_type in {
            "tool.failed",
            "tool.interrupted",
        }:
            self._clear_status()
            self._render_tool_terminal(payload)
            return
        if event_type == "model.request.started":
            self._status("Thinking...")
            return
        if event_type == "model.request.retrying":
            self._clear_status()
            attempt = _string(payload.get("attempt")) or "?"
            maximum = _string(payload.get("max_attempts")) or "?"
            self._line()
            self._emit(f"  Provider retry {attempt}/{maximum}; continuing from durable state.")
            return
        if event_type == "tool.started":
            name = _string(payload.get("name"))
            label = {
                "read_file": "Reading...",
                "search_files": "Searching...",
                "apply_patch": "Applying change...",
                "create_file": "Creating file...",
                "shell": "Running verification...",
            }.get(name, "Working...")
            self._status(label)
            return
        if event_type == "plan.updated":
            self._render_plan(payload)
            return
        if event_type == "context.compaction.started":
            self._line()
            self._emit("  Context pressure detected; compacting observable Session state...")
            return
        if event_type == "context.compacted":
            kind = _string(payload.get("kind")) or "summary"
            self._line()
            self._emit(f"  Context compacted ({kind}); original Session Events are retained.")
            return
        if event_type == "context.compaction.failed":
            self._line()
            self._emit("  Context compaction failed; the Turn was stopped safely.")
            return
        if event_type == "instruction.changed":
            self._line()
            self._emit("  Repository instructions changed; continuation requires review.")

    def permission_block(self, preview: PermissionPreview) -> None:
        """Show only the normalized operation, resources, reason, and choices."""

        self._clear_status()
        self._line()
        self._emit("Permission needed")
        self._emit(f"  Operation: {preview.operation}")
        self._emit(f"  Resources: {', '.join(preview.resources) or '(none)'}")
        self._emit(f"  Reason: {preview.reason}")

    def completion(self, report: CompletionReport) -> None:
        self._line()
        self._emit("Completed")
        self._emit(f"  Outcome: {report.outcome}")
        self._emit(f"  Verification: {', '.join(report.verification)}")
        self._emit(
            "  Changed files: "
            + (", ".join(report.changed_files) if report.changed_files else "none")
        )
        self._emit(
            "  Unresolved work: "
            + ("; ".join(report.unresolved_work) if report.unresolved_work else "none")
        )
        self._emit(f"  Next action: {report.next_action}")

    def failure(self, error_id: str | None) -> None:
        self._line()
        suffix = f" [{error_id}]" if error_id else ""
        self._emit(f"Turn failed{suffix}; the Session remains available for another input.")

    def recovery(self, message: str) -> None:
        self._line()
        self._emit(message)

    async def finish(self) -> None:
        await self._stream.finish()
        self._line()

    def external_line_finished(self) -> None:
        """Synchronize the presenter after a prompt rendered outside its sink."""

        self._output("\n")
        self._line_open = False

    def _status(self, text: str) -> None:
        if not self.interactive:
            return
        self._clear_status()
        self._output(text)
        self._status_active = True
        self._status_text = text

    def _clear_status(self) -> None:
        if self._status_active:
            self._output("\r" + (" " * len(self._status_text)) + "\r")
            self._status_active = False
            self._status_text = ""

    def _render_tool_terminal(self, payload: Mapping[str, JSONValue]) -> None:
        call_id = _string(payload.get("tool_call_id"))
        name, arguments = self._current_calls.pop(call_id, (_string(payload.get("name")), {}))
        outcome = _string(payload.get("outcome")) or "failed"
        marker = "[ok]" if outcome == "success" else "[!]"
        detail = _tool_detail(name, arguments)
        suffix = f" ({detail})" if detail else ""
        status = "completed" if outcome == "success" else outcome
        self._line()
        self._emit(f"  {marker} {name}{suffix} - {status}")

    def _render_plan(self, payload: Mapping[str, JSONValue]) -> None:
        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, Mapping):
            return
        objective = _string(raw_plan.get("objective"))
        raw_steps = raw_plan.get("steps")
        if not isinstance(raw_steps, list):
            return
        steps: list[str] = []
        for item in raw_steps:
            if not isinstance(item, Mapping):
                continue
            status = _string(item.get("status"))
            description = _string(item.get("description"))
            symbol = {"completed": "x", "in-progress": ">"}.get(status, " ")
            steps.append(f"[{symbol}] {description}")
        signature = "|".join([objective, *steps])
        if signature == self._last_plan_signature:
            return
        self._last_plan_signature = signature
        if self.interactive:
            self._clear_status()
            summary = "Plan: " + objective + " | " + " | ".join(steps)
            if self._plan_line_length:
                self._output("\r" + (" " * self._plan_line_length) + "\r")
            self._output(summary)
            self._plan_line_length = len(summary)
            if all("[x]" in step for step in steps):
                self._output("\n")
                self._plan_line_length = 0
            return
        self._line()
        self._emit(f"Plan: {objective}")
        for step in steps:
            self._emit(f"  {step}")

    def _write_fragment(self, text: str) -> None:
        result = self._output(text)
        if inspect.isawaitable(result):
            # The normal CLI output is synchronous.  A custom async sink is
            # supported by BoundedStreamRenderer, but cannot be awaited here;
            # it is intentionally left to the renderer's sink adapter.
            return
        self._line_open = self._line_open or bool(text)

    def _emit(self, text: str) -> None:
        self._output(f"{text}\n")
        self._line_open = False

    def _line(self) -> None:
        if self._plan_line_length:
            self._output("\r" + (" " * self._plan_line_length) + "\r\n")
            self._plan_line_length = 0
        if self._line_open:
            self._output("\n")
            self._line_open = False


class TerminalPermissionInteraction(UserInteraction):
    """Synchronous, fail-closed confirmation adapter for the CLI."""

    def __init__(self, presenter: ConversationPresenter) -> None:
        self.presenter = presenter

    def confirm(self, preview: PermissionPreview) -> ConfirmationChoice:
        self.presenter.permission_block(preview)
        prompt = "  Choose [allow-once/allow-exact-for-session/deny/cancel]"
        while True:
            try:
                value = typer.prompt(prompt, default="deny", show_default=False)
            except (Abort, EOFError, OSError):
                self.presenter.external_line_finished()
                return ConfirmationChoice.DENY
            self.presenter.external_line_finished()
            normalized = value.strip().casefold().replace("_", "-").replace(" ", "-")
            choices = {
                "allow-once": ConfirmationChoice.ALLOW_ONCE,
                "allow": ConfirmationChoice.ALLOW_ONCE,
                "allow-exact-for-session": ConfirmationChoice.ALLOW_FOR_SESSION,
                "allow-for-session": ConfirmationChoice.ALLOW_FOR_SESSION,
                "session": ConfirmationChoice.ALLOW_FOR_SESSION,
                "deny": ConfirmationChoice.DENY,
                "no": ConfirmationChoice.DENY,
                "cancel": ConfirmationChoice.CANCEL,
            }
            if normalized in choices:
                return choices[normalized]
            self.presenter.recovery(
                "  Invalid choice; use allow-once, allow-exact-for-session, deny, or cancel."
            )


def _string(value: object) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)
    return ""


def _tool_detail(name: str, arguments: Mapping[str, object]) -> str:
    if name in {"read_file", "create_file"}:
        return _string(arguments.get("path"))
    if name == "search_files":
        return _string(arguments.get("directory")) or "."
    if name == "apply_patch":
        operations = arguments.get("operations")
        if isinstance(operations, list):
            paths = [
                _string(item.get("path"))
                for item in operations
                if isinstance(item, Mapping) and _string(item.get("path"))
            ]
            return ", ".join(paths[:5])
    if name == "shell":
        command = _string(arguments.get("command"))
        return command[:120] + ("..." if len(command) > 120 else "")
    return ""


__all__ = ["ConversationPresenter", "TerminalPermissionInteraction"]
