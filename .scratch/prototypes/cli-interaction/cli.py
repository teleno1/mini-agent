"""THROWAWAY TUI: manually drive the proposed Mini Agent interaction states."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from state import ScreenState, available_actions, transition

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def render(state: ScreenState) -> None:
    print("\x1b[2J\x1b[H", end="")
    print(f"{BOLD}MINI AGENT | INTERACTION PROTOTYPE{RESET}  {DIM}(throwaway, no real actions){RESET}")
    print(f"{BOLD}Session{RESET}  {state.session_id}    {BOLD}Phase{RESET}  {state.phase}")
    print(f"{BOLD}Context{RESET}  {state.context_percent}%    {BOLD}Task{RESET}  {state.task or '-'}")
    print(f"\n{BOLD}Status{RESET}\n{state.notice}")

    if state.plan:
        print(f"\n{BOLD}Plan{RESET}")
        icons = {"pending": "[ ]", "in progress": "[>]", "completed": "[x]"}
        for step in state.plan:
            print(f"  {icons[step.status]} {step.text} {DIM}({step.status}){RESET}")

    if state.transcript:
        print(f"\n{BOLD}Recent activity{RESET}")
        for line in state.transcript[-5:]:
            print(f"  {line}")

    if state.tool_preview:
        print(f"\n{BOLD}Permission required{RESET}\n  {state.tool_preview}\n  {DIM}{state.permission_reason}{RESET}")
    if state.last_error:
        print(f"\n{BOLD}Error{RESET}\n  {state.last_error}")
    if state.changed_files:
        print(f"\n{BOLD}Changed files{RESET}  {', '.join(state.changed_files)}")
    if state.sessions:
        print(f"\n{BOLD}Sessions{RESET}")
        for row in state.sessions:
            print(f"  {row}")

    actions = available_actions(state)
    print(f"\n{BOLD}Actions{RESET}")
    print("  " + "  ".join(f"{BOLD}{name}{RESET}" for name in actions))
    print(f"{DIM}Type an action. 'task' optionally accepts text after it.{RESET}")


def main() -> None:
    state = ScreenState()
    while True:
        render(state)
        raw = input("\n> ").strip()
        action, _, value = raw.partition(" ")
        if action == "quit":
            break
        state = transition(state, action, value)


if __name__ == "__main__":
    main()
