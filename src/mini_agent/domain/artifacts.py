"""Immutable, session-local Artifact references."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ARTIFACT_PREVIEW_BYTES = 4 * 1024
MAX_ARTIFACT_REREAD_BYTES = 64 * 1024
ARTIFACT_MEDIA_TYPE = "application/json"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """The model-facing identity and integrity metadata for one Artifact."""

    artifact_id: str
    path: str
    media_type: str
    byte_count: int
    sha256: str
    preview: str
    truncated: bool

    def __post_init__(self) -> None:
        if not is_safe_artifact_component(self.artifact_id):
            raise ValueError("Artifact ID must be one safe path component")
        if not self.path.startswith("artifacts/") or not _safe_relative_path(self.path):
            raise ValueError("Artifact path must be a safe Session-relative artifacts path")
        if not self.media_type.strip():
            raise ValueError("Artifact media type cannot be blank")
        if isinstance(self.byte_count, bool) or self.byte_count < 0:
            raise ValueError("Artifact byte count must be non-negative")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("Artifact SHA-256 must be lowercase hexadecimal")

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON shape persisted in events and Tool Results."""

        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "preview": self.preview,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ArtifactReference:
        """Validate a persisted reference without trusting model-controlled paths."""

        artifact_id = value.get("artifact_id")
        path = value.get("path")
        media_type = value.get("media_type")
        byte_count = value.get("byte_count")
        sha256 = value.get("sha256")
        preview = value.get("preview")
        truncated = value.get("truncated")
        if not isinstance(artifact_id, str):
            raise ValueError("Artifact reference requires artifact_id")
        if not isinstance(path, str):
            raise ValueError("Artifact reference requires path")
        if not isinstance(media_type, str):
            raise ValueError("Artifact reference requires media_type")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ValueError("Artifact reference requires integer byte_count")
        if not isinstance(sha256, str):
            raise ValueError("Artifact reference requires sha256")
        if not isinstance(preview, str):
            raise ValueError("Artifact reference requires preview")
        if not isinstance(truncated, bool):
            raise ValueError("Artifact reference requires boolean truncated")
        return cls(artifact_id, path, media_type, byte_count, sha256, preview, truncated)


def _safe_component(value: str) -> bool:
    return (
        bool(value.strip()) and value not in {".", ".."} and "/" not in value and "\\" not in value
    )


def _safe_relative_path(value: str) -> bool:
    parts = value.replace("\\", "/").split("/")
    return (
        len(parts) == 2
        and parts[0] == "artifacts"
        and is_safe_artifact_component(parts[1].rsplit(".", 1)[0])
        and parts[1].endswith(".artifact")
    )


def is_safe_artifact_component(value: str) -> bool:
    """Return whether a model-visible Artifact identity is one safe component."""

    return (
        bool(value.strip()) and value not in {".", ".."} and "/" not in value and "\\" not in value
    )
