"""Deterministic Permission Policy and focused confirmation boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mini_agent.configuration import PermissionMode
from mini_agent.tools.contracts import (
    NormalizedToolCall,
    PermissionDecision,
    PermissionRequest,
    SideEffectCategory,
)


class ConfirmationChoice(StrEnum):
    """The only decisions a confirmation interaction may return."""

    ALLOW_ONCE = "allow-once"
    ALLOW_FOR_SESSION = "allow-exact-for-session"
    DENY = "deny"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class PermissionPreview:
    """Focused, redacted information shown before a risky operation."""

    request: PermissionRequest
    tool: str
    operation: str
    resources: tuple[str, ...]
    reason: str
    side_effect: str
    hazards: tuple[str, ...]
    argument_hash: str


class UserInteraction(Protocol):
    """The narrow confirmation seam used by the host Permission Policy."""

    def confirm(self, preview: PermissionPreview) -> ConfirmationChoice | str | bool:
        """Return one focused confirmation choice."""


PermissionConfirmer = Callable[[PermissionPreview], ConfirmationChoice | str | bool]


@dataclass(frozen=True, slots=True)
class PermissionDecisionMetadata:
    """Redacted audit facts for the most recent decision."""

    scope: str
    matched_rule: str
    reason: str


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """Exact Session authorization identity for one normalized Tool Call."""

    tool: str
    resources: tuple[str, ...]
    argument_hash: str


class PermissionPolicyGate:
    """Implement suggest/auto-edit/full-auto for file and future Shell Tools."""

    def __init__(
        self,
        mode: PermissionMode | str = PermissionMode.SUGGEST,
        *,
        interaction: UserInteraction | PermissionConfirmer | None = None,
    ) -> None:
        self.mode = PermissionMode(mode)
        self.interaction = interaction
        self._session_grants: set[PermissionGrant] = set()
        self._session_id: str | None = None
        self._last_metadata = PermissionDecisionMetadata("none", "default", "")

    def set_mode(self, mode: PermissionMode | str) -> None:
        self.mode = PermissionMode(mode)

    @property
    def session_grants(self) -> frozenset[PermissionGrant]:
        return frozenset(self._session_grants)

    @property
    def last_metadata(self) -> PermissionDecisionMetadata:
        return self._last_metadata

    def clear_session_grants(self) -> None:
        self._session_grants.clear()

    def begin_session(self, session_id: str) -> None:
        """Discard temporary grants when the active Session changes."""

        if getattr(self, "_session_id", None) != session_id:
            self._session_grants.clear()
            self._session_id = session_id

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        key = self._grant_key(request)
        hazards = set(request.risk.hazards)
        if hazards.intersection({"sensitive", "boundary-escape", "catastrophic", "hard-deny"}):
            return self._record(
                PermissionDecision.DENY,
                scope="none",
                rule="hard-deny",
                reason="the requested resource is a host-hard-denied hazard",
            )
        if key in self._session_grants and not hazards.intersection({"delete", "protected-path"}):
            return self._record(
                PermissionDecision.ALLOW,
                scope="session",
                rule="exact-session-grant",
                reason=(
                    "exact Tool, normalized resources, and argument hash match the Session grant"
                ),
            )

        automatic, rule, reason = self._mode_default(request)
        if automatic:
            return self._record(PermissionDecision.ALLOW, scope="none", rule=rule, reason=reason)
        return self._confirm(request, key, rule=rule, reason=reason)

    def _mode_default(self, request: PermissionRequest) -> tuple[bool, str, str]:
        if request.risk.side_effect is SideEffectCategory.READ:
            return True, "safe-read", "safe Workspace read automatically authorized"
        if request.risk.side_effect is SideEffectCategory.WRITE:
            hazards = set(request.risk.hazards)
            if self.mode in {
                PermissionMode.AUTO_EDIT,
                PermissionMode.FULL_AUTO,
            } and not hazards.intersection({"delete", "protected-path"}):
                return (
                    True,
                    "auto-edit-write",
                    "ordinary Add or Update allowed by Permission Policy",
                )
            if "delete" in hazards:
                return False, "delete-confirmation", "deletion always requires focused confirmation"
            if "protected-path" in hazards:
                return (
                    False,
                    "protected-path-confirmation",
                    "Protected Path writes always require confirmation",
                )
            return (
                False,
                "suggest-write-confirmation",
                "Suggest mode requires confirmation for writes",
            )
        if self.mode is PermissionMode.FULL_AUTO:
            return (
                False,
                "shell-confirmation",
                "only recognized local Shell commands may be automatic",
            )
        return False, "side-effect-confirmation", "this side effect requires focused confirmation"

    def _confirm(
        self,
        request: PermissionRequest,
        key: PermissionGrant,
        *,
        rule: str,
        reason: str,
    ) -> PermissionDecision:
        preview = PermissionPreview(
            request=request,
            tool=request.call.name,
            operation=request.risk.summary,
            resources=request.risk.resources,
            reason=reason,
            side_effect=request.risk.side_effect.value,
            hazards=request.risk.hazards,
            argument_hash=_request_argument_hash(request),
        )
        if self.interaction is None:
            return self._record(
                PermissionDecision.DENY,
                scope="none",
                rule=rule,
                reason="confirmation was required but no User Interaction was available",
            )
        try:
            confirmer = getattr(self.interaction, "confirm", None)
            response = confirmer(preview) if callable(confirmer) else self.interaction(preview)  # type: ignore[operator]
        except Exception:
            return self._record(
                PermissionDecision.DENY,
                scope="none",
                rule=rule,
                reason="confirmation interaction failed closed",
            )
        choice = _normalize_choice(response)
        if choice is ConfirmationChoice.ALLOW_FOR_SESSION:
            self._session_grants.add(key)
            return self._record(
                PermissionDecision.ALLOW,
                scope="session",
                rule="user-exact-session-grant",
                reason="user allowed this exact Tool Call for the Session",
            )
        if choice is ConfirmationChoice.ALLOW_ONCE:
            return self._record(
                PermissionDecision.ALLOW,
                scope="once",
                rule="user-allow-once",
                reason="user allowed this exact Tool Call once",
            )
        return self._record(
            PermissionDecision.DENY,
            scope="none",
            rule="user-deny",
            reason="user denied the focused confirmation",
        )

    def _grant_key(self, request: PermissionRequest) -> PermissionGrant:
        argument_hash = (
            request.call.argument_hash
            if isinstance(request.call, NormalizedToolCall)
            else NormalizedToolCall.from_call(request.call).argument_hash
        )
        return PermissionGrant(
            tool=request.call.name,
            resources=tuple(request.risk.resources),
            argument_hash=argument_hash,
        )

    def _record(
        self,
        decision: PermissionDecision,
        *,
        scope: str,
        rule: str,
        reason: str,
    ) -> PermissionDecision:
        self._last_metadata = PermissionDecisionMetadata(scope, rule, reason)
        return decision


def _normalize_choice(
    value: ConfirmationChoice | str | bool | PermissionDecision,
) -> ConfirmationChoice:
    if isinstance(value, bool):
        return ConfirmationChoice.ALLOW_ONCE if value else ConfirmationChoice.DENY
    if isinstance(value, PermissionDecision):
        return (
            ConfirmationChoice.ALLOW_ONCE
            if value is PermissionDecision.ALLOW
            else ConfirmationChoice.DENY
        )
    if isinstance(value, ConfirmationChoice):
        return value
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in {"allow", "allow-once", "yes", "y"}:
        return ConfirmationChoice.ALLOW_ONCE
    if normalized in {
        "allow-for-session",
        "allow-exact-for-session",
        "session",
        "allow-session",
    }:
        return ConfirmationChoice.ALLOW_FOR_SESSION
    return ConfirmationChoice.DENY


def _request_argument_hash(request: PermissionRequest) -> str:
    if isinstance(request.call, NormalizedToolCall):
        return request.call.argument_hash
    return NormalizedToolCall.from_call(request.call).argument_hash
