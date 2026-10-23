"""SQLite persistence for integration connections, credentials, OAuth, and audit."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from ai_platform.integrations.models import (
    ConnectionStatus,
    IntegrationConnection,
    IntegrationProvider,
    OAuthStateError,
)
from ai_platform.storage import SQLiteStorage


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class IntegrationStore:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def initialize(self) -> None:
        with self.storage.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS integration_connections (
                    connection_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    external_account_id TEXT,
                    external_account_name TEXT,
                    external_site_id TEXT,
                    external_site_url TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS integration_credentials (
                    connection_id TEXT PRIMARY KEY
                        REFERENCES integration_connections(connection_id) ON DELETE CASCADE,
                    encrypted_credentials TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS integration_oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS jira_site_selections (
                    selection_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    encrypted_credentials TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS integration_webhook_deliveries (
                    provider TEXT NOT NULL,
                    delivery_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(provider, delivery_id)
                );
                CREATE TABLE IF NOT EXISTS integration_refresh_locks (
                    connection_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS integration_audit_events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS integration_audit_append_only_update
                BEFORE UPDATE ON integration_audit_events BEGIN
                    SELECT RAISE(ABORT, 'integration audit events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS integration_audit_append_only_delete
                BEFORE DELETE ON integration_audit_events BEGIN
                    SELECT RAISE(ABORT, 'integration audit events are append-only');
                END;
                """
            )

    def encrypted_credentials_exist(self) -> bool:
        with self.storage.transaction() as connection:
            return (
                connection.execute("SELECT 1 FROM integration_credentials LIMIT 1").fetchone()
                is not None
                or connection.execute(
                    "SELECT 1 FROM jira_site_selections WHERE consumed_at IS NULL LIMIT 1"
                ).fetchone()
                is not None
            )

    def encrypted_envelopes(self) -> list[tuple[str, str]]:
        """Return persisted envelopes with their encryption context for startup validation."""

        with self.storage.transaction() as connection:
            credentials = connection.execute(
                "SELECT encrypted_credentials FROM integration_credentials"
            ).fetchall()
            selections = connection.execute(
                "SELECT encrypted_credentials FROM jira_site_selections "
                "WHERE consumed_at IS NULL"
            ).fetchall()
        return [
            *((row["encrypted_credentials"], "JIRA") for row in credentials),
            *((row["encrypted_credentials"], "JIRA_PENDING") for row in selections),
        ]

    def list_connections(self) -> list[IntegrationConnection]:
        with self.storage.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_connections ORDER BY provider"
            ).fetchall()
        return [self._connection(row) for row in rows]

    def get_connection(self, provider: IntegrationProvider) -> IntegrationConnection | None:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integration_connections WHERE provider = ?", (provider.value,)
            ).fetchone()
        return self._connection(row) if row else None

    def save_connection(
        self,
        provider: IntegrationProvider,
        *,
        created_by: str,
        display_name: str,
        external_account_id: str | None,
        external_account_name: str | None,
        external_site_id: str | None = None,
        external_site_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        encrypted_credentials: str | None = None,
    ) -> IntegrationConnection:
        now = _iso()
        with self.storage.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT connection_id, created_at FROM integration_connections WHERE provider = ?",
                (provider.value,),
            ).fetchone()
            connection_id = existing["connection_id"] if existing else f"int_{uuid4().hex}"
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO integration_connections (
                    connection_id, provider, status, display_name, external_account_id,
                    external_account_name, external_site_id, external_site_url, metadata_json,
                    created_by, created_at, updated_at, last_checked_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(provider) DO UPDATE SET
                    status=excluded.status, display_name=excluded.display_name,
                    external_account_id=excluded.external_account_id,
                    external_account_name=excluded.external_account_name,
                    external_site_id=excluded.external_site_id,
                    external_site_url=excluded.external_site_url,
                    metadata_json=excluded.metadata_json, created_by=excluded.created_by,
                    updated_at=excluded.updated_at, last_error=NULL
                """,
                (
                    connection_id,
                    provider.value,
                    ConnectionStatus.CONNECTED.value,
                    display_name,
                    external_account_id,
                    external_account_name,
                    external_site_id,
                    external_site_url,
                    json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
                    created_by,
                    created_at,
                    now,
                ),
            )
            if encrypted_credentials is not None:
                connection.execute(
                    """
                    INSERT INTO integration_credentials (
                        connection_id, encrypted_credentials, revision, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(connection_id) DO UPDATE SET
                        encrypted_credentials=excluded.encrypted_credentials,
                        revision=integration_credentials.revision + 1,
                        updated_at=excluded.updated_at
                    """,
                    (connection_id, encrypted_credentials, now),
                )
            row = connection.execute(
                "SELECT * FROM integration_connections WHERE connection_id = ?", (connection_id,)
            ).fetchone()
        return self._connection(row)

    def update_health(
        self,
        provider: IntegrationProvider,
        *,
        status: ConnectionStatus,
        safe_error: str | None,
        metadata: dict[str, Any] | None = None,
        account_name: str | None = None,
    ) -> IntegrationConnection:
        now = _iso()
        with self.storage.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM integration_connections WHERE provider = ?", (provider.value,)
            ).fetchone()
            if row is None:
                raise KeyError(provider.value)
            current_metadata = json.loads(row["metadata_json"])
            if metadata:
                current_metadata.update(metadata)
            connection.execute(
                """
                UPDATE integration_connections SET status=?, last_checked_at=?, last_error=?,
                    metadata_json=?, external_account_name=COALESCE(?, external_account_name),
                    updated_at=? WHERE provider=?
                """,
                (
                    status.value,
                    now,
                    safe_error,
                    json.dumps(current_metadata, separators=(",", ":"), sort_keys=True),
                    account_name,
                    now,
                    provider.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM integration_connections WHERE provider = ?", (provider.value,)
            ).fetchone()
        return self._connection(updated)

    def credentials(self, connection_id: str) -> tuple[str, int] | None:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT encrypted_credentials, revision FROM integration_credentials "
                "WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
        return (row["encrypted_credentials"], row["revision"]) if row else None

    def rotate_credentials(self, connection_id: str, revision: int, envelope: str) -> bool:
        """Compare-and-swap a rotating refresh token under a SQLite writer lock."""

        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE integration_credentials
                SET encrypted_credentials=?, revision=revision + 1, updated_at=?
                WHERE connection_id=? AND revision=?
                """,
                (envelope, _iso(), connection_id, revision),
            )
            return cursor.rowcount == 1

    def acquire_refresh_lock(
        self, connection_id: str, owner: str, *, wait_seconds: float = 25.0
    ) -> None:
        """Acquire a cross-process refresh lease using short SQLite writer transactions."""

        deadline = monotonic() + wait_seconds
        while True:
            now = _now()
            expires = now.timestamp() + 30
            with self.storage.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT owner, expires_at FROM integration_refresh_locks "
                    "WHERE connection_id=?",
                    (connection_id,),
                ).fetchone()
                if row is None or datetime.fromisoformat(row["expires_at"]) <= now:
                    connection.execute(
                        """
                        INSERT INTO integration_refresh_locks (connection_id, owner, expires_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(connection_id) DO UPDATE SET
                            owner=excluded.owner, expires_at=excluded.expires_at
                        """,
                        (
                            connection_id,
                            owner,
                            datetime.fromtimestamp(expires, UTC).isoformat(),
                        ),
                    )
                    return
            if monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for integration credential refresh lock.")
            sleep(0.05)

    def release_refresh_lock(self, connection_id: str, owner: str) -> None:
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM integration_refresh_locks WHERE connection_id=? AND owner=?",
                (connection_id, owner),
            )

    def disconnect(self, provider: IntegrationProvider) -> None:
        with self.storage.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT connection_id FROM integration_connections WHERE provider=?",
                (provider.value,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "DELETE FROM integration_credentials WHERE connection_id=?",
                (row["connection_id"],),
            )
            if provider is IntegrationProvider.JIRA:
                connection.execute(
                    "UPDATE jira_site_selections SET encrypted_credentials='' "
                    "WHERE consumed_at IS NULL"
                )
                connection.execute("DELETE FROM jira_site_selections WHERE consumed_at IS NULL")
            connection.execute(
                """
                UPDATE integration_connections SET status=?, last_error=NULL, updated_at=?
                WHERE provider=?
                """,
                (ConnectionStatus.DISCONNECTED.value, _iso(), provider.value),
            )

    @staticmethod
    def state_hash(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    def create_oauth_state(
        self, state: str, provider: IntegrationProvider, user_id: str, expires_at: datetime
    ) -> None:
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO integration_oauth_states VALUES (?, ?, ?, ?, NULL)",
                (self.state_hash(state), provider.value, user_id, _iso(expires_at)),
            )

    def consume_oauth_state(
        self, state: str, provider: IntegrationProvider, user_id: str | None = None
    ) -> str:
        now = _now()
        with self.storage.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM integration_oauth_states WHERE state_hash=? AND provider=?",
                (self.state_hash(state), provider.value),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                raise OAuthStateError("Authorization state is missing, invalid, or already used.")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise OAuthStateError(
                    "Authorization state has expired. Start the connection again."
                )
            if user_id is not None and row["user_id"] != user_id:
                raise OAuthStateError("Authorization state does not belong to this session.")
            connection.execute(
                "UPDATE integration_oauth_states SET consumed_at=? WHERE state_hash=?",
                (_iso(now), row["state_hash"]),
            )
            return str(row["user_id"])

    def oauth_state_user(self, state: str, provider: IntegrationProvider) -> str:
        """Resolve a live state to the initiating user without consuming it."""

        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM integration_oauth_states WHERE state_hash=? AND provider=?",
                (self.state_hash(state), provider.value),
            ).fetchone()
        if (
            row is None
            or row["consumed_at"] is not None
            or datetime.fromisoformat(row["expires_at"]) <= _now()
        ):
            raise OAuthStateError("Authorization state is missing, expired, or already used.")
        return str(row["user_id"])

    def create_site_selection(
        self,
        user_id: str,
        encrypted_credentials: str,
        resources: list[dict[str, Any]],
        expires_at: datetime,
    ) -> str:
        selection_id = f"sel_{uuid4().hex}"
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO jira_site_selections VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    selection_id,
                    user_id,
                    encrypted_credentials,
                    json.dumps(resources, separators=(",", ":"), sort_keys=True),
                    _iso(expires_at),
                ),
            )
        return selection_id

    def site_selection(self, selection_id: str, user_id: str) -> tuple[str, list[dict[str, Any]]]:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jira_site_selections WHERE selection_id=? AND user_id=?",
                (selection_id, user_id),
            ).fetchone()
        if (
            row is None
            or row["consumed_at"] is not None
            or datetime.fromisoformat(row["expires_at"]) <= _now()
        ):
            raise OAuthStateError("Jira site selection is missing, expired, or already used.")
        return row["encrypted_credentials"], json.loads(row["resources_json"])

    def consume_site_selection(self, selection_id: str, user_id: str) -> None:
        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE jira_site_selections
                SET consumed_at=?, encrypted_credentials='', resources_json='[]'
                WHERE selection_id=? AND user_id=? AND consumed_at IS NULL
                """,
                (_iso(), selection_id, user_id),
            )
            if cursor.rowcount != 1:
                raise OAuthStateError("Jira site selection is missing or already used.")

    def record_delivery(self, delivery_id: str, event_type: str) -> bool:
        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO integration_webhook_deliveries
                    (provider, delivery_id, event_type, received_at)
                VALUES (?, ?, ?, ?)
                """,
                (IntegrationProvider.GITHUB.value, delivery_id, event_type, _iso()),
            )
            return cursor.rowcount == 1

    def audit(
        self,
        event_type: str,
        provider: IntegrationProvider,
        actor_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe = _safe_metadata(metadata or {})
        with self.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO integration_audit_events
                    (event_id, event_type, provider, actor_id, occurred_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    event_type,
                    provider.value,
                    actor_id,
                    _iso(),
                    json.dumps(safe, separators=(",", ":"), sort_keys=True),
                ),
            )

    def audit_events(self) -> list[dict[str, Any]]:
        with self.storage.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_audit_events ORDER BY sequence_id"
            ).fetchall()
        return [
            {
                "sequence_id": row["sequence_id"],
                "event_type": row["event_type"],
                "provider": row["provider"],
                "actor_id": row["actor_id"],
                "occurred_at": row["occurred_at"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _connection(row: sqlite3.Row) -> IntegrationConnection:
        return IntegrationConnection(
            id=row["connection_id"],
            provider=IntegrationProvider(row["provider"]),
            status=ConnectionStatus(row["status"]),
            display_name=row["display_name"],
            external_account_id=row["external_account_id"],
            external_account_name=row["external_account_name"],
            external_site_id=row["external_site_id"],
            external_site_url=row["external_site_url"],
            metadata=json.loads(row["metadata_json"]),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_checked_at=(
                datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None
            ),
            last_error=row["last_error"],
        )


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"access_token", "refresh_token", "client_secret", "private_key", "code"}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key.lower() in forbidden or "secret" in key.lower() or "token" in key.lower():
            continue
        if isinstance(item, dict):
            safe[key] = _safe_metadata(item)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[key] = item
        elif isinstance(item, list):
            safe[key] = [entry for entry in item if isinstance(entry, (str, int, float, bool))]
    return safe
