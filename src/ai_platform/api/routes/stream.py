"""Server-Sent Events: live public task events read from the durable event log.

Protocol (``text/event-stream``):

- ``event: platform_event`` with ``id: <sequence_id>`` and the same JSON as one
  item of ``GET /api/tasks/{id}/events``, strictly in sequence order;
- ``event: conversation`` (no ``id``) with ``{"revision": ...}`` whenever the
  task's browser-message delivery states change, so clients refetch messages;
- ``event: heartbeat`` (no ``id``) while idle. It is a named event rather than
  an SSE comment so browsers can see it: a client that hears nothing for a few
  heartbeat intervals knows the connection is dead even when a proxy keeps it
  open, and reconnects from its last sequence.

The cursor is the durable event sequence. A reconnecting ``EventSource`` sends
``Last-Event-ID`` automatically and the stream resumes with the next event; an
explicit ``?after=N`` does the same for a first connection. Correctness relies
only on SQLite, never on in-memory pub/sub.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from time import monotonic

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ai_platform.api.dependencies import ContextDependency, PresenterDependency
from ai_platform.api.presenters import Presenter
from ai_platform.application import ApplicationContext

router = APIRouter(prefix="/tasks", tags=["stream"])

_RETRY_MILLISECONDS = 3000


def _cursor(last_event_id: str | None, after: int | None) -> int:
    if last_event_id is not None and last_event_id.strip():
        value = last_event_id.strip()
        if not value.isdigit():
            raise HTTPException(
                status_code=422, detail="Last-Event-ID must be a non-negative integer"
            )
        return int(value)
    return after or 0


async def event_stream(
    context: ApplicationContext,
    presenter: Presenter,
    task_id: str,
    cursor: int,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll_seconds: float,
    keepalive_seconds: float,
) -> AsyncIterator[str]:
    """Yield SSE frames for events after ``cursor`` until the client disconnects."""

    yield f"retry: {_RETRY_MILLISECONDS}\n\n"
    revision: str | None = None
    last_sent = monotonic()
    while not await is_disconnected():
        events = await run_in_threadpool(context.storage.get_events_after, task_id, cursor)
        for event in events:
            cursor = event.sequence_id or cursor
            payload = presenter.event(event).model_dump_json()
            yield f"id: {cursor}\nevent: platform_event\ndata: {payload}\n\n"
            last_sent = monotonic()
        current = await run_in_threadpool(context.storage.message_revision, task_id)
        if current != revision:
            revision = current
            yield f"event: conversation\ndata: {json.dumps({'revision': current})}\n\n"
            last_sent = monotonic()
        if monotonic() - last_sent >= keepalive_seconds:
            yield "event: heartbeat\ndata: {}\n\n"
            last_sent = monotonic()
        await asyncio.sleep(poll_seconds)


@router.get("/{task_id}/stream", response_class=StreamingResponse)
async def stream_task(
    task_id: str,
    request: Request,
    context: ContextDependency,
    presenter: PresenterDependency,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    definition = await run_in_threadpool(context.sessions.get_definition, task_id)
    cursor = _cursor(last_event_id, after)
    return StreamingResponse(
        event_stream(
            context,
            presenter,
            definition.id,
            cursor,
            is_disconnected=request.is_disconnected,
            poll_seconds=request.app.state.stream_poll_seconds,
            keepalive_seconds=request.app.state.stream_keepalive_seconds,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
