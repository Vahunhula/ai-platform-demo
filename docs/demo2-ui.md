# Demo 2 — Phase 1 UI

Demo 2 Phase 1 adds browser visibility to the proven Demo 1 platform. It does
not replace the task lifecycle, storage, workspace, routing, executor, or
LangGraph code.

## Architecture

```text
React presentation layer
        |
        | GET /api/*
        v
FastAPI interface layer
        |
        v
TaskSessionService / Platform Core
        |                 |
        v                 v
SQLite task/events    Git workspace
```

The Platform Core remains the source of truth. The API is an interface layer.
The React UI is a presentation layer. FastAPI receives an application context
from the same composition function as the CLI and delegates reads to
`TaskSessionService`. The service continues to use the existing SQLite storage
and `LocalWorkspaceProvider`; the diff endpoint therefore uses the same Git
diff implementation as `ai-platform diff`.

The API is intentionally read-only. Its Pydantic schemas form an explicit
public contract. Event responses use a metadata allowlist and do not expose
workspace paths, lock ownership tokens, process/host details, credentials,
provider session IDs, or hidden reasoning. The task detail returns the task ID
as a safe workspace identifier when a workspace exists, never its filesystem
path.

## Run on the Demo VPS

The Demo 2 work lives on branch `demo2-phase1` in a separate worktree
(`/root/ai-platform-demo-demo2`). The hosted Demo 1 checkout in
`/opt/ai-platform-demo` and its `/usr/local/bin/ai-platform` wrapper are left
untouched, so the Demo 1 CLI keeps working exactly as before.

One-time setup (from the Demo 2 checkout):

```sh
cd /root/ai-platform-demo-demo2
python3 -m venv .venv            # skip if .venv already exists
.venv/bin/python -m pip install -e ".[dev]"
cd web && npm install
```

Terminal A — API (loopback only, port 8765):

```sh
cd /root/ai-platform-demo-demo2
./scripts/serve_api_dev.sh
```

`scripts/serve_api_dev.sh` mirrors `/usr/local/bin/ai-platform`: `umask 0002`
and the shared runtime under `/var/lib/ai-platform`, so the API reads the
existing Demo 1 database and workspaces instead of creating new state. It then
runs `ai-platform serve --host 127.0.0.1 --port 8765`. Port 8000 is **not**
used because another service on this VPS already owns it. Override the port
with `AI_PLATFORM_API_PORT`, or any runtime path with the usual
`AI_PLATFORM_*` variables.

Terminal B — UI:

```sh
cd /root/ai-platform-demo-demo2/web
npm run dev
```

Open `http://localhost:5173`. Both servers bind to `127.0.0.1` only; from a
workstation, tunnel them:

```sh
ssh -L 5173:127.0.0.1:5173 -L 8765:127.0.0.1:8765 root@<vps>
```

The Vite dev server proxies same-origin `/api/*` to `http://127.0.0.1:8765`
(override with `AI_PLATFORM_API_PROXY`). The browser never talks to Claude or
to anything other than this backend. A separately hosted backend can be
selected at build time with `VITE_API_BASE_URL`; no VPS address is hard-coded.

Starting the API does **not** run stale-lock recovery (unlike CLI startup):
browsing must never change task state. It only runs the same idempotent schema
check and `INSERT OR IGNORE` task registration as every CLI command.

## Code layout

```text
src/ai_platform/application.py   shared composition used by CLI and API
src/ai_platform/sessions.py      TaskSessionService (+ read accessors, TaskNotFoundError)
src/ai_platform/api/app.py       FastAPI factory, error handlers
src/ai_platform/api/schemas.py   Pydantic response contracts
src/ai_platform/api/routes/      health.py, tasks.py (GET only)
web/src/types/api.ts             TypeScript mirror of schemas.py
web/src/api/client.ts            fetch wrapper (relative /api, optional base URL)
web/src/components/              sidebar, overview, Chat/Diff/Tests/Trace tabs
scripts/serve_api_dev.sh         dev launcher against the shared runtime
```

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Process health (`{"status":"ok"}`) |
| `GET /api/tasks` | Sidebar task summaries from persisted state |
| `GET /api/tasks/{task_id}` | Definition and current TaskSession summary |
| `GET /api/tasks/{task_id}/events` | Ordered public append-only event history |
| `GET /api/tasks/{task_id}/diff` | Current task workspace Git diff |

All routes are `GET`; any other method returns 405. Unknown task IDs return
404 (`TaskNotFoundError`), other safe platform errors 400, validation errors
422. Any unexpected failure (e.g. a Git error) returns
`{"detail": "Internal server error"}` with status 500; the traceback and any
paths stay in the server log only.

## UI

The desktop shell contains a task sidebar and selected-task overview. It shows
status, difficulty, current model, attempt, writer, workspace presence,
verification status, description, and acceptance criteria from persisted data.

The four Phase 1 tabs are:

- **Chat:** persisted `HUMAN_MESSAGE` and `AGENT_MESSAGE` events only; there is
  no fabricated conversation and no input box.
- **Diff:** the current text returned by the shared workspace diff path.
- **Tests:** persisted `TEST_STARTED`, `TEST_PASSED`, and `TEST_FAILED`
  evidence: command, exit code, duration, and the stored pytest output.
- **Trace:** every public event in SQLite sequence order with a one-line,
  human-readable summary derived only from persisted metadata (for example
  `MODEL_SELECTED — cheap / haiku`) and an expandable metadata view.

Chat needs nothing new from the event model for read-only display: the
platform already persists `HUMAN_MESSAGE` and `AGENT_MESSAGE` events. Phase 2
needs a write path (`POST` message through `TaskSessionService.message`) plus
authenticated identity before an input box can exist.

Refresh is manual. Browsing does not invoke Claude.

## Quality checks

```sh
pytest
ruff check .
git diff --check
cd web
npm run typecheck
npm run build
```

No ESLint is configured (kept out to keep dependencies minimal); `tsc` runs in
strict mode with unused-code checks.

API tests inject a temporary deterministic application context. They cover
health, listing, known and unknown task details, event ordering and filtering,
the shared diff path, and the guarantee that GET requests do not mutate task
records or history. No automated test calls Claude.

## Phase 1 limitations

All control remains CLI-only: start, message, pause, resume, reject, approve,
reset, shell, and live `attach --follow`. The browser has no shell or arbitrary
file-path endpoint and does not communicate with Claude directly.

There is no authentication/SSO, authorization layer, SSE, WebSocket, Redis,
worker, terminal, Jira integration, PR automation, deployment architecture, or
new sandbox. This development server should remain bound to loopback and be
accessed through an SSH tunnel until a later phase adds production security.

## Future / Phase 2+

- Add authenticated browser actions only after authorization semantics exist.
- Add SSE for event updates; `SQLiteStorage.get_events_after(sequence_id)` already
  provides the cursor an SSE endpoint needs.
- Define controlled interactive terminal and approval boundaries separately.
- Add production serving, TLS, SSO/RBAC, and deployment operations.
- Consider richer diff and test views after the read-only contracts stabilize.
- Generate TypeScript types from the OpenAPI schema instead of hand-mirroring.
- Serve the built `web/dist` from FastAPI (single process) for deployment.
- Add ESLint and a small frontend test setup.
- Sanitize absolute server paths (e.g. pytest `rootdir:` in persisted test
  output) before the API is reachable beyond an SSH tunnel.
