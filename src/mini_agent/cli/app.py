"""Minimal conversational CLI for the offline text-only MVP."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console
from typer import core as typer_core
from typer._click.exceptions import UsageError

from mini_agent import __version__
from mini_agent.adapters.clocks import SystemClock
from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.adapters.session_store import SessionStore
from mini_agent.application.cancellation import ForcedInterrupt, InterruptController
from mini_agent.application.ports import ContextBuilder as ContextBuilderPort
from mini_agent.application.rendering import BoundedStreamRenderer
from mini_agent.application.turns import TextTurnApplication
from mini_agent.configuration import (
    ConfigurationError,
    ConfigurationResolver,
    initialize_project,
)
from mini_agent.context import ContextBuilder
from mini_agent.diagnostics import DiagnosticLogger
from mini_agent.providers.fake import ScriptedFakeModelProvider
from mini_agent.providers.openai_compatible import OpenAICompatibleModelProvider


class _DefaultTaskGroup(typer_core.TyperGroup):
    """Treat an unknown first token as the legacy default task command."""

    def resolve_command(self, ctx: Any, args: list[str]) -> tuple[str | None, Any, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except UsageError:
            if args and not args[0].startswith("-"):
                command = self.get_command(ctx, "run")
                if command is not None:
                    return "run", command, args
            raise


app = typer.Typer(
    cls=_DefaultTaskGroup,
    add_completion=False,
    no_args_is_help=False,
    context_settings={"allow_extra_args": True},
    help="An independent, educational terminal coding agent.",
)
config_app = typer.Typer(add_completion=False, help="Inspect effective configuration.")
app.add_typer(config_app, name="config")
console = Console(markup=False, highlight=False, color_system=None)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mini-agent {__version__}")
        raise typer.Exit()


async def _run_turn(
    task: str,
    workspace: Path,
    store: SessionStore,
    session_id: str | None = None,
    *,
    cli_values: dict[str, object] | None = None,
) -> None:
    ids = UUIDIdGenerator()
    resolver = ConfigurationResolver(workspace, cli_values=cli_values)
    diagnostics = DiagnosticLogger(workspace, id_generator=ids)
    try:
        configuration = resolver.resolve()
    except ConfigurationError as exc:
        failure = diagnostics.record_exception(exc)
        typer.echo(
            f"Configuration failed [{failure.error_id or 'unavailable'}]: "
            "correct the configuration source and retry.",
            err=True,
        )
        raise typer.Exit(code=2) from exc
    provider: OpenAICompatibleModelProvider | ScriptedFakeModelProvider
    if configuration.api_key:
        provider = OpenAICompatibleModelProvider.from_configuration(configuration)
        context_builder: ContextBuilderPort | None = cast(
            ContextBuilderPort, ContextBuilder(str(workspace), configuration)
        )
    else:
        provider = ScriptedFakeModelProvider()
        context_builder = None
    application = TextTurnApplication(
        provider=provider,
        clock=SystemClock(),
        id_generator=ids,
        session_store=store,
        context_builder=context_builder,
        configuration=configuration,
        configuration_resolver=resolver,
        diagnostic_logger=diagnostics,
    )
    console.print(f"You: {task}", markup=False, highlight=False)
    console.print("Agent: ", end="", markup=False, highlight=False)
    renderer = BoundedStreamRenderer(
        rich_sink=lambda text: console.print(text, end="", markup=False, highlight=False),
        plain_sink=lambda text: typer.echo(text, nl=False),
        max_queue_size=64,
    )
    turn_task = asyncio.create_task(
        application.run(task, on_event=renderer.observe, session_id=session_id)
    )
    controller = InterruptController(
        turn_task,
        on_acknowledged=lambda: console.print(
            "\nCancelling; allowing cleanup...", markup=False, highlight=False
        ),
    )
    controller.install()
    cancellation_wait: asyncio.Task[bool] | None = None
    try:
        cancellation_wait = asyncio.create_task(controller.cancel_event.wait())
        done, _ = await asyncio.wait(
            {turn_task, cancellation_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancellation_wait in done and turn_task not in done:
            try:
                await asyncio.wait_for(
                    asyncio.shield(turn_task), timeout=controller.cleanup_seconds
                )
            except TimeoutError:
                turn_task.cancel()
                raise typer.Exit(code=ForcedInterrupt.exit_code if controller.forced else 1)
        else:
            await turn_task
        cancellation_wait.cancel()
        if controller.forced:
            raise typer.Exit(code=ForcedInterrupt.exit_code)
    except asyncio.CancelledError:
        if controller.forced:
            raise typer.Exit(code=ForcedInterrupt.exit_code)
        console.print("\nCancelled. The Session was not reported as successful.")
    except typer.Exit:
        raise
    except Exception:
        error_id = diagnostics.last_error_id or "unavailable"
        console.print(
            f"\nError [{error_id}]: the Turn failed; inspect it with "
            f"`mini-agent doctor {error_id}`.",
            markup=False,
            highlight=False,
        )
        raise typer.Exit(code=1)
    finally:
        if cancellation_wait is not None:
            cancellation_wait.cancel()
        if not turn_task.done():
            turn_task.cancel()
        controller.uninstall()
        await renderer.finish()
        console.print()
        if isinstance(provider, OpenAICompatibleModelProvider):
            await provider.aclose()
        if controller.forced:
            raise typer.Exit(code=ForcedInterrupt.exit_code)


def _store_for_current_workspace(workspace: Path | None = None) -> SessionStore:
    return SessionStore(workspace or Path.cwd())


@app.command("init")
def initialize(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm creation of project configuration and ignore rules."),
    ] = False,
) -> None:
    """Initialize safe project defaults without ever writing a credential."""

    confirmed = yes or typer.confirm(
        "Create .mini-agent/config.toml and update .gitignore?", default=False
    )
    if not confirmed:
        typer.echo("Initialization cancelled.")
        return
    try:
        config_path, ignore_path = initialize_project(Path.cwd(), confirmed=True)
    except (ConfigurationError, OSError) as exc:
        typer.echo(f"Initialization failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Created {config_path}")
    if ignore_path is not None:
        typer.echo(f"Updated {ignore_path}")


@config_app.command("show")
def show_config() -> None:
    """Show effective values and their winning, non-secret sources."""

    try:
        configuration = ConfigurationResolver(Path.cwd()).resolve()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(configuration.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))


@app.command("sessions")
def list_sessions() -> None:
    """List durable Sessions in the current Workspace."""

    sessions = _store_for_current_workspace().list_sessions()
    if not sessions:
        typer.echo("No Sessions.")
        return
    for session in sessions:
        preview = session.last_user_message or ""
        typer.echo(f"{session.session_id}\t{session.status}\t{preview}")


@app.command("doctor")
def doctor(
    context: typer.Context,
    error_id: Annotated[str, typer.Argument(help="The diagnostic error ID to resolve.")],
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace containing the diagnostic logs."),
    ] = None,
) -> None:
    """Show one redacted diagnostic record by error ID."""

    selected_workspace = workspace or context.obj.get("workspace", Path.cwd())
    record = DiagnosticLogger(Path(selected_workspace)).find(error_id)
    if record is None:
        typer.echo(f"No diagnostic record found for {error_id}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("resume")
def resume_session(
    session_id: Annotated[str, typer.Argument(help="The Session ID to resume.")],
    task: Annotated[str | None, typer.Argument(help="The next task for the Session.")] = None,
) -> None:
    """Resume a completed text-only Session from its event history."""

    if task is None:
        task = typer.prompt("You")
    asyncio.run(_run_turn(task, Path.cwd(), _store_for_current_workspace(), session_id))


@app.command("run", hidden=True)
def run_task(
    task: Annotated[
        list[str], typer.Argument(help="The task to send to the offline Fake Provider.")
    ],
) -> None:
    """Run the default conversational task command."""

    asyncio.run(_run_turn(" ".join(task), Path.cwd(), _store_for_current_workspace()))


@app.callback(invoke_without_command=True)
def _callback(
    context: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace root for the Session and instructions."),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Provider model override.")] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="OpenAI-compatible Provider Base URL override."),
    ] = None,
    permission_mode: Annotated[
        str | None,
        typer.Option("--permission-mode", help="Permission mode override."),
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
    context.ensure_object(dict)
    context.obj["workspace"] = (workspace or Path.cwd()).resolve()
    if context.invoked_subcommand is not None:
        return
    task = " ".join(context.args) if context.args else None
    if task is None:
        task = typer.prompt("You")
    root = Path(context.obj["workspace"])
    cli_values: dict[str, object] = {
        key: value
        for key, value in {
            "model": model,
            "provider_base_url": base_url,
            "permission_mode": permission_mode,
        }.items()
        if value is not None
    }
    asyncio.run(_run_turn(task, root, _store_for_current_workspace(root), cli_values=cli_values))


def main() -> None:
    """Console-script entry point."""

    app()
