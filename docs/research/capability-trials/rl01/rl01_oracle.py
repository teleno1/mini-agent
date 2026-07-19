"""Independent oracle for the RL-01 reading-list feature trial."""

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

    books = [
        Book("Dune", "Frank Herbert", 1965),
        Book("Kindred", "Octavia Butler", 1979),
        Book("Parable of the Sower", "Octavia Butler", 1993),
    ]
    try:
        selected = ReadingList(books).list_books(after_year=1970)
        formatted = format_books(books, after_year=1970)
        combined = ReadingList(books).list_books(author="Octavia Butler", after_year=1970)
    except (AttributeError, TypeError):
        selected = []
        formatted = ""
        combined = []
    changed = [
        line[3:]
        for line in status.stdout.splitlines()
        if line and line[3:] != ".mini-agent/"
    ]
    checks = {
        "strict_year_selection": [book.title for book in selected] == ["Kindred", "Parable of the Sower"],
        "strict_year_format": formatted == "Kindred — Octavia Butler (1979)\nParable of the Sower — Octavia Butler (1993)",
        "author_and_year_combination": [book.title for book in combined] == ["Kindred", "Parable of the Sower"],
        "default_behavior": ReadingList(books).list_books() == books and format_books(books) == "Dune — Frank Herbert (1965)\nKindred — Octavia Butler (1979)\nParable of the Sower — Octavia Butler (1993)",
        "visible_tests": tests.returncode == 0,
        "focused_source_scope": len(changed) >= 2 and all(path in {"src/reading_list/filters.py", "src/reading_list/store.py", "src/reading_list/formatting.py"} for path in changed),
        "no_generated_or_dependency_change": not any(path.startswith(("tests/", "dist/", ".venv/")) or path in {"pyproject.toml", "uv.lock"} for path in changed),
        "diff_check": diff_check.returncode == 0,
    }
    return {
        "oracle": "rl01-v1",
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
