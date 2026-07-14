"""Minimal conversational CLI for the offline text-only MVP."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console

from mini_agent import __version__
from mini_agent.adapters.clocks import DeterministicClock
from mini_agent.adapters.ids import DeterministicIdGenerator
from mini_agent.application.turns import TextTurnApplication
from mini_agent.domain.streams import TextDelta
from mini_agent.providers.fake import ScriptedFakeModelProvider

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="An independent, educational terminal coding agent.",
)
console = Console(markup=False, highlight=False, color_system=None)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mini-agent {__version__}")
        raise typer.Exit()


async def _run_fake_turn(task: str) -> None:
    application = TextTurnApplication(
        provider=ScriptedFakeModelProvider(),
        clock=DeterministicClock(datetime(2026, 1, 1, tzinfo=UTC)),
        id_generator=DeterministicIdGenerator(),
    )

    console.print(f"You: {task}", markup=False, highlight=False)
    console.print("Agent: ", end="", markup=False, highlight=False)

    async def render(event: object) -> None:
        if isinstance(event, TextDelta):
            console.print(event.text, end="", markup=False, highlight=False)

    await application.run(task, on_event=render)
    console.print()


@app.callback(invoke_without_command=True)
def _callback(
    task: Annotated[
        str | None, typer.Argument(help="The task to send to the offline Fake Provider.")
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed Mini Agent version.",
        ),
    ] = None,
) -> None:
    if task is None:
        task = typer.prompt("You")
    asyncio.run(_run_fake_turn(task))


def main() -> None:
    """Console-script entry point."""

    app()
