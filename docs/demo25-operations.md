# Demo 2.5 Operations Runbook

This runbook describes the frozen Demo 2.5 architecture. Substitute an isolated
runtime for development and tests. The showcase runtime is `/var/lib/ai-platform-demo2`;
never point development commands at it.

## Architecture and paths

The React/Vite static application calls a FastAPI process. FastAPI and the CLI share
SQLite (`platform.db`), task workspaces, and a separate LangGraph checkpoint SQLite
file. SQLite WAL transactions are authoritative for events, message delivery,
idempotency, phase transitions, presence, and the one-writer lease. SSE polls the
durable event sequence; it is not an in-memory event bus.

Runtime paths are configured by `AI_PLATFORM_DATA_DIR`, `AI_PLATFORM_DB_PATH`,
`AI_PLATFORM_CHECKPOINT_DB_PATH`, `AI_PLATFORM_WORKSPACE_ROOT`,
`AI_PLATFORM_TASK_FILE`, and `AI_PLATFORM_DEMO_REPO`. Do not put the database or
workspace root inside a source repository. Do not expose either SQLite file through
the web server.

The established showcase defaults are `/var/lib/ai-platform-demo2/platform.db`,
`/var/lib/ai-platform-demo2/langgraph-checkpoints.db`, and
`/var/lib/ai-platform-demo2/workspaces/`. Confirm the live unit environment rather
than assuming defaults.

## Start, restart, status, and health

For an isolated checkout, create a private temporary runtime and export all four
runtime paths before running `scripts/serve_api_dev.sh`. Enable the runner with
`AI_PLATFORM_ENABLE_RUNNER=1` only when browser-triggered turns are intended.
The development listener is loopback-only by default.

Use the environment's service manager to start, restart, and inspect the configured
service; first verify its working directory and environment point at the intended
release and runtime. A restart is safe between SQLite transactions. On startup the
runner performs stale-writer recovery; plain API-only processes intentionally do not
steal leases.

```bash
systemctl status ai-platform-demo2.service
sudo systemctl restart ai-platform-demo2.service
journalctl -u ai-platform-demo2.service --since '15 minutes ago'
```

These are operator examples, not development commands; Phase 7 validation uses only
temporary runtimes and must not restart the showcase service.

Health checks:

```bash
curl --fail --silent http://127.0.0.1:8765/api/health
sqlite3 "$AI_PLATFORM_DB_PATH" 'PRAGMA quick_check;'
```

Use `ai-platform doctor`, `ai-platform tasks`, and `ai-platform show TASK-ID` for
executor, lock, and task state. Never paste access tokens, cookies, provider session
IDs, or full environment dumps into incident logs.

## SQLite backup and integrity

Stop writers or use SQLite's online backup command; never copy only the main file
while WAL writes are active.

```bash
sqlite3 "$AI_PLATFORM_DB_PATH" ".backup '/safe/private/platform-YYYYMMDD.db'"
sqlite3 "$AI_PLATFORM_CHECKPOINT_DB_PATH" ".backup '/safe/private/checkpoints-YYYYMMDD.db'"
sqlite3 "$AI_PLATFORM_DB_PATH" 'PRAGMA foreign_key_check; PRAGMA integrity_check;'
```

Back up the workspace tree consistently with the databases. Restore into a new,
empty runtime first and run both integrity checks before switching configuration.
Restoring only one database or only workspaces can produce missing checkpoint or
workspace state. Backups contain task text and authentication hashes and must be
access-controlled.

## Frontend and logs

Run `npm ci`, `npm test`, `npm run typecheck`, and `npm run build` under `web/`.
Deploy only the resulting `web/dist` static files using the established web-server
procedure. The API must remain same-origin (or use an explicitly configured allowed
origin), and `AI_PLATFORM_COOKIE_SECURE=1` is required behind HTTPS.

Inspect application/service logs around turn start/end, phase transitions, writer
acquisition or recovery, executor failures, SSE authentication, and task removal.
Server errors returned to browsers are generic. Logs must not be widened to include
cookies, tokens, provider credentials, hidden reasoning, or the full environment.

## Recovery

- Runner unavailable: preserve the queued message, restore the runner, then let
  `TaskTurnRunner` claim it. Reusing its idempotency key does not create a second
  human message.
- Stale writer: confirm the recorded local PID is dead. Start the normal runner/CLI
  recovery path. A recovered task becomes `PAUSED_BY_HUMAN`; inspect Changes and
  explicitly Resume. A lease from another host is not automatically stolen.
- WAITING_FOR_HUMAN: restart, reopen Chat, answer the durable question, and let the
  queued continuation run. Do not reset the task.
- Missing workspace: do not create directories manually. Inspect task state and the
  configured workspace root. Reset only when its destructive semantics are intended.
- Removal interrupted: a task with `removal_started_at` is hidden and rejects new
  mutations/SSE. Retry the same DELETE as a developer; external cleanup and metadata
  deletion are idempotent and task-scoped.
- SSE reconnect loop: verify session validity, `/api/health`, proxy buffering, idle
  timeout, and `X-Accel-Buffering: no`. The browser reconnects from its last sequence;
  inspect the Activity page API separately from SSE.

## Rollback

Stop the service, retain a forensic copy/online backup, restore the matching previous
code and database/workspace/checkpoint backup into an empty runtime, run integrity
and health checks, then start. Schema migrations are additive, but an older binary is
not assumed to understand future data. Never roll back by deleting rows, WAL files,
workspaces, or tombstones by hand.
