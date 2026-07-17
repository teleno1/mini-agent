"""Public acceptance seams shared by the conversational remediation slices.

The helper intentionally composes the production CLI, Fake Provider, and real
JSONL Session Store.  Tests consume observations through those public seams:
rendered output, captured Context Frames, durable events, and persisted
Context Manifests.  It is not a second application harness and does not reach
through private methods.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from mini_agent.adapters.session_store import SessionSnapshot, SessionStore
from mini_agent.application.ports import IDGenerator
from mini_agent.cli.app import create_app
from mini_agent.configuration import EffectiveConfiguration
from mini_agent.context import ContextFrame
from mini_agent.domain.sessions import JSONValue, SessionEvent
from mini_agent.domain.streams import StreamEvent
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.tools.contracts import ToolDefinition

SUPERSEDED_REMEDIATION_CONTRACTS = frozenset(
    {
        "automatic-plan-creation",
        "word-based-permission-confirmation",
        "flat-transcript-formatting",
        "raw-lifecycle-events-as-user-messages",
    }
)
"""Legacy expectations that later remediation slices must not preserve."""


@dataclass(frozen=True, slots=True)
class FakeCliJourney:
    """Observable result of one Fake Provider-driven CLI request."""

    output: str
    exit_code: int
    provider: ScriptedFakeModelProvider
    snapshot: SessionSnapshot

    @property
    def session_id(self) -> str:
        return self.snapshot.session_id

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        """Authoritative durable Session Events for the journey."""

        return self.snapshot.events

    @property
    def context_frames(self) -> tuple[ContextFrame, ...]:
        """Provider-bound Context Frames captured by the Fake Provider."""

        return tuple(
            request for request in self.provider.requests if isinstance(request, ContextFrame)
        )

    @property
    def manifests(self) -> tuple[dict[str, JSONValue], ...]:
        """Persisted non-secret Context Manifest records."""

        if self.snapshot.projection is None:
            return ()
        return self.snapshot.projection.context_manifests


def run_fake_cli_journey(
    workspace: Path,
    task: str,
    *,
    chunks: Sequence[str] = ("Mini Agent is a small, ", "inspectable coding agent."),
    responses: Sequence[Sequence[StreamEvent]] | None = None,
    cli_args: Sequence[str] = (),
    interactive: bool = False,
    input_text: str | None = None,
) -> FakeCliJourney:
    """Run one offline CLI journey and return only public acceptance evidence.

    ``interactive=True`` drives the same production interactive Session loop
    used by a terminal.  The terminal capability check is patched only at the
    adapter boundary because ``CliRunner`` itself is intentionally non-TTY.
    """

    providers: list[ScriptedFakeModelProvider] = []

    def factory(
        configuration: EffectiveConfiguration,
        tool_definitions: Sequence[ToolDefinition],
        id_generator: IDGenerator,
    ) -> ScriptedFakeModelProvider:
        del configuration, tool_definitions, id_generator
        provider = (
            ScriptedFakeModelProvider(responses=responses)
            if responses is not None
            else ScriptedFakeModelProvider(chunks=chunks)
        )
        providers.append(provider)
        return provider

    arguments = ["--workspace", str(workspace), *cli_args]
    if not interactive:
        arguments.append(task)
    prompt_input = input_text
    if interactive and prompt_input is None:
        prompt_input = f"{task}\n/exit\n"
    with patch("mini_agent.cli.app._is_terminal_input", return_value=interactive):
        result = CliRunner().invoke(create_app(factory), arguments, input=prompt_input)
    sessions = SessionStore(workspace).list_sessions()
    if len(sessions) != 1:
        raise AssertionError(f"expected one durable Session, found {len(sessions)}")
    active_providers = [provider for provider in providers if provider.requests]
    if len(active_providers) != 1:
        raise AssertionError(
            f"expected one active Fake Provider, found {len(active_providers)} "
            f"from {len(providers)} compositions"
        )
    return FakeCliJourney(
        output=result.stdout,
        exit_code=result.exit_code,
        provider=active_providers[0],
        snapshot=SessionStore(workspace).read(sessions[0].session_id),
    )


__all__ = [
    "FakeCliJourney",
    "SUPERSEDED_REMEDIATION_CONTRACTS",
    "run_fake_cli_journey",
]
