"""Cross-platform Workspace confinement for model-selected file targets."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4


class WorkspaceError(RuntimeError):
    """Base class for safe, non-secret Workspace failures."""


class WorkspacePathError(WorkspaceError):
    """Raised when a model-selected target is not a safe Workspace path."""

    def __init__(self, code: str, *, sensitive: bool = False) -> None:
        self.code = code
        self.sensitive = sensitive
        messages = {
            "blank": "Workspace target is invalid",
            "absolute": "absolute Workspace targets are not allowed",
            "drive": "drive-changing Workspace targets are not allowed",
            "unc": "UNC Workspace targets are not allowed",
            "traversal": "Workspace traversal is not allowed",
            "device": "device Workspace targets are not allowed",
            "outside": "Workspace target is outside the selected Workspace",
            "missing": "Workspace target does not exist",
            "not-file": "Workspace target is not a file",
            "not-directory": "Workspace target is not a directory",
            "sensitive": "sensitive Workspace target is denied",
            "binary": "binary Workspace target is denied",
            "link": "Workspace links and reparse points cannot be written",
            "path-race": "Workspace target changed after authorization",
            "nul": "Workspace target is invalid",
        }
        # Do not include the requested path: this keeps sensitive names and
        # accidental file contents out of denial details.
        super().__init__(messages.get(code, "Workspace target is denied"))

    @property
    def hard_denial(self) -> bool:
        """Whether host policy must deny rather than report an execution failure."""

        return self.code in {
            "absolute",
            "binary",
            "device",
            "drive",
            "nul",
            "outside",
            "sensitive",
            "traversal",
            "unc",
            "link",
            "path-race",
        }


class SensitiveTargetError(WorkspacePathError):
    def __init__(self) -> None:
        super().__init__("sensitive", sensitive=True)


class BinaryTargetError(WorkspacePathError):
    def __init__(self) -> None:
        super().__init__("binary")


WorkspaceViolation = WorkspacePathError
PathConfinementError = WorkspacePathError


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    """A resolved real path plus the safe Workspace-relative display path."""

    path: Path
    relative_path: str


@dataclass(frozen=True, slots=True)
class WorkspaceWriteTarget:
    """A lexical target whose existing path components are safe to write."""

    path: Path
    relative_path: str
    existed: bool
    parent: Path


_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_BINARY_SUFFIXES = {
    ".7z",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".doc",
    ".docx",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".wav",
    ".webp",
    ".zip",
}
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


class Workspace:
    """Resolve one real Workspace root and enforce every read beneath it."""

    _active_tool_call_id: str | None
    _recovery_path: Path | None

    def __init__(self, root: Path | str, *, checkpoint_directory: Path | None = None) -> None:
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError("could not resolve Workspace root") from exc
        if not resolved.is_dir():
            raise WorkspaceError("Workspace root must be a directory")
        self._root = resolved
        self._active_tool_call_id = None
        self._recovery_path = None
        self._checkpoint_directory = (
            checkpoint_directory.resolve(strict=False)
            if checkpoint_directory is not None
            else resolved / ".mini-agent" / "checkpoints"
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def checkpoint_directory(self) -> Path:
        """Session-local storage reserved for host-created Patch Checkpoints."""

        return self._checkpoint_directory

    @property
    def recovery_directory(self) -> Path:
        """Durable, non-authoritative evidence for an in-flight Tool call."""

        return self._checkpoint_directory.parent / "recovery"

    @property
    def active_tool_call_id(self) -> str | None:
        """The serial Tool call currently executing in this Workspace view."""

        return getattr(self, "_active_tool_call_id", None)

    def begin_tool_recovery(
        self,
        tool_call_id: str,
        tool_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write a best-effort recovery record before a Tool side effect starts.

        The JSONL event log remains authoritative.  This sidecar exists only so
        Resume can inspect process/checkpoint evidence when the process exits
        between ``tool.started`` and its terminal event.
        """

        self._active_tool_call_id = tool_call_id
        self._recovery_path = self.recovery_directory / (
            f"{sha256(tool_call_id.encode('utf-8')).hexdigest()}.json"
        )
        self._write_recovery_record(
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "state": "started",
                **(metadata or {}),
            }
        )

    def update_tool_recovery(self, **updates: Any) -> None:
        """Merge bounded runtime evidence into the active Tool sidecar."""

        path = getattr(self, "_recovery_path", None)
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            record = {
                "tool_call_id": self.active_tool_call_id,
                "state": "unknown",
            }
        if isinstance(record, dict):
            record.update(updates)
            self._write_recovery_record(record)

    def clear_tool_recovery(self, tool_call_id: str | None = None) -> None:
        """Remove evidence only after the corresponding terminal event is durable."""

        if tool_call_id is not None and self.active_tool_call_id not in {None, tool_call_id}:
            return
        path = getattr(self, "_recovery_path", None)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The terminal event is authoritative; a cleanup failure leaves
                # inspectable evidence rather than changing the Tool outcome.
                pass
        self._active_tool_call_id = None
        self._recovery_path = None

    def _write_recovery_record(self, record: dict[str, Any]) -> None:
        path = getattr(self, "_recovery_path", None)
        if path is None:
            return
        temporary = self.recovery_directory / f"{path.name}.{uuid4().hex}.tmp"
        try:
            self.recovery_directory.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def for_session(self, session_id: str) -> Workspace:
        """Return a view sharing this root and using one Session's checkpoints."""

        if not session_id or Path(session_id).name != session_id or session_id in {".", ".."}:
            raise ValueError("invalid Session ID")
        return Workspace(
            self._root,
            checkpoint_directory=self._root
            / ".mini-agent"
            / "sessions"
            / session_id
            / "checkpoints",
        )

    def resolve_read(self, target: str, *, directory: bool = False) -> WorkspaceTarget:
        """Resolve an existing file/directory after lexical and real-path checks."""

        relative = self._validate_relative(target)
        candidate = self._root.joinpath(*relative.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("missing") from exc
        if not _is_within(resolved, self._root):
            raise WorkspacePathError("outside")
        if self._is_sensitive(relative, resolved):
            raise SensitiveTargetError()
        if directory:
            if not resolved.is_dir():
                raise WorkspacePathError("not-directory")
        elif not resolved.is_file():
            raise WorkspacePathError("not-file")
        if not directory and _looks_binary_name(resolved.name):
            raise BinaryTargetError()
        return WorkspaceTarget(resolved, resolved.relative_to(self._root).as_posix())

    def resolve_write(self, target: str, *, allow_missing: bool = True) -> WorkspaceWriteTarget:
        """Resolve a write target and reject links/reparse components.

        The returned path is the lexical path below the selected Workspace.  It
        is intentionally not a real-path alias: callers re-run this check just
        before committing a prepared transaction to close the approval race.
        """

        relative = self._validate_relative(target)
        candidate = self._root.joinpath(*relative.split("/"))
        if relative == ".":
            raise WorkspacePathError("not-file")
        if self._is_sensitive(relative, candidate):
            raise SensitiveTargetError()
        if _looks_binary_name(candidate.name):
            raise BinaryTargetError()
        parent = candidate.parent
        existing_parent = self._nearest_existing_parent(parent)
        self._ensure_no_reparse_components(existing_parent)
        if not _is_within(existing_parent.resolve(strict=True), self._root):
            raise WorkspacePathError("outside")
        if candidate.exists() or candidate.is_symlink():
            self._ensure_no_reparse_components(candidate)
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise WorkspacePathError("missing") from exc
            if not _is_within(resolved, self._root):
                raise WorkspacePathError("outside")
            if self._is_sensitive(relative, resolved):
                raise SensitiveTargetError()
            if not resolved.is_file():
                raise WorkspacePathError("not-file")
            return WorkspaceWriteTarget(candidate, relative, True, parent)
        if not allow_missing:
            raise WorkspacePathError("missing")
        return WorkspaceWriteTarget(candidate, relative, False, parent)

    def recheck_write(self, target: WorkspaceWriteTarget) -> WorkspaceWriteTarget:
        """Repeat the no-link and boundary checks immediately before commit."""

        current = self.resolve_write(target.relative_path, allow_missing=True)
        if current.existed != target.existed:
            raise WorkspacePathError("path-race")
        if current.path != target.path:
            raise WorkspacePathError("path-race")
        return current

    def is_protected_path(self, target: str) -> bool:
        """Return whether a Workspace-relative path is always confirmation-gated."""

        try:
            relative = self._validate_relative(target)
        except WorkspacePathError:
            return False
        return is_protected_relative(relative)

    def read_text_bytes(self, target: WorkspaceTarget) -> bytes:
        """Read bytes only after the caller has completed target authorization."""

        try:
            data = target.path.read_bytes()
        except OSError as exc:
            raise WorkspaceError("could not read Workspace target") from exc
        if b"\x00" in data:
            raise BinaryTargetError()
        return data

    def is_ignored(self, relative_path: str) -> bool:
        """Apply the small, deterministic ignore subset used by fallback search."""

        gitignore = self._root / ".gitignore"
        try:
            lines = gitignore.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return False
        path = relative_path.replace("\\", "/").lstrip("./")
        ignored = False
        for raw_rule in lines:
            rule = raw_rule.strip()
            if not rule or rule.startswith("#"):
                continue
            negated = rule.startswith("!")
            if negated:
                rule = rule[1:]
            anchored = rule.startswith("/")
            rule = rule.lstrip("/")
            directory_rule = rule.endswith("/")
            rule = rule.rstrip("/")
            matched = False
            if directory_rule and (path == rule or path.startswith(rule + "/")):
                matched = True
            elif anchored and _glob_match(path, rule):
                matched = True
            elif not anchored and (
                _glob_match(path, rule) or any(_glob_match(part, rule) for part in path.split("/"))
            ):
                matched = True
            if matched:
                ignored = not negated
        return ignored

    def _validate_relative(self, target: str) -> str:
        if not isinstance(target, str) or not target.strip():
            raise WorkspacePathError("blank")
        if "\x00" in target:
            raise WorkspacePathError("nul")
        portable = target.replace("\\", "/")
        windows = PureWindowsPath(target)
        posix = PurePosixPath(portable)
        if (
            target.startswith("\\\\")
            or windows.anchor.startswith("\\\\")
            or portable.startswith("//")
        ):
            raise WorkspacePathError("unc")
        if windows.drive:
            raise WorkspacePathError("drive")
        if windows.is_absolute() or posix.is_absolute() or target.startswith(("/", "\\")):
            raise WorkspacePathError("absolute")
        parts = [part for part in portable.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise WorkspacePathError("traversal")
        if any(_is_device_component(part) for part in parts):
            raise WorkspacePathError("device")
        if not parts:
            return "."
        relative = "/".join(parts)
        if self._is_sensitive(relative, self._root / relative):
            raise SensitiveTargetError()
        return relative

    def _nearest_existing_parent(self, path: Path) -> Path:
        current = path
        while not current.exists() and not current.is_symlink():
            if current == self._root:
                break
            current = current.parent
        if current.is_symlink():
            raise WorkspacePathError("link")
        if not _is_within(current.resolve(strict=True), self._root):
            raise WorkspacePathError("outside")
        if not current.is_dir():
            raise WorkspacePathError("not-directory")
        return current

    def _ensure_no_reparse_components(self, path: Path) -> None:
        """Reject symlink and Windows reparse components up to ``path``."""

        try:
            relative_parts = path.relative_to(self._root).parts
        except ValueError as exc:
            raise WorkspacePathError("outside") from exc
        current = self._root
        for part in relative_parts:
            current = current / part
            if _is_reparse_point(current):
                raise WorkspacePathError("link")

    def _is_sensitive(self, relative: str, resolved: Path) -> bool:
        parts = [part.lower() for part in relative.replace("\\", "/").split("/")]
        if ".mini-agent" in parts:
            return True
        if any(part in _SENSITIVE_DIRECTORIES for part in parts):
            return True
        name = (resolved.name or relative.rsplit("/", 1)[-1]).lower()
        if name in {
            "credentials",
            "credentials.json",
            "secrets.json",
        }:
            return True
        if name in _SENSITIVE_FILENAMES or name.startswith(
            ("credentials.", "secret.", "secrets.", "token.")
        ):
            return True
        if (name.startswith(".env") or name.endswith(".env")) and name not in _ENV_TEMPLATE_NAMES:
            return True
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".secret", ".secrets")):
            return True
        return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_device_component(component: str) -> bool:
    stem = component.rstrip(" .").split(".", 1)[0].lower()
    return stem in _WINDOWS_DEVICE_NAMES or component.startswith(("\\\\?\\", "\\\\.\\"))


def _looks_binary_name(name: str) -> bool:
    return Path(name).suffix.lower() in _BINARY_SUFFIXES


def _is_reparse_point(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise WorkspacePathError("missing") from exc
    if stat.S_ISLNK(mode):
        return True
    if os.name == "nt":
        try:
            attributes = path.stat().st_file_attributes
        except OSError as exc:
            raise WorkspacePathError("missing") from exc
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


def is_protected_relative(relative: str) -> bool:
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in {"", "."}]
    lowered = [part.lower() for part in parts]
    if ".git" in lowered or ".github" in lowered or ".gitlab" in lowered or ".circleci" in lowered:
        return True
    name = lowered[-1] if lowered else ""
    protected_names = {
        "agents.md",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".travis.yml",
        "appveyor.yml",
        "azure-pipelines.yml",
        "buildkite.yml",
        "jenkinsfile",
        "ci.yml",
        ".gitlab-ci.yml",
        ".drone.yml",
        "security.md",
        "codeowners",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
    }
    return name in protected_names or name.startswith(".github/")


def _glob_match(value: str, pattern: str) -> bool:
    # fnmatch is intentionally used here instead of a shell: the value is
    # never interpolated into a command.
    from fnmatch import fnmatchcase

    return fnmatchcase(value, pattern) or (
        pattern.startswith("**/") and fnmatchcase(value, pattern[3:])
    )
