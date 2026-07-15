"""Structured completion reports for a normal Agent Turn stop."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.domain.sessions import JSONValue


@dataclass(frozen=True, slots=True)
class CompletionReport:
    """The factual hand-off produced after a no-Tool normal model stop."""

    outcome: str
    verification: tuple[str, ...]
    changed_files: tuple[str, ...]
    unresolved_work: tuple[str, ...]
    next_action: str

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "outcome": self.outcome,
            "verification": list(self.verification),
            "changed_files": list(self.changed_files),
            "unresolved_work": list(self.unresolved_work),
            "next_action": self.next_action,
        }

    @property
    def text(self) -> str:
        """Render a stable concise report for CLI and tests."""

        def render(values: tuple[str, ...]) -> str:
            return "; ".join(values) if values else "none"

        return "\n".join(
            (
                f"Outcome: {self.outcome}",
                f"Verification: {render(self.verification)}",
                f"Changed files: {render(self.changed_files)}",
                f"Unresolved work: {render(self.unresolved_work)}",
                f"Next action: {self.next_action}",
            )
        )
