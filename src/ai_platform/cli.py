"""Typer and Rich command-line interface for the foundation demo."""

import json
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.graph import run_task_graph
from ai_platform.models import TaskDefinition, TaskStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks

app = typer.Typer(no_args_is_help=True, help="AI Platform Demo foundation CLI")
console = Console()


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    definitions: list[TaskDefinition]
    storage: SQLiteStorage


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
    return ApplicationContext(settings, definitions, storage)


def _definition_or_exit(context: ApplicationContext, task_id: str) -> TaskDefinition:
    try:
        return get_task(context.definitions, task_id)
    except KeyError:
        console.print(f"[red]Unknown task ID:[/red] {task_id}")
        raise typer.Exit(code=1) from None


@app.command("tasks")
def list_task_command() -> None:
    """List all demo tasks and their persisted runtime status."""

    context = _application_context()
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("ID")
    table.add_column("DIFFICULTY")
    table.add_column("STATUS")
    table.add_column("TITLE")
    for task in context.storage.list_tasks():
        table.add_row(
            task.task_id,
            task.difficulty.value.upper(),
            task.status.value.upper(),
            task.title,
        )
    console.print(table)


@app.command()
def show(task_id: str) -> None:
    """Show a task definition and its acceptance criteria."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    criteria = "\n".join(f"• {item}" for item in task.acceptance_criteria)
    body = (
        f"[bold]Difficulty:[/bold] {task.difficulty.value.upper()}\n\n"
        f"{task.description}\n\n[bold]Acceptance criteria[/bold]\n{criteria}"
    )
    console.print(Panel(body, title=f"{task.id} — {task.title}", border_style="cyan"))


@app.command()
def start(task_id: str) -> None:
    """Run the load-and-route LangGraph foundation workflow."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    context.storage.append_event(
        Event(
            task_id=task.id,
            event_type=EventType.TASK_STARTED,
            actor_type=ActorType.HUMAN,
            actor_id="local-cli-user",
            metadata={},
        )
    )
    context.storage.update_task_status(task.id, TaskStatus.ANALYZING)
    context.storage.append_event(
        Event(
            task_id=task.id,
            event_type=EventType.STATUS_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="foundation-graph",
            metadata={"status": TaskStatus.ANALYZING.value},
        )
    )

    state = run_task_graph(
        task,
        ModelRouter.from_settings(context.settings),
        context.storage,
        context.settings.checkpoint_db_path,
    )
    console.print(f"\n[bold cyan]{task.id} — {task.title}[/bold cyan]")
    console.print(f"Difficulty: [bold]{task.difficulty.value.upper()}[/bold]")
    console.print("\n[green]Minimal LangGraph workflow completed.[/green]")
    console.print(f"Selected tier: [bold]{state['selected_tier'].upper()}[/bold]")
    console.print(f"Configured model: {state['selected_model']}")
    console.print(f"Reason: {state['selection_reason']}")
    console.print(f"Current status: [bold]{state['status'].upper()}[/bold]")
    console.print("\n[yellow]No coding executor was called.[/yellow]")


@app.command()
def trace(task_id: str) -> None:
    """Print the persisted audit-event history for a task."""

    context = _application_context()
    task = _definition_or_exit(context, task_id)
    events = context.storage.get_events(task.id)
    table = Table(title=f"Trace: {task.id}", show_header=True, header_style="bold cyan")
    table.add_column("TIME (UTC)")
    table.add_column("EVENT")
    table.add_column("ACTOR")
    table.add_column("METADATA")
    for event in events:
        table.add_row(
            event.timestamp.strftime("%H:%M:%S"),
            event.event_type.value,
            f"{event.actor_type.value}:{event.actor_id}",
            json.dumps(event.metadata, sort_keys=True),
        )
    console.print(table)


if __name__ == "__main__":
    app()
