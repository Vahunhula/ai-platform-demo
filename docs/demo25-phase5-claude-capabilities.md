# Demo 2.5 Phase 5 Claude Capability Audit

Audit date: 2026-09-25. The installed development runtime contains Claude Agent SDK
`0.2.152` and its bundled Claude Code executable reports `2.1.259`. The integration uses
the SDK package; the product does not invoke the executable as a command proxy.

The SDK exports `ClaudeSDKClient`, structured `ClaudeAgentOptions`, one-shot `query`,
session inspection/mutation helpers, and connected-client controls including `interrupt`,
`get_context_usage`, `get_mcp_status`, `get_server_info`, `set_model`,
`set_permission_mode`, and `rewind_files`. It does not expose a native slash-command
enumeration API or a safe standalone API for invoking an arbitrary Claude Code slash
command. Connected-client controls are meaningful only inside a live SDK client, while the
platform intentionally uses bounded turns owned by `TaskSession` and the existing runner.

| Command/capability | Source | SDK support | Classification | Exposed? | Reason |
|---|---|---|---|---|---|
| `claude/help` | Platform Claude registry | Registry metadata, no model call | ADAPTED | Yes, viewer | One authoritative list includes availability and disabled reasons. |
| `claude/status` | Platform task/executor state | SDK version lookup plus platform state | ADAPTED | Yes, viewer | Reports provider, selected models, active-turn state, phase safety, and whether task-owned resume metadata exists; never credentials, paths, or raw environment. |
| Model query / agent turn | `ClaudeSDKClient.query` | Native, structured | DISABLED | No command in Phase 5 | A free-form command would duplicate the message/runner path. Any future mapping must use the existing queue, runner, and writer lock. |
| Interrupt | `ClaudeSDKClient.interrupt` | Native on a connected client | ADAPTED | Existing `/pause`, not under `claude/` | Platform pause already owns cancellation and lifecycle semantics. |
| Context usage | `ClaudeSDKClient.get_context_usage` | Native on a connected client | DISABLED | No | No persistent client exists outside a bounded turn; starting one just for status would be misleading and potentially billable. |
| Server info | `ClaudeSDKClient.get_server_info` | Native on a connected client | DISABLED | No | Requires a live provider client and may expose implementation metadata not needed by developers. |
| Change model | `ClaudeSDKClient.set_model` | Native on a connected client | ADAPTED | Existing model-routing UI/API | Platform logical-model selection is durable and phase-aware; an ephemeral SDK override would bypass it. |
| Change permission mode | `ClaudeSDKClient.set_permission_mode` | Native on a connected client | DISABLED | Catalogued as `claude/permissions`, unavailable | Could weaken platform read-only phase restrictions. |
| MCP status/control | `get_mcp_status`, `toggle_mcp_server`, `reconnect_mcp_server` | Native on a connected client | FUTURE_INFRA_ONLY | Catalogued as `claude/mcp`, unavailable | Global/integration administration is not a normal developer task capability. Current executor also sets strict empty MCP configuration. |
| Rewind files | `ClaudeSDKClient.rewind_files` | Native on a connected client with checkpointing | DISABLED | No | Mutates workspace history outside platform workflow, artifacts, and writer semantics. |
| List/read/rename/tag/fork provider sessions | SDK session helpers | Native local-session APIs | DISABLED | No | Provider transcripts are executor implementation details, not TaskSession truth. |
| Delete provider session | `delete_session` | Native local-session API | DISABLED | Catalogued as `claude/session-delete`, unavailable | Arbitrary deletion is unsafe. Task removal alone cleans only session IDs recorded in that task's events and scopes lookup to that task workspace. |
| Authentication/login/logout | Claude Code account state | Not a bounded task API | DISABLED | Catalogued as `claude/auth`, unavailable | Must not reveal credentials or alter global authentication. |
| Global configuration | Claude Code settings | Not a bounded task API | DISABLED | Catalogued as `claude/config`, unavailable | Must not alter host or global Claude configuration. |
| Arbitrary CLI slash command | Bundled Claude Code executable | No SDK allowlist/enumeration interface | DISABLED | No | The bridge never runs `claude <user input>`, never uses `shell=True`, and never forwards unknown `claude/...` text as a prompt. |

## Phase safety

`claude/help` and `claude/status` are read-only adaptations and do not start an agent turn.
Status reports `read_only` in Brainstorm, Plan, and Review, `disabled` in Human Review,
and `workspace_write` only in Implementation. A future capability that runs a model must
enter through the existing prepare → runner → writer-lock → executor path and retain the
phase-specific tool allowlist (`Read`, `Glob`, `Grep` in read-only phases).

## Session ownership and removal

The platform currently starts a fresh bounded SDK client for each executor call. It records
the returned provider `session_id` as private event metadata but does not use `resume` or a
provider session as durable task truth. There is no separately deletable remote resource
created by this integration. On task removal, the platform:

1. extracts only session IDs recorded in that task's agent events;
2. asks the SDK to delete matching local transcript state scoped to that task workspace,
   ignoring already-absent transcripts;
3. deletes the task events that held those IDs.

It never logs out Claude, removes global credentials, scans arbitrary provider projects, or
touches another task's provider metadata.
