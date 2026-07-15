"""Atomic, integrity-checked storage for immutable Session Artifacts."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from mini_agent.domain.artifacts import (
    ARTIFACT_PREVIEW_BYTES,
    ArtifactReference,
    is_safe_artifact_component,
)

if TYPE_CHECKING:
    from mini_agent.application.ports import IDGenerator


class ArtifactError(RuntimeError):
    """Base class for Artifact storage and integrity failures."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when a known Artifact identity is not available."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when an Artifact no longer matches its durable digest."""


class ArtifactPersistenceError(ArtifactError):
    """Raised when an Artifact cannot be durably written or verified."""


class ArtifactStore:
    """Store bytes below one Session directory without allowing replacement."""

    def __init__(self, session_directory: Path, *, id_generator: IDGenerator | None = None) -> None:
        self.session_directory = Path(session_directory)
        self.artifacts_directory = self.session_directory / "artifacts"
        self._id_generator = id_generator

    def write(
        self,
        content: bytes,
        *,
        media_type: str,
        artifact_id: str | None = None,
        preview_bytes: int = ARTIFACT_PREVIEW_BYTES,
    ) -> ArtifactReference:
        """Atomically write, replace-protect, and verify one Artifact."""

        if not isinstance(content, bytes):
            raise TypeError("Artifact content must be bytes")
        if preview_bytes < 0:
            raise ValueError("Artifact preview size cannot be negative")
        resolved_id = artifact_id or self._new_id()
        if not is_safe_artifact_component(resolved_id):
            raise ValueError("Artifact ID must be one safe path component")
        self.artifacts_directory.mkdir(parents=True, exist_ok=True)
        target = self.artifacts_directory / f"{resolved_id}.artifact"
        if target.exists():
            raise ArtifactPersistenceError(f"Artifact already exists: {resolved_id}")

        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{resolved_id}.", suffix=".tmp", dir=self.artifacts_directory
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                raise ArtifactPersistenceError(f"Artifact already exists: {resolved_id}")
            os.replace(temporary, target)
            temporary = None
            _fsync_directory(self.artifacts_directory)
            os.chmod(target, stat.S_IREAD)
            verified = target.read_bytes()
        except (OSError, ArtifactPersistenceError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if isinstance(exc, ArtifactPersistenceError):
                raise
            raise ArtifactPersistenceError(f"could not durably write Artifact: {exc}") from exc

        digest = hashlib.sha256(content).hexdigest()
        if verified != content or hashlib.sha256(verified).hexdigest() != digest:
            raise ArtifactIntegrityError(f"Artifact verification failed: {resolved_id}")
        preview = content[:preview_bytes].decode("utf-8", errors="replace")
        return ArtifactReference(
            artifact_id=resolved_id,
            path=f"artifacts/{target.name}",
            media_type=media_type,
            byte_count=len(content),
            sha256=digest,
            preview=preview,
            truncated=len(content) > preview_bytes,
        )

    def read(
        self,
        reference: ArtifactReference,
        *,
        start_byte: int,
        max_bytes: int,
    ) -> tuple[bytes, bool]:
        """Verify the whole Artifact, then return one bounded byte range."""

        if start_byte < 0 or max_bytes < 1:
            raise ValueError("Artifact range must be non-negative and non-empty")
        if not is_safe_artifact_component(reference.artifact_id):
            raise ArtifactNotFoundError("Artifact identity is invalid")
        target = self.session_directory / reference.path
        if (
            not _is_within(target, self.session_directory)
            or target.name != f"{reference.artifact_id}.artifact"
        ):
            raise ArtifactNotFoundError("Artifact identity is invalid")
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ArtifactNotFoundError("Artifact is not available") from exc
        if (
            len(content) != reference.byte_count
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise ArtifactIntegrityError(
                f"Artifact integrity check failed: {reference.artifact_id}"
            )
        end = min(len(content), start_byte + max_bytes)
        return content[start_byte:end], end < len(content)

    def list_orphans(self, committed_ids: set[str]) -> tuple[Path, ...]:
        """Find immutable files with no committed terminal Tool reference."""

        if not self.artifacts_directory.exists():
            return ()
        return tuple(
            sorted(
                (
                    path
                    for path in self.artifacts_directory.glob("*.artifact")
                    if path.stem not in committed_ids
                ),
                key=lambda path: path.name,
            )
        )

    def _new_id(self) -> str:
        if self._id_generator is not None:
            return self._id_generator.new_id("artifact")
        return f"artifact-{uuid4()}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
