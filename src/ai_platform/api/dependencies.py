"""FastAPI dependencies for the shared application context."""

from typing import Annotated

from fastapi import Depends, Request

from ai_platform.api.presenters import Presenter
from ai_platform.application import ApplicationContext
from ai_platform.commands import CommandService
from ai_platform.controls import TaskControlService
from ai_platform.conversation import ConversationService
from ai_platform.runner import TaskTurnRunner
from ai_platform.task_creation import TaskCreationService


def get_context(request: Request) -> ApplicationContext:
    """Return the process-wide core composition created at API startup."""

    return request.app.state.context


def get_conversation(request: Request) -> ConversationService:
    return request.app.state.conversation


def get_controls(request: Request) -> TaskControlService:
    return request.app.state.controls


def get_commands(request: Request) -> CommandService:
    return request.app.state.commands


def get_presenter(request: Request) -> Presenter:
    return request.app.state.presenter


def get_runner(request: Request) -> TaskTurnRunner | None:
    return request.app.state.runner


def get_task_creation(request: Request) -> TaskCreationService:
    return request.app.state.task_creation


ContextDependency = Annotated[ApplicationContext, Depends(get_context)]
ConversationDependency = Annotated[ConversationService, Depends(get_conversation)]
PresenterDependency = Annotated[Presenter, Depends(get_presenter)]
ControlsDependency = Annotated[TaskControlService, Depends(get_controls)]
CommandsDependency = Annotated[CommandService, Depends(get_commands)]
RunnerDependency = Annotated[TaskTurnRunner | None, Depends(get_runner)]
TaskCreationDependency = Annotated[TaskCreationService, Depends(get_task_creation)]
