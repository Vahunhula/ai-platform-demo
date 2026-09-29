"""Single-host execution leases, heartbeats, and safe stale-lock recovery."""

import os
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Event as ThreadEvent
from threading import Thread

from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import ExecutionKind, TaskRecord, TaskStatus
from ai_platform.storage import SQLiteStorage


class LockHealth(StrEnum):
    """Safety assessment for a persisted workspace-writer lease."""

    FREE = "free"
    ACTIVE = "active"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class LockInspection:
    """Human-readable lock state derived without mutating it."""

    health: LockHealth
    heartbeat_age_seconds: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class LockAcquisition:
    """Result of one atomic workspace-lock attempt."""

    acquired: bool
    owner_token: str
    execution_id: str
    current: TaskRecord | None = None
    recovered_stale_lock: bool = False


class ExecutionLockManager:
    """Coordinate one writer per task using a lightweight SQLite lease."""

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        heartbeat_seconds: int,
        stale_seconds: int,
        hostname: str | None = None,
        process_id: int | None = None,
        clock: Callable[[], datetime] | None = None,
        process_checker: Callable[[int], bool | None] | None = None,
    ) -> None:
        if heartbeat_seconds < 1 or stale_seconds <= heartbeat_seconds:
            raise ValueError("Lock stale time must be greater than its positive heartbeat time")
        self.storage = storage
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_seconds = stale_seconds
        self.hostname = hostname or socket.gethostname()
        self.process_id = process_id or os.getpid()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.process_checker = process_checker or process_is_running

    def acquire(
        self,
        task_id: str,
        kind: ExecutionKind,
        actor_id: str,
        execution_id: str,
        *,
        allowed_statuses: set[TaskStatus],
    ) -> LockAcquisition:
        """Acquire a free lock, safely recovering and retrying when state permits."""

        owner = f"{kind.value}:{self.hostname}:{self.process_id}:{execution_id}"
        acquired = self.storage.try_acquire_execution(
            task_id,
            kind,
            owner,
            allowed_statuses=allowed_statuses,
            execution_id=execution_id,
            actor_id=actor_id,
            process_id=self.process_id,
            hostname=self.hostname,
            acquired_at=self.clock(),
        )
        if acquired:
            return LockAcquisition(True, owner, execution_id)

        recovered = self.recover_stale_task(
            task_id,
            recovered_by=f"{actor_id}@{self.hostname}:{self.process_id}",
        )
        if recovered:
            acquired = self.storage.try_acquire_execution(
                task_id,
                kind,
                owner,
                allowed_statuses=allowed_statuses,
                execution_id=execution_id,
                actor_id=actor_id,
                process_id=self.process_id,
                hostname=self.hostname,
                acquired_at=self.clock(),
            )
            if acquired:
                return LockAcquisition(
                    True,
                    owner,
                    execution_id,
                    recovered_stale_lock=True,
                )
        return LockAcquisition(
            False,
            owner,
            execution_id,
            current=self.storage.get_task(task_id),
            recovered_stale_lock=recovered,
        )

    def inspect(self, record: TaskRecord) -> LockInspection:
        """Classify a lock conservatively using lease age and local process state."""

        if record.active_execution is None:
            return LockInspection(LockHealth.FREE, None, "No workspace writer is active")
        heartbeat = (
            record.execution_heartbeat_at or record.execution_started_at or record.updated_at
        )
        age = max(0.0, (self.clock() - heartbeat).total_seconds())
        if age <= self.stale_seconds:
            return LockInspection(LockHealth.ACTIVE, age, "Heartbeat is within the lease window")

        if record.execution_hostname and (
            record.execution_hostname.casefold() != self.hostname.casefold()
        ):
            return LockInspection(
                LockHealth.UNVERIFIABLE,
                age,
                "Heartbeat expired but its owner is on a different host",
            )
        if record.execution_pid is None:
            return LockInspection(
                LockHealth.STALE,
                age,
                "Heartbeat expired and no owner process was recorded",
            )
        running = self.process_checker(record.execution_pid)
        if running is True:
            return LockInspection(
                LockHealth.ACTIVE,
                age,
                "Heartbeat expired but the local owner process is still running",
            )
        if running is None:
            return LockInspection(
                LockHealth.UNVERIFIABLE,
                age,
                "Heartbeat expired but owner-process health could not be verified",
            )
        return LockInspection(
            LockHealth.STALE,
            age,
            "Heartbeat expired and the local owner process is not running",
        )

    def recover_stale_task(self, task_id: str, *, recovered_by: str) -> bool:
        """Recover one demonstrably stale lock and pause unknown workspace state."""

        record = self.storage.get_task(task_id)
        if record is None or record.active_execution is None or not record.execution_owner:
            return False
        inspection = self.inspect(record)
        if inspection.health is not LockHealth.STALE:
            return False
        audit_event = Event(
            task_id=task_id,
            event_type=EventType.STALE_LOCK_RECOVERED,
            actor_type=ActorType.SYSTEM,
            actor_id="execution-lock-manager",
            metadata={
                "lock_type": record.active_execution.value,
                "previous_owner": record.execution_actor_id or record.execution_owner,
                "previous_owner_token": record.execution_owner,
                "previous_execution_id": record.execution_id,
                "previous_pid": record.execution_pid,
                "previous_hostname": record.execution_hostname,
                "previous_heartbeat": record.execution_heartbeat_at.isoformat()
                if record.execution_heartbeat_at
                else None,
                "heartbeat_age_seconds": round(inspection.heartbeat_age_seconds or 0.0, 3),
                "recovered_by": recovered_by,
                "reason": inspection.reason,
            },
        )
        transition = self.storage.recover_execution(
            task_id,
            record.execution_owner,
            record.execution_heartbeat_at,
            audit_event=audit_event,
        )
        return transition is not None

    def recover_all_stale_locks(self, *, recovered_by: str) -> list[str]:
        """Recover safe stale locks during normal command startup."""

        recovered: list[str] = []
        for record in self.storage.list_tasks():
            if self.recover_stale_task(record.task_id, recovered_by=recovered_by):
                recovered.append(record.task_id)
        return recovered

    def refresh_heartbeat(self, task_id: str, owner_token: str) -> bool:
        """Refresh one owned lease using the manager's injectable clock."""

        return self.storage.heartbeat_execution(
            task_id,
            owner_token,
            heartbeat_at=self.clock(),
        )

    @contextmanager
    def heartbeat(self, task_id: str, owner_token: str) -> Iterator[None]:
        """Refresh a live lease in a daemon thread until the operation exits."""

        stopped = ThreadEvent()

        def refresh() -> None:
            while not stopped.wait(self.heartbeat_seconds):
                try:
                    if not self.refresh_heartbeat(task_id, owner_token):
                        return
                except Exception:
                    # A transient heartbeat failure must not hide the operation's real result.
                    continue

        thread = Thread(target=refresh, name=f"lock-heartbeat-{task_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=self.heartbeat_seconds + 1)


def process_is_running(process_id: int) -> bool | None:
    """Check a local process conservatively on Windows and POSIX without signalling it."""

    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_running(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _windows_process_is_running(process_id: int) -> bool | None:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
    if not handle:
        error = ctypes.get_last_error()
        return None if error == 5 else False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
