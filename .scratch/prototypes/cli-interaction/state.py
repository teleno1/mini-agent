"""Pure state machine for the throwaway CLI interaction prototype."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Phase(StrEnum):
    READY = "ready"
    STREAMING = "streaming"
    PERMISSION = "permission"
    COMPACTING = "compacting"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    COMPLETE = "complete"
    SESSIONS = "sessions"


@dataclass(frozen=True)
class Step:
    text: str
    status: str


@dataclass(frozen=True)
class ScreenState:
    phase: Phase = Phase.READY
    session_id: str = "demo-7f3a"
    task: str = ""
    transcript: tuple[str, ...] = ()
    plan: tuple[Step, ...] = ()
    tool_preview: str | None = None
    permission_reason: str | None = None
    notice: str = "Enter a task to begin."
    context_percent: int = 12
    sessions: tuple[str, ...] = ()
    last_error: str | None = None
    changed_files: tuple[str, ...] = ()


def available_actions(state: ScreenState) -> tuple[str, ...]:
    actions = {
        Phase.READY: ("task", "sessions", "quit"),
        Phase.STREAMING: ("stream", "tool", "plan", "compact", "error", "interrupt", "finish"),
        Phase.PERMISSION: ("allow", "deny", "interrupt"),
        Phase.COMPACTING: ("compact-ok", "compact-fail", "interrupt"),
        Phase.INTERRUPTED: ("resume", "new", "sessions", "quit"),
        Phase.ERROR: ("retry", "new", "sessions", "quit"),
        Phase.COMPLETE: ("new", "sessions", "quit"),
        Phase.SESSIONS: ("resume", "back", "quit"),
    }
    return actions[state.phase]


def transition(state: ScreenState, action: str, value: str = "") -> ScreenState:
    if action not in available_actions(state):
        return replace(state, notice=f"Action '{action}' is unavailable while {state.phase}.")

    if action == "task":
        task = value.strip() or "Fix the failing parser test"
        return replace(
            state,
            phase=Phase.STREAMING,
            task=task,
            transcript=(f"You: {task}", "Agent: I'll inspect the repository first."),
            plan=(Step("Inspect relevant files", "in progress"), Step("Make the smallest fix", "pending"), Step("Verify and report", "pending")),
            notice="Model response is streaming. Ctrl+C would interrupt the Turn.",
        )
    if action == "stream":
        return replace(state, transcript=state.transcript + ("Agent: The parser mishandles an empty token sequence...",), context_percent=min(99, state.context_percent + 9), notice="Received another streamed text chunk.")
    if action == "plan":
        return replace(state, plan=(Step("Inspect relevant files", "completed"), Step("Make the smallest fix", "in progress"), Step("Verify and report", "pending")), notice="Plan updated; completed work remains visible.")
    if action == "tool":
        return replace(state, phase=Phase.PERMISSION, tool_preview="apply_patch | modify src/parser.py (+4 -2)", permission_reason="Writes a workspace source file", notice="Review the exact operation before deciding.")
    if action == "allow":
        return replace(state, phase=Phase.STREAMING, transcript=state.transcript + ("Tool: apply_patch completed | src/parser.py (+4 -2)",), tool_preview=None, permission_reason=None, changed_files=("src/parser.py",), notice="Allowed once. Tool result is now part of the Turn.")
    if action == "deny":
        return replace(state, phase=Phase.STREAMING, transcript=state.transcript + ("Tool: apply_patch denied by user",), tool_preview=None, permission_reason=None, notice="Denied. The Agent may propose a different action.")
    if action == "compact":
        return replace(state, phase=Phase.COMPACTING, context_percent=82, notice="Context is near its limit. Creating a structured summary...")
    if action == "compact-ok":
        return replace(state, phase=Phase.STREAMING, context_percent=34, transcript=state.transcript + ("System: Context compacted; full history remains in the Session.",), notice="Compaction completed. Continuing the same Turn.")
    if action == "compact-fail":
        return replace(state, phase=Phase.ERROR, last_error="Context compaction failed after 3 attempts", notice="The Turn stopped before sending an oversized request.")
    if action == "error":
        return replace(state, phase=Phase.ERROR, last_error="Provider connection lost while streaming", notice="No tool is running. Retry starts a new model request in this Turn.")
    if action == "retry":
        return replace(state, phase=Phase.STREAMING, last_error=None, transcript=state.transcript + ("System: Retrying model request (2/3)...",), notice="Retrying with the same durable Session state.")
    if action == "interrupt":
        uncertain = " A running tool would be marked interrupted, never assumed successful." if state.phase == Phase.PERMISSION else ""
        return replace(state, phase=Phase.INTERRUPTED, notice="Turn interrupted; Session was saved." + uncertain)
    if action == "finish":
        return replace(state, phase=Phase.COMPLETE, plan=tuple(replace(step, status="completed") for step in state.plan), transcript=state.transcript + ("Agent: Completed the task and verified the result.",), notice="Finished. Review the summary or start another Session.")
    if action == "sessions":
        return replace(state, phase=Phase.SESSIONS, sessions=("demo-7f3a | interrupted | 2m ago | Fix parser test", "demo-19bd | completed | yesterday | Update README"), notice="Choose a Session ID to resume; this prototype resumes the first row.")
    if action == "resume":
        return replace(state, phase=Phase.STREAMING, task="Fix parser test", sessions=(), transcript=("System: Resumed demo-7f3a", "System: Previous tool state checked; no action was replayed."), plan=(Step("Inspect relevant files", "completed"), Step("Reconcile interrupted work", "in progress"), Step("Verify and report", "pending")), notice="Session resumed. Current instructions and interrupted work were rechecked.")
    if action == "back":
        return replace(state, phase=Phase.READY, sessions=(), notice="Enter a task to begin.")
    if action == "new":
        return ScreenState(notice="Started a fresh in-memory Session. Enter a task.")
    return state
