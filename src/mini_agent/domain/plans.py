"""Observable Plan snapshots maintained by the Agent Loop.

Plans are deliberately small, immutable, and factual.  They are not a place
to persist hidden reasoning: each ``plan.updated`` event contains the complete
current snapshot so a projection can rebuild the visible state without
replaying an in-memory workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.domain.sessions import JSONValue


class PlanStepStatus(StrEnum):
    """The only states a visible Plan step may have."""

    PENDING = "pending"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One user-visible, observable step in a Plan."""

    step_id: str
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    result_summary: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("Plan step ID cannot be blank")
        if not self.description.strip():
            raise ValueError("Plan step description cannot be blank")
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            raise ValueError("Plan step timestamp must be timezone-aware")

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status.value,
            "result_summary": self.result_summary,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, JSONValue]) -> PlanStep:
        step_id = value.get("step_id")
        description = value.get("description")
        status = value.get("status")
        result_summary = value.get("result_summary")
        updated_at = value.get("updated_at")
        if not isinstance(step_id, str) or not isinstance(description, str):
            raise ValueError("Plan steps require an ID and description")
        if not isinstance(status, str):
            raise ValueError("Plan steps require a status")
        try:
            parsed_status = PlanStepStatus(status)
        except ValueError as exc:
            raise ValueError("Plan step status is invalid") from exc
        if result_summary is not None and not isinstance(result_summary, str):
            raise ValueError("Plan step result_summary must be a string or null")
        parsed_updated_at: datetime | None = None
        if updated_at is not None:
            if not isinstance(updated_at, str):
                raise ValueError("Plan step updated_at must be an ISO timestamp or null")
            try:
                parsed_updated_at = datetime.fromisoformat(updated_at)
            except ValueError as exc:
                raise ValueError("Plan step updated_at is not a valid timestamp") from exc
        return cls(
            step_id=step_id,
            description=description,
            status=parsed_status,
            result_summary=result_summary,
            updated_at=parsed_updated_at,
        )


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """A complete immutable Plan snapshot suitable for a durable event."""

    plan_id: str
    objective: str
    steps: tuple[PlanStep, ...]
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("Plan ID cannot be blank")
        if not self.objective.strip():
            raise ValueError("Plan objective cannot be blank")
        if not self.steps:
            raise ValueError("Plan must contain at least one step")
        if self.updated_at.tzinfo is None:
            raise ValueError("Plan timestamp must be timezone-aware")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("Plan step IDs must be unique")
        if sum(step.status is PlanStepStatus.IN_PROGRESS for step in self.steps) > 1:
            raise ValueError("Plan may have at most one in-progress step")

    @property
    def in_progress_step(self) -> PlanStep | None:
        return next(
            (step for step in self.steps if step.status is PlanStepStatus.IN_PROGRESS),
            None,
        )

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "steps": [step.as_dict() for step in self.steps],
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, JSONValue]) -> PlanSnapshot:
        plan_id = value.get("plan_id")
        objective = value.get("objective")
        raw_steps = value.get("steps")
        updated_at = value.get("updated_at")
        if not isinstance(plan_id, str) or not isinstance(objective, str):
            raise ValueError("Plan requires an ID and objective")
        if not isinstance(raw_steps, list):
            raise ValueError("Plan steps must be a list")
        if not isinstance(updated_at, str):
            raise ValueError("Plan updated_at must be an ISO timestamp")
        try:
            parsed_updated_at = datetime.fromisoformat(updated_at)
            steps = tuple(PlanStep.from_dict(step) for step in raw_steps if isinstance(step, dict))
        except (TypeError, ValueError) as exc:
            raise ValueError("Plan snapshot is invalid") from exc
        if len(steps) != len(raw_steps):
            raise ValueError("Plan steps must be objects")
        return cls(plan_id, objective, steps, parsed_updated_at)
