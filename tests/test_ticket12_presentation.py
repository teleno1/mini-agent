from __future__ import annotations

import asyncio

from mini_agent.cli.presentation import ConversationPresenter
from mini_agent.domain.reports import CompletionReport
from mini_agent.domain.streams import ResponseCompleted, TextDelta


def _report() -> CompletionReport:
    return CompletionReport(
        outcome="completed",
        verification=("pytest -q",),
        changed_files=(),
        unresolved_work=(),
        next_action="No further action is required.",
    )


async def _render_response(presenter: ConversationPresenter) -> None:
    await presenter.observe(TextDelta("The parser is consistent."))
    await presenter.observe(ResponseCompleted())
    presenter.completion(_report())
    await presenter.finish()


def test_presenter_uses_grouped_rail_and_trailing_plain_status() -> None:
    output: list[str] = []
    presenter = ConversationPresenter(output=output.append, context_window_tokens=100)
    presenter.user("inspect the parser")
    presenter.on_lifecycle(
        "tool.proposed",
        {"tool_call_id": "call-1", "name": "read_file", "arguments": {"path": "src/parser.py"}},
    )
    presenter.on_lifecycle(
        "tool.completed",
        {"tool_call_id": "call-1", "name": "read_file", "outcome": "success"},
    )
    presenter.on_lifecycle(
        "model.request.completed", {"input_tokens": 40, "output_tokens": 5}
    )
    asyncio.run(_render_response(presenter))

    rendered = "".join(output)
    assert "+ You\n|   > inspect the parser\n|\n| Agent" in rendered
    assert "|   [TOOL START] read_file\n|     src/parser.py" in rendered
    assert "|   [TOOL RESULT] read_file (src/parser.py) - completed" in rendered
    assert "|   > The parser is consistent." in rendered
    assert "|   [COMPLETED] Completed" in rendered
    assert "Status: context 40/100 tokens" in rendered
    assert "Commands: /help  /plan  /config  /sessions  /exit" in rendered
    assert "Turn 1" not in rendered
    assert "\r" not in rendered


def test_presenter_keeps_only_an_incomplete_live_plan_and_clears_completed_plan() -> None:
    output: list[str] = []
    presenter = ConversationPresenter(output=output.append)
    presenter.user("inspect and verify")
    presenter.on_lifecycle(
        "plan.updated",
        {
            "plan": {
                "objective": "inspect and verify",
                "steps": [
                    {"status": "completed", "description": "Inspect the parser"},
                    {"status": "in-progress", "description": "Run verification"},
                ],
            }
        },
    )
    presenter.failure(None)
    rendered = "".join(output)
    assert rendered.count("Plan (live)") == 1
    assert "[x] Inspect the parser" in rendered
    assert "[>] Run verification" in rendered

    output.clear()
    presenter.on_lifecycle(
        "plan.updated",
        {
            "plan": {
                "objective": "inspect and verify",
                "steps": [
                    {"status": "completed", "description": "Inspect the parser"},
                    {"status": "completed", "description": "Run verification"},
                ],
            }
        },
    )
    presenter.completion(_report())
    assert "Plan (live)" not in "".join(output)
