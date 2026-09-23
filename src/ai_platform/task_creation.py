"""Validated task creation with eager, isolated workspace provisioning."""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_platform.auth import AuthenticatedUser, AuthService, UserManagementError
from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import (
    TaskDifficulty,
    TaskRecord,
    TaskStatus,
    VerificationConfig,
    VerificationType,
)
from ai_platform.repositories import RepositoryError, RepositoryService
from ai_platform.storage import SQLiteStorage
from ai_platform.workspace import LocalWorkspaceProvider, WorkspaceError, WorkspaceExistsError

logger = logging.getLogger(__name__)


class TaskCreationError(RuntimeError):
    """A validation failure safe to return to a task creator."""


class TaskProvisioningError(RuntimeError):
    """Workspace or persistence provisioning failed without creating a usable task."""


@dataclass(frozen=True, slots=True)
class CreateTaskCommand:
    title: str
    description: str
    repository_id: str
    base_branch: str
    assignee_user_id: str
    jira_key: str | None


class TaskCreationService:
    """Coordinates registry validation, filesystem provisioning and DB persistence."""

    def __init__(
        self,
        storage: SQLiteStorage,
        repositories: RepositoryService,
        auth: AuthService,
        workspaces: LocalWorkspaceProvider,
    ) -> None:
        self.storage = storage
        self.repositories = repositories
        self.auth = auth
        self.workspaces = workspaces

    def create(self, command: CreateTaskCommand, actor: AuthenticatedUser) -> TaskRecord:
        repository = self.repositories.get(command.repository_id, require_enabled=True)
        try:
            assignee = self.auth.assignable_user(command.assignee_user_id)
            self.repositories.validate_branch(Path(repository.source), command.base_branch)
        except (RepositoryError, UserManagementError) as error:
            raise TaskCreationError(str(error)) from error

        started = datetime.now(UTC)
        for _ in range(16):
            task_id = self._new_task_id()
            try:
                workspace = self.workspaces.create(
                    task_id, Path(repository.source), command.base_branch
                )
                break
            except WorkspaceExistsError:
                # Another process won an extraordinarily unlikely random-ID collision.
                continue
            except WorkspaceError as error:
                logger.exception("Workspace provisioning failed for %s", task_id)
                raise TaskProvisioningError("Workspace provisioning failed") from error
        else:
            raise TaskProvisioningError("Could not allocate a unique task workspace")

        now = datetime.now(UTC)
        record = TaskRecord(
            task_id=task_id,
            title=command.title,
            description=command.description,
            difficulty=TaskDifficulty.MEDIUM,
            status=TaskStatus.READY,
            workspace_path=str(workspace),
            repository_id=repository.id,
            base_branch=command.base_branch,
            assignee_user_id=assignee.user_id,
            jira_key=command.jira_key,
            created_by=actor.username,
            acceptance_criteria=[
                "Complete the requested work",
                "Keep changes scoped to this task",
                "Pass the repository test suite",
            ],
            verification=VerificationConfig(type=VerificationType.PYTEST, targets=["tests"]),
            created_at=now,
            updated_at=now,
        )
        events = [
            Event(
                task_id=task_id,
                timestamp=started,
                event_type=EventType.WORKSPACE_PROVISION_STARTED,
                actor_type=ActorType.SYSTEM,
                actor_id="task-creation-service",
                metadata={"repository_id": repository.id, "base_branch": command.base_branch},
            ),
            Event(
                task_id=task_id,
                event_type=EventType.TASK_CREATED,
                actor_type=ActorType.HUMAN,
                actor_id=actor.username,
                metadata={"title": command.title, "display_name": actor.display_name},
            ),
            Event(
                task_id=task_id,
                event_type=EventType.REPOSITORY_SELECTED,
                actor_type=ActorType.HUMAN,
                actor_id=actor.username,
                metadata={
                    "repository_id": repository.id,
                    "repository_slug": repository.slug,
                    "base_branch": command.base_branch,
                    "display_name": actor.display_name,
                },
            ),
            Event(
                task_id=task_id,
                event_type=EventType.ASSIGNEE_SET,
                actor_type=ActorType.HUMAN,
                actor_id=actor.username,
                metadata={
                    "assignee_user_id": assignee.user_id,
                    "assignee_username": assignee.username,
                    "display_name": actor.display_name,
                },
            ),
            Event(
                task_id=task_id,
                event_type=EventType.WORKSPACE_PROVISIONED,
                actor_type=ActorType.SYSTEM,
                actor_id="local-workspace",
                metadata={"repository_id": repository.id, "base_branch": command.base_branch},
            ),
        ]
        try:
            self.storage.create_managed_task(record, events)
        except Exception as error:
            try:
                self.workspaces.destroy(task_id)
            except Exception:
                logger.exception("Orphan workspace cleanup failed for %s", task_id)
            raise TaskProvisioningError("Task persistence failed after provisioning") from error
        return record

    def _new_task_id(self) -> str:
        for _ in range(16):
            candidate = "TASK-" + secrets.token_hex(4).upper()
            if self.storage.get_task(candidate) is None and not self.workspaces.exists(candidate):
                return candidate
        raise TaskProvisioningError("Could not allocate a unique task ID")
