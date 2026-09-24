# Demo 2.5 Phase 2 domain model

Phase 2 adds durable workflow state without running an autonomous phase workflow.
Lifecycle status continues to describe runtime/control state; `workflow_phase`
independently describes the task's position in:

`BRAINSTORM → PLAN → IMPLEMENTATION → REVIEW → HUMAN_REVIEW`

`COMPLETED` remains a lifecycle status and is not a workflow phase. Developers and
admins may manually move forward or backtrack between any two distinct phases. Each
change is an atomic compare-and-set and an append-only `WORKFLOW_PHASE_CHANGED` event.
Viewer roles cannot mutate workflow state. Automatic transitions are deliberately
disabled until Phase 3.

## Migration and reset

Existing static/legacy tasks migrate to `IMPLEMENTATION`. Existing registered tasks
migrate to `BRAINSTORM` only when they are still `READY`, have no attempts or selected
model, and have no task/agent/test execution event. All other registered tasks migrate
to `IMPLEMENTATION`. Initialization is additive, transactional, and repeat-safe.

Reset retains its existing workspace and lifecycle behavior. It does not change the
workflow phase or remove phase events, artifact versions, or checklist snapshots.

## Artifacts

Artifacts are append-only, task-scoped, and versioned per task and kind. A new version
points to the version it supersedes; older versions remain readable. Supported kinds
are `BRAINSTORM_SUMMARY`, `PLAN`, `IMPLEMENTATION_SUMMARY`, `REVIEW_REPORT`, and
`HUMAN_REVIEW_DECISION`. Each kind has a validated JSON contract. In particular, a
Plan requires `summary`, `files`, `steps`, `tests`, `risks`, and `open_questions`.
Canonical artifacts contain structured JSON, not executable HTML.

## Checklists and readiness

Checklist keys, labels, weights, and blocker flags are owned by platform code. A
submitted result supplies only `PASS`, `FAIL`, or `NEEDS_HUMAN` plus exposed evidence.
Omitted platform items resolve to `NEEDS_HUMAN` with explicit missing-result evidence.
Every evaluation is stored as a new immutable snapshot.

Readiness is deterministic:

`score = passed_weight / total_weight * 100`

An empty or zero-weight checklist scores `0` and is ineligible. Automatic progression
eligibility is true only when the score is at least `98`, there is no blocking `FAIL`,
and there is no blocking `NEEDS_HUMAN`. Phase 2 computes this decision but never acts
on it. A developer can still make an audited manual transition at any score.

## API surface

- Task list/detail and task creation responses include `workflow_phase`.
- `POST /api/tasks/{task_id}/phase` performs an authenticated manual transition.
- `GET|POST /api/tasks/{task_id}/artifacts` reads history/current versions and appends
  validated versions.
- `GET|POST /api/tasks/{task_id}/checklists` reads snapshots and appends evaluations.

Actor and producer identities always come from the authenticated server session.
