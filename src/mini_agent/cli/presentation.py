"""Production-facing conversational terminal presentation.

The Agent Loop remains UI-independent. This module renders a selected
conversation-block view: user and Agent messages are prominent, while Tools,
permissions, Plans, and status remain supporting information.
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
        context_window_tokens: int = 32_000,
    ) -> None:
        self._output = output or (lambda text: typer.echo(text, nl=False))
        self.interactive = interactive
        self._context_window_tokens = max(1, context_window_tokens)
        self._line_open = False
        self._block_open = False
        self._agent_text_open = False
        self._status_active = False
        self._status_text = ""
        self._response_has_text = False
        self._current_calls: dict[str, tuple[str, Mapping[str, object]]] = {}
        self._live_plan: Mapping[str, object] | None = None
        self._context_tokens = 0
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

    def set_context_window(self, tokens: int) -> None:
        """Set the non-secret denominator displayed by the trailing status."""

        self._context_window_tokens = max(1, tokens)

    def user(self, task: str) -> None:
        self._end_previous_block()
        self._emit("+ You")
        self._emit(f"|   > {task}")
        self._emit("|")
        self._emit("| Agent")
        self._block_open = True

    async def observe(self, event: StreamEvent) -> None:
        if isinstance(event, ResponseCompleted):
            await self._stream.observe(event)
            await self._stream.wait_idle()
            if self._response_has_text:
                self._line()
            self._response_has_text = False
            self._agent_text_open = False
            return
        if isinstance(event, ResponseFailed):
            if not self._response_has_text:
                self._start_agent_text()
                self._response_has_text = True
            await self._stream.observe(event)
            await self._stream.wait_idle()
            self._line()
            self._response_has_text = False
            self._agent_text_open = False
            return
        if isinstance(event, TextDelta):
            if not self._response_has_text:
                self._start_agent_text()
                self._response_has_text = True
        await self._stream.observe(event)

    async def mark_incomplete(self) -> None:
        """Close a cancelled response with an explicit partial-output marker."""

        if not self._response_has_text:
            return
        await self._stream.wait_idle()
        self._write_fragment(" [stream incomplete]")
        self._line()
        self._response_has_text = False
        self._agent_text_open = False

    def on_lifecycle(self, event_type: str, payload: Mapping[str, JSONValue]) -> None:
        """Render a durable lifecycle fact as concise supporting activity."""

        if event_type == "tool.proposed":
            self._clear_status()
            self._render_live_plan()
            call_id = _string(payload.get("tool_call_id"))
            arguments = payload.get("arguments")
            name = _string(payload.get("name"))
            normalized_arguments = arguments if isinstance(arguments, Mapping) else {}
            self._current_calls[call_id] = (name, normalized_arguments)
            self._line()
            self._emit(f"|   [TOOL START] {name}")
            detail = _tool_detail(name, normalized_arguments)
            if detail:
                self._emit(f"|     {detail}")
            return
        if event_type in {"tool.completed", "tool.failed", "tool.interrupted"}:
            self._clear_status()
            self._render_tool_terminal(payload)
            return
        if event_type == "model.request.started":
            self._status("Thinking...")
            return
        if event_type == "model.request.retrying":
            attempt = _string(payload.get("attempt")) or "?"
            maximum = _string(payload.get("max_attempts")) or "?"
            self._line()
            self._emit(f"|   Provider retry {attempt}/{maximum}; continuing from durable state.")
            return
        if event_type == "model.request.completed":
            self._clear_status()
            self._context_tokens = _nonnegative_int(payload.get("input_tokens"))
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
            raw_plan = payload.get("plan")
            if isinstance(raw_plan, Mapping):
                self._live_plan = raw_plan
            return
        if event_type == "plan.reset":
            self._live_plan = None
            return
        if event_type == "context.compaction.started":
            self._line()
            self._emit("|   Context pressure detected; compacting observable Session state...")
            return
        if event_type == "context.compacted":
            kind = _string(payload.get("kind")) or "summary"
            self._line()
            self._emit(f"|   Context compacted ({kind}); original Session Events are retained.")
            return
        if event_type == "context.compaction.failed":
            self._line()
            self._emit("|   Context compaction failed; the Turn was stopped safely.")
            return
        if event_type == "instruction.changed":
            self._line()
            self._emit("|   Repository instructions changed; continuation requires review.")

    def permission_block(self, preview: PermissionPreview) -> None:
        """Show the normalized operation and numeric choices inline with the Agent."""

        self._clear_status()
        self._line()
        self._emit("|   [PERMISSION] Permission needed")
        self._emit(f"|     Operation: {preview.operation}")
        self._emit(f"|     Resources: {', '.join(preview.resources) or '(none)'}")
        self._emit(f"|     Reason: {preview.reason}")
        self._emit(
            "|     Choose [1 allow once / 2 allow exact for Session / 3 deny / 4 cancel]"
        )

    def completion(self, report: CompletionReport) -> None:
        self._line()
        self._emit("|   [COMPLETED] Completed")
        self._emit(f"|     Outcome: {report.outcome}")
        self._emit(f"|     Verification: {', '.join(report.verification)}")
        self._emit(
            "|     Changed files: "
            + (", ".join(report.changed_files) if report.changed_files else "none")
        )
        self._emit(
            "|     Unresolved work: "
            + ("; ".join(report.unresolved_work) if report.unresolved_work else "none")
        )
        self._emit(f"|     Next action: {report.next_action}")
        self._live_plan = None

    def failure(self, error_id: str | None) -> None:
        self._render_live_plan()
        self._line()
        suffix = f" [{error_id}]" if error_id else ""
        self._emit(
            f"|   [FAILURE] Turn failed{suffix}; the Session remains available for another input."
        )

    def recovery(self, message: str) -> None:
        self._line()
        self._emit(f"|   {message}" if self._block_open else message)

    async def finish(self) -> None:
        await self._stream.finish()
        self._clear_status()
        self._render_live_plan()
        self._line()
        self._emit("---")
        used = self._context_tokens
        percentage = used / self._context_window_tokens * 100
        self._emit(
            f"Status: context {used}/{self._context_window_tokens} tokens ({percentage:.1f}%)"
        )
        self._emit("Commands: /help  /plan  /config  /sessions  /exit")

    def external_line_finished(self) -> None:
        """Synchronize the presenter after a prompt rendered outside its sink."""

        self._output("\n")
        self._line_open = False

    def _render_tool_terminal(self, payload: Mapping[str, JSONValue]) -> None:
        call_id = _string(payload.get("tool_call_id"))
        name, arguments = self._current_calls.pop(call_id, (_string(payload.get("name")), {}))
        outcome = _string(payload.get("outcome")) or "failed"
        detail = _tool_detail(name, arguments)
        suffix = f" ({detail})" if detail else ""
        status = "completed" if outcome == "success" else outcome
        self._line()
        self._emit(f"|   [TOOL RESULT] {name}{suffix} - {status}")

    def _render_live_plan(self) -> None:
        plan = self._live_plan
        if plan is None:
            return
        self._clear_status()
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return
        if all(
            isinstance(item, Mapping) and _string(item.get("status")) == "completed"
            for item in raw_steps
        ):
            self._live_plan = None
            return
        self._line()
        self._emit("|   Plan (live)")
        objective = _string(plan.get("objective"))
        if objective:
            self._emit(f"|     {objective}")
        for item in raw_steps:
            if not isinstance(item, Mapping):
                continue
            status = _string(item.get("status"))
            symbol = {"completed": "x", "in-progress": ">"}.get(status, " ")
            self._emit(f"|     [{symbol}] {_string(item.get('description'))}")
        self._live_plan = None

    def _start_agent_text(self) -> None:
        self._clear_status()
        self._line()
        self._write_fragment("|   > ")
        self._agent_text_open = True

    def _end_previous_block(self) -> None:
        if self._block_open:
            self._line()
            self._output("\n")
            self._block_open = False

    def _status(self, text: str) -> None:
        if not self.interactive:
            return
        self._clear_status()
        self._output(f"|   {text}")
        self._status_active = True
        self._status_text = f"|   {text}"

    def _clear_status(self) -> None:
        if not self._status_active:
            return
        if self.interactive:
            self._output("\r" + (" " * len(self._status_text)) + "\r")
        self._status_active = False
        self._status_text = ""

    def _write_fragment(self, text: str) -> None:
        if self._agent_text_open and text:
            text = text.replace("\n", "\n|   > ")
        result = self._output(text)
        if inspect.isawaitable(result):
            return
        self._line_open = self._line_open or bool(text)

    def _emit(self, text: str) -> None:
        self._output(f"{text}\n")
        self._line_open = False

    def _line(self) -> None:
        if self._line_open:
            self._output("\n")
            self._line_open = False


class TerminalPermissionInteraction(UserInteraction):
    """Synchronous, fail-closed confirmation adapter for the CLI."""

    def __init__(self, presenter: ConversationPresenter, *, interactive: bool) -> None:
        self.presenter = presenter
        self._is_interactive = interactive

    @property
    def is_interactive(self) -> bool:
        return self._is_interactive

    def confirm(self, preview: PermissionPreview) -> ConfirmationChoice:
        if not self.is_interactive:
            return ConfirmationChoice.DENY
        self.presenter.permission_block(preview)
        prompt = "  Choice"
        choices = {
            "1": ConfirmationChoice.ALLOW_ONCE,
            "2": ConfirmationChoice.ALLOW_FOR_SESSION,
            "3": ConfirmationChoice.DENY,
            "4": ConfirmationChoice.CANCEL,
        }
        while True:
            try:
                value = typer.prompt(prompt, show_default=False)
            except (Abort, EOFError, OSError):
                self.presenter.external_line_finished()
                return ConfirmationChoice.DENY
            self.presenter.external_line_finished()
            choice = choices.get(value.strip())
            if choice is not None:
                return choice
            self.presenter.recovery("Invalid choice; enter 1, 2, 3, or 4.")


def _string(value: object) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)
    return ""


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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
