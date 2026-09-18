"""FastAPI dependencies for the shared application context."""

from typing import Annotated

from fastapi import Depends, Request

from ai_platform.api.presenters import Presenter
from ai_platform.application import ApplicationContext
from ai_platform.conversation import ConversationService
from ai_platform.runner import TaskTurnRunner


def get_context(request: Request) -> ApplicationContext:
    """Return the process-wide core composition created at API startup."""

    return request.app.state.context


def get_conversation(request: Request) -> ConversationService:
    return request.app.state.conversation


def get_presenter(request: Request) -> Presenter:
    return request.app.state.presenter


def get_runner(request: Request) -> TaskTurnRunner | None:
    return request.app.state.runner


ContextDependency = Annotated[ApplicationContext, Depends(get_context)]
ConversationDependency = Annotated[ConversationService, Depends(get_conversation)]
PresenterDependency = Annotated[Presenter, Depends(get_presenter)]
RunnerDependency = Annotated[TaskTurnRunner | None, Depends(get_runner)]
