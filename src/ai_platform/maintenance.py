"""Bounded maintenance for obsolete local demos and proven isolation-smoke tasks."""

from dataclasses import dataclass

from ai_platform.auth import AuthenticatedUser
from ai_platform.models import TaskRecord
from ai_platform.removal import TaskRemovalService
from ai_platform.storage import SQLiteStorage


@dataclass(frozen=True, slots=True)
class LegacyCleanupCandidate:
    record: TaskRecord
    reason: str
    deletion_method: str = "TaskRemovalService (trusted legacy classification)"


class LegacyCleanupService:
    """Classify narrowly and delegate all resource deletion to TaskRemovalService."""

    def __init__(self, storage: SQLiteStorage, removal: TaskRemovalService) -> None:
        self.storage = storage
        self.removal = removal

    def candidates(self) -> list[LegacyCleanupCandidate]:
        candidates = []
        for record in self.storage.list_tasks():
            if record.task_id in {"DEMO-1", "DEMO-2", "DEMO-3"} and record.created_by is None:
                candidates.append(LegacyCleanupCandidate(record, "predefined local demo"))
            elif record.title.startswith("Isolation Smoke ") and (
                record.description or ""
            ).startswith("Production Phase 1.1 workspace isolation smoke"):
                candidates.append(
                    LegacyCleanupCandidate(record, "proven Phase 1.1 isolation smoke")
                )
        return candidates

    def remove(self, candidate: LegacyCleanupCandidate, actor: AuthenticatedUser) -> None:
        self.storage.classify_legacy_cleanup_candidate(candidate.record.task_id)
        self.removal.remove(candidate.record.task_id, actor)
