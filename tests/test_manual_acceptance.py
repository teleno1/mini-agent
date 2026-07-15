from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_manual_acceptance.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("prepare_manual_acceptance", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_acceptance_cases_are_discoverable() -> None:
    module = _load_script()

    assert module.available_cases() == (
        "01-slugify",
        "02-order-total",
        "03-stale-command",
    )


def test_prepare_case_copies_template_and_requires_explicit_reset(tmp_path: Path) -> None:
    module = _load_script()
    module.RUNS_ROOT = tmp_path

    destination = module.prepare_case("01-slugify")

    assert destination == (tmp_path / "01-slugify").resolve()
    assert (destination / "TASK.md").is_file()
    assert not tuple(destination.rglob("__pycache__"))
    assert not tuple(destination.rglob("*.pyc"))
    (destination / "text_utils.py").write_text("changed", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--reset"):
        module.prepare_case("01-slugify")

    reset_destination = module.prepare_case("01-slugify", reset=True)
    assert "def slugify" in (reset_destination / "text_utils.py").read_text(encoding="utf-8")


def test_prepare_case_rejects_unknown_name(tmp_path: Path) -> None:
    module = _load_script()
    module.RUNS_ROOT = tmp_path

    with pytest.raises(ValueError, match="unknown case"):
        module.prepare_case("not-a-case")


@pytest.mark.parametrize(
    ("case_name", "failure_count"),
    (("01-slugify", 3), ("02-order-total", 1), ("03-stale-command", 3)),
)
def test_case_baseline_has_expected_failing_tests(case_name: str, failure_count: int) -> None:
    module = _load_script()
    case = module.CASES_ROOT / case_name

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=case,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert f"failures={failure_count}" in result.stderr


def test_stale_command_fails_because_documented_test_module_is_missing() -> None:
    module = _load_script()
    case = module.CASES_ROOT / "03-stale-command"

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_retry_delay", "-v"],
        cwd=case,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "test_retry_delay" in result.stderr
