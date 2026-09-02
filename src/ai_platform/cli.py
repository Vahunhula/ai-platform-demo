"""Thin Typer/Rich client for the shared TaskSession application service."""

import json
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ai_platform.approval import ApprovalError
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors import AgentExecutor, ClaudeAgentExecutor
from ai_platform.identity import HumanIdentity, LocalIdentityProvider
from ai_platform.models import TaskDefinition
from ai_platform.router import ModelRouter
from ai_platform.sessions import TaskSession, TaskSessionError, TaskSessionService, TurnOutcome
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workspace import LocalWorkspaceProvider, WorkspaceError

app = typer.Typer(no_args_is_help=True, help="AI Platform Demo CLI")
console = Console()


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    definitions: list[TaskDefinition]
    storage: SQLiteStorage
    workspaces: LocalWorkspaceProvider
    sessions: TaskSessionService


def _application_context() -> ApplicationContext:
    settings = Settings.from_env()
    definitions = load_tasks(settings.tasks_path)
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()
    for definition in definitions:
        if storage.create_task(definition):
            storage.append_event(
                Event(
                    task_id=definition.id,
                    event_type=EventType.TASK_CREATED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="task-loader",
                    metadata={"title": definition.title},
                )
            )
    workspaces = LocalWorkspaceProvider(
        settings.workspace_root, settings.project_root / "demo_repo"
    )
    sessions = TaskSessionService(
        settings,
        definitions,
        storage,
        workspaces,
        ModelRouter.from_settings(settings),
        lambda: _executor(settings),
    )
    return ApplicationContext(settings, definitions, storage, workspaces, sessions)


def _definition_or_exit(context: ApplicationContext, task_id: str) -> TaskDefinition:
    try:
        return get_task(context.definitions, task_id)
    except KeyError:
        console.print(f"[red]Unknown task ID:[/red] {escape(task_id)}")
        raise typer.Exit(code=1) from None


def _identity_or_exit() -> HumanIdentity:
    try:
        return LocalIdentityProvider().get_current_user()
    except ValueError as error:
        console.print(f"[red]Identity error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1) from None


def _executor(settings: Settings) -> AgentExecutor:
    if settings.executor.lower() == "claude":
        return ClaudeAgentExecutor(settings)
    raise TaskSessionError(
        f"Unsupported executor: {settings.executor}. Set AI_PLATFORM_EXECUTOR=claude."
    )


@app.command("tasks")
def list_task_command() -> None:
    """List all demo tasks and their persisted runtime status."""

    context = _application_context()
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("ID")
    table.add_column("DIFFICULTY")
    table.add_column("STATUS")
    table.add_column("WRITER")
    table.add_column("TITLE")
    for task in context.storage.list_tasks():
        table.add_row(
            task.task_id,
            task.difficulty.value.upper(),
            task.status.value.upper(),
            task.active_execution.value.upper() if task.active_execution else "-",
            task.title,
        )
    console.print(table)


@app.command()
def show(task_id: str) -> None:
    """Show a task definition, acceptance criteria, and verification target."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
    targets = ", ".join(task.verification.targets)
    body = (
        f"[bold]Difficulty:[/bold] {task.difficulty.value.upper()}\n\n"
        f"{task.description}\n\n[bold]Acceptance criteria[/bold]\n{criteria}\n\n"
        f"[bold]Verification:[/bold] pytest {targets}"
    )
    console.print(Panel(body, title=f"{task.id} - {task.title}", border_style="cyan"))


@app.command()
def start(task_id: str) -> None:
    """Create the task workspace and run its initial bounded agent turn."""

    context = _application_context()
    try:
        outcome = context.sessions.start(task_id, _identity_or_exit())
    except TaskSessionError as error:
        _exit_with_error(error)
    _render_turn_outcome(context.sessions.get_session(task_id), outcome)


@app.command()
def attach(
    task_id: str,
    follow: bool = typer.Option(False, "--follow", help="Stream new events using SQLite polling"),
) -> None:
    """Display the shared task, participants, conversation, and optional live activity."""

    context = _application_context()
    try:
        human = _identity_or_exit()
        context.sessions.connect(task_id, human)
        session = context.sessions.get_session(task_id)
    except TaskSessionError as error:
        _exit_with_error(error)
    _render_session(session, context.settings.project_root)
    if not follow:
        return

    cursor = max((event.sequence_id or 0 for event in session.events), default=0)
    console.print("\n[cyan]Following shared activity. Press Ctrl+C to stop.[/cyan]")
    try:
        for event in context.sessions.follow_events(session.definition.id, cursor):
            _render_live_event(event)
    except KeyboardInterrupt:
        console.print("\n[dim]Follow stopped.[/dim]")


@app.command()
def message(task_id: str, content: str) -> None:
    """Append a shared human instruction and continue from human review when possible."""

    context = _application_context()
    try:
        outcome = context.sessions.message(task_id, content, _identity_or_exit())
        session = context.sessions.get_session(task_id)
    except TaskSessionError as error:
        _exit_with_error(error)
    _render_turn_outcome(session, outcome)


@app.command("diff")
def diff_command(task_id: str) -> None:
    """Show the actual Git diff in a task workspace."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    if not context.workspaces.exists(task.id):
        console.print(f"No workspace exists for {task.id}.")
        return
    try:
        diff_text = context.workspaces.get_diff(task.id)
    except WorkspaceError as error:
        _exit_with_error(error)
    console.print(f"[bold cyan]{task.id} - Current Diff[/bold cyan]\n")
    if not diff_text.strip():
        console.print("Workspace is clean.")
        return
    console.print(Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=False))


@app.command()
def trace(task_id: str) -> None:
    """Print the complete normalized, append-only TaskSession history."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    events = context.storage.get_events(task.id)
    table = Table(title=f"{task.id} - Trace", show_header=True, header_style="bold cyan")
    table.add_column("SEQ", justify="right")
    table.add_column("TIME (UTC)", no_wrap=True)
    table.add_column("TYPE", no_wrap=True)
    table.add_column("ACTOR", no_wrap=True)
    table.add_column("EVENT", no_wrap=True)
    table.add_column("DETAILS")
    for event in events:
        table.add_row(
            str(event.sequence_id or "-"),
            event.timestamp.strftime("%H:%M:%S"),
            event.actor_type.value.upper(),
            event.actor_id,
            event.event_type.value,
            _format_metadata(event.metadata, 500),
        )
    console.print(table)


@app.command()
def pause(task_id: str) -> None:
    """Pause a task or cooperatively interrupt its active Claude turn."""

    context = _application_context()
    try:
        deferred = context.sessions.pause(task_id, _identity_or_exit())
    except TaskSessionError as error:
        _exit_with_error(error)
    if deferred:
        console.print(
            f"[yellow]{escape(task_id.upper())} pause requested.[/yellow] "
            "The active SDK client will be interrupted cooperatively; the task will not "
            "start another agent turn."
        )
    else:
        console.print(f"[green]{escape(task_id.upper())} is PAUSED_BY_HUMAN.[/green]")


@app.command("shell")
def shell_command(task_id: str) -> None:
    """Open an exclusive operating-system shell in a paused task workspace."""

    context = _application_context()
    try:
        session = context.sessions.get_session(task_id)
        console.print(f"Opening shell in [cyan]{escape(str(session.record.workspace_path))}[/cyan]")
        console.print("Exit the shell to return to AI Platform.")
        changes = context.sessions.shell(task_id, _identity_or_exit())
    except TaskSessionError as error:
        _exit_with_error(error)
    if changes:
        console.print("[green]Manual workspace changes recorded:[/green]")
        for change in changes:
            console.print(f"  {escape(change.path)} ({change.change_type})")
    else:
        console.print("No manual workspace changes detected.")


@app.command()
def resume(
    task_id: str,
    message: str | None = typer.Option(None, "--message", help="Optional human instruction"),
) -> None:
    """Resume Claude from the current, authoritative paused workspace."""

    context = _application_context()
    try:
        outcome = context.sessions.resume(task_id, _identity_or_exit(), message)
        session = context.sessions.get_session(task_id)
    except TaskSessionError as error:
        _exit_with_error(error)
    _render_turn_outcome(session, outcome)


@app.command()
def reject(task_id: str, message: str) -> None:
    """Reject the current result with feedback and run a correction turn."""

    context = _application_context()
    try:
        outcome = context.sessions.reject(task_id, message, _identity_or_exit())
        session = context.sessions.get_session(task_id)
    except TaskSessionError as error:
        _exit_with_error(error)
    _render_turn_outcome(session, outcome)


@app.command()
def approve(task_id: str) -> None:
    """Attribute human approval and complete a verified task without pushing."""

    context = _application_context()
    try:
        context.sessions.approve(task_id, _identity_or_exit())
    except (ApprovalError, TaskSessionError) as error:
        _exit_with_error(error)
    console.print(f"[green]{escape(task_id.upper())} approved and completed.[/green]")
    console.print("Workspace retained for inspection.")


@app.command()
def reset(
    task_id: str,
    yes: bool = typer.Option(False, "--yes", help="Skip destructive confirmation"),
) -> None:
    """Delete one workspace and reset runtime state while retaining history."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    workspace = context.workspaces.get_path(task.id)
    console.print(f"This will delete: [bold red]{escape(str(workspace))}[/bold red]")
    console.print("Task/event history will be retained; runtime state will return to READY.")
    if not yes and not typer.confirm(f"Reset {task.id}?"):
        console.print("Reset cancelled.")
        raise typer.Exit()
    try:
        context.sessions.reset(task.id, _identity_or_exit())
    except TaskSessionError as error:
        _exit_with_error(error)
    console.print(f"[green]{task.id} reset to READY.[/green]")


def _render_session(session: TaskSession, project_root: Path) -> None:
    record = session.record
    workspace = (
        _display_path(Path(record.workspace_path), project_root)
        if record.workspace_path
        else "Not created"
    )
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Status", record.status.value.upper())
    details.add_row("Difficulty", record.difficulty.value.upper())
    model_tier = record.selected_tier.value.upper() if record.selected_tier else "-"
    details.add_row("Model tier", model_tier)
    details.add_row("Model", record.selected_model or "-")
    details.add_row("Attempt", str(record.attempt))
    details.add_row("Verification", record.verification_status.value.upper())
    details.add_row("Workspace", workspace)
    active_writer = record.active_execution.value.upper() if record.active_execution else "None"
    details.add_row("Active writer", active_writer)
    if record.pause_requested:
        details.add_row("Pause", "REQUESTED")
    console.print(Panel(details, title=f"{session.definition.id} - {session.definition.title}"))

    console.print("[bold]Participants seen in durable history[/bold]")
    if session.participants:
        for actor_type, actor_id, display_name in session.participants:
            label = display_name if display_name == actor_id else f"{display_name} ({actor_id})"
            console.print(f"  - {escape(label)} ({actor_type.value})")
    else:
        console.print("  None")

    console.print("\n[bold]Conversation[/bold]")
    if session.conversation:
        for event in session.conversation[-20:]:
            message = str(event.metadata.get("message", ""))
            console.print(
                f"  {event.timestamp.strftime('%H:%M:%S')} [cyan]{escape(event.actor_id)}[/cyan]"
            )
            console.print(f"    {escape(message)}")
    else:
        console.print("  No messages yet")

    console.print("\n[bold]Changed files[/bold]")
    if session.changed_files:
        for change in session.changed_files:
            console.print(f"  - {escape(change.path)} ({change.change_type})")
    else:
        console.print("  None")

    console.print("\n[bold]Recent activity[/bold]")
    for event in session.events[-10:]:
        _render_live_event(event)


def _render_live_event(event: Event) -> None:
    details = _format_metadata(event.metadata, 220)
    actor = event.actor_id if event.actor_type is not ActorType.SYSTEM else "system"
    console.print(
        f"  {event.timestamp.strftime('%H:%M:%S')} "
        f"[cyan]{escape(actor)}[/cyan] {event.event_type.value} {escape(details)}"
    )


def _render_turn_outcome(session: TaskSession, outcome: TurnOutcome) -> None:
    if not outcome.agent_started:
        color = "yellow" if outcome.queued else "green"
        console.print(f"[{color}]{escape(outcome.detail)}[/{color}]")
        return
    state = outcome.state or {}
    verification = "PASS" if state.get("verification_passed") else "NOT PASSED"
    status = session.record.status.value.upper()
    console.print(f"[bold cyan]{session.definition.id} - {session.definition.title}[/bold cyan]")
    console.print(f"Agent turn complete. Verification: {verification}. Status: {status}.")


def _exit_with_error(error: Exception) -> None:
    console.print(f"[red]{escape(str(error))}[/red]")
    raise typer.Exit(code=1)


def _format_metadata(metadata: dict, limit: int) -> str:
    if not metadata:
        return ""
    visible = {key: value for key, value in metadata.items() if key != "display_name"}
    rendered = json.dumps(visible, sort_keys=True, ensure_ascii=False)
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "..."


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    app()
