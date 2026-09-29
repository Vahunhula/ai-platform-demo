"""Web authentication for Demo 2: provisioned users, access tokens and sessions.

The boundary the rest of the platform sees is small:

    AuthenticatedUser  — who is making a request (id, username, display name, role)
    AuthService        — login(username, token) → session; resolve(session) → user

TaskSessionService, ConversationService and TaskControlService never import this
module's storage details; the HTTP layer resolves an ``AuthenticatedUser`` and
passes it on. A later company SSO/OIDC provider replaces ``login`` (and how the
session is created) without touching task semantics.

Secrets: access tokens and session tokens are 256-bit values from ``secrets``.
Only their SHA-256 digests are stored; high-entropy random tokens do not need a
slow password hash. Plaintext values exist only in the creating call's result.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from ai_platform.identity import HumanIdentity
from ai_platform.storage import SQLiteStorage

ACCESS_TOKEN_PREFIX = "ap_"
_USERNAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class Role(StrEnum):
    VIEWER = "viewer"
    DEVELOPER = "developer"
    ADMIN = "admin"

    @property
    def can_modify_tasks(self) -> bool:
        """Send messages and use lifecycle controls (all task mutations)."""

        return self in {Role.DEVELOPER, Role.ADMIN}


class AuthError(RuntimeError):
    """Base class for authentication/user-management errors."""


class InvalidCredentialsError(AuthError):
    """Generic login failure: never says which part was wrong."""

    def __init__(self) -> None:
        super().__init__("Invalid credentials")


class UserManagementError(AuthError):
    """Invalid provisioning request (CLI)."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """The server-side identity of one request; never taken from request data."""

    user_id: str
    username: str
    display_name: str
    role: Role

    @property
    def human(self) -> HumanIdentity:
        """The core's actor. ``username`` is immutable, so it is the stable actor ID."""

        return HumanIdentity(actor_id=self.username, display_name=self.display_name)


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    username: str
    display_name: str
    role: Role
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token_id: str
    username: str
    secret: str  # shown once, never stored


@dataclass(frozen=True, slots=True)
class NewSession:
    user: AuthenticatedUser
    secret: str  # goes into the HttpOnly cookie, never stored
    expires_at: datetime


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class AuthService:
    """Local provider: CLI-provisioned users and access tokens, database sessions."""

    def __init__(self, storage: SQLiteStorage, *, session_ttl: timedelta) -> None:
        self.storage = storage
        self.session_ttl = session_ttl

    # ---- provisioning (CLI) ----------------------------------------------

    def add_user(self, username: str, display_name: str, role: Role) -> UserRecord:
        username = username.strip().lower()
        display_name = display_name.strip()
        if not username or len(username) > 64 or not set(username) <= _USERNAME_CHARACTERS:
            raise UserManagementError(
                "Username must be 1-64 characters of lowercase letters, digits, '.', '_' or '-'"
            )
        if not display_name or len(display_name) > 100 or "\n" in display_name:
            raise UserManagementError("Display name must be 1-100 characters on one line")
        with self.storage.transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise UserManagementError(f"User {username} already exists")
            record = UserRecord(str(uuid4()), username, display_name, role, True, _now())
            connection.execute(
                """
                INSERT INTO users (user_id, username, display_name, role, enabled, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (record.user_id, username, display_name, role.value, record.created_at.isoformat()),
            )
        return record

    def list_users(self) -> list[UserRecord]:
        with self.storage.transaction() as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self._user(row) for row in rows]

    def assignable_users(self) -> list[UserRecord]:
        """Return enabled users with developer capability, never auth material."""

        return [user for user in self.list_users() if user.enabled and user.role.can_modify_tasks]

    def assignable_user(self, user_id: str) -> UserRecord:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ? AND enabled = 1", (user_id,)
            ).fetchone()
        if row is None:
            raise UserManagementError("Unknown or disabled assignee")
        user = self._user(row)
        if not user.role.can_modify_tasks:
            raise UserManagementError("Assignee must have developer capability")
        return user

    def set_enabled(self, username: str, enabled: bool) -> UserRecord:
        """Disabling also revokes every session of the user immediately."""

        user = self._user_by_name(username)
        now = _now().isoformat()
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE users SET enabled = ? WHERE user_id = ?", (int(enabled), user.user_id)
            )
            if not enabled:
                connection.execute(
                    "UPDATE web_sessions SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user.user_id),
                )
        return self._user_by_name(username)

    def revoke_sessions(self, username: str) -> int:
        user = self._user_by_name(username)
        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (_now().isoformat(), user.user_id),
            )
        return cursor.rowcount

    def create_token(self, username: str) -> IssuedToken:
        user = self._user_by_name(username)
        secret = ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = "tok_" + secrets.token_hex(6)
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO auth_tokens (token_id, user_id, token_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_id, user.user_id, _digest(secret), _now().isoformat()),
            )
        return IssuedToken(token_id, user.username, secret)

    def list_tokens(self) -> list[tuple[str, str, datetime, datetime | None]]:
        """(token_id, username, created_at, revoked_at) — never hashes."""

        with self.storage.transaction() as connection:
            rows = connection.execute(
                """
                SELECT t.token_id, u.username, t.created_at, t.revoked_at
                FROM auth_tokens t JOIN users u ON u.user_id = t.user_id
                ORDER BY u.username, t.created_at
                """
            ).fetchall()
        return [
            (
                row["token_id"],
                row["username"],
                datetime.fromisoformat(row["created_at"]),
                datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            )
            for row in rows
        ]

    def revoke_token(self, token_id: str) -> None:
        """Revoking a token blocks new logins and ends the sessions it created."""

        now = _now().isoformat()
        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE auth_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (now, token_id),
            )
            if cursor.rowcount != 1:
                raise UserManagementError(f"No active token {token_id}")
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
                (now, token_id),
            )

    # ---- login / sessions (HTTP) -----------------------------------------

    def login(self, username: str, token: str) -> NewSession:
        """Exchange a valid access token for a new web session, or fail generically."""

        presented = _digest(token)
        with self.storage.transaction() as connection:
            rows = connection.execute(
                """
                SELECT u.*, t.token_id, t.token_hash FROM users u
                JOIN auth_tokens t ON t.user_id = u.user_id
                WHERE u.username = ? AND u.enabled = 1 AND t.revoked_at IS NULL
                """,
                (username.strip().lower(),),
            ).fetchall()
        match = next(
            (row for row in rows if hmac.compare_digest(row["token_hash"], presented)), None
        )
        if match is None:
            raise InvalidCredentialsError()
        user = self._authenticated(match)
        secret = secrets.token_urlsafe(32)
        created = _now()
        expires = created + self.session_ttl
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO web_sessions
                    (session_id, user_id, token_id, session_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    user.user_id,
                    match["token_id"],
                    _digest(secret),
                    created.isoformat(),
                    expires.isoformat(),
                ),
            )
        return NewSession(user, secret, expires)

    def resolve(self, session_secret: str | None) -> AuthenticatedUser | None:
        """Return the user of a live session, or None (unknown/expired/revoked/disabled)."""

        if not session_secret:
            return None
        with self.storage.transaction() as connection:
            row = connection.execute(
                """
                SELECT u.* FROM web_sessions s JOIN users u ON u.user_id = s.user_id
                WHERE s.session_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                  AND u.enabled = 1
                """,
                (_digest(session_secret), _now().isoformat()),
            ).fetchone()
        return self._authenticated(row) if row else None

    def logout(self, session_secret: str | None) -> None:
        if not session_secret:
            return
        with self.storage.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE web_sessions SET revoked_at = ? "
                "WHERE session_hash = ? AND revoked_at IS NULL",
                (_now().isoformat(), _digest(session_secret)),
            )

    def users_by_id(self, user_ids: list[str]) -> dict[str, AuthenticatedUser]:
        if not user_ids:
            return {}
        placeholders = ", ".join("?" for _ in user_ids)
        with self.storage.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM users WHERE enabled = 1 AND user_id IN ({placeholders})",
                user_ids,
            ).fetchall()
        return {row["user_id"]: self._authenticated(row) for row in rows}

    def display_names(self) -> dict[str, str]:
        """username → current display name, for events that recorded no name."""

        with self.storage.transaction() as connection:
            rows = connection.execute("SELECT username, display_name FROM users").fetchall()
        return {row["username"]: row["display_name"] for row in rows}

    # ---- helpers ----------------------------------------------------------

    def _user_by_name(self, username: str) -> UserRecord:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
            ).fetchone()
        if row is None:
            raise UserManagementError(f"Unknown user {username}")
        return self._user(row)

    @staticmethod
    def _user(row) -> UserRecord:
        return UserRecord(
            row["user_id"],
            row["username"],
            row["display_name"],
            Role(row["role"]),
            bool(row["enabled"]),
            datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _authenticated(row) -> AuthenticatedUser:
        return AuthenticatedUser(
            row["user_id"], row["username"], row["display_name"], Role(row["role"])
        )
