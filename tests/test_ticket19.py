from __future__ import annotations

from mini_agent.application import build_completion_report
from mini_agent.cli.presentation import ConversationPresenter
from mini_agent.tools.contracts import ToolCall, ToolOutcome, ToolResult


def _shell_call(call_id: str, command: str) -> ToolCall:
    return ToolCall(
        tool_call_id=call_id,
        name="shell",
        arguments={"command": command},
    )


def _shell_result(call: ToolCall, outcome: ToolOutcome, code: str) -> ToolResult:
    if outcome is ToolOutcome.SUCCESS:
        return ToolResult.succeeded(call, {"command": call.arguments["command"], "exit_code": 0})
    return ToolResult.failed(
        call,
        outcome=outcome,
        category="tool-execution",
        code=code,
        message=f"Shell attempt ended with {outcome.value}",
    )


def test_completion_report_only_verifies_successful_shell_results() -> None:
    attempts = (
        (_shell_call("call-success", "pytest -q"), ToolOutcome.SUCCESS, "completed"),
        (_shell_call("call-denied", "pytest -q denied"), ToolOutcome.DENIED, "permission"),
        (_shell_call("call-invalid", "pytest -q invalid"), ToolOutcome.INVALID, "invalid"),
        (_shell_call("call-failed", "pytest -q failed"), ToolOutcome.FAILED, "exit-code"),
        (
            _shell_call("call-timeout", "pytest -q timeout"),
            ToolOutcome.INTERRUPTED,
            "timeout",
        ),
        (
            _shell_call("call-cancelled", "pytest -q cancelled"),
            ToolOutcome.CANCELLED,
            "cancelled",
        ),
        (
            _shell_call("call-interrupted", "pytest -q interrupted"),
            ToolOutcome.INTERRUPTED,
            "interrupted",
        ),
    )

    report = build_completion_report(
        [(call, _shell_result(call, outcome, code)) for call, outcome, code in attempts]
    )

    assert report.verification == ("pytest -q",)
    assert len(report.unresolved_work) == 6
    for (call, outcome, _code), unresolved in zip(
        attempts[1:], report.unresolved_work, strict=True
    ):
        assert outcome.value in unresolved
        assert call.arguments["command"] in unresolved
    assert report.outcome == "completed-with-unresolved-work"
    assert "rerun verification" in report.next_action


def test_completion_report_preserves_a_failed_attempt_after_a_successful_retry() -> None:
    failed = _shell_call("call-failed", "pytest -q")
    passed = _shell_call("call-passed", "pytest -q")

    report = build_completion_report(
        [
            (failed, _shell_result(failed, ToolOutcome.FAILED, "exit-code")),
            (passed, _shell_result(passed, ToolOutcome.SUCCESS, "completed")),
        ]
    )

    assert report.verification == ("pytest -q",)
    assert len(report.unresolved_work) == 1
    assert "failed" in report.unresolved_work[0]
    assert "exit-code" in report.unresolved_work[0]


def test_completion_report_marks_verification_unavailable_without_success() -> None:
    call = _shell_call("call-denied", "pytest -q")
    report = build_completion_report(
        [(call, _shell_result(call, ToolOutcome.DENIED, "permission"))]
    )

    assert report.verification == ("unavailable",)
    assert report.outcome == "completed-with-unresolved-work"
    assert "verification" in report.next_action.casefold()
    assert "safe" in report.next_action.casefold()


def test_cli_renders_unavailable_verification_and_unsuccessful_shell_observation() -> None:
    change = ToolCall(tool_call_id="call-change", name="apply_patch", arguments={})
    call = _shell_call("call-timeout", "pytest -q")
    report = build_completion_report(
        [
            (change, ToolResult.succeeded(change, {"changed_files": ["src/main.py"]})),
            (call, _shell_result(call, ToolOutcome.INTERRUPTED, "timeout")),
        ]
    )
    output: list[str] = []
    presenter = ConversationPresenter(output=output.append)

    presenter.completion(report)

    rendered = "".join(output)
    verification_line = next(line for line in rendered.splitlines() if "Verification:" in line)
    assert verification_line.strip() == "|     Verification: unavailable"
    assert "Unresolved work:" in rendered
    assert "interrupted (timeout)" in rendered
    assert "Verification is unavailable" in rendered
    assert "src/main.py" in rendered


def test_cli_renders_successful_verification_separately_from_failed_retry() -> None:
    failed = _shell_call("call-failed", "pytest -q")
    passed = _shell_call("call-passed", "pytest -q")
    report = build_completion_report(
        [
            (failed, _shell_result(failed, ToolOutcome.FAILED, "exit-code")),
            (passed, _shell_result(passed, ToolOutcome.SUCCESS, "completed")),
        ]
    )
    output: list[str] = []
    presenter = ConversationPresenter(output=output.append)

    presenter.completion(report)

    rendered = "".join(output)
    verification_line = next(line for line in rendered.splitlines() if "Verification:" in line)
    unresolved_line = next(line for line in rendered.splitlines() if "Unresolved work:" in line)
    assert verification_line.strip() == "|     Verification: pytest -q"
    assert "failed (exit-code)" in unresolved_line
