"""Independent SH-01 oracle for the self-hosted capability trial.

The trial workspace is supplied as an argument so this checker can stay outside
the model-writable checkout. It exercises public ToolCall/ToolResult values and
does not import the repository's test modules.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _shell_call(tool_call_id: str, command: str):
    from mini_agent.tools.contracts import ToolCall

    return ToolCall(tool_call_id=tool_call_id, name="shell", arguments={"command": command})


def _shell_result(call, outcome, code: str):
    from mini_agent.tools.contracts import ToolOutcome, ToolResult

    if outcome is ToolOutcome.SUCCESS:
        return ToolResult.succeeded(call, {"command": call.arguments["command"], "exit_code": 0})
    return ToolResult.failed(
        call,
        outcome=outcome,
        category="tool-execution",
        code=code,
        message=f"Shell attempt ended with {outcome.value}",
    )


def _run(source_root: Path) -> dict[str, object]:
    sys.path.insert(0, str(source_root / "src"))
    from mini_agent.application import build_completion_report
    from mini_agent.tools.contracts import ToolOutcome

    denied_call = _shell_call("oracle-denied", "pytest -q denied")
    failed_call = _shell_call("oracle-failed", "pytest -q")
    passed_call = _shell_call("oracle-passed", "pytest -q")
    success_call = _shell_call("oracle-success", "python -m pytest -q")

    failed_retry_report = build_completion_report(
        [
            (failed_call, _shell_result(failed_call, ToolOutcome.FAILED, "exit-code")),
            (passed_call, _shell_result(passed_call, ToolOutcome.SUCCESS, "completed")),
        ]
    )
    success_report = build_completion_report(
        [(success_call, _shell_result(success_call, ToolOutcome.SUCCESS, "completed"))]
    )
    denied_report = build_completion_report(
        [
            (denied_call, _shell_result(denied_call, ToolOutcome.DENIED, "permission")),
        ]
    )

    expected = {
        "denied_only": {"verification": ["unavailable"], "unresolved_count": 1},
        "failed_then_success": {
            "verification": ["pytest -q"],
            "unresolved_count": 1,
            "unresolved_contains_failed": True,
        },
        "success_only": {"verification": ["python -m pytest -q"], "unresolved_count": 0},
    }
    actual = {
        "denied_only": denied_report.as_dict(),
        "failed_then_success": failed_retry_report.as_dict(),
        "success_only": success_report.as_dict(),
    }
    checks = {
        "denied_only": actual["denied_only"]["verification"]
        == expected["denied_only"]["verification"]
        and len(actual["denied_only"]["unresolved_work"]) == 1,
        "failed_then_success": actual["failed_then_success"]["verification"]
        == expected["failed_then_success"]["verification"]
        and len(actual["failed_then_success"]["unresolved_work"]) == 1
        and "failed" in actual["failed_then_success"]["unresolved_work"][0],
        "success_only": actual["success_only"]["verification"]
        == expected["success_only"]["verification"]
        and not actual["success_only"]["unresolved_work"],
    }
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    checks["scope"] = diff == ["src/mini_agent/application/agent.py"]
    return {
        "oracle": "sh01-v1",
        "expected": expected,
        "actual": actual,
        "checks": checks,
        "passed": all(checks.values()),
        "changed_files": diff,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = _run(args.source_root.resolve())
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
