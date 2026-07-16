"""The production conversational command line for Mini Agent."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from typer import Abort
from typer import core as typer_core
from typer._click.exceptions import UsageError

from mini_agent import __version__
from mini_agent.adapters.clocks import SystemClock
from mini_agent.adapters.ids import UUIDIdGenerator
from mini_agent.adapters.session_store import (
    ResumeChoice,
    SessionStore,
    SessionStoreError,
)
from mini_agent.application.agent import AgentTurnApplication, AgentTurnResult
from mini_agent.application.cancellation import ForcedInterrupt, InterruptController
from mini_agent.application.permissions import PermissionPolicyGate
from mini_agent.application.ports import IDGenerator, LifecycleObserver, ModelProvider
from mini_agent.cli.presentation import ConversationPresenter, TerminalPermissionInteraction
from mini_agent.configuration import (
    ConfigurationError,
    ConfigurationResolver,
    EffectiveConfiguration,
    SessionConfigurationError,
    SessionConfigurationService,
    SessionOverrideConfirmationRequired,
    initialize_project,
    redact_secrets,
)
from mini_agent.context import ContextBuilder
from mini_agent.diagnostics import DiagnosticLogger
from mini_agent.providers.composition import ProviderFactory, production_provider_factory
from mini_agent.tools import (
    ApplyPatchTool,
    ArtifactReadTool,
    CreateFileTool,
    ReadFileTool,
    SearchFilesTool,
    ShellTool,
    Tool,
    ToolRegistry,
    Workspace,
)


class _DefaultTaskGroup(typer_core.TyperGroup):
    """Treat an unknown first token as the default one-shot task command."""

    def resolve_command(self, ctx: Any, args: list[str]) -> tuple[str | None, Any, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except UsageError:
            if args and not args[0].startswith("-"):
                command = self.get_command(ctx, "run")
                if command is not None:
                    return "run", command, args
            raise


@dataclass(frozen=True, slots=True)
class TurnRunOutcome:
    """The CLI-level result, including failures that should return to a REPL."""

    session_id: str | None
    result: AgentTurnResult | None = None
    configuration_error: bool = False
    runtime_failure: bool = False
    forced_interrupt: bool = False


def _raise_for_outcome(outcome: TurnRunOutcome) -> None:
    """Map a Turn result to command exit semantics; success returns normally."""

    if outcome.forced_interrupt:
        raise typer.Exit(code=ForcedInterrupt.exit_code)
    if outcome.configuration_error:
        raise typer.Exit(code=2)
    if outcome.runtime_failure:
        raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mini-agent {__version__}")
        raise typer.Exit()


def _workspace_from_context(context: typer.Context, override: Path | None = None) -> Path:
    context.ensure_object(dict)
    value = override or context.obj.get("workspace", Path.cwd())
    return Path(value).expanduser().resolve()


def _cli_values_from_context(context: typer.Context) -> dict[str, object]:
    context.ensure_object(dict)
    raw = context.obj.get("cli_values", {})
    return dict(cast(dict[str, object], raw))


def _provider_factory_from_context(context: typer.Context) -> ProviderFactory:
    context.ensure_object(dict)
    factory = context.find_root().obj.get("provider_factory")
    if not callable(factory):
        return production_provider_factory
    return cast(ProviderFactory, factory)


def _is_terminal_input() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, OSError):
        return False


def _report_configuration_failure(
    workspace: Path,
    exc: ConfigurationError,
    *,
    session_id: str | None = None,
    id_generator: IDGenerator | None = None,
) -> None:
    diagnostics = DiagnosticLogger(
        workspace,
        id_generator=id_generator or UUIDIdGenerator(),
    )
    failure = diagnostics.record_exception(exc, session_id=session_id)
    message = redact_secrets(exc)
    typer.echo(
        f"Configuration failed [{failure.error_id or 'unavailable'}]: {message}",
        err=True,
    )


def _startup_provider_check(
    workspace: Path,
    store: SessionStore,
    *,
    cli_values: dict[str, object],
    provider_factory: ProviderFactory,
    session_id: str | None = None,
) -> bool:
    """Validate the Provider composition before entering an interactive Session."""

    provider: ModelProvider | None = None
    try:
        _application, provider, _resolver, _configuration, _diagnostics = _build_application(
            workspace,
            store,
            cli_values=cli_values,
            session_id=None,
            interaction=TerminalPermissionInteraction(
                ConversationPresenter(interactive=True),
                interactive=_is_terminal_input(),
            ),
            permission_gate=PermissionPolicyGate(),
            provider_factory=provider_factory,
        )
        return True
    except ConfigurationError as exc:
        _report_configuration_failure(
            workspace,
            exc,
            session_id=session_id,
        )
        return False
    finally:
        if provider is not None:
            asyncio.run(_close_provider(provider))


def _tool_registry(store: SessionStore) -> ToolRegistry:
    tools: tuple[Tool, ...] = cast(
        tuple[Tool, ...],
        (
            ReadFileTool(),
            SearchFilesTool(),
            ApplyPatchTool(),
            CreateFileTool(),
            ShellTool(),
            ArtifactReadTool(store),
        ),
    )
    return ToolRegistry(tools)


def _resolve_configuration(
    workspace: Path,
    *,
    cli_values: dict[str, object],
    store: SessionStore,
    session_id: str | None,
) -> tuple[ConfigurationResolver, EffectiveConfiguration]:
    resolver = ConfigurationResolver(workspace, cli_values=cli_values)
    overrides: dict[str, object] | None = None
    if session_id is not None:
        overrides = dict(store.resume(session_id).configuration_overrides)
    return resolver, resolver.resolve(session_overrides=overrides)


def _build_application(
    workspace: Path,
    store: SessionStore,
    *,
    cli_values: dict[str, object],
    session_id: str | None,
    interaction: TerminalPermissionInteraction,
    permission_gate: PermissionPolicyGate,
    provider_factory: ProviderFactory = production_provider_factory,
) -> tuple[
    AgentTurnApplication,
    ModelProvider,
    ConfigurationResolver,
    EffectiveConfiguration,
    DiagnosticLogger,
]:
    resolver, configuration = _resolve_configuration(
        workspace,
        cli_values=cli_values,
        store=store,
        session_id=session_id,
    )
    ids = UUIDIdGenerator()
    diagnostics = DiagnosticLogger(workspace, id_generator=ids)
    registry = _tool_registry(store)
    provider = provider_factory(configuration, registry.definitions(), ids)
    application = AgentTurnApplication(
        provider=provider,
        workspace=Workspace(workspace),
        tool_registry=registry,
        clock=SystemClock(),
        id_generator=ids,
        session_store=store,
        context_builder=ContextBuilder(str(workspace), configuration),
        configuration=configuration,
        configuration_resolver=resolver,
        permission_gate=permission_gate,
        user_interaction=interaction,
        diagnostic_logger=diagnostics,
    )
    return application, provider, resolver, configuration, diagnostics


async def _close_provider(
    provider: ModelProvider,
) -> None:
    close = getattr(provider, "aclose", None)
    if callable(close):
        result = close()
        if hasattr(result, "__await__"):
            await result
    return


async def _run_turn(
    task: str,
    workspace: Path,
    store: SessionStore,
    session_id: str | None = None,
    *,
    cli_values: dict[str, object] | None = None,
    permission_gate: PermissionPolicyGate | None = None,
    interactive: bool = False,
    user_already_rendered: bool = False,
    provider_factory: ProviderFactory = production_provider_factory,
) -> TurnRunOutcome:
    """Run one bounded Turn and return failures to the caller's conversation."""

    ids = UUIDIdGenerator()
    values = dict(cli_values or {})
    presenter = ConversationPresenter(interactive=interactive)
    gate = permission_gate or PermissionPolicyGate()
    interaction = TerminalPermissionInteraction(presenter, interactive=interactive)
    gate.interaction = interaction
    selected_session = session_id
    diagnostics: DiagnosticLogger | None = None
    provider: ModelProvider | None = None
    cancellation_wait: asyncio.Task[bool] | None = None
    turn_task: asyncio.Task[AgentTurnResult] | None = None

    try:
        try:
            application, provider, _resolver, _configuration, diagnostics = _build_application(
                workspace,
                store,
                cli_values=values,
                session_id=selected_session,
                interaction=interaction,
                permission_gate=gate,
                provider_factory=provider_factory,
            )
        except ConfigurationError as exc:
            _report_configuration_failure(
                workspace,
                exc,
                session_id=selected_session,
                id_generator=ids,
            )
            return TurnRunOutcome(selected_session, configuration_error=True)

        if selected_session is None:
            selected_session = ids.new_id("session")
            with store.create(selected_session):
                pass

        if not user_already_rendered:
            presenter.user(task)
        turn_task = asyncio.create_task(
            application.run(
                task,
                on_event=presenter.observe,
                session_id=selected_session,
                on_lifecycle=cast(LifecycleObserver, presenter.on_lifecycle),
            )
        )
        controller = InterruptController(
            turn_task,
            on_acknowledged=lambda: presenter.recovery(
                "Cancelling; allowing the active operation to clean up..."
            ),
        )
        controller.install()
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
                    await presenter.mark_incomplete()
                    return TurnRunOutcome(
                        selected_session,
                        forced_interrupt=True,
                    )
            else:
                result = await turn_task
                presenter.completion(result.completion_report)
                return TurnRunOutcome(selected_session, result=result)
            if controller.forced:
                await presenter.mark_incomplete()
                return TurnRunOutcome(selected_session, forced_interrupt=True)
            if turn_task.done():
                result = await turn_task
                presenter.completion(result.completion_report)
                return TurnRunOutcome(selected_session, result=result)
            return TurnRunOutcome(selected_session, runtime_failure=True)
        except asyncio.CancelledError:
            await presenter.mark_incomplete()
            if controller.forced:
                return TurnRunOutcome(selected_session, forced_interrupt=True)
            presenter.recovery(
                "Cancelled. The Session was preserved and was not reported as successful."
            )
            return TurnRunOutcome(selected_session, runtime_failure=True)
        except Exception:
            error_id = diagnostics.last_error_id if diagnostics is not None else None
            presenter.failure(error_id)
            return TurnRunOutcome(selected_session, runtime_failure=True)
        finally:
            if cancellation_wait is not None:
                cancellation_wait.cancel()
            if not turn_task.done():
                turn_task.cancel()
            controller.uninstall()
    except KeyboardInterrupt:
        await presenter.mark_incomplete()
        presenter.recovery("Interrupted. The Session remains available for Resume.")
        return TurnRunOutcome(selected_session, forced_interrupt=True)
    except SessionStoreError as exc:
        if diagnostics is None:
            diagnostics = DiagnosticLogger(workspace, id_generator=ids)
        failure = diagnostics.record_exception(exc, session_id=selected_session)
        presenter.failure(failure.error_id)
        return TurnRunOutcome(selected_session, runtime_failure=True)
    except Exception:
        error_id = diagnostics.last_error_id if diagnostics is not None else None
        presenter.failure(error_id)
        return TurnRunOutcome(selected_session, runtime_failure=True)
    finally:
        if cancellation_wait is not None:
            cancellation_wait.cancel()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        await presenter.finish()
        if provider is not None:
            await _close_provider(provider)


def _display_sessions(workspace: Path) -> None:
    sessions = SessionStore(workspace).list_sessions()
    if not sessions:
        typer.echo("No Sessions.")
        return
    for session in sessions:
        preview = (session.last_user_message or "").replace("\n", " ")
        typer.echo(f"{session.session_id}\t{session.status}\t{preview}")


def _safe_prompt(prompt: str) -> str | None:
    try:
        value = typer.prompt(prompt)
    except (Abort, EOFError, OSError):
        return None
    return value.strip() or None


def _resume_choice() -> ResumeChoice:
    while True:
        try:
            value = typer.prompt(
                "Recovery [inspect/abandon/retry/exit]",
                default=ResumeChoice.EXIT.value,
                show_default=False,
            )
        except (Abort, EOFError, OSError):
            return ResumeChoice.EXIT
        try:
            return ResumeChoice(value.strip().lower())
        except ValueError:
            typer.echo("Choose inspect, abandon, retry, or exit.", err=True)


def _print_resume_inspection(inspection: Any) -> None:
    if inspection.instruction_change:
        typer.echo("Instruction change detected; current AGENTS.md hashes are recorded on Resume.")
        typer.echo(
            json.dumps(
                {
                    "previous": inspection.previous_instruction_hashes,
                    "current": inspection.current_instruction_hashes,
                },
                ensure_ascii=False,
            )
        )
    if inspection.requires_recovery:
        typer.echo("Interrupted work found; no Tool Result has been assumed successful.")
        for item in inspection.interrupted_tools:
            typer.echo(json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True))


async def _retry_interrupted_cli(
    workspace: Path,
    store: SessionStore,
    session_id: str,
    *,
    cli_values: dict[str, object],
    gate: PermissionPolicyGate,
    provider_factory: ProviderFactory = production_provider_factory,
) -> bool:
    interactive = _is_terminal_input()
    presenter = ConversationPresenter(interactive=interactive)
    interaction = TerminalPermissionInteraction(presenter, interactive=interactive)
    gate.interaction = interaction
    provider: ModelProvider | None = None
    try:
        application, provider, _resolver, _configuration, diagnostics = _build_application(
            workspace,
            store,
            cli_values=cli_values,
            session_id=session_id,
            interaction=interaction,
            permission_gate=gate,
            provider_factory=provider_factory,
        )
        result = await application.retry_interrupted(session_id)
        presenter.recovery(
            "Recovery retry recorded as new Tool calls; no model completion was assumed."
        )
        for tool_result in result.tool_results:
            presenter.recovery(
                f"  {tool_result.tool_name}: {tool_result.outcome.value} - {tool_result.text}"
            )
        return True
    except Exception:
        error_id = locals().get("diagnostics", None)
        if isinstance(error_id, DiagnosticLogger):
            presenter.failure(error_id.last_error_id)
        else:
            presenter.failure(None)
        return False
    finally:
        if provider is not None:
            await _close_provider(provider)
        await presenter.finish()


def _canonical_config_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _config_value(key: str, value: str) -> object:
    if key in {
        "max_model_requests",
        "max_tool_calls",
        "max_active_seconds",
        "context_window_tokens",
        "response_reserve_tokens",
        "artifact_threshold_bytes",
        "instruction_file_bytes",
        "instruction_chain_bytes",
    }:
        return int(value, 10)
    return value


def _show_effective_config(
    workspace: Path,
    *,
    cli_values: Mapping[str, object] | None = None,
    store: SessionStore | None = None,
    session_id: str | None = None,
) -> object:
    resolver = ConfigurationResolver(workspace, cli_values=cli_values)
    overrides = None
    if store is not None and session_id is not None:
        overrides = store.resume(session_id).configuration_overrides
    configuration = resolver.resolve(session_overrides=overrides)
    typer.echo(json.dumps(configuration.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return configuration


def _interactive_config(
    arguments: list[str],
    workspace: Path,
    store: SessionStore,
    session_id: str | None,
    cli_values: dict[str, object],
) -> None:
    if not arguments or arguments == ["show"]:
        _show_effective_config(
            workspace,
            cli_values=cli_values,
            store=store,
            session_id=session_id,
        )
        return
    reset = arguments[0].lower() == "reset"
    if reset:
        overrides: dict[str, object] = {}
    else:
        tokens = arguments[1:] if arguments[0].lower() in {"set", "update"} else arguments
        if len(tokens) == 2 and "=" not in tokens[0]:
            tokens = [f"{tokens[0]}={tokens[1]}"]
        overrides = {}
        for token in tokens:
            if "=" not in token:
                typer.echo("Usage: /config set key=value", err=True)
                return
            raw_key, raw_value = token.split("=", 1)
            key = _canonical_config_key(raw_key)
            try:
                overrides[key] = _config_value(key, raw_value)
            except ValueError:
                typer.echo(f"Invalid integer value for {key}.", err=True)
                return

    if session_id is None:
        cli_values.clear()
        cli_values.update(overrides)
        typer.echo("Configuration will apply to the next Session.")
        return

    resolver = ConfigurationResolver(workspace, cli_values=cli_values)
    service = SessionConfigurationService(resolver, store)
    try:
        configuration = service.update(session_id, overrides, reset=reset)
    except SessionOverrideConfirmationRequired:
        choice = _safe_prompt("Allow the less restrictive permission mode? [yes/no]")
        if choice is None or choice.casefold() not in {"y", "yes"}:
            typer.echo("Configuration change denied; the previous Session setting remains active.")
            return
        try:
            configuration = service.update(
                session_id,
                overrides,
                reset=reset,
                confirm_less_restrictive=True,
            )
        except SessionConfigurationError as exc:
            typer.echo(f"Configuration change rejected: {exc}", err=True)
            return
    except SessionConfigurationError as exc:
        typer.echo(f"Configuration change rejected: {exc}", err=True)
        return
    typer.echo("Configuration updated for the next operation.")
    typer.echo(json.dumps(configuration.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def _interactive_loop(
    workspace: Path,
    store: SessionStore,
    *,
    cli_values: dict[str, object],
    provider_factory: ProviderFactory,
) -> None:
    session_id: str | None = None
    gate = PermissionPolicyGate()
    interactive = _is_terminal_input()
    typer.echo("Mini Agent interactive Session. Type /help or /exit.")
    while True:
        task = _safe_prompt("You")
        if task is None:
            typer.echo("Session ended.")
            return
        lowered = task.casefold()
        if lowered in {"/exit", "/quit", "/q"}:
            typer.echo("Session ended.")
            return
        if lowered in {"/help", "help"}:
            typer.echo("Commands: /config [show|set key=value|reset], /sessions, /exit")
            continue
        if lowered == "/sessions":
            _display_sessions(workspace)
            continue
        if lowered.startswith("/config"):
            remainder = task[len("/config") :].strip()
            _interactive_config(
                remainder.split() if remainder else [], workspace, store, session_id, cli_values
            )
            continue
        outcome = asyncio.run(
            _run_turn(
                task,
                workspace,
                store,
                session_id,
                cli_values=cli_values,
                permission_gate=gate,
                interactive=interactive,
                user_already_rendered=True,
                provider_factory=provider_factory,
            )
        )
        if outcome.session_id is not None:
            session_id = outcome.session_id
        if outcome.forced_interrupt:
            raise typer.Exit(code=ForcedInterrupt.exit_code)


def initialize(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm creation of project configuration and ignore rules."),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace to initialize."),
    ] = None,
) -> None:
    """Initialize safe project defaults without ever writing a credential."""

    root = _workspace_from_context(context, workspace)
    try:
        confirmed = yes or typer.confirm(
            "Create .mini-agent/config.toml and update .gitignore?", default=False
        )
    except (Abort, EOFError, OSError):
        confirmed = False
    if not confirmed:
        typer.echo("Initialization cancelled.")
        return
    try:
        config_path, ignore_path = initialize_project(root, confirmed=True)
    except (ConfigurationError, OSError) as exc:
        typer.echo(f"Initialization failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Created {config_path}")
    if ignore_path is not None:
        typer.echo(f"Updated {ignore_path}")


def show_config(
    context: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace whose configuration is shown."),
    ] = None,
) -> None:
    """Show effective values and their winning, non-secret sources."""

    root = _workspace_from_context(context, workspace)
    try:
        _show_effective_config(root)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def list_sessions(
    context: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace whose Sessions are listed."),
    ] = None,
) -> None:
    """List durable Sessions in the selected Workspace."""

    _display_sessions(_workspace_from_context(context, workspace))


def doctor(
    context: typer.Context,
    error_id: Annotated[str, typer.Argument(help="The diagnostic error ID to resolve.")],
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace containing the diagnostic logs."),
    ] = None,
) -> None:
    """Show one redacted diagnostic record by error ID."""

    selected_workspace = _workspace_from_context(context, workspace)
    record = DiagnosticLogger(selected_workspace).find(error_id)
    if record is None:
        typer.echo(f"No diagnostic record found for {error_id}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


def resume_session(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument(help="The Session ID to resume.")],
    task: Annotated[str | None, typer.Argument(help="The next task for the Session.")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Workspace containing the Session."),
    ] = None,
) -> None:
    """Resume a Session with explicit, evidence-based interruption recovery."""

    root = _workspace_from_context(context, workspace)
    store = SessionStore(root)
    values = _cli_values_from_context(context)
    provider_factory = _provider_factory_from_context(context)
    if not _startup_provider_check(
        root,
        store,
        cli_values=values,
        provider_factory=provider_factory,
        session_id=session_id,
    ):
        raise typer.Exit(code=2)
    gate = PermissionPolicyGate()
    try:
        inspection = store.inspect_resume(session_id)
        if inspection.blocked_reason is not None:
            typer.echo(f"Resume blocked: {inspection.blocked_reason}", err=True)
            raise typer.Exit(code=1)
        _print_resume_inspection(inspection)
        if inspection.instruction_change:
            store.record_instruction_change(session_id)
        if inspection.requires_recovery:
            while True:
                choice = _resume_choice()
                if choice is ResumeChoice.INSPECT:
                    recovery_outcome = store.reconcile_resume(session_id, choice)
                    typer.echo("Inspection recorded. Evidence:")
                    for item in recovery_outcome.inspection.interrupted_tools:
                        typer.echo(json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True))
                    continue
                if choice is ResumeChoice.EXIT:
                    typer.echo("Resume exited; history was left without a guessed result.")
                    return
                if choice is ResumeChoice.RETRY:
                    if not asyncio.run(
                        _retry_interrupted_cli(
                            root,
                            store,
                            session_id,
                            cli_values=values,
                            gate=gate,
                            provider_factory=provider_factory,
                        )
                    ):
                        raise typer.Exit(code=1)
                else:
                    store.reconcile_resume(session_id, choice)
                    typer.echo(
                        "Interrupted work was abandoned; its uncertain result remains in history."
                    )
                break
        prompted_task = task is None
        if task is None:
            task = _safe_prompt("You")
        if task is None:
            return
        turn_outcome = asyncio.run(
            _run_turn(
                task,
                root,
                store,
                session_id,
                cli_values=values,
                permission_gate=gate,
                interactive=_is_terminal_input(),
                user_already_rendered=prompted_task,
                provider_factory=provider_factory,
            )
        )
        _raise_for_outcome(turn_outcome)
    except SessionStoreError as exc:
        typer.echo(f"Resume failed safely: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def run_task(context: typer.Context, task: Annotated[list[str], typer.Argument()]) -> None:
    """Run one default conversational task."""

    root = _workspace_from_context(context)
    provider_factory = _provider_factory_from_context(context)
    outcome = asyncio.run(
        _run_turn(
            " ".join(task),
            root,
            SessionStore(root),
            cli_values=_cli_values_from_context(context),
            interactive=False,
            provider_factory=provider_factory,
        )
    )
    _raise_for_outcome(outcome)


def _callback_impl(
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
    *,
    provider_factory: ProviderFactory,
) -> None:
    del version
    context.ensure_object(dict)
    context.obj["provider_factory"] = provider_factory
    root = (workspace or Path.cwd()).expanduser().resolve()
    context.obj["workspace"] = root
    context.obj["cli_values"] = {
        key: value
        for key, value in {
            "model": model,
            "provider_base_url": base_url,
            "permission_mode": permission_mode,
        }.items()
        if value is not None
    }
    if context.invoked_subcommand is not None:
        return
    task = " ".join(context.args).strip() if context.args else ""
    if task:
        outcome = asyncio.run(
            _run_turn(
                task,
                root,
                SessionStore(root),
                cli_values=_cli_values_from_context(context),
                interactive=False,
                provider_factory=provider_factory,
            )
        )
        _raise_for_outcome(outcome)
        return
    if not _startup_provider_check(
        root,
        SessionStore(root),
        cli_values=_cli_values_from_context(context),
        provider_factory=provider_factory,
    ):
        raise typer.Exit(code=2)
    _interactive_loop(
        root,
        SessionStore(root),
        cli_values=_cli_values_from_context(context),
        provider_factory=provider_factory,
    )


def create_app(provider_factory: ProviderFactory = production_provider_factory) -> typer.Typer:
    """Compose a CLI with an explicit Provider factory.

    The default composition is production-only. Tests and offline artifact
    smoke runs must pass their Fake Provider factory explicitly.
    """

    command_app = typer.Typer(
        cls=_DefaultTaskGroup,
        add_completion=False,
        no_args_is_help=False,
        context_settings={"allow_extra_args": True},
        help="An independent, educational terminal coding agent.",
    )
    command_config_app = typer.Typer(add_completion=False, help="Inspect effective configuration.")
    command_app.add_typer(command_config_app, name="config")
    command_app.command("init")(initialize)
    command_config_app.command("show")(show_config)
    command_app.command("sessions")(list_sessions)
    command_app.command("doctor")(doctor)
    command_app.command("resume")(resume_session)
    command_app.command("run", hidden=True)(run_task)

    def callback(
        context: typer.Context,
        workspace: Annotated[
            Path | None,
            typer.Option("--workspace", help="Workspace root for the Session and instructions."),
        ] = None,
        model: Annotated[
            str | None, typer.Option("--model", help="Provider model override.")
        ] = None,
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
        _callback_impl(
            context,
            workspace,
            model,
            base_url,
            permission_mode,
            version,
            provider_factory=provider_factory,
        )

    command_app.callback(invoke_without_command=True)(callback)
    return command_app


app = create_app()


def main() -> None:
    """Console-script entry point."""

    app()
