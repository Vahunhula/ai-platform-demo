# Demo 2.5 Phase 4: Platform Slash Commands

Phase 4 adds a bounded platform command namespace. A trimmed input beginning with `/` is a
platform command; a slash elsewhere in ordinary text remains a human message. These commands are
not Claude Code commands, are never added to an agent prompt, and cannot execute a shell.

## Architecture

The backend `CommandRegistry` is the ordered catalog and owns each command's description, usage,
argument hints, required capability, mutating flag, and handler. `CommandService` parses and
audits an invocation, calculates task-aware availability, and invokes that registered handler.
Handlers delegate mutations to `WorkflowPhaseService` or `TaskControlService`; they do not write
task lifecycle or workflow state themselves.

The browser, API, and `ai-platform command` CLI all use this same service and registry. Existing
dedicated controls and CLI commands remain supported and continue to use the same core services.

## Parsing and arguments

The parser trims surrounding whitespace and recognizes a command only when the remaining text
starts with `/`. It uses POSIX-style tokenization solely for quoted arguments; it performs no
expansion or execution. Unknown names, unmatched quotes, extra arguments, and missing required
arguments are structured validation errors and are not forwarded to Claude.

`/reject` requires feedback. `/resume` and phase commands accept optional free text. `/help` accepts
an optional command name. Other commands accept no arguments.

## Permissions and availability

Authenticated viewers may use `/status`, `/tests`, and `/help`. Mutating commands require the
existing developer capability, which admins inherit. The server derives the actor from the web
session; request bodies have no actor field.

Availability is calculated on the server. Lifecycle commands reuse `TaskControlService` control
availability, including writer, queue, verification, lifecycle, Human Review, and runner checks.
Phase commands reuse the manual `WorkflowPhaseService` path and are disabled when already at the
requested phase. Disabled commands remain in metadata with a reason so clients render rather than
reimplement these rules.

## API

- `GET /api/tasks/{task_id}/commands` returns task- and user-aware registry metadata.
- `POST /api/tasks/{task_id}/commands` accepts `command_text` and an idempotency-shaped
  `client_command_id`. It returns `200` for completed operations and `202` when an existing agent
  turn was accepted.

Errors follow existing API conventions: `401` unauthenticated, `403` insufficient role, `409`
unavailable task state, `422` invalid arguments/body, `400` malformed or unknown command, `404`
unknown task, and `503` unavailable runner/executor.

## Command catalog

| Command | Arguments | Delegation |
| --- | --- | --- |
| `/brainstorm` | optional reason | manual phase transition to `BRAINSTORM` |
| `/plan` | optional reason | manual phase transition to `PLAN` |
| `/implement` | optional reason | manual phase transition to `IMPLEMENTATION` |
| `/review` | optional reason | manual phase transition to `REVIEW` |
| `/human-review` | optional reason | manual phase transition to `HUMAN_REVIEW` |
| `/approve` | none | existing approve control |
| `/reject` | required feedback | existing reject/correction control |
| `/pause` | none | existing cooperative pause control |
| `/resume` | optional instruction | existing resume control |
| `/status` | none | current task/workflow/readiness snapshot |
| `/tests` | none | latest persisted platform verification result; never runs tests |
| `/help` | optional command | registry-derived help |

## Audit and live updates

Every invocation appends `COMMAND_INVOKED`, followed by `COMMAND_SUCCEEDED` or `COMMAND_FAILED`, to
the existing append-only task event log. Metadata includes the command, normalized arguments,
client command ID, actor display name, lifecycle status, workflow phase, result category, and a
safe result or failure reason. Existing event presentation redacts configured paths and allowlists
metadata. The normal authenticated SSE stream carries these events and its durable sequence ID
continues to support reconnect through `Last-Event-ID`.

## Browser and CLI

The chat composer fetches registry metadata, filters it by prefix, shows unavailable commands and
their server-provided reasons, and supports Up, Down, Enter, Escape, and mouse selection. Leading
slash submissions use the command endpoint; all other submissions retain the Phase 3 conversation
path. Command history is rendered as human command and Platform result activity, never as an agent
message.

Generic CLI usage is:

```text
ai-platform command TASK_ID "/status"
ai-platform command TASK_ID "/reject Fix the failing edge case"
```

CLI identity continues to come from `AI_PLATFORM_USER` or the local operating-system account.

## Namespace boundary

Phase 4 owns only the `/` platform namespace above. The future `claude/` bridge, Claude-native
commands, authentication/configuration commands, and session management are explicitly not part of
this phase.
