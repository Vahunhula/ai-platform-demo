# Demo 2.5 Security Boundaries

## Authentication, roles, and browser identity

Users and access tokens are provisioned outside the browser. Access and session
tokens are high-entropy random values; only SHA-256 digests are stored. Login errors
are generic. The browser receives a `HttpOnly`, `SameSite=Strict` session cookie;
production HTTPS must enable `Secure`. Logout, token revocation, user disabling, and
expiry invalidate sessions.

Roles are viewer, developer, and admin. Viewers can read the globally visible task
catalog but cannot create, message, control, configure, or remove tasks. Demo 2.5
deliberately has global visibility for authenticated users; it is not a tenant or
project authorization system. Developer and admin currently have the same task
mutation capability. Every browser actor is resolved from the server-side session.
Extra actor fields are rejected and actor-like headers are ignored.

Mutation requests are protected by the strict cookie plus same-origin checks using
`Origin` and `Sec-Fetch-Site`. Deployments using a distinct trusted origin must list
it explicitly. SSE requires the same authenticated session, revalidates it while
open, reads only the requested task ID, and ends on revocation or task deletion.

## Workspace and repository isolation

The registered repository is an administrator-controlled path. Browser input selects
an opaque repository ID and a validated Git branch, never a host path. Task IDs use a
restricted character set. `LocalWorkspaceProvider` derives paths below one resolved
workspace root, uses argument-vector subprocess calls with `shell=False`, and refuses
unsafe IDs, parent/root deletion, symlinked workspace deletion, and deletion of the
registered source repository. Diff, reset, execution, and removal resolve through
that provider. Each removal and checkpoint cleanup is keyed to exactly one task.

This local provider is a process/filesystem isolation boundary for a trusted demo
host, not a hostile-code sandbox. Implementation-phase model code can write within
its task working directory; operating-system/container isolation is deferred.

## Workflow and command boundaries

Brainstorm, Plan, and Review are read-only and fail their gate if the workspace
changes. Review is a fresh invocation over structured upstream artifacts.
Implementation is write-enabled only in the task workspace. Human Review never
starts an autonomous model turn; a normal message remains durable without invoking
AI. Phase changes use compare-and-set transitions and readiness gates.

Platform commands are registry-backed: phase commands, approve, reject, pause,
resume, status, tests, and help. Arguments are parsed, not passed to a shell. There
is no `/delete` command. Claude capabilities are an independent allowlist:
`claude/help` and `claude/status` are safe adaptations. Global auth/config,
permission-mode changes, session deletion, MCP administration, and arbitrary
`claude/<text>` execution are disabled.

## Removal, output, and secrets

Permanent removal requires `COMPLETED` plus `CONFIRMED` or `DEFERRED`, no active
writer, and no queued instruction. DELETE also requires developer authorization.
The UI requires a second explicit confirmation, but the server guard is authoritative.
A durable removal marker excludes racing messages, commands, presence, and SSE;
retries finish only that task's workspace, checkpoints, events, workflow data, queue,
presence, model preferences, and task-session metadata. Tombstones prevent seeded
tasks from reappearing.

SQL values are parameterized. Fixed table identifiers used for checkpoint cleanup
are not supplied by clients. React renders strings without raw HTML. Public event
metadata is allowlisted and host paths are redacted; provider session IDs, lock
tokens, PIDs/hostnames, credentials, cookies, and environment variables are not API
fields. Unexpected browser errors are generic rather than stack traces.

## Practical guarantees and limitations

SQLite transactions provide one stored human message per task/idempotency key,
single-task writer exclusion, command idempotency, and compare-and-set state changes.
This is not a distributed exactly-once system. A provider may have performed remote
work before a process crash; recovery therefore pauses uncertain workspace state for
human inspection. Stale leases from another host are deliberately not stolen. All
authenticated users can currently read all tasks. There is no multi-tenant RBAC,
network egress sandbox, Kubernetes isolation, or browser shell in Demo 2.5.
