"""Independent oracle for the RL-02 constrained-refactor trial."""

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
    return subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, check=False)


def evaluate(workspace: Path) -> dict[str, object]:
    tests = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], workspace)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True, check=False)
    diff_check = subprocess.run(["git", "diff", "--check"], cwd=workspace, capture_output=True, text=True, check=False)
    sys.path.insert(0, str(workspace / "src"))
    from reading_list import Book, ReadingList
    from reading_list.formatting import format_books

    books = [Book("Dune", "Frank Herbert", 1965), Book("Kindred", "Octavia Butler", 1979)]
    model_source = (workspace / "src/reading_list/models.py").read_text(encoding="utf-8")
    formatting_source = (workspace / "src/reading_list/formatting.py").read_text(encoding="utf-8")
    changed = [
        line[3:]
        for line in status.stdout.splitlines()
        if line and line[3:] != ".mini-agent/"
    ]
    book = Book("Dune", "Frank Herbert", 1965)
    label_method = getattr(book, "display_label", None) or getattr(book, "display", None)
    label = label_method() if label_method is not None else None
    delegated = any(f".{name}()" in formatting_source for name in ("display_label", "display"))
    checks = {
        "display_label_behavior": label == "Dune — Frank Herbert (1965)",
        "formatter_output_unchanged": format_books(books) == "Dune — Frank Herbert (1965)\nKindred — Octavia Butler (1979)",
        "author_output_unchanged": format_books(books, author="Octavia Butler") == "Kindred — Octavia Butler (1979)",
        "store_behavior_unchanged": ReadingList(books).list_books(author="Octavia Butler") == books[1:],
        "model_owns_label": "def display_label(" in model_source or "def display(" in model_source,
        "formatter_delegates": delegated and "f\"{book.title}" not in formatting_source,
        "exact_refactor_scope": sorted(changed) == ["src/reading_list/formatting.py", "src/reading_list/models.py"],
        "no_tests_or_dependency_change": not any(path.startswith("tests/") or path in {"pyproject.toml", "uv.lock"} for path in changed),
        "visible_tests": tests.returncode == 0,
        "diff_check": diff_check.returncode == 0,
    }
    return {
        "oracle": "rl02-v1",
        "workspace": str(workspace),
        "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "test_exit": tests.returncode,
        "test_output": tests.stdout + tests.stderr,
        "changed_files": changed,
        "diff_check_exit": diff_check.returncode,
        "diff_check_output": diff_check.stdout + diff_check.stderr,
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
