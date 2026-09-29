"""Bounded, sequence-stable Activity history for long-lived tasks."""

import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest

from ai_platform.api.app import create_app
from ai_platform.events import ActorType, Event, EventType
from tests.test_messaging import _client, _context


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _add_events(context, count: int) -> None:  # noqa: ANN001
    now = datetime.now(UTC).isoformat()
    with context.storage.transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO events (
                id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
            ) VALUES (?, 'DEMO-1', ?, ?, ?, 'pagination-test', ?)
            """,
            (
                (
                    f"pagination-event-{index}",
                    now,
                    EventType.STATUS_CHANGED.value,
                    ActorType.SYSTEM.value,
                    json.dumps({"index": index}),
                )
                for index in range(count)
            ),
        )


def test_ten_thousand_event_pages_are_complete_stable_and_indexed(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _add_events(context, 10_000)
    with context.storage.transaction() as connection:
        expected = [
            row[0]
            for row in connection.execute(
                "SELECT sequence_id FROM events WHERE task_id = 'DEMO-1' ORDER BY sequence_id DESC"
            )
        ]
        plan = " ".join(
            str(column)
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT sequence_id FROM events "
                "WHERE task_id = ? AND sequence_id < ? ORDER BY sequence_id DESC LIMIT ?",
                ("DEMO-1", expected[0] + 1, 51),
            )
            for column in row
        )
    assert len(expected) >= 10_000
    assert "idx_events_task_sequence" in plan
    first_stream_batch = context.storage.get_events_after("DEMO-1", 0, limit=200)
    second_stream_batch = context.storage.get_events_after(
        "DEMO-1", first_stream_batch[-1].sequence_id or 0, limit=200
    )
    assert len(first_stream_batch) == len(second_stream_batch) == 200
    assert first_stream_batch[-1].sequence_id < second_stream_batch[0].sequence_id

    started = monotonic()
    page, has_more = context.storage.get_event_page("DEMO-1", order="desc", limit=50)
    newest_page_elapsed = monotonic() - started
    collected = [event.sequence_id for event in page]
    assert collected == expected[:50]
    assert has_more is True

    # An append after page one must not shift the exclusive older cursor.
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.STATUS_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="concurrent-insert",
            metadata={},
        )
    )
    cursor = collected[-1]
    while has_more:
        page, has_more = context.storage.get_event_page(
            "DEMO-1", order="desc", limit=200, cursor=cursor
        )
        sequences = [event.sequence_id for event in page]
        collected.extend(sequences)
        if sequences:
            cursor = sequences[-1]

    assert collected == expected
    assert len(collected) == len(set(collected))
    # A generous sanity bound catches a lost index without becoming a benchmark assertion.
    assert newest_page_elapsed < 2.0

    oldest, oldest_more = context.storage.get_event_page("DEMO-1", order="asc", limit=50)
    assert [event.sequence_id for event in oldest] == list(reversed(expected))[:50]
    assert oldest_more is True


@pytest.mark.anyio
async def test_activity_page_api_is_bounded_and_legacy_events_remain_compatible(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _add_events(context, 275)
    app = create_app(context)
    async with _client(app) as client:
        newest = await client.get("/api/tasks/DEMO-1/events/page")
        legacy = await client.get("/api/tasks/DEMO-1/events")
        invalid = await client.get("/api/tasks/DEMO-1/events/page?order=desc&after_sequence=1")

    assert newest.status_code == 200
    body = newest.json()
    assert body["order"] == "desc"
    assert body["limit"] == 50
    assert len(body["items"]) == 50
    assert body["has_more"] is True
    assert body["next_before_sequence"] == body["items"][-1]["sequence_id"]
    assert [item["sequence_id"] for item in body["items"]] == sorted(
        (item["sequence_id"] for item in body["items"]), reverse=True
    )
    assert len(legacy.json()) > 275
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_large_chat_projection_is_deterministic_without_per_message_queries(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _add_events(context, 5_000)
    now = datetime.now(UTC).isoformat()
    with context.storage.transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO events (
                id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
            ) VALUES (?, 'DEMO-1', ?, ?, ?, 'chat-load', ?)
            """,
            (
                (
                    f"chat-load-{index}",
                    now,
                    EventType.HUMAN_MESSAGE.value,
                    ActorType.HUMAN.value,
                    json.dumps({"message": f"Message {index}", "display_name": "Load User"}),
                )
                for index in range(200)
            ),
        )
    app = create_app(context)
    started = monotonic()
    async with _client(app) as client:
        first = await client.get("/api/tasks/DEMO-1/messages")
        second = await client.get("/api/tasks/DEMO-1/messages")
    elapsed = monotonic() - started

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()) == 200
    assert len({item["sequence_id"] for item in first.json()}) == 200
    # This includes authentication and two full projections; it is deliberately broad.
    assert elapsed < 5.0
