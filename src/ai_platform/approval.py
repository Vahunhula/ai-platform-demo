"""Human approval lifecycle operation."""

from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import TaskStatus, VerificationStatus
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import WorkflowPhase


class ApprovalError(RuntimeError):
    """Raised when a task cannot be approved in its current state."""


def approve_task(
    storage: SQLiteStorage,
    task_id: str,
    actor_id: str,
    display_name: str | None = None,
) -> None:
    """Approve a verified task in Human Review and retain its workspace and history."""

    task = storage.get_task(task_id)
    if task is None:
        raise ApprovalError(f"Unknown task ID: {task_id}")
    if (
        task.status is not TaskStatus.WAITING_FOR_HUMAN
        or task.verification_status is not VerificationStatus.PASSED
        or task.workflow_phase is not WorkflowPhase.HUMAN_REVIEW
    ):
        raise ApprovalError(
            f"Cannot approve {task_id}. Current phase: {task.workflow_phase.value}, "
            f"verification status: {task.verification_status.value.upper()}"
        )
    # Platform rule: a task cannot be approved while accepted human instructions
    # are still queued. try_complete_verified_task re-checks this atomically.
    if pending := storage.pending_message_count(task_id):
        raise ApprovalError(
            f"Cannot approve {task_id} while {pending} accepted human instruction(s) "
            "are still queued for the agent."
        )
    if not storage.try_complete_verified_task(task_id):
        latest = storage.get_task(task_id)
        status = latest.status.value.upper() if latest else "UNKNOWN"
        raise ApprovalError(f"Cannot approve {task_id}. Current status: {status}")
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.HUMAN_APPROVED,
            actor_type=ActorType.HUMAN,
            actor_id=actor_id,
            metadata={"display_name": display_name or actor_id},
        )
    )
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.STATUS_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="approval",
            metadata={
                "from": TaskStatus.WAITING_FOR_HUMAN.value,
                "to": TaskStatus.COMPLETED.value,
            },
        )
    )
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.TASK_COMPLETED,
            actor_type=ActorType.SYSTEM,
            actor_id="approval",
            metadata={"workspace_retained": True},
        )
    )
