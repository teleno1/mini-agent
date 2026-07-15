"""Path-scoped, bounded loading of trusted ``AGENTS.md`` instructions."""

from __future__ import annotations

import hashlib
import re
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


class InstructionError(RuntimeError):
    """Base class for instruction discovery failures."""


class InstructionBoundaryError(InstructionError):
    """Raised when discovery would cross the real Workspace boundary."""


class InstructionLoadError(InstructionError):
    """Raised when relevant instructions cannot be read safely."""


class InstructionConflictError(InstructionError):
    """Raised when multiple targets carry contradictory effective rules."""


class InstructionWarning(UserWarning):
    """Visible warning for an instruction source that blocks automatic work."""


DEFAULT_INSTRUCTION_FILE_BYTES = 32 * 1024
DEFAULT_INSTRUCTION_CHAIN_BYTES = 128 * 1024
_RULE_LINE = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9_. /-]{1,80})\s*:\s*(\S.*)$")


@dataclass(frozen=True, slots=True)
class InstructionDocument:
    """One complete UTF-8 instruction file and its integrity metadata."""

    path: Path
    relative_path: str
    content: str
    byte_count: int
    sha256: str
    token_estimate: int
    depth: int


@dataclass(frozen=True, slots=True)
class InstructionIssue:
    """A bounded-read issue that must be visible to the caller."""

    path: Path
    reason: str
    blocks_automatic_work: bool = True


@dataclass(frozen=True, slots=True)
class InstructionSet:
    """Effective instructions for one or more Workspace targets."""

    workspace_root: Path
    targets: tuple[str, ...]
    documents: tuple[InstructionDocument, ...]
    target_documents: Mapping[str, tuple[str, ...]]
    conflicts: tuple[str, ...]
    issues: tuple[InstructionIssue, ...]

    @property
    def automatic_work_blocked(self) -> bool:
        return bool(self.conflicts or self.issues)

    @property
    def content(self) -> str:
        """Return instructions in root-to-nearest order with source labels."""

        sections = [
            f"# Instructions from {document.relative_path}\n{document.content}"
            for document in self.documents
        ]
        return "\n\n".join(sections)

    @property
    def hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple((document.relative_path, document.sha256) for document in self.documents)

    @property
    def token_estimate(self) -> int:
        return sum(document.token_estimate for document in self.documents)

    def require_automatic_work(self) -> None:
        """Fail closed before an automatic operation uses incomplete instructions."""

        if self.conflicts:
            raise InstructionConflictError(
                "conflicting AGENTS.md rules: " + "; ".join(self.conflicts)
            )
        if self.issues:
            details = "; ".join(f"{issue.path}: {issue.reason}" for issue in self.issues)
            raise InstructionLoadError(f"AGENTS.md instructions are not safely usable: {details}")


class InstructionLoader:
    """Discover ``AGENTS.md`` only inside one resolved Workspace."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        max_file_bytes: int = DEFAULT_INSTRUCTION_FILE_BYTES,
        max_chain_bytes: int = DEFAULT_INSTRUCTION_CHAIN_BYTES,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise InstructionBoundaryError(f"Workspace root is not a directory: {root}")
        if max_file_bytes < 1 or max_chain_bytes < max_file_bytes:
            raise ValueError("instruction limits must be positive and chain >= file")
        self.workspace_root = root
        self.max_file_bytes = max_file_bytes
        self.max_chain_bytes = max_chain_bytes

    def load(self, targets: Iterable[Path | str] = ()) -> InstructionSet:
        target_paths = tuple(self._normalise_target(target) for target in targets)
        if not target_paths:
            target_paths = (self.workspace_root,)
        target_names = tuple(self._relative(path) for path in target_paths)

        documents_by_path: dict[Path, InstructionDocument] = {}
        chains: dict[str, tuple[Path, ...]] = {}
        issues: list[InstructionIssue] = []
        for target_name, target_path in zip(target_names, target_paths, strict=True):
            chain_paths = self._candidate_chain(target_path)
            valid_chain: list[Path] = []
            chain_bytes = 0
            for candidate in chain_paths:
                if not candidate.exists():
                    continue
                try:
                    document = self._read(candidate, len(valid_chain))
                except InstructionError as exc:
                    issue = InstructionIssue(candidate, str(exc))
                    issues.append(issue)
                    warnings.warn(
                        f"{candidate}: {exc}; relevant automatic work is blocked",
                        InstructionWarning,
                        stacklevel=2,
                    )
                    continue
                chain_bytes += document.byte_count
                valid_chain.append(candidate)
                documents_by_path[candidate] = document
                if chain_bytes > self.max_chain_bytes:
                    issue = InstructionIssue(
                        candidate,
                        f"instruction chain exceeds {self.max_chain_bytes} bytes",
                    )
                    issues.append(issue)
                    warnings.warn(
                        f"{candidate}: {issue.reason}; relevant automatic work is blocked",
                        InstructionWarning,
                        stacklevel=2,
                    )
                    break
            chains[target_name] = tuple(valid_chain)

        conflicts = _find_conflicts(chains, documents_by_path)
        ordered_paths = sorted(
            documents_by_path,
            key=lambda path: (len(path.relative_to(self.workspace_root).parts), str(path)),
        )
        return InstructionSet(
            workspace_root=self.workspace_root,
            targets=target_names,
            documents=tuple(documents_by_path[path] for path in ordered_paths),
            target_documents={
                target: tuple(self._relative(path) for path in chain)
                for target, chain in chains.items()
            },
            conflicts=tuple(conflicts),
            issues=tuple(issues),
        )

    discover = load

    def _normalise_target(self, target: Path | str) -> Path:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        if _contains_symlink(candidate, self.workspace_root):
            raise InstructionBoundaryError(f"target crosses a symlink boundary: {candidate}")
        resolved = candidate.resolve()
        if not _is_within(resolved, self.workspace_root):
            raise InstructionBoundaryError(f"target is outside the Workspace: {target}")
        return resolved

    def _candidate_chain(self, target: Path) -> tuple[Path, ...]:
        if target.name == "AGENTS.md":
            directory = target.parent
        elif target.is_dir():
            directory = target
        else:
            directory = target.parent
        directories: list[Path] = []
        current = directory
        while True:
            directories.append(current)
            if current == self.workspace_root:
                break
            if current.parent == current or not _is_within(current.parent, self.workspace_root):
                raise InstructionBoundaryError(
                    f"instruction discovery escaped the Workspace: {target}"
                )
            current = current.parent
        return tuple(directory / "AGENTS.md" for directory in reversed(directories))

    def _read(self, path: Path, depth: int) -> InstructionDocument:
        if _contains_symlink(path, self.workspace_root):
            raise InstructionBoundaryError("AGENTS.md symlinks are not trusted")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise InstructionLoadError(f"cannot read instructions: {exc}") from exc
        if len(data) > self.max_file_bytes:
            raise InstructionLoadError(f"file exceeds {self.max_file_bytes} bytes")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InstructionLoadError("file is not valid UTF-8") from exc
        return InstructionDocument(
            path=path,
            relative_path=self._relative(path),
            content=content,
            byte_count=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            token_estimate=max(1, (len(content) + 3) // 4),
            depth=depth,
        )

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self.workspace_root)
        return "." if str(relative) == "." else relative.as_posix()


def _find_conflicts(
    chains: Mapping[str, tuple[Path, ...]], documents: Mapping[Path, InstructionDocument]
) -> list[str]:
    if len(chains) < 2:
        return []
    effective_rules: dict[str, dict[str, tuple[str, str]]] = {}
    for target, chain in chains.items():
        rules: dict[str, tuple[str, str]] = {}
        for path in chain:
            document = documents[path]
            for line in document.content.splitlines():
                match = _RULE_LINE.match(line)
                if match:
                    key = " ".join(match.group(1).lower().split())
                    rules[key] = (match.group(2).strip(), document.relative_path)
                if line.strip().lower().startswith("conflict:"):
                    rules[f"explicit:{target}"] = (line.strip()[9:].strip(), document.relative_path)
        effective_rules[target] = rules

    conflicts: list[str] = []
    targets = tuple(effective_rules)
    for index, left_target in enumerate(targets):
        for right_target in targets[index + 1 :]:
            shared = set(effective_rules[left_target]) & set(effective_rules[right_target])
            for key in sorted(shared):
                left = effective_rules[left_target][key]
                right = effective_rules[right_target][key]
                if left[0] != right[0]:
                    conflicts.append(
                        f"{key!r} differs for {left_target} ({left[1]}) and "
                        f"{right_target} ({right[1]})"
                    )
    return conflicts


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _contains_symlink(path: Path, root: Path) -> bool:
    if path == root:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
