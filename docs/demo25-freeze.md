# Demo 2.5 Freeze

## Architecture and completed scope

Demo 2.5 is a React/FastAPI application over SQLite WAL, an append-only event log,
task-owned local Git workspaces, LangGraph checkpoints, an authenticated SSE stream,
and an allowlisted agent executor bridge. Phases 1–6 established durable tasks,
model routing, baseline-aware verification, workflow phases/readiness, collaboration,
authentication, commands, Claude adaptations, and the Chat/Activity workspace UI.
Phase 7 hardens removal discoverability, bounded Activity history, recovery/security
evidence, operations documentation, and release tests without redesigning that model.

Lifecycle states include READY, ANALYZING/RUNNING execution states,
WAITING_FOR_HUMAN, PAUSED_BY_HUMAN, FAILED, and COMPLETED. Workflow phases are
BRAINSTORM, PLAN, IMPLEMENTATION, REVIEW, and HUMAN_REVIEW. Terminal dispositions are
CONFIRMED and DEFERRED.

## Supported controls

UI/API controls are Start, Pause, Resume, Confirm (wire action `approve`), Reject,
Defer, Reset, messages, model preferences, and explicit permanent removal. Platform
slash commands are `/brainstorm`, `/plan`, `/implement`, `/review`, `/human-review`,
`/approve`, `/reject`, `/pause`, `/resume`, `/status`, `/tests`, and `/help`.
There is intentionally no deletion slash command. Supported Claude adaptations are
only `claude/help` and `claude/status`; the UI exposes disabled registry entries for
forbidden capabilities.

## Event and deletion contracts

The legacy `GET /api/tasks/{id}/events` full-list response remains compatible.
Activity uses `GET /api/tasks/{id}/events/page?order=desc&limit=50` with an exclusive
`before_sequence`; ascending consumers can use exclusive `after_sequence`. The limit
range is 1–200. SSE remains chronological, unpaginated, and uses Last-Event-ID.

Removal is never automatic. A task must be COMPLETED and CONFIRMED or DEFERRED, with
no active writer or queued instruction. The backend returns both `can_remove` and an
authoritative disabled reason. The UI always shows Remove task, requires permanent
confirmation, closes task state on success, and keeps a tombstone so seeded tasks do
not resurrect.

## Known limitations

- Chat is reconstructed efficiently from durable events/artifacts but remains an
  unbounded response and DOM timeline. Server-side Chat pagination is deferred; the
  measured Demo 2.5 histories do not justify changing projection semantics at freeze.
- The legacy events endpoint remains unbounded for compatibility. New browser code
  uses the bounded Activity endpoint; old callers must migrate deliberately.
- Authenticated task visibility is global. There is no tenant/project ACL layer.
- SQLite and local workspaces target a single trusted host. Guarantees are practical
  transactional/idempotent guarantees, not distributed exactly-once execution.
- A crash after remote provider side effects can require human inspection. A stale
  lease owned by another host is not automatically stolen.
- The local workspace provider is not a hostile-code container or egress sandbox.

## Deferred post-2.5

GitHub/Jira integrations, pull-request creation or automatic commit/push, Kubernetes
workspaces, a browser shell, Odoo sandboxing, new providers, Compound/institutional
memory, an Infra role, arbitrary Claude passthrough, multi-tenant RBAC, and all Phase 8
work are explicitly outside this freeze.
