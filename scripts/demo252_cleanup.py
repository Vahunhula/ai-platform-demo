"""Audit or apply the bounded Demo 2.5.2 legacy-task cleanup."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ai_platform.application import create_application_context
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.maintenance import LegacyCleanupService
from ai_platform.removal import TaskRemovalService

PRODUCTION_ROOT = Path("/var/lib/ai-platform-demo2")


def _backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as incoming, sqlite3.connect(destination) as outgoing:
        incoming.backup(outgoing)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args()
    context = create_application_context()
    runtime = context.settings.data_dir.resolve()
    if runtime == PRODUCTION_ROOT and not args.allow_production:
        parser.error("production cleanup requires --allow-production")
    removal = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    )
    cleanup = LegacyCleanupService(context.storage, removal)
    candidates = cleanup.candidates()
    table = Table(
        "task_id",
        "title",
        "lifecycle",
        "workflow_phase",
        "workspace",
        "reason",
        "deletion method",
    )
    for item in candidates:
        record = item.record
        table.add_row(
            record.task_id,
            record.title,
            record.status.value,
            record.workflow_phase.value,
            str(context.workspaces.get_path(record.task_id)),
            item.reason,
            item.deletion_method,
        )
    Console().print(table)
    if not args.apply:
        return
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = runtime / "backups"
    _backup(context.settings.db_path, backup_root / f"platform-pre-demo252-{timestamp}.db")
    _backup(
        context.settings.checkpoint_db_path,
        backup_root / f"checkpoints-pre-demo252-{timestamp}.db",
    )
    actor = AuthenticatedUser("maintenance", "maintenance", "Maintenance", Role.ADMIN)
    for item in candidates:
        cleanup.remove(item, actor)
    for database in (context.settings.db_path, context.settings.checkpoint_db_path):
        with sqlite3.connect(database) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Integrity check failed for {database}: {result}")


if __name__ == "__main__":
    main()
