"""Install each local artifact into an isolated environment and smoke-test it."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    return str(project["version"])


def _python_path(environment: Path) -> Path:
    directory = environment / ("Scripts" if os.name == "nt" else "bin")
    return directory / ("python.exe" if os.name == "nt" else "python")


def _launcher_path(environment: Path) -> Path:
    directory = environment / ("Scripts" if os.name == "nt" else "bin")
    return directory / ("mini-agent.exe" if os.name == "nt" else "mini-agent")


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _smoke_artifact(artifact: Path, temporary: Path, version: str) -> None:
    environment = temporary / artifact.name.replace(".", "-")
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = _python_path(environment)
    launcher = _launcher_path(environment)
    environment_variables = os.environ.copy()
    environment_variables.pop("PYTHONPATH", None)
    for key in ("MINI_AGENT_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        environment_variables.pop(key, None)

    _run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact)],
        cwd=temporary,
        env=environment_variables,
    )
    help_output = _run([str(launcher), "--help"], cwd=temporary, env=environment_variables)
    version_output = _run([str(launcher), "--version"], cwd=temporary, env=environment_variables)
    fake_composition = (
        "from mini_agent.cli.app import create_app; "
        "from mini_agent.providers.fake import fake_provider_factory; "
        "create_app(fake_provider_factory)()"
    )
    smoke_output = _run(
        [str(python), "-c", fake_composition, "Explain Mini Agent"],
        cwd=temporary,
        env=environment_variables,
    )
    if "Mini Agent" not in help_output:
        raise RuntimeError(f"{artifact.name} --help did not identify Mini Agent")
    if version_output.strip() != f"mini-agent {version}":
        raise RuntimeError(f"{artifact.name} --version reported {version_output!r}")
    if "Agent: Mini Agent is a small, inspectable coding agent." not in smoke_output:
        raise RuntimeError(f"{artifact.name} Fake Provider smoke journey failed")


def main() -> None:
    artifacts = sorted((*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")))
    if len(artifacts) != 2:
        raise RuntimeError("build artifacts before running the installation smoke test")
    version = _project_version()
    with tempfile.TemporaryDirectory(prefix="mini-agent-artifacts-") as temporary:
        for artifact in artifacts:
            _smoke_artifact(artifact, Path(temporary), version)
    print("Installed wheel and source distribution successfully")


if __name__ == "__main__":
    main()
