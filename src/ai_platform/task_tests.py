"""Task-owned test specifications, safe uploads, and workspace materialization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from uuid import uuid4

from ai_platform.models import TaskTestFile, TestFileSource

TASK_TEST_ROOT = Path(".ai-platform/tests")
UPLOADED_TEST_ROOT = TASK_TEST_ROOT / "uploaded"
GENERATED_TEST_ROOT = TASK_TEST_ROOT / "generated"
ALLOWED_TEST_EXTENSIONS = frozenset({".py", ".js", ".ts", ".json", ".yaml", ".yml"})
MAX_TEST_FILE_BYTES = 256 * 1024
MAX_TEST_FILES = 10
MAX_TOTAL_TEST_BYTES = 1024 * 1024
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class TaskTestError(RuntimeError):
    """A safe validation or materialization failure."""


@dataclass(frozen=True, slots=True)
class UploadedTestInput:
    filename: str
    content: str


def validate_uploads(files: list[UploadedTestInput]) -> list[UploadedTestInput]:
    if len(files) > MAX_TEST_FILES:
        raise TaskTestError(f"At most {MAX_TEST_FILES} test files may be uploaded")
    total = 0
    names: set[str] = set()
    validated: list[UploadedTestInput] = []
    for item in files:
        name = item.filename.strip()
        path = PurePath(name)
        if (
            not name
            or path.is_absolute()
            or len(path.parts) != 1
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or not _SAFE_FILENAME.fullmatch(name)
        ):
            raise TaskTestError(f"Unsafe test filename: {item.filename!r}")
        if Path(name).suffix.lower() not in ALLOWED_TEST_EXTENSIONS:
            raise TaskTestError(f"Unsupported test file type: {Path(name).suffix or '<none>'}")
        if name.casefold() in names:
            raise TaskTestError(f"Duplicate test filename: {name}")
        encoded = item.content.encode("utf-8")
        if len(encoded) > MAX_TEST_FILE_BYTES:
            raise TaskTestError(f"Test file is too large: {name}")
        if "\x00" in item.content:
            raise TaskTestError(f"Test file is not valid text: {name}")
        total += len(encoded)
        names.add(name.casefold())
        validated.append(UploadedTestInput(name, item.content))
    if total > MAX_TOTAL_TEST_BYTES:
        raise TaskTestError("Uploaded test files exceed the total size limit")
    return validated


def materialize_uploaded_tests(
    task_id: str,
    workspace: Path,
    files: list[UploadedTestInput],
    created_by: str,
) -> list[TaskTestFile]:
    """Write validated text files only under this task's platform-owned test tree."""

    validated = validate_uploads(files)
    root = (workspace / UPLOADED_TEST_ROOT).resolve()
    workspace = workspace.resolve()
    if not root.is_relative_to(workspace):
        raise TaskTestError("Task test directory escapes the workspace")
    root.mkdir(parents=True, exist_ok=True)
    records: list[TaskTestFile] = []
    for item in validated:
        destination = (root / item.filename).resolve()
        if destination.parent != root or destination.exists() or destination.is_symlink():
            raise TaskTestError(f"Test filename cannot be materialized safely: {item.filename}")
        destination.write_text(item.content, encoding="utf-8")
        relative = destination.relative_to(workspace).as_posix()
        records.append(
            TaskTestFile(
                file_id=f"testfile_{uuid4().hex}",
                task_id=task_id,
                filename=item.filename,
                relative_path=relative,
                source=TestFileSource.UPLOADED,
                content=item.content,
                created_by=created_by,
            )
        )
    return records


def task_test_targets(files: list[TaskTestFile]) -> list[str]:
    """Return executable pytest targets; non-Python uploads remain visible but inert."""

    return [item.relative_path for item in files if Path(item.filename).suffix.lower() == ".py"]


def rematerialize_task_tests(workspace: Path, files: list[TaskTestFile]) -> None:
    """Restore durable test files after a managed workspace reset."""

    workspace = workspace.resolve()
    for item in files:
        destination = (workspace / item.relative_path).resolve()
        if not destination.is_relative_to(workspace / TASK_TEST_ROOT):
            raise TaskTestError("Stored task test path escapes the platform test directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item.content, encoding="utf-8")
