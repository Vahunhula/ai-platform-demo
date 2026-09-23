"""Durable, administrator-managed repository registry and trust-boundary validation."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ai_platform.storage import SQLiteStorage

_SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class RepositoryError(RuntimeError):
    """Safe repository-registry validation failure."""


@dataclass(frozen=True, slots=True)
class Repository:
    id: str
    slug: str
    display_name: str
    source_type: str
    source: str
    default_branch: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class RepositoryService:
    """Own registered local Git templates; browser clients only ever see IDs."""

    def __init__(self, storage: SQLiteStorage, workspace_root: Path) -> None:
        self.storage = storage
        self.workspace_root = workspace_root.resolve()

    def register_local(
        self, slug: str, display_name: str, source: Path, default_branch: str
    ) -> Repository:
        slug = slug.strip().lower()
        display_name = display_name.strip()
        source = source.expanduser().resolve()
        if not _SLUG.fullmatch(slug):
            raise RepositoryError("Slug must use lowercase letters, digits, '.', '_' or '-'")
        if not display_name or len(display_name) > 120 or "\n" in display_name:
            raise RepositoryError("Display name must be 1-120 characters on one line")
        self.validate_source(source)
        self.validate_branch(source, default_branch)
        now = datetime.now(UTC)
        repository = Repository(
            id="repo_" + uuid4().hex[:12],
            slug=slug,
            display_name=display_name,
            source_type="local_git",
            source=str(source),
            default_branch=default_branch,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.storage.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO repositories (
                        repository_id, slug, display_name, source_type, source,
                        default_branch, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        repository.id,
                        repository.slug,
                        repository.display_name,
                        repository.source_type,
                        repository.source,
                        repository.default_branch,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except Exception as error:
            if "UNIQUE constraint failed: repositories.slug" in str(error):
                raise RepositoryError(f"Repository slug {slug} already exists") from error
            raise
        return repository

    def list(self, *, enabled_only: bool = False) -> list[Repository]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self.storage.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM repositories {where} ORDER BY display_name, slug"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, repository_id: str, *, require_enabled: bool = False) -> Repository:
        with self.storage.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE repository_id = ?", (repository_id,)
            ).fetchone()
        if row is None:
            raise RepositoryError("Unknown repository")
        repository = self._from_row(row)
        if require_enabled and not repository.enabled:
            raise RepositoryError("Repository is disabled")
        return repository

    def disable(self, slug: str) -> Repository:
        now = datetime.now(UTC).isoformat()
        with self.storage.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE repositories SET enabled = 0, updated_at = ? WHERE slug = ?",
                (now, slug.strip().lower()),
            )
            if cursor.rowcount != 1:
                raise RepositoryError(f"Unknown repository slug {slug}")
            row = connection.execute(
                "SELECT * FROM repositories WHERE slug = ?", (slug.strip().lower(),)
            ).fetchone()
        return self._from_row(row)

    def validate_source(self, source: Path) -> None:
        if not source.is_dir():
            raise RepositoryError("Repository source directory does not exist")
        if source == Path("/") or source.is_relative_to(self.workspace_root):
            raise RepositoryError("Repository source is not an approved template location")
        git = shutil.which("git")
        if not git:
            raise RepositoryError("Git is required to register repositories")
        result = self._git(source, "rev-parse", "--show-toplevel")
        try:
            root = Path(result.stdout.strip()).resolve()
        except OSError as error:
            raise RepositoryError("Repository source has an invalid Git root") from error
        if not source.is_relative_to(root):
            raise RepositoryError("Repository source is not within its Git repository")

    def validate_branch(self, source: Path, branch: str) -> None:
        if not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch:
            raise RepositoryError("Base branch is not a safe branch name")
        self._git(source, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")

    @staticmethod
    def _git(source: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        git = shutil.which("git")
        if not git:
            raise RepositoryError("Git is required")
        repository_root = next(
            (candidate for candidate in (source, *source.parents) if (candidate / ".git").exists()),
            None,
        )
        if repository_root is None:
            raise RepositoryError("Repository source is not within a Git working tree")
        completed = subprocess.run(
            [git, "-c", f"safe.directory={repository_root}", *arguments],
            cwd=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if completed.returncode != 0:
            raise RepositoryError("Repository or branch could not be validated")
        return completed

    @staticmethod
    def _from_row(row) -> Repository:
        return Repository(
            id=row["repository_id"],
            slug=row["slug"],
            display_name=row["display_name"],
            source_type=row["source_type"],
            source=row["source"],
            default_branch=row["default_branch"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
