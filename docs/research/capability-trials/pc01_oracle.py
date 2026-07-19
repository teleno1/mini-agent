"""Independent oracle for the PC-01 external Python repair trial."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _run(command: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(workspace / "src")
    return subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def evaluate(workspace: Path) -> dict[str, object]:
    test_run = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        workspace,
    )
    scope_run = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    diff_run = subprocess.run(
        ["git", "diff", "--check"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )

    sys.path.insert(0, str(workspace / "src"))
    from parcel_counter import shipping_price

    invalid_ok = False
    try:
        shipping_price(-0.01)
    except ValueError:
        invalid_ok = True
    nonfinite_ok = False
    try:
        shipping_price(float("inf"))
    except ValueError:
        nonfinite_ok = True

    changed_files = [
        line[3:] if len(line) >= 3 else line
        for line in scope_run.stdout.splitlines()
        if line
    ]
    expected_scope = ["src/parcel_counter/pricing.py"]
    checks = {
        "boundary": shipping_price(5.0) == 8,
        "normal_case": shipping_price(0.5) == 5,
        "heavy_case": shipping_price(5.01) == 12,
        "invalid_input": invalid_ok and nonfinite_ok,
        "visible_tests": test_run.returncode == 0,
        "scope": changed_files == expected_scope,
        "diff_check": diff_run.returncode == 0,
    }
    return {
        "oracle": "pc01-v1",
        "workspace": str(workspace),
        "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "test_exit": test_run.returncode,
        "test_output": test_run.stdout + test_run.stderr,
        "changed_files": changed_files,
        "diff_check_exit": diff_run.returncode,
        "diff_check_output": diff_run.stdout + diff_run.stderr,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.workspace.resolve())
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()

