"""Exact text Patch and no-overwrite file creation Tools.

The transaction in this module is deliberately file-scoped.  It provides
logical all-or-nothing behavior for ordinary failures and leaves a durable
Checkpoint when a process can no longer prove what happened.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from mini_agent.tools.contracts import (
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolLimits,
    ToolOutcome,
    ToolResult,
)
from mini_agent.tools.workspace import (
    BinaryTargetError,
    Workspace,
    WorkspaceError,
    WorkspacePathError,
    WorkspaceWriteTarget,
    is_protected_relative,
)

MAX_PATCH_FILES = 10
MAX_PATCH_BYTES = 256 * 1024
MAX_RESULT_BYTES = 64 * 1024


class PatchOperationKind(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


class PatchOperation(BaseModel):
    """One exact Add, Update, or Delete operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    operation: Literal["add", "update", "delete"] = Field(
        validation_alias=AliasChoices("operation", "op", "action", "kind")
    )
    path: str = Field(min_length=1)
    old_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("old_text", "old", "expected", "original"),
    )
    new_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("new_text", "new", "content", "replacement", "text"),
    )
    expected_sha256: str | None = Field(
        default=None,
        validation_alias=AliasChoices("expected_sha256", "old_sha256", "expected_hash"),
        min_length=64,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_shape(self) -> PatchOperation:
        if self.operation == "add":
            if self.new_text is None:
                raise ValueError("add operation requires content")
            if self.old_text is not None or self.expected_sha256 is not None:
                raise ValueError("add operation cannot contain an expected old value")
        elif self.operation == "update":
            if self.old_text is None or self.new_text is None:
                raise ValueError("update operation requires old and new text")
            if not self.old_text:
                raise ValueError("update operation requires non-empty exact context")
        elif self.operation == "delete":
            if self.new_text is not None:
                raise ValueError("delete operation cannot contain replacement text")
        return self


class ApplyPatchInput(BaseModel):
    """Bounded exact patch request accepted by ``apply_patch``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    operations: tuple[PatchOperation, ...] = Field(
        min_length=1,
        max_length=MAX_PATCH_FILES,
        validation_alias=AliasChoices("operations", "patches", "files"),
    )

    @model_validator(mode="after")
    def validate_bound(self) -> ApplyPatchInput:
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_PATCH_BYTES:
            raise ValueError("patch request exceeds the 256 KiB limit")
        if len({operation.path.replace("\\", "/") for operation in self.operations}) != len(
            self.operations
        ):
            raise ValueError("a patch cannot contain the same path more than once")
        return self


class CreateFileInput(BaseModel):
    """One UTF-8 file creation request."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    path: str = Field(min_length=1)
    content: str = Field(
        validation_alias=AliasChoices("content", "text", "new_text"),
    )
    create_parents: bool = Field(
        default=True,
        validation_alias=AliasChoices("create_parents", "make_parents", "parents"),
    )

    @model_validator(mode="after")
    def validate_bound(self) -> CreateFileInput:
        try:
            encoded = self.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("file content must be valid UTF-8") from exc
        if len(encoded) > MAX_PATCH_BYTES:
            raise ValueError("file content exceeds the 256 KiB limit")
        return self


class WriteValidationError(ValueError):
    """A bounded, non-secret validation failure before any target write."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _PreparedChange:
    operation: str
    target: WorkspaceWriteTarget
    original_exists: bool
    original_bytes: bytes | None
    original_hash: str | None
    original_file_id: tuple[int, int] | None
    new_bytes: bytes | None
    temporary: Path | None


@dataclass(frozen=True, slots=True)
class _CheckpointSnapshot:
    relative_path: str
    existed: bool
    digest: str | None
    data_path: Path | None


class PatchCheckpoint:
    """Durable before-image for one Patch Transaction."""

    def __init__(
        self,
        checkpoint_id: str,
        directory: Path,
        snapshots: tuple[_CheckpointSnapshot, ...],
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.directory = directory
        self.snapshots = snapshots

    @classmethod
    def create(cls, workspace: Workspace, changes: Iterable[_PreparedChange]) -> PatchCheckpoint:
        checkpoint_id = f"checkpoint-{uuid4().hex}"
        directory = workspace.checkpoint_directory / checkpoint_id
        snapshot_directory = directory / "before"
        snapshot_directory.mkdir(parents=True, exist_ok=False)
        snapshots: list[_CheckpointSnapshot] = []
        manifest: list[dict[str, object]] = []
        try:
            for index, change in enumerate(changes):
                data_path: Path | None = None
                digest = change.original_hash
                if change.original_exists:
                    if change.original_bytes is None or digest is None:
                        raise WriteValidationError(
                            "checkpoint", "Checkpoint before-image is missing"
                        )
                    data_path = snapshot_directory / f"{index:04d}.bin"
                    _atomic_write(data_path, change.original_bytes)
                snapshot = _CheckpointSnapshot(
                    change.target.relative_path,
                    change.original_exists,
                    digest,
                    data_path,
                )
                snapshots.append(snapshot)
                manifest.append(
                    {
                        "path": snapshot.relative_path,
                        "existed": snapshot.existed,
                        "sha256": snapshot.digest,
                        "bytes": len(change.original_bytes or b"") if snapshot.existed else 0,
                    }
                )
            _atomic_write(
                directory / "manifest.json",
                (
                    json.dumps(
                        {
                            "checkpoint_id": checkpoint_id,
                            "files": manifest,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        except BaseException:
            # An incomplete Checkpoint is not evidence of a committed Patch.
            _remove_tree(directory)
            raise
        return cls(checkpoint_id, directory, tuple(snapshots))

    async def rollback(
        self,
        workspace: Workspace,
        applied_paths: set[str],
        created_directories: tuple[Path, ...],
        expected_hashes: Mapping[str, str | None],
    ) -> tuple[dict[str, str], ...]:
        evidence: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for snapshot in reversed(self.snapshots):
            if snapshot.relative_path not in applied_paths:
                continue
            try:
                target = workspace.resolve_write(snapshot.relative_path, allow_missing=True)
                current_hash = _sha256(target.path.read_bytes()) if target.existed else None
                expected_hash = expected_hashes.get(snapshot.relative_path)
                if snapshot.existed:
                    if snapshot.data_path is None:
                        raise WriteValidationError(
                            "checkpoint", "Checkpoint before-image is missing"
                        )
                    if current_hash not in {None, expected_hash}:
                        raise WriteValidationError(
                            "rollback-race",
                            "target changed while rollback evidence was being collected",
                        )
                    data = snapshot.data_path.read_bytes()
                    _restore_file(target, data)
                    evidence.append(
                        {
                            "path": snapshot.relative_path,
                            "action": "restored",
                            "sha256": _sha256(data),
                        }
                    )
                elif target.existed and current_hash == expected_hash:
                    _ensure_no_link_target(workspace, target)
                    target.path.unlink()
                    evidence.append(
                        {"path": snapshot.relative_path, "action": "removed-created-file"}
                    )
                elif not target.existed:
                    evidence.append({"path": snapshot.relative_path, "action": "already-absent"})
                else:
                    raise WriteValidationError(
                        "rollback-race",
                        "created target changed while rollback evidence was being collected",
                    )
                await asyncio.sleep(0)
            except BaseException as exc:
                failures.append(
                    {
                        "path": snapshot.relative_path,
                        "action": "rollback-failed",
                        "reason": _safe_exception_code(exc),
                    }
                )
        for directory in sorted(
            created_directories, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                directory.rmdir()
                evidence.append({"path": str(directory), "action": "removed-created-directory"})
            except FileNotFoundError:
                continue
            except OSError:
                # A non-empty directory is useful evidence that an external
                # actor raced the transaction; it must not be recursively removed.
                failures.append(
                    {
                        "path": str(directory),
                        "action": "rollback-failed",
                        "reason": "directory-not-empty",
                    }
                )
        return tuple(evidence + failures)


class _WriteTool:
    name: str
    side_effect = SideEffectCategory.WRITE
    limits = ToolLimits.bounded(timeout_seconds=30.0, max_output_bytes=MAX_RESULT_BYTES)

    def _failure(
        self,
        call_id: str,
        code: str,
        message: str,
        *,
        category: str = "tool-execution",
        outcome: ToolOutcome = ToolOutcome.FAILED,
        data: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult.failed(
            ToolCall(tool_call_id=call_id, name=self.name, arguments={}),
            outcome=outcome,
            category=category,
            code=code,
            message=message,
            data=data,
        )

    def _risk_hazards(self, paths: Iterable[str], *, delete: bool = False) -> tuple[str, ...]:
        hazards = {"delete"} if delete else set[str]()
        if any(is_protected_relative(path) for path in paths):
            hazards.add("protected-path")
        return tuple(sorted(hazards))


class ApplyPatchTool(_WriteTool):
    """Apply exact multi-file text changes as one reviewable transaction."""

    name = "apply_patch"
    description = "Apply exact Add, Update, and Delete UTF-8 text operations transactionally."
    input_model: ClassVar[type[BaseModel]] = ApplyPatchInput

    def preflight(self, workspace: Workspace, arguments: BaseModel) -> tuple[str, ...]:
        request = _as_patch_input(arguments)
        changes = _prepare_changes(
            workspace,
            request,
            create_temporary=False,
            allow_missing_parents=False,
        )
        return tuple(change.target.relative_path for change in changes)

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        request = _as_patch_input(arguments)
        paths = tuple(operation.path for operation in request.operations)
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=paths,
            hazards=self._risk_hazards(
                paths,
                delete=any(operation.operation == "delete" for operation in request.operations),
            ),
            summary=f"apply an exact text Patch to {len(paths)} Workspace file(s)",
        )

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        request = _as_patch_input(arguments)
        call_id = str(
            workspace.active_tool_call_id or getattr(arguments, "tool_call_id", "apply-patch")
        )
        return await _run_transaction(
            workspace,
            request.operations,
            call_id=call_id,
            tool_name=self.name,
            create_parents=False,
        )


class CreateFileTool(_WriteTool):
    """Create one UTF-8 file without ever overwriting an existing target."""

    name = "create_file"
    description = "Create one bounded UTF-8 file, optionally creating missing parents."
    input_model: ClassVar[type[BaseModel]] = CreateFileInput

    def preflight(self, workspace: Workspace, arguments: BaseModel) -> tuple[str, ...]:
        request = _as_create_input(arguments)
        target = workspace.resolve_write(request.path, allow_missing=True)
        if target.existed:
            raise WriteValidationError(
                "exists", "create_file refuses to overwrite an existing file"
            )
        _validate_text_bytes(request.content)
        return (target.relative_path,)

    def assess(self, arguments: BaseModel) -> RiskAssessment:
        request = _as_create_input(arguments)
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(request.path,),
            hazards=self._risk_hazards((request.path,)),
            summary="create one new Workspace file without overwriting",
        )

    async def execute(self, workspace: Workspace, arguments: BaseModel) -> ToolResult:
        request = _as_create_input(arguments)
        call_id = str(
            workspace.active_tool_call_id or getattr(arguments, "tool_call_id", "create-file")
        )
        operation = PatchOperation(operation="add", path=request.path, new_text=request.content)
        # ``create_parents`` is carried out by the same transaction helper; the
        # standalone operation remains strict when a caller opts out.
        return await _run_transaction(
            workspace,
            (operation,),
            call_id=call_id,
            tool_name=self.name,
            create_parents=request.create_parents,
        )


async def _run_transaction(
    workspace: Workspace,
    operations: Iterable[PatchOperation],
    *,
    call_id: str,
    tool_name: str,
    create_parents: bool = True,
) -> ToolResult:
    changes: tuple[_PreparedChange, ...] = ()
    checkpoint: PatchCheckpoint | None = None
    created_directories: tuple[Path, ...] = ()
    applied_paths: set[str] = set()
    expected_hashes: dict[str, str | None] = {}
    try:
        operation_tuple = tuple(operations)
        workspace.update_tool_recovery(
            state="preparing",
            operation_count=len(operation_tuple),
            paths=[operation.path for operation in operation_tuple],
        )
        changes = _prepare_changes(
            workspace,
            ApplyPatchInput.model_construct(operations=operation_tuple),
            create_temporary=False,
            allow_missing_parents=create_parents,
        )
        expected_hashes = {
            change.target.relative_path: (
                _sha256(change.new_bytes) if change.new_bytes is not None else None
            )
            for change in changes
        }
        created_directories = _create_missing_parents(workspace, operation_tuple, create_parents)
        changes = _prepare_changes(
            workspace,
            ApplyPatchInput.model_construct(operations=operation_tuple),
            create_temporary=True,
            allow_missing_parents=True,
        )
        checkpoint = PatchCheckpoint.create(workspace, changes)
        workspace.update_tool_recovery(
            state="checkpointed",
            checkpoint_id=checkpoint.checkpoint_id,
            expected_hashes=expected_hashes,
        )
        _recheck_before_commit(workspace, changes)
        workspace.update_tool_recovery(state="committing")
        for change in changes:
            _commit_change(workspace, change)
            applied_paths.add(change.target.relative_path)
            workspace.update_tool_recovery(
                state="committing",
                applied_paths=sorted(applied_paths),
            )
            # Give cooperative cancellation a boundary between atomic file
            # replacements.  A cancellation is rolled back below.
            await asyncio.sleep(0)
        workspace.update_tool_recovery(
            state="committed",
            applied_paths=sorted(applied_paths),
            expected_hashes=expected_hashes,
        )
        return ToolResult.succeeded(
            ToolCall(tool_call_id=call_id, name=tool_name, arguments={}),
            _transaction_data(changes, checkpoint, rolled_back=False, evidence=()),
        )
    except asyncio.CancelledError:
        if checkpoint is None:
            _cleanup_temporary(changes)
            _remove_created_directories(created_directories)
            return ToolResult.failed(
                ToolCall(tool_call_id=call_id, name=tool_name, arguments={}),
                outcome=ToolOutcome.CANCELLED,
                category="cancellation",
                code="cancelled-before-commit",
                message="file transaction was cancelled before commit",
            )
        evidence = await checkpoint.rollback(
            workspace, applied_paths, created_directories, expected_hashes
        )
        _cleanup_temporary(changes)
        uncertain = any(item.get("action") == "rollback-failed" for item in evidence)
        return ToolResult.failed(
            ToolCall(tool_call_id=call_id, name=tool_name, arguments={}),
            outcome=ToolOutcome.INTERRUPTED if uncertain else ToolOutcome.CANCELLED,
            category="cancellation",
            code="rollback-failed" if uncertain else "cancelled",
            message=(
                "file transaction was interrupted and rollback could not be proven"
                if uncertain
                else "file transaction was cancelled and rolled back"
            ),
            data={
                "checkpoint_id": checkpoint.checkpoint_id,
                "rolled_back": not uncertain,
                "rollback_evidence": evidence,
            },
        )
    except (WriteValidationError, WorkspacePathError, BinaryTargetError, WorkspaceError) as exc:
        if checkpoint is not None and applied_paths:
            evidence = await checkpoint.rollback(
                workspace, applied_paths, created_directories, expected_hashes
            )
            _cleanup_temporary(changes)
            uncertain = any(item.get("action") == "rollback-failed" for item in evidence)
            return _tool_failure(
                call_id,
                tool_name,
                outcome=ToolOutcome.INTERRUPTED if uncertain else ToolOutcome.FAILED,
                category="tool-execution",
                code="rollback-failed" if uncertain else getattr(exc, "code", "commit-failed"),
                message=(
                    "file transaction failed and rollback could not be proven"
                    if uncertain
                    else "file transaction failed and was rolled back"
                ),
                data={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "rolled_back": not uncertain,
                    "rollback_evidence": evidence,
                },
            )
        _cleanup_temporary(changes)
        _remove_created_directories(created_directories)
        if isinstance(exc, WorkspacePathError) and exc.hard_denial:
            return _tool_failure(
                call_id,
                tool_name,
                outcome=ToolOutcome.DENIED,
                category="permission",
                code=exc.code,
                message=str(exc),
            )
        return _tool_failure(
            call_id,
            tool_name,
            outcome=ToolOutcome.FAILED,
            category="tool-validation"
            if isinstance(exc, WriteValidationError)
            else "tool-execution",
            code=getattr(exc, "code", "write-failed"),
            message=str(exc),
        )
    except Exception:
        if checkpoint is None:
            _cleanup_temporary(changes)
            _remove_created_directories(created_directories)
            return _tool_failure(
                call_id,
                tool_name,
                outcome=ToolOutcome.FAILED,
                category="tool-execution",
                code="prepare-failed",
                message="file transaction could not be prepared",
            )
        evidence = await checkpoint.rollback(
            workspace, applied_paths, created_directories, expected_hashes
        )
        _cleanup_temporary(changes)
        uncertain = any(item.get("action") == "rollback-failed" for item in evidence)
        return _tool_failure(
            call_id,
            tool_name,
            outcome=ToolOutcome.INTERRUPTED if uncertain else ToolOutcome.FAILED,
            category="tool-execution",
            code="rollback-failed" if uncertain else "commit-failed",
            message=(
                "file transaction failed and rollback could not be proven"
                if uncertain
                else "file transaction failed and was rolled back"
            ),
            data={
                "checkpoint_id": checkpoint.checkpoint_id,
                "rolled_back": not uncertain,
                "rollback_evidence": evidence,
            },
        )
    finally:
        _cleanup_temporary(changes)


def _prepare_changes(
    workspace: Workspace,
    request: ApplyPatchInput,
    *,
    create_temporary: bool,
    allow_missing_parents: bool,
) -> tuple[_PreparedChange, ...]:
    changes: list[_PreparedChange] = []
    normalized_paths: set[str] = set()
    for operation in request.operations:
        target = workspace.resolve_write(operation.path, allow_missing=True)
        if target.relative_path in normalized_paths:
            raise WriteValidationError(
                "duplicate-path", "a patch cannot contain the same target twice"
            )
        normalized_paths.add(target.relative_path)
        if (
            operation.operation == "add"
            and not target.parent.exists()
            and not allow_missing_parents
        ):
            raise WriteValidationError("parent-missing", "parent directory does not exist")
        original_bytes: bytes | None = None
        original_hash: str | None = None
        file_id: tuple[int, int] | None = None
        if target.existed:
            try:
                original_bytes = target.path.read_bytes()
            except OSError as exc:
                raise WorkspaceError("could not read Workspace target") from exc
            _validate_existing_text(original_bytes)
            original_hash = _sha256(original_bytes)
            file_id = _file_id(target.path)
        if operation.operation == "add":
            if target.existed:
                raise WriteValidationError("exists", "Add refuses to overwrite an existing file")
            new_bytes = _validate_text_bytes(operation.new_text or "")
        elif operation.operation == "update":
            if not target.existed or original_bytes is None:
                raise WriteValidationError("missing", "Update target does not exist")
            if operation.expected_sha256 is not None and operation.expected_sha256 != original_hash:
                raise WriteValidationError(
                    "context-mismatch", "Update target hash no longer matches"
                )
            current_text = _decode_text(original_bytes)
            old_text = operation.old_text or ""
            occurrences = current_text.count(old_text)
            if occurrences == 0:
                raise WriteValidationError(
                    "context-mismatch", "Update context does not match exactly"
                )
            if occurrences > 1:
                raise WriteValidationError(
                    "ambiguous-context", "Update context matches more than once"
                )
            new_bytes = _validate_text_bytes(
                current_text.replace(old_text, operation.new_text or "", 1)
            )
        else:
            if not target.existed or original_bytes is None:
                raise WriteValidationError("missing", "Delete target does not exist")
            if operation.expected_sha256 is not None and operation.expected_sha256 != original_hash:
                raise WriteValidationError(
                    "context-mismatch", "Delete target hash no longer matches"
                )
            if (
                operation.old_text is not None
                and _decode_text(original_bytes) != operation.old_text
            ):
                raise WriteValidationError(
                    "context-mismatch", "Delete target does not match exactly"
                )
            new_bytes = None
        temporary = None
        if create_temporary and new_bytes is not None:
            temporary = _prepare_temporary(target.path.parent, new_bytes)
        changes.append(
            _PreparedChange(
                operation=operation.operation,
                target=target,
                original_exists=target.existed,
                original_bytes=original_bytes,
                original_hash=original_hash,
                original_file_id=file_id,
                new_bytes=new_bytes,
                temporary=temporary,
            )
        )
    return tuple(changes)


def _create_missing_parents(
    workspace: Workspace,
    operations: tuple[PatchOperation, ...],
    create_parents: bool,
) -> tuple[Path, ...]:
    created: list[Path] = []
    if not create_parents:
        for operation in operations:
            target = workspace.resolve_write(operation.path, allow_missing=True)
            if not target.parent.exists():
                raise WriteValidationError("parent-missing", "parent directory does not exist")
        return ()
    try:
        for operation in operations:
            if operation.operation != "add":
                continue
            target = workspace.resolve_write(operation.path, allow_missing=True)
            missing: list[Path] = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                if parent == workspace.root:
                    break
                parent = parent.parent
            for directory in reversed(missing):
                try:
                    directory.mkdir()
                except FileExistsError:
                    if not directory.is_dir():
                        raise WorkspacePathError("not-directory")
                if directory != workspace.root:
                    workspace.resolve_read(
                        directory.relative_to(workspace.root).as_posix(), directory=True
                    )
                if directory not in created:
                    created.append(directory)
    except BaseException:
        _remove_created_directories(tuple(created))
        raise
    return tuple(created)


def _recheck_before_commit(workspace: Workspace, changes: tuple[_PreparedChange, ...]) -> None:
    for change in changes:
        current = workspace.recheck_write(change.target)
        if current.existed != change.original_exists:
            raise WorkspacePathError("path-race")
        if change.original_exists:
            if change.original_hash is None or change.original_file_id is None:
                raise WriteValidationError("path-race", "write target identity is unavailable")
            if _sha256(current.path.read_bytes()) != change.original_hash:
                raise WorkspacePathError("path-race")
            if _file_id(current.path) != change.original_file_id:
                raise WorkspacePathError("path-race")


def _commit_change(workspace: Workspace, change: _PreparedChange) -> None:
    workspace.recheck_write(change.target)
    if change.operation == "delete":
        change.target.path.unlink()
        return
    if change.temporary is None:
        raise WriteValidationError("temporary", "prepared file content is missing")
    if change.original_exists:
        os.replace(change.temporary, change.target.path)
    else:
        try:
            os.link(change.temporary, change.target.path)
        except (FileExistsError, OSError) as exc:
            if change.target.path.exists():
                raise WorkspacePathError("path-race") from exc
            # Some Windows filesystems do not expose hard links for all
            # temporary locations.  O_EXCL keeps creation no-overwrite even in
            # that fallback; it is only used for new files.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(change.target.path, flags)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(change.new_bytes or b"")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor != -1:
                    os.close(descriptor)
        try:
            change.temporary.unlink(missing_ok=True)
        except OSError:
            # The target is already installed; a leftover host temp file is
            # safer than reporting an uncommitted file that cannot be rolled back.
            pass


def _transaction_data(
    changes: tuple[_PreparedChange, ...],
    checkpoint: PatchCheckpoint,
    *,
    rolled_back: bool,
    evidence: tuple[dict[str, str], ...],
) -> dict[str, Any]:
    files = []
    for change in changes:
        old_text = _decode_text(change.original_bytes or b"") if change.original_exists else ""
        new_text = _decode_text(change.new_bytes or b"") if change.new_bytes is not None else ""
        diff = _review_diff(change.target.relative_path, old_text, new_text)
        files.append(
            {
                "path": change.target.relative_path,
                "operation": change.operation,
                "added_lines": len(new_text.splitlines()) if change.operation != "delete" else 0,
                "removed_lines": len(old_text.splitlines()) if change.operation != "add" else 0,
                "old_sha256": change.original_hash,
                "new_sha256": _sha256(change.new_bytes) if change.new_bytes is not None else None,
                "diff": diff,
            }
        )
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "changed_files": [item["path"] for item in files],
        "files": files,
        "rolled_back": rolled_back,
        "rollback_evidence": evidence,
    }


def _review_diff(relative_path: str, old_text: str, new_text: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) <= MAX_RESULT_BYTES:
        return diff
    return (
        encoded[: MAX_RESULT_BYTES - 64].decode("utf-8", errors="ignore") + "\n[diff truncated]\n"
    )


def _prepare_temporary(parent: Path, data: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".mini-agent-patch-", dir=parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        if descriptor != -1:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _restore_file(target: WorkspaceWriteTarget, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _prepare_temporary(target.parent, data)
    try:
        os.replace(temporary, target.path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_existing_text(data: bytes) -> None:
    if b"\x00" in data:
        raise BinaryTargetError()
    _decode_text(data)


def _validate_text_bytes(text: str) -> bytes:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WriteValidationError("utf8", "file content must be valid UTF-8") from exc
    if b"\x00" in data:
        raise BinaryTargetError()
    if len(data) > MAX_PATCH_BYTES:
        raise WriteValidationError("size", "file content exceeds the 256 KiB limit")
    return data


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BinaryTargetError() from exc


def _file_id(path: Path) -> tuple[int, int]:
    information = path.stat()
    return (information.st_dev, information.st_ino)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_patch_input(arguments: BaseModel) -> ApplyPatchInput:
    return (
        arguments
        if isinstance(arguments, ApplyPatchInput)
        else ApplyPatchInput.model_validate(arguments)
    )


def _as_create_input(arguments: BaseModel) -> CreateFileInput:
    return (
        arguments
        if isinstance(arguments, CreateFileInput)
        else CreateFileInput.model_validate(arguments)
    )


def _tool_failure(
    call_id: str,
    tool_name: str,
    *,
    outcome: ToolOutcome,
    category: str,
    code: str,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> ToolResult:
    return ToolResult.failed(
        ToolCall(tool_call_id=call_id, name=tool_name, arguments={}),
        outcome=outcome,
        category=category,
        code=code,
        message=message,
        data=data,
    )


def _cleanup_temporary(changes: Iterable[_PreparedChange]) -> None:
    for change in changes:
        if change.temporary is not None:
            change.temporary.unlink(missing_ok=True)


def _remove_created_directories(directories: Iterable[Path]) -> None:
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        else:
            child.rmdir()
    path.rmdir()


def _ensure_no_link_target(workspace: Workspace, target: WorkspaceWriteTarget) -> None:
    workspace.recheck_write(target)


def _safe_exception_code(exc: BaseException) -> str:
    if isinstance(exc, WorkspacePathError):
        return exc.code
    if isinstance(exc, WriteValidationError):
        return exc.code
    return type(exc).__name__.lower()


# Short aliases keep the public Tool vocabulary easy to discover.
PatchInput = ApplyPatchInput
CreateFileRequest = CreateFileInput
Checkpoint = PatchCheckpoint
