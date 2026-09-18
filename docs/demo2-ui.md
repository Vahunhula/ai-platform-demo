# Demo 2 — Web UI (Phases 1–4)

Demo 2 adds a browser interface to the proven Demo 1 platform without replacing
its task lifecycle, storage, workspace, routing, executor, or LangGraph code.

- **Phase 1** (`demo2-phase1`): read-only FastAPI + React shell — task list,
  overview, Chat/Diff/Tests/Trace from persisted state.
- **Phase 2** (`demo2-phase2`): real task conversation — the browser can send a
  message that continues the task's shared TaskSession, with a durable FIFO
  queue, a background turn runner, and live updates over Server-Sent Events.
- **Phase 3** (`demo2-phase3`): task lifecycle controls in the browser — start,
  pause, resume, approve, reject, reset — through the same core methods as the
  CLI commands of the same names. Shell/terminal stays CLI/SSH-only.
- **Phase 4** (`demo2-phase4`): real multi-user identity — CLI-provisioned users
  and access tokens, HttpOnly session cookies, viewer/developer/admin roles,
  per-user attribution and idempotency, presence, and database-enforced
  correctness across several API processes and runners.

**The Platform Core remains the source of truth. The API is an interface layer.
The React UI is a presentation layer.**

## Architecture

```text
Browser (React, Vite dev server :5173)
   │
   ├── Chat            GET/POST /api/tasks/{id}/messages
   ├── Task controls   POST /api/tasks/{id}/{start|pause|resume|approve|reject|reset}
   └── SSE             GET /api/tasks/{id}/stream   (one realtime channel for everything)
   │
   ▼
FastAPI (127.0.0.1:8765) — thin routes; presenters.py filters + redacts all output
   │
   ▼
Shared platform services
   ├── ConversationService (conversation.py)   chat: validation, idempotency, queue
   └── TaskControlService  (controls.py)        controls: actor, availability, idempotency
   │
   ▼
TaskSessionService (sessions.py) — the same methods the CLI calls
   │   prepare_start / prepare_resume / prepare_reject   (quick: checks, intent events, lock)
   │   run_prepared / continue_conversation               (long: the agent turn)
   │   pause / approve / reset                            (quick state changes)
   ▼
TaskTurnRunner (runner.py) — background threads; one turn per task at a time
   │
   ▼
writer lock → LangGraph → Claude executor → same task workspace → pytest verification
   │
   ▼
append-only events ──► SSE
```

The CLI and the browser share one lifecycle core:

```text
                    ┌── CLI:  ai-platform start|resume|reject → prepare_* → run_prepared (same process)
TaskSessionService ─┤          ai-platform pause|approve|reset → same method
                    └── HTTP: POST /start|/resume|/reject → prepare_* (lock held) → 202
                                                          → runner.run_prepared_turn()
                              POST /pause|/approve|/reset → same method → 200
```

Chat uses the Phase 2 path: the CLI's `message` and the browser runner both
end in `continue_conversation()`.

With Phase 4, several people share one TaskSession, possibly through several
API processes:

```text
Browser A (Vakho) ─┐
                   │
Browser B (Alex) ──┼── FastAPI process(es) ── 127.0.0.1, SSH tunnel
                   │      │
Browser C (An) ────┘      ├── AuthService        session cookie → AuthenticatedUser
                          ├── ConversationService (author = session user)
                          ├── TaskControlService  (actor = session user, role-checked)
                          └── SSE / Presence
                                  │
                                  ▼
                   TaskSession (one per task, shared by everyone)
                                  │
                                  ▼
                   writer lock → LangGraph → Agent → shared task workspace
```

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
  reason; use the lifecycle controls (start/resume/reset) for those states.
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

## One active writer per task (database-enforced)

Nothing new writes a workspace: every turn goes through Demo 1's
`ExecutionLockManager` (atomic `UPDATE … WHERE active_execution IS NULL`).
Since Phase 4, **every** correctness guarantee is a database operation, so any
number of API processes can share one runtime; there is no in-process lock:

| Operation | What makes it safe across processes |
| --- | --- |
| start | the atomic writer-lock acquire; nothing is written before it |
| resume / reject | the writer lock is taken **first**; only the winner then records `HUMAN_MESSAGE`/`HUMAN_RESUMED`/`HUMAN_REJECTED` (Phase 3 recorded them before the lock) |
| pause | `request_pause` is a compare-and-set: a concurrent second pause changes nothing and gets 409 |
| approve | one conditional `UPDATE` (status, verification, no writer, **no queued instruction**) |
| reset | holds the writer lock (Demo 1's `human_shell` kind) while deleting the workspace; state reset + lock release are one update |
| chat enqueue | the idempotency check, task-state check, event and queue row are one immediate transaction |
| queue claim | one conditional `UPDATE` per row (oldest QUEUED, none RUNNING for the task) |

The reordering of resume/reject changes only failure paths (e.g. a Claude
preflight failure no longer leaves a recorded-but-unanswered rejection); the
successful event sequence and all CLI output are unchanged. A second pause while
a pause request is pending is now refused instead of recording a duplicate.

Different tasks can run in parallel; one task never does.

## Idempotency (per user)

`client_message_id` (8–100 chars `[A-Za-z0-9_-]`, the UI uses a UUID) is
required and is scoped to its **author**: `(task_id, actor_id,
client_message_id)` is unique, where `actor_id` is the authenticated username.

- a retry by the same user with the same key and text returns **202** with the
  original `message_id` and `"duplicate": true` — no second event, no second turn;
- the same user reusing a key with different text gets **409**;
- another user's identical key is an unrelated message;
- the UI keeps a failed send's key, so "Retry" is always safe.

**Migration.** Phase 2 made `(task_id, client_message_id)` unique. SQLite cannot
drop a table constraint, so on first open Phase 4 copies `message_queue` row for
row into a table with the new constraint, in one transaction
(`_create_or_migrate_message_queue`). Every Phase 2 row already stores its
author in `actor_id`, so nothing is backfilled and no event is touched. The step
runs once (it checks the table's SQL); later opens change nothing — verified by
a test that builds a real Phase 2 database with the `5172e4a` storage code.

## Background runner

`TaskTurnRunner` is an in-process dispatcher (no Redis/Celery):

- enabled only with `AI_PLATFORM_ENABLE_RUNNER=1`; without it the API accepts
  and durably queues messages but never executes them (useful for inspection);
- polls the durable queue every second and is woken immediately after a POST;
- tests drive it synchronously (`run_pending()`) with fake executors.

**Several runners are supported (Phase 4).** Every runner-enabled API process
can share one runtime: a queue row is claimed by exactly one runner (atomic SQL),
each task still has one writer (the core lock), and a RUNNING claim is declared
"interrupted" only once it is older than the stale-lock threshold (default 60 s)
and does not hold the task's lock — so one process never fails a claim another
process made a moment ago (a real bug in the Phase 2 design, found in the Phase 4
audit and covered by a test). Validated with two real API processes.

**Restart behavior.** Accepted messages are durable before the 202 is sent.
QUEUED messages survive any restart and run when a runner-enabled API starts
again. If a process stops *during* a turn, the turn is interrupted (as with a
killed CLI turn): its writer lock becomes stale (recovered by the next CLI
command or lock attempt → `PAUSED_BY_HUMAN`) and the message is later marked
FAILED ("The API process stopped before this message's agent turn finished.").

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
- **Authentication:** the stream requires the session cookie (401 otherwise; no
  token in the URL) and re-checks it every heartbeat interval — revoking the
  session, disabling the user or expiry ends an open stream; reconnects get 401.
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

## Task lifecycle controls (Phase 3)

Each browser control calls the core method behind the CLI command of the same
name; nothing in FastAPI or React changes task state itself.

| Control | Core method | Allowed when | Effect (canonical events) | HTTP |
| --- | --- | --- | --- | --- |
| Start | `prepare_start` + `run_prepared` | `READY`, no workspace | `TASK_STARTED`, `STATUS_CHANGED`, `WORKSPACE_CREATED`, `MODEL_SELECTED`, `AGENT_*`, `TEST_*` → `WAITING_FOR_HUMAN`/`FAILED` | 202, turn runs in background |
| Pause | `pause` | started, not `COMPLETED`, not already paused / pause-requested | `HUMAN_PAUSED` (+ `STATUS_CHANGED`) | 200 |
| Resume | `prepare_resume` + `run_prepared` | `PAUSED_BY_HUMAN`, no writer | optional `HUMAN_MESSAGE`, `HUMAN_RESUMED`, continuation turn | 202 |
| Approve | `approve` (`approval.approve_task`) | `WAITING_FOR_HUMAN`, verification `PASSED`, no writer, no queued chat | `HUMAN_APPROVED`, `STATUS_CHANGED`, `TASK_COMPLETED{workspace_retained}` | 200 |
| Reject | `prepare_reject` + `run_prepared` | `WAITING_FOR_HUMAN`, no writer | `HUMAN_REJECTED`, `HUMAN_MESSAGE`, correction turn | 202 |
| Reset | `reset` | no writer; not already pristine `READY` | `WORKSPACE_RESET`, `TASK_RESET` | 200 |

**Semantics worth knowing** (unchanged from Demo 1):

- **Pause is cooperative.** With no agent running it takes effect at once. During
  an agent turn it only sets a pause request: the SDK client is interrupted
  cooperatively and the graph stops at its next safe point, then the task becomes
  `PAUSED_BY_HUMAN`. The UI shows "Pause requested" until then; it never fakes
  the paused state and never kills processes.
- **Reject is not failure.** It records the feedback and immediately runs a
  correction turn on the same workspace, followed by verification.
- **Approve completes the platform task only** — no commit, push, merge or
  deploy; the workspace is kept. Only a reset reopens a completed task.
- **A task cannot be approved while accepted human instructions are still
  queued.** Otherwise "Also fix X" could be accepted (QUEUED) and then orphaned
  by an immediate Approve, leaving a completed task that never ran the
  instruction. This is a platform rule in `TaskControlService`, reported as the
  approve action's reason and enforced (409) on the request.
- **Reset deletes the task workspace** (including uncommitted changes) and returns
  the task to `READY` with tier, attempts and verification cleared. The event
  history (trace, tests, conversation) is kept. Checkpoints are not deleted.

**Async start/resume/reject.** The request runs the quick `prepare_*` part —
the same checks, intent events and executor preflight as the CLI — and takes the
task's writer lock. Then it returns **202** and the runner runs the turn. No
turn needs a queue: they all require an idle task (otherwise 409), and holding
the lock before replying means an accepted turn can never be overtaken. If the
API process dies mid-turn, it is the existing stale-lock case (recovered by the
next CLI command or lock attempt → `PAUSED_BY_HUMAN`), exactly as for a killed
CLI turn. These three controls need `AI_PLATFORM_ENABLE_RUNNER=1`; without it
they are reported unavailable (503 if called).

**Idempotency.** Start/resume/reject require `client_action_id`. The execution
ID is derived from it and the **authenticated user**
(`uuid5(task, user_id, action, client_action_id)`) and stored by the core in the
writer lock and in every event of the turn. A retry by the same user finds that
execution and returns the original acceptance (`"duplicate": true`), even when
the two copies race through different API processes (the loser re-checks after
losing the lock). Another user's identical key is a different request and gets
the normal state conflict. Double clicks with different keys produce one 202
and 409s — by the writer lock alone. Pause/approve/reset rely on their atomic
preconditions (a repeat is a clean 409). No new table was needed. (Phase 3
execution IDs did not include the user; old retries simply no longer match.)

**Availability comes from the backend.** `GET /api/tasks/{id}` includes

```json
"actions": {
  "start":   {"allowed": false, "reason": "Task can only be started from READY (it is WAITING_FOR_HUMAN)."},
  "pause":   {"allowed": true,  "reason": null},
  "approve": {"allowed": true,  "reason": null},
  ...
}
```

computed in `TaskControlService` from the core's own preconditions; the same
check guards each request, and the core remains the final arbiter. React only
shows the allowed buttons (plus a "Why not…" list of reasons).

**Confirmation.** Resume (optional instruction), approve, reject (required
feedback) and reset open an in-app dialog. Reset lists exactly what is deleted
and what is kept, and its endpoint requires `{"confirm": true}` — the HTTP
equivalent of the CLI prompt. There is no force or other destructive option.

**Errors** are client-safe: e.g. "Task cannot be started because it is already
running.", "Task cannot be approved until it is waiting for human review.",
"Task is already paused.", "Another execution currently owns this task." (lock
details such as host names and PIDs are never returned).

## Identity and authentication (Phase 4)

**Why local access tokens.** A three-person internal tool needs real, distinct
identities now; company SSO can come later. Phase 4 therefore ships a small local
provider behind a narrow boundary:

```text
access token ──► POST /api/auth/login ──► AuthService.login ──► web session
                                                                   │  (HttpOnly cookie)
request ──► require_user ──► AuthService.resolve(cookie) ──► AuthenticatedUser
                                                                   │  .human (actor)
                                  ConversationService / TaskControlService / core
```

`TaskSessionService`, `ConversationService`, `TaskControlService` and the event
model only see an `AuthenticatedUser` / `HumanIdentity`. Replacing the provider
with OIDC/SSO means replacing `AuthService.login` (and the login screen) — task
semantics do not change.

**Users.** `users(user_id, username, display_name, role, enabled, created_at)`.
`user_id` is a UUID used for sessions, tokens, presence and control idempotency.
The `username` is immutable, unique and is the **actor ID** written into events,
so provisioning `vakho`, `alex`, `an` matches the Demo 1 history (whose actors
are those OS usernames). `display_name` is presentation only; events also record
the display name at the time. No user is hard-coded.

**Roles.** Checked centrally (`Role.can_modify_tasks`, `require_developer`):

| Role | Can |
| --- | --- |
| viewer | read tasks, conversation, diff, tests, trace; SSE; presence |
| developer | + send messages, start, pause, resume, approve, reject, reset |
| admin | same task capabilities as developer (user management stays in the CLI) |

**Provisioning (CLI only, no web admin):**

```sh
ai-platform users add --username vakho --display-name "Vakho" --role developer
ai-platform users list
ai-platform auth-token create vakho      # prints ap_… once; never shown again
ai-platform auth-token list              # ids and status only
ai-platform auth-token revoke tok_…      # no new logins + ends the sessions it created
ai-platform users disable vakho          # blocks login, ends all sessions; history kept
ai-platform users enable vakho
ai-platform users logout-all vakho       # ends every session of the user
```

**Secrets.** Access tokens (`ap_` + 256 random bits) and session tokens (256
bits) come from Python's `secrets`. Only SHA-256 digests are stored (a fast hash
is appropriate for high-entropy random tokens; there are no human passwords);
the login check uses `hmac.compare_digest`. Plaintext tokens exist only in the
CLI output and in the cookie — never in the database, events, logs or API
responses. Login failures are one generic `401 Invalid credentials` (a wrong
token and an unknown or disabled user look the same).

**Sessions.** `web_sessions(session_id, user_id, token_id, session_hash,
created_at, expires_at, revoked_at)`. Lifetime `AI_PLATFORM_SESSION_HOURS`
(default **12 h**). Cookie `ai_platform_session`: `HttpOnly`,
`SameSite=Strict`, `Path=/`, and `Secure` when `AI_PLATFORM_COOKIE_SECURE=1`
(off by default only because the demo is plain HTTP through an SSH tunnel —
**production requires HTTPS and the Secure cookie**). Logout revokes the
session. **Revocation policy:** revoking an access token blocks new logins *and*
ends the sessions created with it; disabling a user or `logout-all` ends all of
the user's sessions immediately. Expired or revoked sessions get 401 and the UI
returns to the login screen; task state is unaffected.

**Authorization** is enforced server-side and kept separate: 401 (no/invalid
session) → 403 (role) → 409 (task state). All `/api` routes except
`/api/health` and `/api/auth/login|logout` require a session; hidden buttons are
presentation only.

**Cross-site protection.** The session cookie is `SameSite=Strict`, the UI and
API are same-origin (the Vite proxy preserves `Host`), there is no CORS, and a
middleware refuses any non-GET `/api` request whose `Sec-Fetch-Site` is
`cross-site` or whose `Origin` differs from the request's `Host` (unless listed
in `AI_PLATFORM_ALLOWED_ORIGINS`). Limitation: this relies on modern browser
headers and cookie rules; there is no separate CSRF token.

**No spoofing.** Request schemas forbid unknown fields (`actor_id`, `role`,
`command`, `force` → 422); headers such as `X-User` are ignored. The author of a
message or action is always the session user. `AI_PLATFORM_WEB_ACTOR` is gone:
the API logs a warning if it is still set and never falls back to it, to the OS
user or to root.

## Presence and collaboration (Phase 4)

**Presence** (`task_presence(task_id, user_id, last_seen)`) answers "who is
viewing this task". The UI heartbeats `POST /api/tasks/{id}/presence` every 15 s
while a task is open (a POST, so GETs stay observational); a user is present
while their last heartbeat is younger than **45 s**. Old rows are deleted lazily
on later heartbeats (no cron). Presence is ephemeral and is **never written to
the event log**. It exposes only user id, username and display name — no
session, IP or host data; disabled users drop out.

**Collaboration.** Everyone sees the one TaskSession: the same SSE events, the
same conversation and trace, actor-attributed ("Vakho started the task", "Alex
paused the task", "An resumed the task"). Chat shows the author of each human
message (your own on the right). Stale UI is normal: if Alex clicks Approve
after Vakho already approved, the server answers 409 with a short reason and the
UI refetches — it shows COMPLETED; nothing breaks. Event and message responses
include `actor_display_name` (the name recorded on the event, else the user's
current name, else the actor ID) next to the unchanged `actor_id`.

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
| platform data directory (e.g. `/var/lib/ai-platform-demo2`) | `<platform-data>` |
| platform source checkout | `<platform-root>` |

Only those configured prefixes are replaced (whole path components); relative
paths such as `app/messages.py` and unrelated paths such as `/usr/lib/python3.12`
are kept. Persisted history is never rewritten. Historical verification
commands recorded by the Demo 1 install keep their interpreter path
(`/opt/ai-platform-demo/.venv/bin/python`); the UI shows it as `python`.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | `{"status":"ok"}` (no session needed) |
| `POST /api/auth/login` | `{"username","token"}` → user + `Set-Cookie` (401 `Invalid credentials`) |
| `GET /api/auth/me` | current user `{id, username, display_name, role, can_modify_tasks}` |
| `POST /api/auth/logout` | revokes the session, clears the cookie (204) |
| `POST /api/tasks/{id}/presence` | heartbeat; returns current viewers |
| `GET /api/tasks/{id}/presence` | current viewers `{user_id, username, display_name}` |
| `GET /api/config` | `runner_enabled`, `max_message_length`, `presence_heartbeat_seconds` |
| `GET /api/tasks` | sidebar summaries (+ `updated_at`) |
| `GET /api/tasks/{id}` | task detail + `agent_working`, `queued_messages`, `messaging {accepting, reason}` |
| `GET /api/tasks/{id}/events` | ordered public event history |
| `GET /api/tasks/{id}/diff` | workspace diff (same path as `ai-platform diff`) |
| `GET /api/tasks/{id}/messages` | ordered conversation: `id, role, actor_id, content, timestamp, sequence_id, turn_id, status, error, client_message_id, channel` |
| `POST /api/tasks/{id}/messages` | `{"message", "client_message_id"}` → **202** `{status:"accepted", message_id, client_message_id, task_id, message_status, duplicate}` |
| `GET /api/tasks/{id}/stream` | SSE (above) |
| `POST /api/tasks/{id}/start` | `{"client_action_id"}` → **202** |
| `POST /api/tasks/{id}/resume` | `{"client_action_id", "message"?}` → **202** |
| `POST /api/tasks/{id}/reject` | `{"client_action_id", "message"}` → **202** |
| `POST /api/tasks/{id}/pause` | no body → **200** (`deferred: true` when the agent is mid-turn) |
| `POST /api/tasks/{id}/approve` | no body → **200** |
| `POST /api/tasks/{id}/reset` | `{"confirm": true}` → **200** |

Control responses: `{status: "accepted"|"completed", action, task_id,
task_status, execution_id, client_action_id, duplicate, deferred}`.

Errors: 401 no/expired session or invalid login · 403 viewer role or cross-site
request · 404 unknown task · 409 action/message not valid in the current state,
another execution owns the task, or idempotency key reused · 422 invalid body (empty, > 8000 chars, extra fields, bad key, bad
`Last-Event-ID`) · 503 runner disabled
(turn-launching controls) or agent executor unavailable · 500 `{"detail":"Internal server
error"}` with details only in the server log. `POST /messages`, the six
control routes, presence heartbeats and login/logout are the only non-GET routes
(asserted by a test). There is no
shell, terminal, filesystem or command endpoint.

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
- **Login screen (Phase 4):** username + access token. The token is kept only in
  component state for the request (never in local/session storage) and cleared.
- **Header:** current user, role (viewers see "read-only access"), Log out,
  stream state (`live` / `reconnecting…`), Refresh.
- **Presence:** "Viewing" initials + names in the task header.
- **Task header (Phase 3):** status, live chips (Agent working, Pause requested,
  queued messages, writer, verification, model), and only the lifecycle buttons
  the backend allows, with "Why not…" reasons for the rest. Each action shows
  working → accepted/error feedback; retryable failures keep their idempotency
  key. State and buttons then update from SSE without a page refresh.

## Run on the Demo VPS

Demo 2 lives in the worktree `/root/ai-platform-demo-demo2`. `/opt/ai-platform-demo`,
its `/usr/local/bin/ai-platform` wrapper, and the `demo-1` tag are untouched.

**Two runtimes, never mixed:**

```text
/var/lib/ai-platform         Demo 1 — FROZEN (used only by /usr/local/bin/ai-platform)
/var/lib/ai-platform-demo2   Demo 2 — users, auth, resets, showcase (this checkout)
```

`/var/lib/ai-platform-demo2` was created once as an exact `cp -a` copy of the
Demo 1 runtime (same owners, `ai-platform` group, setgid directories and
modes; identical history), then initialized with the Phase 4 schema. Everything
Demo 2 does from now on happens there.

**Runtime helper.** `scripts/demo2-env.sh` sets the four runtime paths (plus the
task file and demo repository of this checkout) to the Demo 2 runtime. Variables
that are already set win, so a copied test runtime can still be used, but any
path inside `/var/lib/ai-platform` is **refused** unless
`AI_PLATFORM_ALLOW_DEMO1_RUNTIME=1` is set deliberately. Source it before every
manual CLI command from this checkout:

```sh
cd /root/ai-platform-demo-demo2
. scripts/demo2-env.sh        # prints: demo2-env: runtime /var/lib/ai-platform-demo2 …
.venv/bin/ai-platform users list
```

Without it, a manual `.venv/bin/ai-platform` call uses the repository-relative
`data/` defaults (never Demo 1). The launcher sources the same helper, so it can
no longer fall back to `/var/lib/ai-platform`.

One-time setup:

```sh
cd /root/ai-platform-demo-demo2
.venv/bin/python -m pip install -e ".[dev]"
cd web && npm install
```

Users (already provisioned on the Demo 2 runtime: `vakho`, `alex`, `an`, all
developers). Tokens are created by an administrator and handed to their owner
privately — each is printed once:

```sh
cd /root/ai-platform-demo-demo2
. scripts/demo2-env.sh
.venv/bin/ai-platform auth-token create vakho
.venv/bin/ai-platform auth-token create alex
.venv/bin/ai-platform auth-token create an
# more users: .venv/bin/ai-platform users add --username NAME --display-name "Name" --role developer
```

Terminal A — API (loopback only, port 8765, Demo 2 runtime by default):

```sh
cd /root/ai-platform-demo-demo2
AI_PLATFORM_ENABLE_RUNNER=1 ./scripts/serve_api_dev.sh
```

Terminal B — UI:

```sh
cd /root/ai-platform-demo-demo2/web
npm run dev
```

Workstation (each developer):

```sh
ssh -L 5173:127.0.0.1:5173 root@<vps>
# open http://localhost:5173 and sign in with username + access token
# (a second identity: use a private/incognito window)
```

Phase 4 code adds `users`, `auth_tokens`, `web_sessions` and `task_presence`
and creates or migrates `message_queue` in the runtime it opens — additive, and
the frozen Demo 1 CLI still works on such a database (verified), but this is why
Demo 2 never opens the Demo 1 runtime. To experiment without touching the
showcase runtime, point `AI_PLATFORM_DATA_DIR` (and the three other paths) at a
copy. A second API process (e.g. `AI_PLATFORM_API_PORT=8766
./scripts/serve_api_dev.sh`) can share the runtime, with or without its own
runner. Even with authentication, keep the API on 127.0.0.1 behind the SSH
tunnel — this is not a public deployment.

Vite proxies `/api/*` (including SSE) to `http://127.0.0.1:8765` (override with
`AI_PLATFORM_API_PROXY`) and **preserves the browser's `Host`**, which the
same-origin check requires. Port 8000 belongs to another service on this VPS.

## Code layout

```text
src/ai_platform/application.py     shared composition for CLI and API (+ injectable executor)
src/ai_platform/sessions.py        TaskSessionService: continue_conversation(), human_message_event()
src/ai_platform/conversation.py    ConversationService: submit/list, acceptance rules, idempotency
src/ai_platform/controls.py        TaskControlService: availability, roles, controls, action idempotency
src/ai_platform/auth.py            AuthService (replaceable provider): users, tokens, sessions
src/ai_platform/presence.py        PresenceService: TTL presence outside the event log
src/ai_platform/runner.py          TaskTurnRunner: durable FIFO claims, one turn per task at a time
src/ai_platform/storage.py         + message_queue table and atomic queue operations
src/ai_platform/api/presenters.py  metadata allowlist + PathRedactor + response builders
src/ai_platform/api/security.py    require_user (401), require_developer (403), same-origin middleware
src/ai_platform/api/routes/        health, auth, config, tasks, messages, controls, presence, stream
web/src/api/useTaskStream.ts       EventSource lifecycle, cursor, watchdog reconnect
web/src/components/ChatPanel.tsx   conversation + composer
web/src/components/TaskControls.tsx lifecycle buttons, dialogs, feedback
web/src/components/Login.tsx       sign-in (token never stored in the browser)
web/src/api/usePresence.ts         presence heartbeat
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
server. `tests/test_controls.py` covers every control's happy and invalid paths,
async start/resume, idempotent retries, parallel start/resume clicks (one turn),
chat-during-start, cooperative pause, approve refused mid-turn or with queued
chat, reject as a correction turn, reset confirmation/semantics, availability
per state, disabled controls without actor/runner, lock-detail redaction, and a
full start → chat → pause → resume → approve loop. `tests/test_auth.py` covers
hashing (no plaintext token or session in the database), cookie flags, generic
login failures, `/auth/me`, logout, expiry, disable, token and session
revocation, 401 everywhere except health/login, viewer 403s, actor spoofing,
cross-site refusal, secrets absent from events/logs, and the provisioning CLI.
`tests/test_multiuser.py` covers per-user message/control idempotency, the real
Phase 2 → Phase 4 migration (idempotent, rows preserved), start/resume races
through two independently composed API processes, a same-user duplicate racing
through both, two runners draining one queue, fresh claims not stolen, presence
TTL/ephemerality, per-user trace attribution with a stale approve, and SSE for
two viewers plus revoked streams (open and new).

## Current limitations

- **Still CLI/SSH-only:** human shell (`ai-platform shell`), `attach --follow`,
  `doctor`, user/token management; no browser terminal, file editor or commands.
- Local access tokens, not SSO; no login rate limiting (tokens are 256-bit
  random); cross-site protection relies on SameSite + Origin/Sec-Fetch-Site.
- Loopback + SSH tunnel only; plain HTTP in the demo (Secure cookie off).
- Presence refreshes with the ~15 s heartbeat, not instantly.
- Agent text arrives when the SDK turn ends, not token by token.
- Start/resume/reject need `AI_PLATFORM_ENABLE_RUNNER=1` on the API process
  that receives them.
- SQLite remains the single runtime store (fine for a few users and processes).

## Future / Phase 5+

- Company SSO/OIDC replacing the local token provider (same `AuthService` boundary).
- Engineering workspace experience; browser terminal / human shell — Phase 5.
- Polish, security review, HTTPS deployment and freeze — Phase 6.
- Persist agent activity incrementally during a turn (live tool activity).
- Optional stream of public assistant text if the SDK allows it without
  bypassing durable state; never hidden reasoning.
- Serve the built UI from FastAPI; production deployment with TLS.
- Generate TypeScript types from OpenAPI; ESLint and frontend tests.
- Markdown rendering for agent messages (sanitized).
- Generic (non-task) conversations, Jira/Slack/PR integrations.
