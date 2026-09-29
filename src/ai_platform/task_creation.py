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
    TaskOrigin,
    TaskRecord,
    TaskStatus,
    TestGenerationStatus,
    TestSpecification,
    VerificationConfig,
    VerificationType,
)
from ai_platform.repositories import RepositoryError, RepositoryService
from ai_platform.storage import SQLiteStorage
from ai_platform.task_tests import (
    TaskTestError,
    UploadedTestInput,
    materialize_uploaded_tests,
    validate_uploads,
)
from ai_platform.workflow import WorkflowPhase
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
    acceptance_test_stories: str | None = None
    uploaded_test_files: tuple[UploadedTestInput, ...] = ()
    origin: TaskOrigin = TaskOrigin.USER
    disposable: bool = False


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
        if command.origin is TaskOrigin.SYSTEM_TEST and not command.disposable:
            raise TaskCreationError("SYSTEM_TEST tasks must be disposable")
        try:
            uploads = validate_uploads(list(command.uploaded_test_files))
        except TaskTestError as error:
            raise TaskCreationError(str(error)) from error
        stories = (command.acceptance_test_stories or "").strip() or None
        repository = self.repositories.get(command.repository_id, require_enabled=True)
        try:
            assignee = self.auth.assignable_user(command.assignee_user_id)
            self.repositories.validate_branch(Path(repository.source), command.base_branch)
            source_commit = self.repositories.resolve_commit(
                Path(repository.source), command.base_branch
            )
        except (RepositoryError, UserManagementError) as error:
            raise TaskCreationError(str(error)) from error

        started = datetime.now(UTC)
        for _ in range(16):
            task_id = self._new_task_id()
            try:
                workspace = self.workspaces.create(task_id, Path(repository.source), source_commit)
                self.workspaces.exclude_platform_paths(task_id)
                uploaded_records = materialize_uploaded_tests(
                    task_id, workspace, uploads, actor.username
                )
                break
            except WorkspaceExistsError:
                # Another process won an extraordinarily unlikely random-ID collision.
                continue
            except (WorkspaceError, TaskTestError) as error:
                if self.workspaces.exists(task_id):
                    self.workspaces.destroy(task_id)
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
            origin=command.origin,
            disposable=command.disposable,
            workflow_phase=WorkflowPhase.BRAINSTORM,
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
                metadata={
                    "repository_id": repository.id,
                    "base_branch": command.base_branch,
                    "source_commit": source_commit,
                },
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
                metadata={
                    "repository_id": repository.id,
                    "base_branch": command.base_branch,
                    "source_commit": source_commit,
                },
            ),
        ]
        specification = (
            TestSpecification(
                task_id=task_id,
                original_text=stories,
                created_by=actor.username,
                generation_status=TestGenerationStatus.PENDING,
            )
            if stories
            else None
        )
        try:
            self.storage.create_managed_task(
                record,
                events,
                test_specification=specification,
                test_files=uploaded_records,
            )
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
