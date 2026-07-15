"""Prepare a clean workspace for a Mini Agent manual acceptance case."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = REPOSITORY_ROOT / "manual_tests" / "ticket09" / "cases"
RUNS_ROOT = REPOSITORY_ROOT / ".manual-runs" / "ticket09"


def available_cases() -> tuple[str, ...]:
    """Return the stable names of all bundled manual acceptance cases."""

    return tuple(sorted(path.name for path in CASES_ROOT.iterdir() if path.is_dir()))


def prepare_case(case_name: str, *, reset: bool = False) -> Path:
    """Copy one immutable case template into its ignored working directory."""

    cases = available_cases()
    if case_name not in cases:
        choices = ", ".join(cases)
        raise ValueError(f"unknown case {case_name!r}; choose one of: {choices}")

    source = CASES_ROOT / case_name
    destination = RUNS_ROOT / case_name
    if destination.exists():
        if not reset:
            raise FileExistsError(
                f"{destination} already exists; pass --reset to replace the previous run"
            )
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return destination.resolve()


def main() -> int:
    """CLI entry point used from a source checkout."""

    parser = argparse.ArgumentParser(
        description="Prepare an isolated Ticket 09 manual acceptance workspace."
    )
    parser.add_argument("case", nargs="?", choices=available_cases())
    parser.add_argument("--list", action="store_true", help="list the available cases")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="replace an existing working copy of the selected case",
    )
    args = parser.parse_args()

    if args.list:
        for case_name in available_cases():
            print(case_name)
        return 0
    if args.case is None:
        parser.error("provide a case name or use --list")

    destination = prepare_case(args.case, reset=args.reset)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
