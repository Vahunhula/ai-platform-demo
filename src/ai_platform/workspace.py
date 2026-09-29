"""Replaceable task workspace abstraction and local Git-backed implementation."""

import json
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from abc import ABC, abstractmethod
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class WorkspaceError(RuntimeError):
    """A safe, user-facing workspace operation failure."""


class WorkspaceExistsError(WorkspaceError):
    """Raised when creation would overwrite an existing task workspace."""


class FileChange(BaseModel):
    """One Git-derived workspace change."""

    path: str
    change_type: str


class WorkspaceFileState(FileChange):
    """Git status plus a digest used to attribute manual changes."""

    digest: str


class WorkspaceSnapshot(BaseModel):
    """Compact state of every currently changed workspace file."""

    files: dict[str, WorkspaceFileState]

    @property
    def fingerprint(self) -> str:
        """Return a stable digest of changed paths, classifications, and contents."""

        payload = {
            path: state.model_dump(mode="json") for path, state in sorted(self.files.items())
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class WorkspaceProvider(ABC):
    """Lifecycle and inspection contract for task-owned workspaces."""

    @abstractmethod
    def create(self, task_id: str) -> Path:
        """Copy a clean source repository and return the new workspace path."""

    @abstractmethod
    def exists(self, task_id: str) -> bool:
        """Return whether a workspace already exists."""

    @abstractmethod
    def get_path(self, task_id: str) -> Path:
        """Return the path assigned to a task."""

    @abstractmethod
    def get_diff(self, task_id: str) -> str:
        """Return the current Git diff from the baseline commit."""

    @abstractmethod
    def get_changed_files(self, task_id: str) -> list[FileChange]:
        """Return Git-derived modified, added, and deleted files."""

    @abstractmethod
    def snapshot(self, task_id: str) -> WorkspaceSnapshot:
        """Capture enough Git state to attribute changes between two moments."""

    @abstractmethod
    def destroy(self, task_id: str) -> None:
        """Destroy only the validated task workspace."""


class LocalWorkspaceProvider(WorkspaceProvider):
    """Copy the demo repository into a task-owned local Git workspace."""

    def __init__(self, root: Path, source_repository: Path) -> None:
        self.root = root.resolve()
        self.source_repository = source_repository.resolve()

    def create(
        self,
        task_id: str,
        source_repository: Path | None = None,
        base_branch: str | None = None,
    ) -> Path:
        workspace = self.get_path(task_id)
        if workspace.exists():
            raise WorkspaceExistsError(
                f"{task_id} already has a workspace. Use 'ai-platform attach {task_id}' "
                f"or explicitly reset it with 'ai-platform reset {task_id}'."
            )
        source = (source_repository or self.source_repository).resolve()
        if not source.is_dir():
            raise WorkspaceError("Registered repository source does not exist")
        git = shutil.which("git")
        if not git:
            raise WorkspaceError("Git is required for task workspace tracking but was not found")

        self.root.mkdir(parents=True, exist_ok=True)
        try:
            if base_branch is None:
                shutil.copytree(
                    source,
                    workspace,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache"
                    ),
                )
            else:
                self._export_git_template(source, base_branch, workspace, git)
            self._prepare_shared_tree(workspace)
            self._initialize_git(workspace, git)
        except Exception:
            if workspace.exists() and workspace.parent.resolve() == self.root:
                shutil.rmtree(workspace)
            raise
        return workspace

    def exclude_platform_paths(self, task_id: str) -> None:
        """Keep platform-owned task tests out of the product repository diff."""

        workspace = self._existing_workspace(task_id)
        exclude = workspace / ".git" / "info" / "exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        rule = ".ai-platform/\n"
        if rule not in existing.splitlines(keepends=True):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                existing + ("" if not existing or existing.endswith("\n") else "\n") + rule
            )

    def _export_git_template(self, source: Path, branch: str, workspace: Path, git: str) -> None:
        """Export a registered directory exactly as it exists on a validated branch."""

        top = self._run_git(source, git, "rev-parse", "--show-toplevel").stdout.strip()
        repository_root = Path(top).resolve()
        relative = source.relative_to(repository_root)
        arguments = ["archive", "--format=tar", branch]
        if relative != Path("."):
            arguments.extend(["--", relative.as_posix()])
        archive = subprocess.run(
            [git, "-c", f"safe.directory={repository_root}", *arguments],
            cwd=repository_root,
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )
        if archive.returncode != 0:
            raise WorkspaceError("Git could not export the registered repository branch")
        temporary = Path(tempfile.mkdtemp(prefix=".provision-", dir=self.root))
        try:
            archive_path = temporary / "repository.tar"
            archive_path.write_bytes(archive.stdout)
            extracted = temporary / "content"
            extracted.mkdir()
            with tarfile.open(archive_path) as bundle:
                bundle.extractall(extracted, filter="data")
            exported = extracted / relative if relative != Path(".") else extracted
            if not exported.is_dir():
                raise WorkspaceError("Registered repository path is absent from the branch")
            shutil.move(str(exported), workspace)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def exists(self, task_id: str) -> bool:
        return self.get_path(task_id).is_dir()

    def get_path(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError(f"Unsafe task ID for workspace path: {task_id!r}")
        return self.root / task_id

    def get_diff(self, task_id: str) -> str:
        workspace = self._existing_workspace(task_id)
        tracked = self._git(workspace, "diff", "--no-ext-diff", "HEAD").stdout
        untracked = self._git(
            workspace, "ls-files", "--others", "--exclude-standard", "-z"
        ).stdout.split("\0")
        additions = [
            self._git_untracked_diff(workspace, relative_path)
            for relative_path in untracked
            if relative_path
        ]
        return tracked + "".join(additions)

    def get_changed_files(self, task_id: str) -> list[FileChange]:
        return [
            FileChange(path=state.path, change_type=state.change_type)
            for state in self.snapshot(task_id).files.values()
        ]

    def snapshot(self, task_id: str) -> WorkspaceSnapshot:
        workspace = self._existing_workspace(task_id)
        output = self._git(
            workspace,
            "-c",
            "status.renames=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout
        files: dict[str, WorkspaceFileState] = {}
        for entry in output.split("\0"):
            if len(entry) < 4:
                continue
            code = entry[:2]
            relative_path = entry[3:].replace("\\", "/")
            path = workspace / Path(relative_path)
            if code == "??" or "A" in code:
                change_type = "added"
            elif "D" in code:
                change_type = "deleted"
            else:
                change_type = "modified"
            digest = sha256(path.read_bytes()).hexdigest() if path.is_file() else "<missing>"
            files[relative_path] = WorkspaceFileState(
                path=relative_path,
                change_type=change_type,
                digest=digest,
            )
        return WorkspaceSnapshot(files=files)

    @staticmethod
    def changes_between(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> list[FileChange]:
        """Describe files whose Git state or content changed between snapshots."""

        changes: list[FileChange] = []
        for path in sorted(before.files.keys() | after.files.keys()):
            old = before.files.get(path)
            new = after.files.get(path)
            if old == new:
                continue
            change_type = new.change_type if new else "reverted"
            changes.append(FileChange(path=path, change_type=change_type))
        return changes

    def destroy(self, task_id: str) -> None:
        candidate = self.get_path(task_id)
        if candidate.is_symlink():
            raise WorkspaceError("Refusing to remove a symlinked workspace")
        workspace = candidate.resolve()
        if workspace != candidate or workspace.parent != self.root:
            raise WorkspaceError("Refusing to remove a path outside the workspace root")
        if (
            workspace == self.source_repository
            or workspace in self.source_repository.parents
            or self.source_repository in workspace.parents
        ):
            raise WorkspaceError("Refusing to remove the source repository")
        if workspace.exists():
            with suppress(FileNotFoundError):
                shutil.rmtree(workspace)

    def _existing_workspace(self, task_id: str) -> Path:
        workspace = self.get_path(task_id)
        if workspace.is_symlink() or workspace.resolve().parent != self.root:
            raise WorkspaceError("Refusing to use a symlinked or escaped workspace")
        if not workspace.is_dir():
            raise WorkspaceError(f"No workspace exists for {task_id}")
        return workspace

    def _initialize_git(self, workspace: Path, git: str) -> None:
        self._git(workspace, "init", executable=git)
        self._git(workspace, "config", "user.name", "AI Platform Demo", executable=git)
        self._git(workspace, "config", "user.email", "demo@localhost", executable=git)
        self._git(workspace, "add", ".", executable=git)
        self._git(workspace, "commit", "-m", "Baseline", executable=git)

    @staticmethod
    def _git_untracked_diff(workspace: Path, relative_path: str) -> str:
        """Render an untracked file without changing the workspace's Git index."""

        git = shutil.which("git")
        if not git:
            raise WorkspaceError("Git is required but was not found")
        completed = subprocess.run(
            [
                git,
                "-c",
                f"safe.directory={workspace.resolve()}",
                "diff",
                "--no-index",
                "--",
                "/dev/null",
                relative_path,
            ],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if completed.returncode not in {0, 1}:
            detail = (completed.stderr or completed.stdout).strip()[:1000]
            raise WorkspaceError(f"Git diff for untracked file failed: {detail}")
        return completed.stdout

    @staticmethod
    def _prepare_shared_tree(workspace: Path) -> None:
        """Allow trusted members of the workspace's inherited group to collaborate."""

        for path in (workspace, *workspace.rglob("*")):
            mode = path.stat().st_mode
            if path.is_dir():
                path.chmod(mode | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_ISGID)
            else:
                path.chmod(mode | stat.S_IRGRP | stat.S_IWGRP)

    @staticmethod
    def _git(
        workspace: Path, *arguments: str, executable: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        git = executable or shutil.which("git")
        if not git:
            raise WorkspaceError("Git is required but was not found")
        completed = subprocess.run(
            [git, "-c", f"safe.directory={workspace.resolve()}", *arguments],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:1000]
            raise WorkspaceError(f"Git {' '.join(arguments)} failed: {detail}")
        return completed

    @staticmethod
    def _run_git(
        workspace: Path, executable: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        repository_root = next(
            (
                candidate
                for candidate in (workspace, *workspace.parents)
                if (candidate / ".git").exists()
            ),
            workspace,
        )
        completed = subprocess.run(
            [executable, "-c", f"safe.directory={repository_root.resolve()}", *arguments],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError("Git repository inspection failed")
        return completed
