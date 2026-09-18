"""Ephemeral task presence: who is viewing a task right now.

Presence is collaboration metadata, not task history: it lives in the
``task_presence`` table (one row per task and user, overwritten by each
heartbeat) and is never written to the append-only event log. A user counts as
present while their last heartbeat is younger than the TTL; expired rows are
deleted lazily on the next heartbeat.
"""

from datetime import UTC, datetime, timedelta

from ai_platform.auth import AuthenticatedUser, AuthService
from ai_platform.storage import SQLiteStorage

PRESENCE_TTL = timedelta(seconds=45)
HEARTBEAT_INTERVAL_SECONDS = 15
_CLEANUP_AFTER = timedelta(hours=1)


class PresenceService:
    def __init__(
        self, storage: SQLiteStorage, auth: AuthService, *, ttl: timedelta = PRESENCE_TTL
    ) -> None:
        self.storage = storage
        self.auth = auth
        self.ttl = ttl

    def heartbeat(self, task_id: str, user: AuthenticatedUser) -> list[AuthenticatedUser]:
        now = datetime.now(UTC)
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO task_presence (task_id, user_id, last_seen) VALUES (?, ?, ?)
                ON CONFLICT (task_id, user_id) DO UPDATE SET last_seen = excluded.last_seen
                """,
                (task_id, user.user_id, now.isoformat()),
            )
            connection.execute(
                "DELETE FROM task_presence WHERE last_seen < ?",
                ((now - _CLEANUP_AFTER).isoformat(),),
            )
        return self.active(task_id)

    def active(self, task_id: str) -> list[AuthenticatedUser]:
        cutoff = (datetime.now(UTC) - self.ttl).isoformat()
        with self.storage.transaction() as connection:
            rows = connection.execute(
                "SELECT user_id FROM task_presence WHERE task_id = ? AND last_seen > ?",
                (task_id, cutoff),
            ).fetchall()
        users = self.auth.users_by_id([row["user_id"] for row in rows])
        return sorted(users.values(), key=lambda user: user.username)
