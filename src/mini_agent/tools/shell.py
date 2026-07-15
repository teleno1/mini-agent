"""Bounded, host-controlled Shell execution.

The Shell Tool is deliberately a small process adapter, not an operating-system
sandbox.  Permission policy decides whether a command may run; this module then
keeps the command inside a validated working directory, passes a redacted
environment, bounds its output and duration, and cleans up its process group.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from mini_agent.configuration import redact_secrets
from mini_agent.tools.contracts import (
    CancellationBehavior,
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolLimits,
    ToolOutcome,
    ToolResult,
)
from mini_agent.tools.workspace import Workspace, WorkspaceError, WorkspacePathError

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 10 * 60.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
_READ_TERMINATION_GRACE_SECONDS = 1.0
_ESCALATION_GRACE_SECONDS = 1.0
_CHUNK_SIZE = 8192
_ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SENSITIVE_DIRECTORIES = {".aws", ".azure", ".docker", ".gcloud", ".kube", ".ssh"}
_SENSITIVE_FILENAMES = {
    "application_default_credentials.json",
    "cookies.sqlite",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secret.json",
    "secrets.json",
}
_WINDOWS_JOB_HANDLES: dict[int, int] = {}


class ShellCommandClass(StrEnum):
    """The explainable classification used by the Permission Policy."""

    LOCAL_READ = "recognized-local-read"
    LOCAL_BUILD = "recognized-local-build"
    LOCAL_TEST = "recognized-local-test"
    CHAINING = "chaining"
    REDIRECTION = "redirection"
    INTERPRETER = "interpreter"
    NETWORK = "network"
    INSTALL = "install"
    GIT_WRITE = "git-write"
    DELETION = "deletion"
    INTERACTIVE = "interactive"
    DETACHED = "detached"
    SENSITIVE_TARGET = "sensitive-target"
    BOUNDARY_ESCAPE = "boundary-escape"
    QUOTING_AMBIGUITY = "quoting-ambiguity"
    UNKNOWN_EXECUTABLE = "unknown-executable"


@dataclass(frozen=True, slots=True)
class ShellCommandClassification:
    """Pure, redacted facts explaining a command's Permission decision."""

    category: ShellCommandClass
    hazards: tuple[str, ...]
    rule: str
    reason: str
    recognized: bool = False


class ShellInput(BaseModel):
    """Validated model input for one bounded Shell invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    command: str = Field(min_length=1)
    working_directory: str = Field(
        default=".",
        min_length=1,
        validation_alias=AliasChoices("working_directory", "cwd", "workdir"),
    )
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_TIMEOUT_SECONDS,
    )
    max_output_bytes: int = Field(
        default=DEFAULT_MAX_OUTPUT_BYTES,
        ge=1,
        le=MAX_OUTPUT_BYTES,
        validation_alias=AliasChoices("max_output_bytes", "max_output"),
    )

    @model_validator(mode="after")
    def validate_command(self) -> ShellInput:
        if not self.command.strip():
            raise ValueError("command cannot be blank")
        if not self.working_directory.strip():
            raise ValueError("working_directory cannot be blank")
        return self


class _ShellExecution:
    """Internal process result before it is converted to a Tool Result."""

    def __init__(
        self,
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
        output_truncated: bool,
        duration_seconds: float,
        termination: str,
        uncertain: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.output_truncated = output_truncated
        self.duration_seconds = duration_seconds
        self.termination = termination
        self.uncertain = uncertain


class ShellTool:
    """Run one non-interactive Shell command with bounded host resources."""

    name = "shell"
    description = (
        "Run one non-interactive, permission-gated PowerShell or POSIX Shell command "
        "in a Workspace-relative directory with bounded output and duration."
    )
    side_effect = SideEffectCategory.EXECUTE
    limits = ToolLimits(
        # The input model supplies the 120-second default; the Tool limit is
        # the host-enforced ten-minute ceiling used by AgentTurnApplication.
        timeout_seconds=MAX_TIMEOUT_SECONDS,
        max_output_bytes=MAX_OUTPUT_BYTES,
        cancellation=CancellationBehavior.COOPERATIVE,
    )
    input_model: ClassVar[type[BaseModel]] = ShellInput

    def preflight(self, workspace: Workspace, arguments: BaseModel) -> tuple[str, ...]:
        request = _as_shell_input(arguments)
        target = workspace.resolve_read(request.working_directory, directory=True)
        return (target.relative_path,)

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        request = _as_shell_input(arguments)
        classification = classify_shell_command(request.command)
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(request.working_directory,),
            hazards=classification.hazards,
            summary=(
                f"run {classification.reason} in Workspace directory "
                f"{_display_path(request.working_directory)}"
            ),
        )

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        request = _as_shell_input(arguments)
        call_id = str(getattr(arguments, "tool_call_id", "shell"))
        try:
            target = workspace.resolve_read(request.working_directory, directory=True)
            classification = classify_shell_command(request.command)
            if any(hazard in {"interactive", "detached"} for hazard in classification.hazards):
                return _shell_failure(
                    call_id,
                    code="unsupported-process",
                    message="interactive and detached Shell programs are not supported",
                    outcome=ToolOutcome.DENIED,
                    category="permission",
                    working_directory=target.relative_path,
                    command_class=classification.category.value,
                )
            if any(
                hazard in {"sensitive", "boundary-escape", "catastrophic"}
                for hazard in classification.hazards
            ):
                return _shell_failure(
                    call_id,
                    code="unsafe-command",
                    message=classification.reason,
                    outcome=ToolOutcome.DENIED,
                    category="permission",
                    working_directory=target.relative_path,
                    command_class=classification.category.value,
                )
        except WorkspacePathError as exc:
            return _workspace_failure(call_id, exc)
        except WorkspaceError:
            return _shell_failure(
                call_id,
                code="working-directory",
                message="Shell working directory could not be validated",
            )

        environment, removed_secrets = filtered_child_environment()
        started = time.monotonic()
        try:
            process = await _spawn_process(
                request.command,
                cwd=target.path,
                environment=environment,
            )
        except (OSError, ValueError) as exc:
            return _shell_failure(
                call_id,
                code="spawn-failed",
                message="Shell process could not be started",
                detail=str(exc),
                working_directory=target.relative_path,
            )

        try:
            execution = await _wait_for_process(
                process,
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
                started=started,
                removed_secrets=removed_secrets,
                working_directory=target.relative_path,
            )
        except asyncio.CancelledError:
            execution = await _cancel_process(
                process,
                max_output_bytes=request.max_output_bytes,
                started=started,
                removed_secrets=removed_secrets,
                working_directory=target.relative_path,
            )

        data = _execution_data(execution, target.relative_path)
        if execution.uncertain:
            return ToolResult.failed(
                _call(call_id),
                outcome=ToolOutcome.INTERRUPTED,
                category="cancellation",
                code="termination-uncertain",
                message=_execution_message(
                    "Shell termination could not be proven; side effects are uncertain",
                    execution,
                ),
                data=data,
            )
        if execution.termination == "cancelled":
            return ToolResult.failed(
                _call(call_id),
                outcome=ToolOutcome.CANCELLED,
                category="cancellation",
                code="cancelled",
                message=_execution_message(
                    "Shell command was cancelled after cooperative cleanup", execution
                ),
                data=data,
            )
        if execution.termination == "timeout":
            return ToolResult.failed(
                _call(call_id),
                category="tool-timeout",
                code="timeout",
                message=_execution_message("Shell command exceeded its time limit", execution),
                data=data,
            )
        if execution.returncode != 0:
            return ToolResult.failed(
                _call(call_id),
                category="tool-execution",
                code="exit-code",
                message=_execution_message(
                    f"Shell command exited with code {execution.returncode}", execution
                ),
                data=data,
            )
        return ToolResult.succeeded(_call(call_id), data)


def classify_shell_command(
    command: str,
    *,
    platform: str | None = None,
) -> ShellCommandClassification:
    """Classify a Shell command without executing or resolving its arguments.

    The classifier is intentionally conservative.  Only a small explicit set
    of local read/build/test forms receives the ``recognized-local`` hazard;
    every other form remains confirmation-gated (or hard-denied when it is an
    unsupported process or an obvious boundary escape).
    """

    if not isinstance(command, str) or not command.strip():
        return _classification(
            ShellCommandClass.UNKNOWN_EXECUTABLE,
            ("unknown-executable",),
            "unknown-executable",
            "an empty or invalid Shell command is not recognized",
        )

    try:
        early_tokens = tuple(
            _strip_quotes(token) for token in _split_command(command, platform=platform)
        )
    except ValueError:
        early_tokens = ()
    if early_tokens and _contains_sensitive_target(early_tokens):
        return _classification(
            ShellCommandClass.SENSITIVE_TARGET,
            ("sensitive", "hard-deny"),
            "sensitive-target",
            "a command argument names a sensitive host target and is hard-denied",
        )

    syntax = _scan_shell_syntax(command)
    if syntax.unmatched_quote or syntax.command_substitution or syntax.variable_expansion:
        return _classification(
            ShellCommandClass.QUOTING_AMBIGUITY,
            ("quoting-ambiguity", "environment-expansion")
            if syntax.variable_expansion
            else ("quoting-ambiguity",),
            "quoting-ambiguity",
            (
                "environment expansion makes the Shell operation dependent on "
                "untrusted process state"
                if syntax.variable_expansion
                else "quoting or command substitution makes the Shell operation ambiguous"
            ),
        )
    if syntax.detached:
        return _classification(
            ShellCommandClass.DETACHED,
            ("detached",),
            "detached-process",
            "background or detached Shell jobs are not supported",
        )
    if syntax.chaining:
        return _classification(
            ShellCommandClass.CHAINING,
            ("chaining",),
            "command-chaining",
            "multiple Shell commands or a pipeline require confirmation",
        )
    if syntax.redirection:
        return _classification(
            ShellCommandClass.REDIRECTION,
            ("redirection",),
            "redirection",
            "Shell input or output redirection requires confirmation",
        )

    try:
        tokens = _split_command(command, platform=platform)
    except ValueError:
        return _classification(
            ShellCommandClass.QUOTING_AMBIGUITY,
            ("quoting-ambiguity",),
            "quoting-ambiguity",
            "the command cannot be parsed unambiguously",
        )
    if not tokens:
        return _classification(
            ShellCommandClass.UNKNOWN_EXECUTABLE,
            ("unknown-executable",),
            "unknown-executable",
            "the executable is not recognized",
        )

    normalized = tuple(_strip_quotes(token) for token in tokens)
    if _contains_boundary_path(normalized):
        return _classification(
            ShellCommandClass.BOUNDARY_ESCAPE,
            ("boundary-escape",),
            "boundary-escape",
            "an argument names an absolute or escaping path outside the Workspace",
        )
    if _contains_sensitive_target(normalized):
        return _classification(
            ShellCommandClass.SENSITIVE_TARGET,
            ("sensitive", "hard-deny"),
            "sensitive-target",
            "a command argument names a sensitive host target and is hard-denied",
        )

    executable = _executable_name(normalized[0])
    if _has_explicit_executable_path(normalized[0]):
        return _classification(
            ShellCommandClass.UNKNOWN_EXECUTABLE,
            ("unknown-executable",),
            "explicit-executable-path",
            "an explicit executable path is not in the recognized local command set",
        )
    arguments = tuple(token.lower() for token in normalized[1:])
    if executable in _INTERACTIVE_EXECUTABLES or executable in _DETACHED_EXECUTABLES:
        category = (
            ShellCommandClass.DETACHED
            if executable in _DETACHED_EXECUTABLES
            else ShellCommandClass.INTERACTIVE
        )
        hazard = category.value.removeprefix("recognized-local-")
        return _classification(
            category,
            (hazard,),
            hazard,
            f"{executable} is not a supported non-interactive program",
        )
    if executable in _INTERPRETERS or executable in {"env", "xargs"}:
        return _classification(
            ShellCommandClass.INTERPRETER,
            ("interpreter",),
            "interpreter",
            f"{executable} can execute model-selected program text",
        )
    if _looks_like_network(executable, arguments):
        return _classification(
            ShellCommandClass.NETWORK,
            ("network",),
            "network",
            f"{executable} can access a network or remote repository",
        )
    if _looks_like_install(executable, arguments):
        return _classification(
            ShellCommandClass.INSTALL,
            ("install",),
            "install",
            f"{executable} can install or modify dependencies",
        )
    if _looks_like_deletion(executable, arguments):
        hazards = ["delete"]
        if _looks_catastrophic_deletion(arguments):
            hazards.append("catastrophic")
        return _classification(
            ShellCommandClass.DELETION,
            tuple(hazards),
            "deletion",
            f"{executable} can delete Workspace content",
        )
    if executable == "git" and arguments and arguments[0] in _GIT_WRITE_COMMANDS:
        hazards = ["git-write"]
        if arguments[0] in {"clean", "reset"} and "--hard" in arguments:
            hazards.append("catastrophic")
        return _classification(
            ShellCommandClass.GIT_WRITE,
            tuple(hazards),
            "git-write",
            f"git {arguments[0]} changes repository state",
        )

    local_category = _recognized_local_category(executable, arguments)
    if local_category is not None:
        return _classification(
            local_category,
            ("recognized-local", local_category.value),
            "recognized-local",
            (
                f"{local_category.value.removeprefix('recognized-local-')} "
                "command is local and bounded"
            ),
            recognized=True,
        )

    return _classification(
        ShellCommandClass.UNKNOWN_EXECUTABLE,
        ("unknown-executable",),
        "unknown-executable",
        f"{executable or 'the executable'} is not in the recognized local command set",
    )


def filtered_child_environment(
    environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return a child environment without provider or credential variables.

    Values removed from the environment are returned only so captured output
    can be redacted if a child prints a known value.  No model-provided mapping
    is accepted by :class:`ShellInput`.
    """

    source = dict(os.environ if environment is None else environment)
    safe: dict[str, str] = {}
    removed: list[str] = []
    for key, value in source.items():
        if _sensitive_environment_name(key) or _sensitive_environment_value(value):
            if value:
                removed.append(value)
            continue
        if isinstance(value, str):
            safe[key] = value
    return safe, tuple(removed)


@dataclass(frozen=True, slots=True)
class _ShellSyntax:
    chaining: bool = False
    redirection: bool = False
    detached: bool = False
    command_substitution: bool = False
    variable_expansion: bool = False
    unmatched_quote: bool = False


def _scan_shell_syntax(command: str) -> _ShellSyntax:
    single = False
    double = False
    escaped = False
    chaining = redirection = detached = command_substitution = variable_expansion = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not single:
            escaped = True
            index += 1
            continue
        if char == "`" and not single:
            if double and index + 1 < len(command) and command[index + 1] == "`":
                index += 2
                continue
            command_substitution = True
            index += 1
            continue
        if char == "'" and not double:
            single = not single
            index += 1
            continue
        if char == '"' and not single:
            double = not double
            index += 1
            continue
        if not single and char == "$" and index + 1 < len(command):
            if command[index + 1] == "(":
                command_substitution = True
                index += 2
                continue
            if command[index + 1].isalpha() or command[index + 1] in "_{":
                variable_expansion = True
        if not single and char == "%" and command.find("%", index + 1) >= 0:
            variable_expansion = True
        if single or double:
            index += 1
            continue
        if char in "\r\n;|":
            chaining = True
        elif char in "<>":
            redirection = True
        elif char == "&":
            if index + 1 < len(command) and command[index + 1] in "&":
                chaining = True
                index += 1
            else:
                detached = True
        index += 1
    return _ShellSyntax(
        chaining=chaining,
        redirection=redirection,
        detached=detached,
        command_substitution=command_substitution,
        variable_expansion=variable_expansion,
        unmatched_quote=single or double or escaped,
    )


def _split_command(command: str, *, platform: str | None) -> list[str]:
    is_windows = (platform or ("windows" if os.name == "nt" else "posix")).lower() in {
        "windows",
        "win32",
        "nt",
    }
    return shlex.split(command, posix=not is_windows)


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _executable_name(token: str) -> str:
    normalized = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return normalized.removesuffix(".exe")


def _has_explicit_executable_path(token: str) -> bool:
    portable = token.replace("\\", "/")
    return "/" in portable or re.match(r"^[a-zA-Z]:", portable) is not None


def _contains_boundary_path(tokens: tuple[str, ...]) -> bool:
    for token in tokens:
        portable = token.replace("\\", "/")
        if (
            portable.startswith(("/", "//", "~/"))
            or re.match(r"^[a-zA-Z]:/", portable) is not None
            or any(part == ".." for part in portable.split("/"))
        ):
            return True
    return False


def _contains_sensitive_target(tokens: tuple[str, ...]) -> bool:
    for token in tokens[1:]:
        candidate = token.rsplit("=", 1)[-1].replace("\\", "/")
        parts = [part.lower() for part in candidate.split("/") if part not in {"", "."}]
        if not parts:
            continue
        if ".mini-agent" in parts or any(part in _SENSITIVE_DIRECTORIES for part in parts):
            return True
        name = parts[-1]
        if name in _ENV_TEMPLATE_NAMES:
            continue
        if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
            return True
        if name in _SENSITIVE_FILENAMES or name.startswith(
            ("credentials.", "secret.", "secrets.", "token.")
        ):
            return True
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".secret", ".secrets")):
            return True
    return False


def _looks_like_network(executable: str, arguments: tuple[str, ...]) -> bool:
    if executable in {
        "curl",
        "wget",
        "ftp",
        "scp",
        "ssh",
        "sftp",
        "invoke-webrequest",
        "irm",
        "iwr",
    }:
        return True
    if (
        executable == "git"
        and arguments
        and arguments[0]
        in {
            "clone",
            "fetch",
            "pull",
            "push",
            "remote",
        }
    ):
        return True
    return any(re.match(r"^(?:https?|ssh|git)://", argument) is not None for argument in arguments)


def _looks_like_install(executable: str, arguments: tuple[str, ...]) -> bool:
    if executable in {
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "uv",
        "brew",
        "apt",
        "apt-get",
        "cargo",
    }:
        return any(
            argument in {"install", "add", "update", "upgrade", "remove", "uninstall"}
            for argument in arguments
        )
    return executable in {"choco", "winget", "dnf", "yum", "pacman", "gem"}


def _looks_like_deletion(executable: str, arguments: tuple[str, ...]) -> bool:
    return (
        executable
        in {
            "rm",
            "rmdir",
            "del",
            "erase",
            "remove-item",
            "remove-itemproperty",
        }
        or (executable == "git" and bool(arguments) and arguments[0] == "clean")
        or (executable == "find" and "-delete" in arguments)
    )


def _looks_catastrophic_deletion(arguments: tuple[str, ...]) -> bool:
    return any(argument in {"/", "\\", "*", "/s", "-recurse"} for argument in arguments)


def _recognized_local_category(
    executable: str,
    arguments: tuple[str, ...],
) -> ShellCommandClass | None:
    if executable in {
        "pwd",
        "ls",
        "dir",
        "get-childitem",
        "gci",
        "cat",
        "type",
        "get-content",
        "gc",
        "head",
        "tail",
        "wc",
        "grep",
        "rg",
        "select-string",
        "findstr",
        "find",
        "file",
        "echo",
        "printf",
        "test-path",
        "get-location",
        "git",
    }:
        if executable == "git" and (not arguments or arguments[0] not in _GIT_READ_COMMANDS):
            return None
        if executable == "find" and any(
            argument in {"-delete", "-exec", "-execdir"} for argument in arguments
        ):
            return None
        return ShellCommandClass.LOCAL_READ
    if executable in {
        "pytest",
        "ruff",
        "mypy",
    }:
        if any(
            argument in {"test", "check", "verify", "lint"} or "test" in argument
            for argument in arguments
        ):
            return ShellCommandClass.LOCAL_TEST
        if any(argument in {"build", "compile", "package"} for argument in arguments):
            return ShellCommandClass.LOCAL_BUILD
        if executable in {"pytest", "mypy"}:
            return ShellCommandClass.LOCAL_TEST
    if executable in {"make", "ninja", "cmake"}:
        if executable == "cmake" and "--build" not in arguments:
            return None
        if any(argument in {"test", "check", "verify"} for argument in arguments):
            return ShellCommandClass.LOCAL_TEST
        return ShellCommandClass.LOCAL_BUILD
    if executable == "ruff" and arguments[:1] in {("check",), ("format",)}:
        return ShellCommandClass.LOCAL_TEST
    return None


_GIT_READ_COMMANDS = frozenset(
    {"status", "diff", "log", "show", "branch", "tag", "ls-files", "rev-parse"}
)
_GIT_WRITE_COMMANDS = frozenset(
    {
        "add",
        "apply",
        "branch",
        "checkout",
        "clean",
        "commit",
        "config",
        "merge",
        "mv",
        "rebase",
        "reset",
        "restore",
        "rm",
        "stash",
        "switch",
        "tag",
    }
)
_INTERPRETERS = frozenset(
    {
        "bash",
        "cmd",
        "csh",
        "fish",
        "node",
        "perl",
        "php",
        "powershell",
        "pwsh",
        "python",
        "python2",
        "python3",
        "ruby",
        "sh",
        "zsh",
    }
)
_INTERACTIVE_EXECUTABLES = frozenset(
    {"less", "more", "nano", "top", "htop", "vim", "vi", "emacs", "ssh", "sftp"}
)
_DETACHED_EXECUTABLES = frozenset({"nohup", "setsid", "disown", "start", "start-process"})


def _classification(
    category: ShellCommandClass,
    hazards: tuple[str, ...],
    rule: str,
    reason: str,
    *,
    recognized: bool = False,
) -> ShellCommandClassification:
    return ShellCommandClassification(
        category, tuple(dict.fromkeys(hazards)), rule, reason, recognized
    )


def _as_shell_input(arguments: BaseModel) -> ShellInput:
    if not isinstance(arguments, ShellInput):
        return ShellInput.model_validate(arguments.model_dump(mode="json"))
    return arguments


def _call(call_id: str) -> ToolCall:
    return ToolCall(tool_call_id=call_id, name="shell", arguments={})


def _workspace_failure(call_id: str, exc: WorkspacePathError) -> ToolResult:
    return ToolResult.failed(
        _call(call_id),
        outcome=ToolOutcome.DENIED if exc.hard_denial else ToolOutcome.FAILED,
        category="permission" if exc.hard_denial else "tool-execution",
        code=exc.code,
        message=str(exc),
    )


def _shell_failure(
    call_id: str,
    *,
    code: str,
    message: str,
    category: str = "tool-execution",
    outcome: ToolOutcome = ToolOutcome.FAILED,
    detail: str | None = None,
    **data: Any,
) -> ToolResult:
    if detail:
        message = f"{message}: {redact_secrets(detail)}"
    return ToolResult.failed(
        _call(call_id),
        outcome=outcome,
        category=category,
        code=code,
        message=message,
        data=data,
    )


def _shell_argv(command: str) -> tuple[str, ...]:
    if os.name == "nt":
        return ("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command)
    return ("/bin/sh", "-c", command)


async def _spawn_process(
    command: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> asyncio.subprocess.Process:
    argv = _shell_argv(command)
    if os.name == "nt":
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200),
        )
        _create_windows_job(process.pid)
        return process
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


async def _wait_for_process(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    started: float,
    removed_secrets: tuple[str, ...],
    working_directory: str,
) -> _ShellExecution:
    collector = _OutputCollector(max_output_bytes)
    stdout_task = asyncio.create_task(collector.read("stdout", process.stdout))
    stderr_task = asyncio.create_task(collector.read("stderr", process.stderr))
    termination = "completed"
    uncertain = False
    try:
        returncode: int | None = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        termination = "timeout"
        returncode, uncertain = await _terminate_process(process)
    finally:
        await _finish_output_readers(process, stdout_task, stderr_task)
        _close_windows_job(process.pid)
    stdout, stderr, truncated = collector.values(removed_secrets)
    del working_directory
    return _ShellExecution(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        output_truncated=truncated,
        duration_seconds=round(time.monotonic() - started, 6),
        termination=termination,
        uncertain=uncertain,
    )


async def _cancel_process(
    process: asyncio.subprocess.Process,
    *,
    max_output_bytes: int,
    started: float,
    removed_secrets: tuple[str, ...],
    working_directory: str,
) -> _ShellExecution:
    collector = _OutputCollector(max_output_bytes)
    stdout_task = asyncio.create_task(collector.read("stdout", process.stdout))
    stderr_task = asyncio.create_task(collector.read("stderr", process.stderr))
    returncode, uncertain = await _terminate_process(process)
    await _finish_output_readers(process, stdout_task, stderr_task)
    _close_windows_job(process.pid)
    stdout, stderr, truncated = collector.values(removed_secrets)
    del working_directory
    return _ShellExecution(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        output_truncated=truncated,
        duration_seconds=round(time.monotonic() - started, 6),
        termination="cancelled",
        uncertain=uncertain,
    )


async def _terminate_process(process: asyncio.subprocess.Process) -> tuple[int | None, bool]:
    """Interrupt a process group cooperatively, then escalate if necessary."""

    if process.returncode is not None:
        return process.returncode, False
    uncertain = False
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            _kill_process_group(process.pid, signal.SIGINT)
    except (OSError, ProcessLookupError, AttributeError):
        uncertain = True
    try:
        returncode: int | None = await asyncio.wait_for(
            process.wait(), timeout=_READ_TERMINATION_GRACE_SECONDS
        )
        return returncode, uncertain
    except TimeoutError:
        pass

    try:
        if os.name == "nt":
            tree_stopped = await _windows_terminate_process_tree(process.pid)
            if not tree_stopped:
                uncertain = True
                process.terminate()
        else:
            _kill_process_group(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, AttributeError):
        uncertain = True
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=_ESCALATION_GRACE_SECONDS)
        return returncode, uncertain
    except TimeoutError:
        pass

    try:
        if os.name == "nt":
            tree_stopped = await _windows_terminate_process_tree(process.pid)
            if not tree_stopped:
                uncertain = True
                process.kill()
        else:
            _kill_process_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, ProcessLookupError, AttributeError):
        uncertain = True
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=_ESCALATION_GRACE_SECONDS)
    except TimeoutError:
        returncode = None
        uncertain = True
    return returncode, uncertain


def _kill_process_group(pid: int, signum: int) -> None:
    killpg = cast(Callable[[int, int], None], getattr(os, "killpg"))
    killpg(pid, signum)


def _create_windows_job(pid: int) -> None:
    """Attach a kill-on-close Job Object so descendants share Shell cleanup."""

    if os.name != "nt":
        return
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.restype = wintypes.BOOL

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(job)
            return
        process_handle = kernel32.OpenProcess(0x1101, False, pid)
        if not process_handle or not kernel32.AssignProcessToJobObject(job, process_handle):
            if process_handle:
                kernel32.CloseHandle(process_handle)
            kernel32.CloseHandle(job)
            return
        kernel32.CloseHandle(process_handle)
        _WINDOWS_JOB_HANDLES[pid] = int(job)
    except (AttributeError, OSError, TypeError, ValueError):
        return


def _close_windows_job(pid: int) -> None:
    if os.name != "nt":
        return
    handle = _WINDOWS_JOB_HANDLES.pop(pid, None)
    if handle is None:
        return
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return


async def _windows_terminate_process_tree(pid: int) -> bool:
    """Use Windows' process-tree control for escalation after CTRL_BREAK."""

    if os.name != "nt":
        return False
    try:
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await asyncio.wait_for(taskkill.wait(), timeout=2.0)
        return returncode == 0
    except (OSError, TimeoutError, ValueError):
        return False


async def _finish_output_readers(
    process: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[None],
    stderr_task: asyncio.Task[None],
) -> None:
    try:
        await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=1.0)
    except (TimeoutError, asyncio.CancelledError):
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        _close_process_streams(process)
    finally:
        _close_process_streams(process)


def _close_process_streams(process: asyncio.subprocess.Process) -> None:
    transport = getattr(process, "_transport", None)
    close = getattr(transport, "close", None)
    if callable(close):
        close()
    for stream in (process.stdout, process.stderr):
        stream_transport = getattr(stream, "_transport", None)
        stream_close = getattr(stream_transport, "close", None)
        if callable(stream_close):
            stream_close()


class _OutputCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._remaining = limit
        self._lock = asyncio.Lock()
        self._buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self._truncated = False

    async def read(self, name: str, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_CHUNK_SIZE)
            if not chunk:
                return
            async with self._lock:
                take = min(len(chunk), self._remaining)
                if take:
                    self._buffers[name].extend(chunk[:take])
                    self._remaining -= take
                if take < len(chunk):
                    self._truncated = True

    def values(self, secrets: tuple[str, ...]) -> tuple[str, str, bool]:
        stdout = redact_secrets(bytes(self._buffers["stdout"]).decode("utf-8", "replace"), secrets)
        stderr = redact_secrets(bytes(self._buffers["stderr"]).decode("utf-8", "replace"), secrets)
        stdout, stderr, redaction_truncated = _bound_rendered_output(stdout, stderr, self.limit)
        return stdout, stderr, self._truncated or redaction_truncated


def _bound_rendered_output(stdout: str, stderr: str, limit: int) -> tuple[str, str, bool]:
    """Keep redaction expansion within the same absolute response bound."""

    stdout_bytes = stdout.encode("utf-8")
    stderr_bytes = stderr.encode("utf-8")
    if len(stdout_bytes) + len(stderr_bytes) <= limit:
        return stdout, stderr, False
    bounded_stdout = stdout_bytes[:limit].decode("utf-8", "ignore")
    remaining = max(0, limit - len(bounded_stdout.encode("utf-8")))
    bounded_stderr = stderr_bytes[:remaining].decode("utf-8", "ignore")
    return bounded_stdout, bounded_stderr, True


def _execution_data(execution: _ShellExecution, working_directory: str) -> dict[str, Any]:
    return {
        "working_directory": working_directory,
        "stdout": execution.stdout,
        "stderr": execution.stderr,
        "stdout_bytes": len(execution.stdout.encode("utf-8")),
        "stderr_bytes": len(execution.stderr.encode("utf-8")),
        "output_bytes": len(execution.stdout.encode("utf-8"))
        + len(execution.stderr.encode("utf-8")),
        "output_truncated": execution.output_truncated,
        "exit_code": execution.returncode,
        "duration_seconds": execution.duration_seconds,
        "termination": execution.termination,
    }


def _execution_message(message: str, execution: _ShellExecution) -> str:
    output: list[str] = []
    if execution.stdout:
        output.append(f"stdout:\n{execution.stdout}")
    if execution.stderr:
        output.append(f"stderr:\n{execution.stderr}")
    if execution.output_truncated:
        output.append("output was truncated at the configured limit")
    return "\n".join([message, *output])


def _sensitive_environment_name(name: str) -> bool:
    normalized = name.upper().replace("-", "_")
    if normalized in {
        "MINI_AGENT_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_DEFAULT_PROFILE",
        "AWS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "DOCKER_AUTH_CONFIG",
        "DOCKER_CONFIG",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "KUBECONFIG",
        "NPM_TOKEN",
        "PIP_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "UV_INDEX_URL",
        "CLOUDSDK_CONFIG",
        "AZURE_CONFIG_DIR",
        "SSH_AUTH_SOCK",
    }:
        return True
    return any(
        marker in normalized
        for marker in (
            "API_KEY",
            "ACCESS_TOKEN",
            "AUTH_TOKEN",
            "AUTH",
            "TOKEN",
            "PASSWORD",
            "PASSWD",
            "SECRET",
            "CREDENTIAL",
            "PRIVATE_KEY",
            "AUTHORIZATION",
        )
    )


def _sensitive_environment_value(value: str) -> bool:
    if "://" in value:
        try:
            parsed = urlparse(value)
        except ValueError:
            return True
        if parsed.username is not None or parsed.password is not None:
            return True
    return (
        re.search(
            r"(?i)(?:bearer\s+|basic\s+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|glpat-[a-z0-9_-]{8,})",
            value,
        )
        is not None
    )


def _display_path(path: str) -> str:
    return path.replace("\\", "/") or "."


__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_OUTPUT_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "ShellCommandClass",
    "ShellCommandClassification",
    "ShellInput",
    "ShellTool",
    "classify_shell_command",
    "filtered_child_environment",
]
