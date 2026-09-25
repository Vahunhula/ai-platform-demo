"""Thin Typer/Rich client for the shared TaskSession application service."""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.approval import ApprovalError
from ai_platform.auth import AuthenticatedUser, AuthService, Role, UserManagementError
from ai_platform.commands import CommandError, CommandService
from ai_platform.config import Settings
from ai_platform.controls import TaskControlService
from ai_platform.doctor import CheckStatus, inspect_environment
from ai_platform.events import ActorType, Event
from ai_platform.identity import HumanIdentity, LocalIdentityProvider
from ai_platform.locks import ExecutionLockManager
from ai_platform.models import TaskDefinition
from ai_platform.repositories import RepositoryError
from ai_platform.sessions import TaskSession, TaskSessionError, TurnOutcome
from ai_platform.workflow_services import WorkflowError
from ai_platform.workspace import WorkspaceError

app = typer.Typer(no_args_is_help=True, help="AI Platform Demo CLI")
SERVE_GRACEFUL_SHUTDOWN_SECONDS = 3
console = Console()


class _SynchronousCommandRunner:
    """CLI adapter preserving its existing foreground turn behavior."""

    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def run_prepared_turn(self, prepared) -> None:  # noqa: ANN001
        self.context.sessions.run_prepared(prepared)


def _application_context() -> ApplicationContext:
    return create_application_context(lock_recovery_client="cli-startup")


@app.command()
def doctor() -> None:
    """Inspect local prerequisites without initializing or changing task state."""

    try:
        settings = Settings.from_env()
        checks = inspect_environment(settings)
    except (OSError, ValueError) as error:
        console.print(f"[red]Configuration error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1) from None
    table = Table(title="AI Platform Environment", show_header=True, header_style="bold cyan")
    table.add_column("CHECK")
    table.add_column("STATUS")
    table.add_column("DETAIL")
    colors = {CheckStatus.OK: "green", CheckStatus.WARN: "yellow", CheckStatus.FAIL: "red"}
    for check in checks:
        table.add_row(
            check.name,
            f"[{colors[check.status]}]{check.status.value}[/{colors[check.status]}]",
            check.detail,
        )
    console.print(table)
    if any(check.status is CheckStatus.FAIL for check in checks):
        raise typer.Exit(code=1)


def _definition_or_exit(context: ApplicationContext, task_id: str) -> TaskDefinition:
    try:
        return context.sessions.get_definition(task_id)
    except TaskSessionError as error:
        _exit_with_error(error)


def _identity_or_exit() -> HumanIdentity:
    try:
        return LocalIdentityProvider().get_current_user()
    except ValueError as error:
        console.print(f"[red]Identity error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1) from None


def _cli_user() -> AuthenticatedUser:
    human = _identity_or_exit()
    return AuthenticatedUser(
        user_id=human.actor_id,
        username=human.actor_id,
        display_name=human.display_name,
        role=Role.DEVELOPER,
    )


@app.command("command")
def platform_command(task_id: str, command_text: str) -> None:
    """Execute one registered platform slash command for a task."""

    context = _application_context()
    controls = TaskControlService(
        context.sessions,
        context.storage,
        _SynchronousCommandRunner(context),
    )
    service = CommandService(context.sessions, context.storage, controls)
    try:
        result = service.execute(task_id, command_text, _cli_user(), f"cli-{uuid4().hex}")
    except (CommandError, TaskSessionError, WorkflowError) as error:
        _exit_with_error(error)
    console.print(f"[bold cyan]{escape(result.command)}[/bold cyan] {escape(result.message)}")
    console.print_json(data=result.data)


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
    for task in context.sessions.list_tasks():
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
    _render_session(session, context.settings.project_root, context.sessions.locks)
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
    try:
        task = context.sessions.get_definition(task_id)
        workspace_exists = context.workspaces.exists(task.id)
        diff_text = context.sessions.get_diff(task.id)
    except (TaskSessionError, WorkspaceError) as error:
        _exit_with_error(error)
    if not workspace_exists:
        console.print(f"No workspace exists for {task.id}.")
        return
    console.print(f"[bold cyan]{task.id} - Current Diff[/bold cyan]\n")
    if not diff_text.strip():
        console.print("Workspace is clean.")
        return
    console.print(Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=False))


@app.command()
def trace(task_id: str) -> None:
    """Print the complete normalized, append-only TaskSession history."""

    context = _application_context()
    try:
        task = context.sessions.get_definition(task_id)
        events = context.sessions.get_events(task.id)
    except TaskSessionError as error:
        _exit_with_error(error)
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


users_app = typer.Typer(no_args_is_help=True, help="Manage web users (Demo 2 authentication).")
tokens_app = typer.Typer(no_args_is_help=True, help="Issue and revoke web login access tokens.")
app.add_typer(users_app, name="users")
app.add_typer(tokens_app, name="auth-token")

repositories_app = typer.Typer(
    no_args_is_help=True, help="Manage trusted local repository templates."
)
app.add_typer(repositories_app, name="repository")


def _repository_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except RepositoryError as error:
        _exit_with_error(error)
        raise


@repositories_app.command("list")
def repository_list() -> None:
    """List registered repositories without exposing server source paths."""

    table = Table(show_header=True, header_style="bold cyan")
    for column in ("SLUG", "DISPLAY NAME", "TYPE", "DEFAULT BRANCH", "ENABLED"):
        table.add_column(column)
    for repository in _application_context().repositories.list():
        table.add_row(
            repository.slug,
            escape(repository.display_name),
            repository.source_type,
            repository.default_branch,
            "yes" if repository.enabled else "no",
        )
    console.print(table)


@repositories_app.command("add")
def repository_add(
    slug: str = typer.Option(..., "--slug"),
    display_name: str = typer.Option(..., "--display-name"),
    source: Path = typer.Option(..., "--source"),  # noqa: B008
    default_branch: str = typer.Option(..., "--default-branch"),
) -> None:
    """Register a server-side local Git repository or directory template."""

    context = _application_context()
    repository = _repository_call(
        lambda: context.repositories.register_local(
            slug, display_name, source, default_branch
        )
    )
    console.print(
        f"Repository [bold]{repository.slug}[/bold] registered as {repository.id}."
    )


@repositories_app.command("disable")
def repository_disable(slug: str) -> None:
    """Disable task creation from a registered repository."""

    repository = _repository_call(
        lambda: _application_context().repositories.disable(slug)
    )
    console.print(f"Repository {repository.slug} disabled.")


def _auth_service() -> AuthService:
    context = _application_context()
    return AuthService(
        context.storage, session_ttl=timedelta(hours=context.settings.session_hours)
    )


def _auth_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except UserManagementError as error:
        _exit_with_error(error)
        raise  # unreachable; keeps type checkers honest


@users_app.command("list")
def users_list() -> None:
    """List web users (never tokens or hashes)."""

    table = Table(show_header=True, header_style="bold cyan")
    for column in ("USERNAME", "DISPLAY NAME", "ROLE", "ENABLED", "CREATED (UTC)"):
        table.add_column(column)
    for user in _auth_service().list_users():
        table.add_row(
            user.username,
            escape(user.display_name),
            user.role.value,
            "yes" if user.enabled else "no",
            user.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@users_app.command("add")
def users_add(
    username: str = typer.Option(..., "--username", help="Immutable login name / actor ID"),
    display_name: str = typer.Option(..., "--display-name", help="Name shown in the UI"),
    role: str = typer.Option("developer", "--role", help="viewer, developer or admin"),
) -> None:
    """Provision a web user."""

    try:
        chosen = Role(role.strip().lower())
    except ValueError:
        _exit_with_error(UserManagementError("Role must be viewer, developer or admin"))
        return
    user = _auth_call(lambda: _auth_service().add_user(username, display_name, chosen))
    console.print(f"User [bold]{user.username}[/bold] created with role {user.role.value}.")
    console.print(f"Issue a login token with: ai-platform auth-token create {user.username}")


@users_app.command("disable")
def users_disable(username: str) -> None:
    """Block logins and end every existing session of a user (history is kept)."""

    user = _auth_call(lambda: _auth_service().set_enabled(username, False))
    console.print(f"User {user.username} disabled; existing sessions revoked.")


@users_app.command("enable")
def users_enable(username: str) -> None:
    """Re-enable a disabled user (they must log in again)."""

    user = _auth_call(lambda: _auth_service().set_enabled(username, True))
    console.print(f"User {user.username} enabled.")


@users_app.command("logout-all")
def users_logout_all(username: str) -> None:
    """Revoke every active web session of a user."""

    count = _auth_call(lambda: _auth_service().revoke_sessions(username))
    console.print(f"Revoked {count} session(s) of {username}.")


@tokens_app.command("create")
def token_create(username: str) -> None:
    """Issue a login access token. The secret is printed once and never stored."""

    issued = _auth_call(lambda: _auth_service().create_token(username))
    console.print(f"Token [bold]{issued.token_id}[/bold] created for {issued.username}\n")
    # Printed plainly (no markup/highlighting) so it can be copied exactly.
    console.print(issued.secret, markup=False, highlight=False, soft_wrap=True)
    console.print("\nThis token will not be shown again.")


@tokens_app.command("list")
def token_list() -> None:
    """List token IDs and their status (never secrets or hashes)."""

    table = Table(show_header=True, header_style="bold cyan")
    for column in ("TOKEN ID", "USER", "CREATED (UTC)", "STATUS"):
        table.add_column(column)
    for token_id, username, created, revoked in _auth_service().list_tokens():
        table.add_row(
            token_id,
            username,
            created.strftime("%Y-%m-%d %H:%M"),
            f"revoked {revoked:%Y-%m-%d %H:%M}" if revoked else "active",
        )
    console.print(table)


@tokens_app.command("revoke")
def token_revoke(token_id: str) -> None:
    """Revoke a token: no new logins, and the sessions it created end now."""

    _auth_call(lambda: _auth_service().revoke_token(token_id))
    console.print(f"Token {token_id} revoked; its sessions ended.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="HTTP bind address"),
    port: int = typer.Option(8765, min=1, max=65535, help="HTTP port"),
) -> None:
    """Serve the HTTP API for the web UI (loopback only by default)."""

    import uvicorn

    # Open SSE streams never finish on their own; bound the graceful shutdown so
    # Ctrl+C/SIGTERM closes them (clients resume via Last-Event-ID) instead of hanging.
    uvicorn.run(
        "ai_platform.api.app:app",
        host=host,
        port=port,
        timeout_graceful_shutdown=SERVE_GRACEFUL_SHUTDOWN_SECONDS,
    )


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


def _render_session(
    session: TaskSession,
    project_root: Path,
    lock_manager: ExecutionLockManager | None = None,
) -> None:
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
    if record.active_execution:
        details.add_row("Lock actor", record.execution_actor_id or "-")
        details.add_row("Execution ID", record.execution_id or "-")
        details.add_row("Lock process", str(record.execution_pid or "-"))
        details.add_row("Lock host", record.execution_hostname or "-")
        details.add_row(
            "Lock acquired",
            record.execution_started_at.isoformat() if record.execution_started_at else "-",
        )
        details.add_row(
            "Lock heartbeat",
            record.execution_heartbeat_at.isoformat() if record.execution_heartbeat_at else "-",
        )
        if lock_manager is not None:
            inspection = lock_manager.inspect(record)
            age = (
                f", {inspection.heartbeat_age_seconds:.1f}s old"
                if inspection.heartbeat_age_seconds is not None
                else ""
            )
            details.add_row("Lock health", f"{inspection.health.value.upper()}{age}")
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
