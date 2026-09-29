# Demo 2.5.2 pre-change audit

Captured 2026-09-29 before production mutation.

- Development checkout before update: `6d3fd00b80d49003dad4ce9eb09d9c82eb89501b` (clean, two commits behind).
- `origin/main`: `e859e1491e07000fb9f5d1d2e46bf8920e671523`.
- Production checkout: `/root/ai-platform-demo-demo2`, commit `e859e1491e07000fb9f5d1d2e46bf8920e671523`.
- Production service: `ai-platform-demo2.service` (active); runtime data: `/var/lib/ai-platform-demo2`.
- Workspace root: `/var/lib/ai-platform-demo2/workspaces`.
- Platform DB: `/var/lib/ai-platform-demo2/platform.db` (`PRAGMA integrity_check`: `ok`).
- LangGraph DB: `/var/lib/ai-platform-demo2/langgraph-checkpoints.db` (`PRAGMA integrity_check`: `ok`).
- Registered repository: `repo_39b86b41fe90`, `python-demo`, local Git source
  `/root/ai-platform-demo-demo2/demo_repo`, branch `demo2-runtime-setup`.
- Release tags: `demo-1`, `demo-2.5-phase5`, `demo-2.5-phase6`, `demo-2.5`.
- Production workspaces: `DEMO-1`, `TASK-1DB7CA8B`, `TASK-5E7E079A`,
  `TASK-73890D97`, `TASK-DF6180E2`.

## Tasks and classification

| Task | Title | Class/reason |
|---|---|---|
| DEMO-1 | Fix welcome typo | predefined local demo (`tasks.json`) |
| DEMO-2 | Fix discount calculation | predefined local demo (`tasks.json`) |
| DEMO-3 | Unify user display name formatting | predefined local demo (`tasks.json`) |
| TASK-1DB7CA8B | Isolation Smoke A | automated Phase 1.1 isolation smoke; description explicitly identifies it |
| TASK-5E7E079A | Isolation Smoke B | automated Phase 1.1 isolation smoke; description explicitly identifies it |
| TASK-73890D97 | Addition | retained normal user task |
| TASK-DF6180E2 | Implement Script | retained normal user task |

## Shared-test provenance

The registered source contains these Git-tracked files:

- `demo_repo/tests/test_messages.py` — DEMO-1-only failing fixture.
- `demo_repo/tests/test_discounts.py` — DEMO-2-only failing fixture.
- `demo_repo/tests/test_users.py` — DEMO-3-only failing fixture.

They entered Git in foundation commit `5bcf7eb`. `TaskCreationService` resolves the
registered base branch to a commit and calls `LocalWorkspaceProvider.create`.
`LocalWorkspaceProvider._export_git_template` uses `git archive` for the registered
`demo_repo` subdirectory and moves the complete export into the new workspace.
Therefore all three tracked files are copied as baseline content. No bootstrap,
seed, or post-creation hook generates or inserts them.

Checkpoint rows before cleanup: DEMO-1 70/425 checkpoints/writes, DEMO-2
69/443, TASK-1DB7CA8B 6/41, TASK-73890D97 55/371, TASK-DF6180E2 12/92.
DEMO-3 and TASK-5E7E079A have no checkpoint rows.
