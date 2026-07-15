"""Controlled model Tool access to Session-local immutable Artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from mini_agent.adapters.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from mini_agent.domain.artifacts import MAX_ARTIFACT_REREAD_BYTES
from mini_agent.tools.contracts import (
    MAX_TOOL_RESPONSE_BYTES,
    RiskAssessment,
    SideEffectCategory,
    ToolCall,
    ToolLimits,
    ToolOutcome,
    ToolResult,
)
from mini_agent.tools.workspace import Workspace

if TYPE_CHECKING:
    from mini_agent.adapters.session_store import SessionStore


class ArtifactReadInput(BaseModel):
    """An identity-based bounded range; a model never supplies a filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    artifact_id: str = Field(min_length=1)
    start_byte: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("start_byte", "offset"),
    )
    max_bytes: int = Field(
        default=8 * 1024,
        ge=1,
        le=MAX_ARTIFACT_REREAD_BYTES,
        validation_alias=AliasChoices("max_bytes", "length"),
    )


class ArtifactReadTool:
    """Read only a verified, bounded range of a known Session Artifact."""

    name = "read_artifact"
    description = (
        "Read a bounded byte range from a known immutable Tool Result Artifact by identity."
    )
    side_effect = SideEffectCategory.READ
    input_model: ClassVar[type[BaseModel]] = ArtifactReadInput
    limits = ToolLimits.bounded(
        timeout_seconds=30.0,
        max_output_bytes=MAX_ARTIFACT_REREAD_BYTES,
    )

    def __init__(self, session_store: SessionStore | None = None, *, session_id: str | None = None):
        self._session_store = session_store
        self._session_id = session_id

    def assess(self, arguments: ArtifactReadInput) -> RiskAssessment:
        return RiskAssessment(
            side_effect=self.side_effect,
            resources=(f"artifact:{arguments.artifact_id}",),
            hazards=(),
            summary="read a verified bounded Artifact range",
        )

    def preflight(self, workspace: Workspace, arguments: ArtifactReadInput) -> tuple[str, ...]:
        del workspace
        return (f"artifact:{arguments.artifact_id}",)

    async def execute(self, workspace: Workspace, arguments: ArtifactReadInput) -> ToolResult:
        call = _internal_call(self.name)
        session_id = self._session_id or _session_id_from_workspace(workspace)
        if session_id is None:
            return ToolResult.failed(
                call,
                category="tool-validation",
                code="session-required",
                message="Artifact rereads require an active Session",
            )
        store = self._session_store
        if store is None:
            from mini_agent.adapters.session_store import SessionStore

            store = SessionStore(workspace.root)
        try:
            reference, content, truncated = store.read_artifact(
                session_id,
                arguments.artifact_id,
                start_byte=arguments.start_byte,
                max_bytes=arguments.max_bytes,
            )
        except ArtifactNotFoundError:
            return ToolResult.failed(
                call,
                category="tool-validation",
                code="artifact-not-found",
                message="Artifact identity is not available in this Session",
            )
        except ArtifactIntegrityError:
            return ToolResult.failed(
                call,
                category="tool-execution",
                code="artifact-integrity-failed",
                message="Artifact integrity verification failed",
            )
        except ValueError:
            return ToolResult.failed(
                call,
                category="tool-validation",
                code="invalid-range",
                message="Artifact range is invalid",
            )
        return _bounded_result(
            call.tool_call_id,
            reference.as_dict(),
            start_byte=arguments.start_byte,
            content=content,
            truncated=truncated,
        )


def _internal_call(name: str) -> ToolCall:
    """Create the temporary correlation ID replaced by the Tool Registry."""

    return ToolCall(tool_call_id="artifact-reader", name=name, arguments={})


def _bounded_result(
    tool_call_id: str,
    reference: dict[str, object],
    *,
    start_byte: int,
    content: bytes,
    truncated: bool,
) -> ToolResult:
    """Keep the serialized reread result below the absolute Tool ceiling."""

    def create(candidate: bytes) -> ToolResult:
        candidate_truncated = truncated or len(candidate) < len(content)
        next_start_byte = start_byte + len(candidate) if candidate_truncated else None
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name="read_artifact",
            outcome=ToolOutcome.SUCCESS,
            data={
                "artifact": reference,
                "start_byte": start_byte,
                "byte_count": len(candidate),
                "content": candidate.decode("utf-8", errors="replace"),
                "truncated": candidate_truncated,
                "next_start_byte": next_start_byte,
            },
        )

    result = create(content)
    if len(result.text.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES:
        return result
    low = 0
    high = len(content)
    best = create(b"")
    while low <= high:
        middle = (low + high) // 2
        candidate = content[:middle].decode("utf-8", errors="ignore").encode("utf-8")
        candidate_result = create(candidate)
        if len(candidate_result.text.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES:
            best = candidate_result
            low = middle + 1
        else:
            high = middle - 1
    return best


def _session_id_from_workspace(workspace: Workspace) -> str | None:
    checkpoint_directory = workspace.checkpoint_directory
    if checkpoint_directory.name != "checkpoints":
        return None
    session_directory = checkpoint_directory.parent
    if session_directory.parent.name != "sessions":
        return None
    return session_directory.name
