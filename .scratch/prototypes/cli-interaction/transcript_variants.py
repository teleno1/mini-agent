"""THROWAWAY: compare terminal transcript layouts for Issue #3.

Question: can one user request visibly contain an Agent's short progress notes,
Plan updates, and Tool calls before the Agent's formal answer, while later user
requests remain distinguishable without explicit Turn labels?

Run from the repository root:
    python .scratch/prototypes/cli-interaction/transcript_variants.py

This prototype is in-memory and has no model, filesystem, or Session
persistence. Commands: n/p move through the sample, plan toggles explicit Plan
Mode, and r resets the sample. At the permission checkpoint, choose one of the
four numeric authorization choices.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PlanStep:
    status: str
    text: str


@dataclass(frozen=True)
class PlanUpdate:
    label: str
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True)
class Entry:
    kind: str
    text: str = ""
    detail: str = ""
    outcome: str = ""
    plan_update: PlanUpdate | None = None


@dataclass(frozen=True)
class Conversation:
    user: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class DemoScene:
    conversations: tuple[Conversation, ...]
    context_used: float
    status: str


@dataclass(frozen=True)
class DemoState:
    scene: int = 0
    plan_mode: bool = False
    permission_error: str = ""


def note(text: str) -> Entry:
    return Entry("note", text=text)


def tool(name: str, detail: str, outcome: str) -> Entry:
    return Entry("tool", text=name, detail=detail, outcome=outcome)


def answer(text: str) -> Entry:
    return Entry("answer", text=text)


def plan(label: str, steps: tuple[PlanStep, ...]) -> Entry:
    return Entry("plan", plan_update=PlanUpdate(label, steps))


INSPECTING = (
    note("I'll inspect the parser path and focused test before I answer."),
    plan(
        "Plan created",
        (
            PlanStep("current", "Inspect relevant files"),
            PlanStep("next", "Make the smallest fix"),
            PlanStep("next", "Verify and report"),
        ),
    ),
    tool("read_file", "src/parser.py", "completed"),
    note("The empty-token branch is the only mismatch; I'm checking expected behavior."),
    tool("search_files", "tests/ for empty-token", "completed"),
)

PERMISSION = INSPECTING + (
    note("I found the mismatch. The smallest exact patch is ready; source edits need approval."),
    plan(
        "Inspection completed",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("current", "Make the smallest fix"),
            PlanStep("next", "Verify and report"),
        ),
    ),
    tool("apply_patch", "src/parser.py (+4 -2)", "permission needed"),
)

DENIED = PERMISSION[:-1] + (
    tool("apply_patch", "src/parser.py (+4 -2)", "denied"),
    note("I couldn't apply the patch without permission; no source files were changed."),
    plan(
        "Permission denied",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("current", "Make the smallest fix"),
            PlanStep("next", "Verify and report"),
        ),
    ),
    answer("The change is ready, but permission was denied, so no files were changed."),
)

CANCELLED = PERMISSION[:-1] + (
    tool("apply_patch", "src/parser.py (+4 -2)", "cancelled"),
    note("The pending patch was cancelled; no source files were changed."),
    plan(
        "Permission cancelled",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("current", "Make the smallest fix"),
            PlanStep("next", "Verify and report"),
        ),
    ),
    answer("The pending patch was cancelled, so no files were changed."),
)

VERIFYING = PERMISSION[:-1] + (
    tool("apply_patch", "src/parser.py (+4 -2)", "allowed once"),
    note("The patch is in place. I'm running only the focused test before reporting."),
    plan(
        "Patch completed",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("done", "Make the smallest fix"),
            PlanStep("current", "Verify and report"),
        ),
    ),
    tool("shell", "pytest tests/test_parser.py", "running"),
)

SESSION_VERIFYING = PERMISSION[:-1] + (
    tool("apply_patch", "src/parser.py (+4 -2)", "allowed exact for Session"),
    note("The exact Tool, resource set, and argument hash are allowed for this Session; I'm verifying the patch."),
    plan(
        "Patch completed",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("done", "Make the smallest fix"),
            PlanStep("current", "Verify and report"),
        ),
    ),
    tool("shell", "pytest tests/test_parser.py", "running"),
)

COMPLETE = VERIFYING[:-1] + (
    tool("shell", "pytest tests/test_parser.py", "completed"),
    plan(
        "Verification completed",
        (
            PlanStep("done", "Inspect relevant files"),
            PlanStep("done", "Make the smallest fix"),
            PlanStep("done", "Verify and report"),
        ),
    ),
    answer("The focused test passes, and the parser now handles the empty sequence."),
)

FOLLOW_UP = (
    note("I'll reread the changed file and confirm the recorded verification result."),
    tool("read_file", "src/parser.py", "completed"),
    answer("Changed src/parser.py; pytest tests/test_parser.py passes. No unresolved work."),
)


def scene_data(scene: int) -> DemoScene:
    if scene == 0:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", INSPECTING),),
            8.4,
            "One user request; Agent is inspecting before the formal answer.",
        )
    if scene == 1:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", PERMISSION),),
            12.6,
            "Permission is required; choose 1, 2, 3, or 4 to continue this same request.",
        )
    if scene == 2:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", VERIFYING),),
            18.4,
            "The same Agent workflow has patched the file and started verification.",
        )
    if scene == 3:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", COMPLETE),),
            21.7,
            "The Agent completed every Plan step before giving the formal answer.",
        )
    if scene == 4:
        return DemoScene(
        (
            Conversation("Find why the parser test fails for an empty token sequence.", COMPLETE),
            Conversation("Can you show the final verification summary?", FOLLOW_UP),
        ),
        22.4,
        "A later user request starts a new conversation after the Plan is complete.",
    )
    if scene == 5:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", DENIED),),
            15.2,
            "Permission was denied; the same user request ends without changing files.",
        )
    if scene == 6:
        return DemoScene(
            (Conversation("Find why the parser test fails for an empty token sequence.", SESSION_VERIFYING),),
            18.7,
            "The exact Tool/resource/argument grant applies for this Session.",
        )
    return DemoScene(
        (Conversation("Find why the parser test fails for an empty token sequence.", CANCELLED),),
        15.2,
        "The pending Tool Call was cancelled; the same user request ends without changing files.",
    )


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def mark(status: str) -> str:
    return {"done": "[x]", "current": "[>]", "next": "[ ]"}[status]


def plan_lines(update: PlanUpdate) -> list[str]:
    return [
        "",
        f"Plan (updated) - {update.label}",
        *[f"  {mark(step.status)} {step.text}" for step in update.steps],
        "",
    ]


def tool_lines(entry: Entry, indent: str = "  ", permission_error: str = "") -> list[str]:
    lines = [f"{indent}[TOOL START] {entry.text}", f"{indent}  {entry.detail}"]
    if entry.outcome == "permission needed":
        lines.append(f"{indent}  execution paused: permission required")
        if permission_error:
            lines.append(f"{indent}  [PERMISSION] {permission_error}")
        lines.append(
            f"{indent}  [PERMISSION] Choose [1 allow once / 2 allow exact for Session / 3 deny / 4 cancel]:"
        )
    elif entry.outcome == "running":
        lines.append(f"{indent}  execution in progress")
    else:
        lines.append(f"{indent}[TOOL RESULT] {entry.outcome}")
    return lines


def latest_plan(demo: DemoScene) -> PlanUpdate | None:
    for conversation in reversed(demo.conversations):
        for entry in reversed(conversation.entries):
            if entry.kind == "plan" and entry.plan_update is not None:
                return entry.plan_update
    return None


def render_conversation(state: DemoState, demo: DemoScene) -> list[str]:
    """The selected layout: '+' starts a conversation and '|' rails it through Agent's last line."""
    lines = ["MINI AGENT", ""]
    for index, conversation in enumerate(demo.conversations):
        if index:
            lines.append("")
        lines += ["+ You", f"|   > {conversation.user}", "|", "| Agent"]
        # Plan is a live bottom snapshot; do not repeat every update in the
        # conversation body.
        entries = tuple(entry for entry in conversation.entries if entry.kind != "plan")
        previous_kind: str | None = None
        for entry in entries:
            if entry.kind == "note":
                if previous_kind == "tool" and lines[-1] != "|":
                    lines.append("|")
                lines.append(f"|   > {entry.text}")
            elif entry.kind == "tool":
                lines += tool_lines(entry, "|   ", state.permission_error)
            elif entry.kind == "plan" and entry.plan_update is not None:
                lines += ["|"] + [f"| {line}" if line else "|" for line in plan_lines(entry.plan_update)]
            else:
                if lines[-1] != "|":
                    lines.append("|")
                lines.append(f"|   > {entry.text}")
            previous_kind = entry.kind
        # The rail ends with the Agent's final line. The following separator is
        # intentionally unmarked so the next '+' visibly starts a new exchange.
        lines.append("")
    return lines


def render(state: DemoState) -> None:
    clear_screen()
    demo = scene_data(state.scene)
    width = 76
    harness = [
        "[PROTOTYPE ONLY] This banner and the harness controls are not product UI.",
        f"[PROTOTYPE ONLY] Stage {state.scene + 1}/8 | {demo.status}",
        (
            "[PROTOTYPE ONLY] Commands: permission 1/2/3/4 | p step | "
            "plan toggle | r reset | q quit"
            if state.scene == 1
            else "[PROTOTYPE ONLY] Commands: n/p step | r reset | plan toggle | q quit"
        ),
        "",
    ]
    transcript = render_conversation(state, demo)
    context = f"{demo.context_used:.1f}k/32k({demo.context_used / 32 * 100:.1f}%)"
    slash_commands = "/help  /plan  /compact  /sessions"
    footer: list[str] = []
    if state.plan_mode:
        current_plan = latest_plan(demo)
        if current_plan is not None and any(step.status != "done" for step in current_plan.steps):
            footer += [
                "Plan",
                *[f"  {mark(step.status)} {step.text}" for step in current_plan.steps],
                "",
            ]
    footer += ["-" * width, f"{context:<28}{slash_commands:>{width - 28}}"]

    # The footer follows the newest conversation content. It is not pinned to
    # the terminal window and scrolls naturally with the transcript.
    output = harness + transcript + [""] + footer
    print("\n".join(output))


def main() -> None:
    state = DemoState()
    while True:
        render(state)
        try:
            command = input("> ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if command in {"q", "quit", "exit"}:
            return
        if state.scene == 1:
            if command == "1":
                state = replace(state, scene=2, permission_error="")
            elif command == "2":
                state = replace(state, scene=6, permission_error="")
            elif command == "3":
                state = replace(state, scene=5, permission_error="")
            elif command == "4":
                state = replace(state, scene=7, permission_error="")
            else:
                state = replace(state, permission_error="Invalid choice. Enter 1, 2, 3, or 4.")
        elif command in {"n", "next"}:
            next_scene = {0: 1, 2: 3, 3: 4}.get(state.scene, state.scene)
            state = replace(state, scene=next_scene)
        elif command in {"p", "prev", "previous"}:
            previous_scene = {5: 1, 6: 1, 7: 1}.get(state.scene, max(0, state.scene - 1))
            state = replace(state, scene=previous_scene)
        elif command in {"plan", "plan on", "plan off"}:
            state = replace(state, plan_mode=not state.plan_mode)
        elif command in {"r", "reset"}:
            state = DemoState()


if __name__ == "__main__":
    main()
