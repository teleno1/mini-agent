"""Cross-platform Workspace confinement for model-selected file targets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


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
            "nul": "Workspace target is invalid",
        }
        # Do not include the requested path: this keeps sensitive names and
        # accidental file contents out of denial details.
        super().__init__(messages.get(code, "Workspace target is denied"))


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

    def __init__(self, root: Path | str) -> None:
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError("could not resolve Workspace root") from exc
        if not resolved.is_dir():
            raise WorkspaceError("Workspace root must be a directory")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

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
        for raw_rule in lines:
            rule = raw_rule.strip()
            if not rule or rule.startswith("#") or rule.startswith("!"):
                continue
            anchored = rule.startswith("/")
            rule = rule.lstrip("/")
            directory_rule = rule.endswith("/")
            rule = rule.rstrip("/")
            if directory_rule and (path == rule or path.startswith(rule + "/")):
                return True
            if anchored and _glob_match(path, rule):
                return True
            if not anchored and (
                _glob_match(path, rule) or any(_glob_match(part, rule) for part in path.split("/"))
            ):
                return True
        return False

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


def _glob_match(value: str, pattern: str) -> bool:
    # fnmatch is intentionally used here instead of a shell: the value is
    # never interpolated into a command.
    from fnmatch import fnmatchcase

    return fnmatchcase(value, pattern) or (
        pattern.startswith("**/") and fnmatchcase(value, pattern[3:])
    )
