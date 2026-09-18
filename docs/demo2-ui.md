# Demo 2 — Web UI (Phase 1 + Phase 2)

Demo 2 adds a browser interface to the proven Demo 1 platform without replacing
its task lifecycle, storage, workspace, routing, executor, or LangGraph code.

- **Phase 1** (`demo2-phase1`): read-only FastAPI + React shell — task list,
  overview, Chat/Diff/Tests/Trace from persisted state.
- **Phase 2** (`demo2-phase2`): real task conversation — the browser can send a
  message that continues the task's shared TaskSession, with a durable FIFO
  queue, a background turn runner, and live updates over Server-Sent Events.

**The Platform Core remains the source of truth. The API is an interface layer.
The React UI is a presentation layer.**

## Architecture

```text
Browser (React, Vite dev server :5173)
   │
   ├── GET  /api/tasks/{id}/messages     durable conversation
   ├── POST /api/tasks/{id}/messages     the only browser mutation (202 Accepted)
   └── GET  /api/tasks/{id}/stream       SSE: live public events
   │
   ▼
FastAPI (127.0.0.1:8765)   routes are thin; presenters.py filters + redacts output
   │
   ▼
ConversationService (conversation.py)          TaskSessionService (sessions.py)
   │  validate, idempotency, acceptance rules      ▲
   │  HUMAN_MESSAGE event + queue row (1 txn)      │ continue_conversation()
   ▼                                               │ (same method the CLI uses)
SQLite: events (audit + conversation)          TaskTurnRunner (runner.py)
        message_queue (delivery state) ──claim──►  one turn per message, FIFO
                                                   │
                                                   ▼
                              writer lock → LangGraph → Claude executor
                                                   │
                                                   ▼
                                  same task workspace → pytest verification
                                                   │
                                                   ▼
                                           durable public events ──► SSE
```

The CLI and the browser share the **message core**:

```text
                  ┌── CLI:   ai-platform message  → record HUMAN_MESSAGE → continue_conversation()
TaskSessionService┤
                  └── HTTP:  POST /messages → record HUMAN_MESSAGE + queue row
                                           → TaskTurnRunner → continue_conversation()
```

`continue_conversation()` is the extracted tail of Demo 1's `message()`:
the same status checks, `_try_continuation`, executor preflight, one-writer lock,
`_run_agent_turn`, `run_task_graph(continuation=True)`, verification and event
recording. The CLI does not go through HTTP and does not use the queue; its
behavior is unchanged.

## Conversation source of truth

The conversation of a task **is its append-only event history**:
`HUMAN_MESSAGE` and `AGENT_MESSAGE` events (Demo 1's `TaskSession.conversation`).
Legacy Demo 1 messages therefore appear unchanged in `GET /messages`; nothing
is backfilled or rewritten.

Phase 2 adds one table, `message_queue`, created additively by
`CREATE TABLE IF NOT EXISTS` in `SQLiteStorage.initialize()`. It is **not** a
second copy of the conversation — it holds no message text. One row per browser
message records only what the event log cannot express:

| column | purpose |
| --- | --- |
| `client_message_id` (unique per task) | idempotency key from the browser |
| `event_sequence_id` (unique) | the HUMAN_MESSAGE event holding the text |
| `status` | `queued` / `running` / `completed` / `failed` |
| `execution_id` | the agent turn (same ID as the turn's events) |
| `error` | public reason when the message's turn could not run or failed |

The HUMAN_MESSAGE event and its queue row are written **in one SQLite
transaction**, so there is never a message without delivery state or vice versa.
Web messages carry `"channel": "web"` and `"message_id"` in their event metadata;
existing metadata keys and meanings are unchanged. Demo 1 code ignores the
extra table, so the database stays compatible with the frozen `demo-1` CLI.

## Message lifecycle

```text
POST ─► validate ─► HUMAN_MESSAGE event + queue row (QUEUED) ─► 202 Accepted
                                   │
                  runner claims oldest QUEUED message (atomic SQL)
                                   │
                                RUNNING ──► continue_conversation(execution_id,
                                   │         through_sequence_id = this message)
                   ┌───────────────┼─────────────────────────────┐
                   ▼               ▼                             ▼
              COMPLETED         FAILED                    back to QUEUED
       (turn ended WAITING_  (turn ended FAILED,     (another writer, e.g. a CLI
        FOR_HUMAN or PAUSED)  preflight error, task   turn, holds the lock: retried
                              no longer continuable,  on the next tick)
                              process stopped mid-turn)
```

- **Accepted states:** a message is accepted when the task is
  `WAITING_FOR_HUMAN` (a turn starts) or a turn is in progress
  (`ANALYZING`/`IMPLEMENTING`/`VERIFYING` — the message queues). `READY`,
  `PAUSED_BY_HUMAN`, `COMPLETED` and `FAILED` return **409** with a readable
  reason; start/resume/reset remain CLI actions.
- **Held:** a queued message waits while a turn is running or the task is
  `PAUSED_BY_HUMAN` (e.g. a human paused mid-turn from the CLI); it becomes
  eligible again when the task returns to `WAITING_FOR_HUMAN`.
- **Failed without a turn:** if the task becomes `COMPLETED`/`FAILED`/`READY`
  before the message's turn could run, the message is marked FAILED with that
  reason. The message itself always stays in the conversation.
- **Per-message context:** each queued message gets its own FIFO turn, whose
  "recent human instructions" end at that message (`through_sequence_id`).
  Agent replies recorded meanwhile are still included. CLI turns use the
  unchanged Demo 1 context window.

## One active writer per task

Nothing new writes a workspace: every turn goes through Demo 1's
`ExecutionLockManager` (atomic `UPDATE … WHERE active_execution IS NULL`). On
top of that:

- `claim_next_message` only claims when **no message of the task is RUNNING**
  (single SQL statement in an immediate transaction);
- the runner runs at most one worker thread per task;
- if the lock is taken by someone else (a CLI turn), the claim is returned to the
  queue — never a second writer.

Different tasks can run in parallel; one task never does.

## Idempotency

`client_message_id` (8–100 chars `[A-Za-z0-9_-]`, the UI uses a UUID) is
required. `(task_id, client_message_id)` is unique:

- a retry with the same key and text returns **202** with the original
  `message_id` and `"duplicate": true` — no second event, no second turn;
- the same key with different text returns **409**;
- the UI keeps a failed send's key, so "Retry" is always safe.

## Background runner

`TaskTurnRunner` is an in-process dispatcher thread (no Redis/Celery):

- enabled only with `AI_PLATFORM_ENABLE_RUNNER=1`; without it the API accepts
  and durably queues messages but never executes them (useful for inspection);
- polls the durable queue every second and is woken immediately after a POST;
- tests drive it synchronously (`run_pending()`) with fake executors.

**Restart behavior.** Accepted messages are durable before the 202 is sent.
QUEUED messages survive any restart and run when a runner-enabled API starts
again. If the process stops *during* a turn, the turn is interrupted (as with a
killed CLI turn): its writer lock becomes stale (recovered by the next CLI
command → `PAUSED_BY_HUMAN`), and on the next runner start that message is
marked FAILED ("The API process stopped before this message's agent turn
finished."). Run **at most one runner-enabled API process per runtime**.

## Server-Sent Events

`GET /api/tasks/{id}/stream` (`text/event-stream`), polling SQLite every 0.5 s:

```text
retry: 3000

id: 257
event: platform_event
data: {"sequence_id":257,"event_type":"AGENT_STARTED", ...}   same JSON as /events

event: conversation
data: {"revision":"3:2026-09-18T06:06:56+00:00"}               message states changed

event: heartbeat
data: {}                                                        every 10 s when idle
```

- **Ordering:** events are emitted strictly by durable `sequence_id`.
- **Cursor:** `?after=N` for the first connection; `Last-Event-ID` (sent
  automatically by `EventSource` on reconnect) takes precedence. Reconnecting
  with `Last-Event-ID: 201` resumes at the first event after #201 — no replay.
- **No in-memory pub/sub:** correctness depends only on the event table.
- **Heartbeat** is a named event (not a comment) so the browser can see it. The
  UI reconnects on its own from its last sequence if it hears nothing for 25 s
  or if `EventSource` gives up (e.g. a proxy answering 5xx while the API
  restarts). This was found during manual validation: the Vite dev proxy keeps a
  browser's stream open after the API dies.
- **Shutdown:** open streams never end on their own, so `ai-platform serve`
  bounds uvicorn's graceful shutdown to 3 s; clients then resume via the cursor.
- The same presenter as `/events` is used, so SSE can never expose more.

## Web actor (not authentication)

Phase 2 has a **single server-configured web actor**:

```sh
AI_PLATFORM_WEB_ACTOR=vakho      # 1-64 chars: letters, digits, . _ -
```

- every browser message is recorded as that actor; the request schema rejects
  any extra field (`actor_id` → 422), so the browser cannot choose an identity;
- if unset, messaging is disabled: POST returns **503** with a configuration
  message and the composer is read-only. It never falls back to the OS user
  (the dev API may run as root);
- **this is not authentication.** Anyone who can reach the API (loopback + SSH
  tunnel) acts as that actor. Multi-user identity and authentication belong to
  Phase 4.

## Output sanitization

`api/presenters.py` is the single exit path for public data (JSON and SSE):

1. **Metadata allowlist** (Phase 1): lock owner tokens, hostnames, PIDs, Claude
   `session_id`, workspace paths and tool inputs are never copied.
2. **Path redaction** (Phase 2) of this server's runtime locations inside the
   remaining text (pytest output, errors, message text):

| server path | shown as |
| --- | --- |
| `<workspace root>/<TASK-ID>…` | `<task-workspace>…` |
| workspace root | `<workspace-root>` |
| platform data directory (`/var/lib/ai-platform`) | `<platform-data>` |
| platform source checkout | `<platform-root>` |

Only those configured prefixes are replaced (whole path components); relative
paths such as `app/messages.py` and unrelated paths such as `/usr/lib/python3.12`
are kept. Persisted history is never rewritten. Historical verification
commands recorded by the Demo 1 install keep their interpreter path
(`/opt/ai-platform-demo/.venv/bin/python`); the UI shows it as `python`.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | `{"status":"ok"}` |
| `GET /api/config` | `messaging_enabled`, `web_actor`, `runner_enabled`, `max_message_length` |
| `GET /api/tasks` | sidebar summaries (+ `updated_at`) |
| `GET /api/tasks/{id}` | task detail + `agent_working`, `queued_messages`, `messaging {accepting, reason}` |
| `GET /api/tasks/{id}/events` | ordered public event history |
| `GET /api/tasks/{id}/diff` | workspace diff (same path as `ai-platform diff`) |
| `GET /api/tasks/{id}/messages` | ordered conversation: `id, role, actor_id, content, timestamp, sequence_id, turn_id, status, error, client_message_id, channel` |
| `POST /api/tasks/{id}/messages` | `{"message", "client_message_id"}` → **202** `{status:"accepted", message_id, client_message_id, task_id, message_status, duplicate}` |
| `GET /api/tasks/{id}/stream` | SSE (above) |

Errors: 404 unknown task · 409 state cannot receive messages / idempotency key
reused · 422 invalid body (empty, > 8000 chars, extra fields, bad key, bad
`Last-Event-ID`) · 503 messaging not configured · 500 `{"detail":"Internal server
error"}` with details only in the server log. `POST /messages` is the only
non-GET route (asserted by a test).

## UI

- **Sidebar:** task ID, status, title, difficulty, tier, writer, "updated …";
  refreshed on live events and every 30 s.
- **Chat (primary tab):** scrollable conversation (human right, agent left,
  actor, sequence, time, plain text with wrapping/newlines — React escaping,
  no Markdown), delivery badges (Queued / Agent turn running / Failed + reason),
  "Agent is working…" with queued count, composer (Enter sends, Shift+Enter
  newline, disabled with the server's reason when not accepting), optimistic
  pending messages with safe Retry.
- **Live updates:** every SSE event is appended to Trace/Tests immediately and
  triggers a debounced refetch of task detail, messages and task list — the
  backend stays the source of truth; React does not re-derive state. Diff is
  refetched after workspace-changing events. Switching tasks closes the old
  stream and opens a new one after the new task's history is loaded.
- **Header:** web actor, stream state (`live` / `reconnecting…`), Refresh.

## Run on the Demo VPS

Demo 2 lives in the worktree `/root/ai-platform-demo-demo2` (branch
`demo2-phase2`). `/opt/ai-platform-demo`, its `/usr/local/bin/ai-platform`
wrapper, and the `demo-1` tag are untouched.

One-time setup:

```sh
cd /root/ai-platform-demo-demo2
.venv/bin/python -m pip install -e ".[dev]"
cd web && npm install
```

Terminal A — API with messaging and the runner (loopback only, port 8765):

```sh
cd /root/ai-platform-demo-demo2
AI_PLATFORM_WEB_ACTOR=vakho AI_PLATFORM_ENABLE_RUNNER=1 ./scripts/serve_api_dev.sh
```

Without the two variables the API is read-only (Phase 1 behavior). The
launcher mirrors `/usr/local/bin/ai-platform` (`umask 0002`, shared runtime
under `/var/lib/ai-platform`); any `AI_PLATFORM_*` variable overrides it —
e.g. point `AI_PLATFORM_DATA_DIR`, `AI_PLATFORM_WORKSPACE_ROOT`,
`AI_PLATFORM_DB_PATH` and `AI_PLATFORM_CHECKPOINT_DB_PATH` at a copied runtime
to experiment safely. Starting Phase 2 against a runtime adds the empty
`message_queue` table to its database (additive; Demo 1 ignores it).

Terminal B — UI:

```sh
cd /root/ai-platform-demo-demo2/web
npm run dev
```

Workstation:

```sh
ssh -L 5173:127.0.0.1:5173 -L 8765:127.0.0.1:8765 root@<vps>
# open http://localhost:5173
```

Vite proxies `/api/*` (including the SSE stream) to `http://127.0.0.1:8765`
(override with `AI_PLATFORM_API_PROXY`); `VITE_API_BASE_URL` selects another
backend at build time. Port 8000 belongs to another service on this VPS.

## Code layout

```text
src/ai_platform/application.py     shared composition for CLI and API (+ injectable executor)
src/ai_platform/sessions.py        TaskSessionService: continue_conversation(), human_message_event()
src/ai_platform/conversation.py    ConversationService: submit/list, acceptance rules, idempotency
src/ai_platform/runner.py          TaskTurnRunner: durable FIFO claims, one turn per task at a time
src/ai_platform/storage.py         + message_queue table and atomic queue operations
src/ai_platform/api/presenters.py  metadata allowlist + PathRedactor + response builders
src/ai_platform/api/routes/        health, config, tasks, messages, stream
web/src/api/useTaskStream.ts       EventSource lifecycle, cursor, watchdog reconnect
web/src/components/ChatPanel.tsx   conversation + composer
```

## Quality checks

```sh
pytest                 # no test calls Claude; fake executors + isolated runtimes
ruff check .
git diff --check
cd web && npm run typecheck && npm run build
```

`tests/test_messaging.py` covers legacy conversation reads, acceptance, persist-
before-execute, actor spoofing, validation errors, disabled messaging,
idempotency (no duplicate event or turn), the blocking concurrency/FIFO scenario
(A RUNNING, B/C QUEUED, one writer), restart durability, failure handling,
interrupted turns, the unchanged CLI path, redaction, non-mutating GETs, and SSE
ordering/leak-freedom/`Last-Event-ID` resume/heartbeats over a real uvicorn
server.

## Current limitations (still CLI-only)

- start, pause, resume, approve, reject, reset, human shell, `attach --follow`;
- a task must have been started (and not be paused/completed/failed) to chat;
- agent text arrives when the SDK turn ends (Demo 1 persists activities after
  the call), not token by token; live progress comes from platform events;
- single web actor, no authentication; loopback + SSH tunnel only;
- one runner-enabled API process per runtime.

## Future / Phase 3+

- Browser task controls (start/pause/resume/approve/reject/reset) — Phase 3.
- Authentication, multi-user identity, authorization — Phase 4.
- Browser terminal / human shell — Phase 5.
- Persist agent activity incrementally during a turn (live tool activity).
- Optional stream of public assistant text if the SDK allows it without
  bypassing durable state; never hidden reasoning.
- Serve the built UI from FastAPI; production deployment with TLS.
- Generate TypeScript types from OpenAPI; ESLint and frontend tests.
- Markdown rendering for agent messages (sanitized).
- Generic (non-task) conversations, Jira/Slack/PR integrations.
