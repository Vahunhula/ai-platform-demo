# Demo 2.5 Phase 6: interactive workflow workspace

Phase 6 fixes the primary usability problem left after Phase 5: the workflow
engine worked, but its output didn't. Running `/plan` moved the phase and,
from Chat's point of view, nothing else happened -- the actual plan, a
blocking question, or the reason the task was waiting for a human all lived
only in Trace, as a low-level event a human had to go find and decode. This
phase's invariant is:

> If the human needs to read it or respond to it, it must appear directly in
> Chat. Activity/Trace is for observability, never a requirement for ordinary
> task interaction.

## Chat projection architecture

Chat is a **projection**, not a second copy of anything. The durable sources
of truth are unchanged from Phase 2/3: the append-only `events` table
(`HUMAN_MESSAGE`, `AGENT_MESSAGE`, `WORKFLOW_PHASE_*`, `COMMAND_*`, ...),
`workflow_artifacts` (versioned Brainstorm Summary / Plan / Implementation
Summary / Review Report / Human Review Decision), and `checklist_evaluations`
(the readiness gate's evidence). Phase 6 adds one read path over that data:

    durable events + artifacts + checklist evaluations
        -> ConversationService.list_chat_items()   (ai_platform/conversation.py)
        -> Presenter.chat_item()                    (ai_platform/api/presenters.py)
        -> GET /api/tasks/{id}/messages
        -> ChatPanel (React)

`list_chat_items` walks one task's event history once, in sequence order, and
turns each chat-relevant event into a `ChatItem` (a small dataclass carrying
just the event plus whichever of a delivery record, a workflow artifact, or a
checklist evaluation it needs). Because a checklist evaluation has no direct
foreign key to the gate/waiting event it belongs to, the projection walks
each phase's evaluations in lockstep with that phase's
`WORKFLOW_PHASE_GATE_EVALUATED` events -- `_persist_gate` (graph.py) always
creates exactly one evaluation immediately before emitting that event, so a
per-phase cursor pairs them correctly even across repeated reruns of the same
phase. `Presenter.chat_item` then renders each `ChatItem` into the public,
already-formatted `MessageResponse` the browser receives; the browser never
inspects raw event metadata or artifact JSON to decide what happened.

The existing `/api/tasks/{id}/messages` URL was kept (no new endpoint) since
its response was already "the browser's Chat timeline" conceptually; Phase 6
just widens what durably belongs on that timeline. `MessageResponse` grew
additively (`type`, `title`, `workflow_phase`, `artifact_kind`,
`artifact_version`, `readiness_score`, `requires_human_input`,
`blocking_checks`, `logical_model`, `concrete_model`); every pre-Phase-6
field (`role`, `content`, `sequence_id`, `status`, ...) is unchanged, so
nothing that read the old shape broke.

## Chat item types

| `type` | Durable source | Purpose |
| --- | --- | --- |
| `human_message` | `HUMAN_MESSAGE` event | A person's message (unchanged from Phase 2). |
| `agent_message` | `AGENT_MESSAGE` event | A raw agent chat message (tool-call narration), unchanged. |
| `phase_result` | `WORKFLOW_PHASE_OUTPUT_CREATED` + the referenced `WorkflowArtifact` | The actual Brainstorm/Plan/Implementation/Review/Human-Review output, rendered as readable text (never JSON), titled e.g. `Claude · Plan`, carrying `artifact_version` and `logical_model`/`concrete_model`. |
| `human_input_required` | `WORKFLOW_PHASE_WAITING_FOR_HUMAN` + the matching checklist evaluation | The explicit question(s) blocking automatic progression, titled `Needs your input · <Phase>`, with `readiness_score`, `requires_human_input: true`, and a `blocking_checks` list (key/label/status/evidence). |
| `platform_activity` | `WORKFLOW_PHASE_STARTED` / `WORKFLOW_PHASE_CHANGED` | Short narration -- "Plan phase started", "Plan passed readiness gate · 100% / Moving to Implementation...". `WORKFLOW_PHASE_GATE_EVALUATED` itself stays Activity-only: its outcome is exactly what the following waiting/changed event reports, so surfacing it too would just repeat the same fact in Chat. |
| `command_result` | `COMMAND_INVOKED` / `COMMAND_SUCCEEDED` / `COMMAND_FAILED` | A slash command and its result, now interleaved chronologically with everything else (previously the browser rendered all command events after all messages, out of order). |

No hidden reasoning, provider secrets, or absolute host paths are ever placed
on a chat item; the same `PathRedactor` used for events and diffs redacts
artifact and evidence text before it reaches `MessageResponse`.

## Interactive phase flow

For Brainstorm, Plan, Implementation, and Review, whenever the graph
(`ai_platform/graph.py`) actually runs that phase:

1. `WORKFLOW_PHASE_STARTED` is emitted -> Chat shows "*Phase* phase started".
2. The phase produces a `WorkflowArtifact` -> Chat shows the actual output
   (`Claude · <Phase>` with the rendered summary/steps/findings).
3. The readiness gate evaluates that output:
   - **Eligible**: `WORKFLOW_PHASE_CHANGED` (`AUTOMATIC`) -> Chat shows
     "*Phase* passed readiness gate · *score*% / Moving to *next phase*...".
   - **Not eligible**: `WORKFLOW_PHASE_WAITING_FOR_HUMAN` -> Chat shows the
     `human_input_required` card with the actual blocking questions.

Human Review is unchanged and still runs no agent: its Review Report and
verification result already reach Chat as `phase_result`/durable messages,
and Approve/Reject/Defer stay HTTP control actions (`controls.py`), not
something a plain Chat message can trigger.

## Human input flow

A human's answer to a `human_input_required` card is **the same durable
message path Phase 2 already had** -- there is no separate "answer question"
mechanism:

    POST /messages -> ConversationService.submit -> HUMAN_MESSAGE event
        -> message_queue row -> TaskTurnRunner -> run_task_graph(continuation=True)
        -> the current workflow phase reruns with the new human context
        -> a new WorkflowArtifact VERSION is created
        -> the new phase_result (and, if resolved, the next platform_activity)
           appear in the same Chat, right after the original question

`tests/test_chat_projection.py::test_plan_blocker_shows_as_human_input_required_and_rerun_produces_v2`
exercises this exact path end-to-end with a scripted (`FakeAgentExecutor`)
Plan blocker.

## Workflow UI

**Lifecycle** (`TaskStatus`: `READY`, `ANALYZING`/`IMPLEMENTING`/`VERIFYING`,
`WAITING_FOR_HUMAN`, `PAUSED_BY_HUMAN`, `COMPLETED`, `FAILED`), **workflow
phase** (`BRAINSTORM` -> `PLAN` -> `IMPLEMENTATION` -> `REVIEW` ->
`HUMAN_REVIEW`), and **disposition** (`CONFIRMED`/`DEFERRED`/none) are three
independent axes, exactly as Phase 2/3 defined them, and Phase 6 keeps them
visually distinct rather than folding them into one badge: the header's
status chip is lifecycle, `WorkflowProgress` is phase, and the "Disposition"
chip/fact is disposition.

`WorkflowProgress` (`web/src/components/WorkflowProgress.tsx`) renders the
five phases as a row of dots: done (reached, per durable
`WORKFLOW_PHASE_STARTED`/`WORKFLOW_PHASE_OUTPUT_CREATED` history, so a manual
backtrack still shows as visited), current, waiting (current +
`WAITING_FOR_HUMAN`), or upcoming -- never assuming strictly linear
progress.

## Readiness UI

`TaskDetail.latest_readiness` (phase/score/eligible) is unchanged from Phase
3 and still drives the workflow-progress readiness badge. The Summary tab
additionally fetches `GET /tasks/{id}/checklists` and renders the current
phase's full evaluation: every checklist item's label, PASS/FAIL/NEEDS_HUMAN
status, weight, and blocking flag, plus the overall score and the platform's
fixed 98%-and-no-blockers auto-progression rule -- all backend-owned; React
never recomputes eligibility.

## Artifact UI

`phase_result` chat items render each artifact kind's payload as readable
text (`_render_artifact_body` in `presenters.py`): a summary paragraph plus
labeled bullet sections (Files likely affected, Steps, Validation, Risks,
Open questions for a Plan; Critical/Major/Minor findings and the
requirements/tests/convention assessment for a Review Report; and so on).
Nothing is dumped as raw JSON, and `artifact_version` is shown so a rerun's
new version is visibly distinct from the one before it; Chat is append-only,
so both versions (and the question that triggered the rerun) stay visible in
order, and the Summary tab's "Latest artifacts" list resolves to the current
version per kind.

## Tabs

The single-page task view now has six tabs (`Chat` default):

- **Chat** -- the projection described above: human/agent messages, phase
  output, waiting-for-human questions, phase narration, and command results,
  oldest to newest, with the composer at the bottom.
- **Changes** -- unchanged workspace diff view (was "Diff").
- **Tests** -- unchanged persisted verification view, still distinguishing a
  known baseline failure from a new task regression (Phase 2.2).
- **Activity** -- the renamed Trace tab; see below.
- **Summary** -- new: task id/repository/base branch, lifecycle, workflow
  phase, disposition, current model, verification status, changed-file count,
  pending instruction count, the current readiness checklist, the pending
  question (if `WAITING_FOR_HUMAN`), and the latest artifact per kind.
- **Workspace** -- new: repository, base branch, workspace id, executor
  state, active writer, attempt count, verification status, created/updated
  timestamps. No absolute host filesystem path is ever exposed; `workspace_id`
  is the task id, not a path.

## Activity / Trace

- **Newest-first by default.** `GET /tasks/{id}/events` gained an additive
  `order=asc|desc` query parameter (default `asc`, i.e. unchanged) so no
  existing caller's behavior changes; the browser's Activity tab requests
  display in `desc` by reversing the already-fetched list client-side and
  defaults its own toggle to newest-first. This is presentation only.
- **Canonical sequence is untouched.** Sequence numbers are still assigned by
  SQLite's single-writer, append-only insert path (`storage.py`) and are
  still strictly increasing; nothing about storage or numbering changed.
- **SSE stays chronological.** `event_stream` (`routes/stream.py`) is
  unmodified: it still delivers `platform_event` frames in forward sequence
  order with `id: <sequence_id>`, and a reconnecting `EventSource`'s
  `Last-Event-ID` still resumes from the right cursor.
- **Pagination.** The Activity tab paginates the already-loaded event list
  client-side (40 at a time, "Load N older/newer events"), so a live SSE
  arrival becomes the new first item under the newest-first default without a
  refetch, and "load more" never duplicates or skips an event since it slices
  one consistent, deduplicated array. (`GET /tasks/{id}/events` itself
  remains the existing unbounded endpoint; a true server-side cursor for it
  was left out of this phase's scope to avoid touching a contract three other
  call sites already depend on -- see "Deferred" in the final report.)

## Commands

Slash commands (`/brainstorm`, `/plan`, `/implement`, `/review`,
`/human-review`, `/approve`, `/reject`, `/pause`, `/resume`, `/status`,
`/tests`, `/help`) and the separate `claude/...` namespace are unchanged in
`commands.py`/`claude_commands.py`. What changed is presentation: a command's
`COMMAND_INVOKED`/`COMMAND_SUCCEEDED`/`COMMAND_FAILED` events are now
`command_result` chat items, interleaved by sequence with everything else in
Chat (previously the browser rendered them in a separate block after all
messages, which was chronologically wrong whenever a command and a message
interleaved). Command audit events remain in Activity as before.

## SSE / real-time

Unchanged wiring, reused rather than extended: the existing `onEvent`
handler in `App.tsx` already schedules a debounced refetch of task detail and
messages on *every* incoming `platform_event`, regardless of type. Since the
new chat item types are events that already flow over that same SSE stream,
a phase result or a waiting-for-human question appearing live required no
new SSE plumbing -- only the `/messages` response needed to include them.
Reconnect, `Last-Event-ID`, and the periodic re-authentication check are all
untouched.

## Persistence / restart

Chat items are pure reads over durable tables (events, artifacts, checklist
evaluations); nothing is cached in memory across the API/runner process.
`tests/test_chat_projection.py::test_chat_reconstructs_from_durable_state_after_restart`
creates one `ApplicationContext` (and app) pointed at a task resting on a
Plan blocker, reads `/messages`, then creates a **second**, independent
context/app against the same on-disk SQLite database (simulating an API
restart) and asserts the same chat items -- same types, same ids, same
order -- come back.

## Tests

- Backend: 200 passed (194 baseline + 6 new in
  `tests/test_chat_projection.py`), `ruff check .` clean, `git diff --check`
  clean.
- Frontend: 15 passed (5 baseline + 10 new across `ChatPanel.test.tsx`,
  `tabs.test.tsx`, `WorkflowProgress.test.tsx`), `tsc -b` (typecheck and
  build) clean.

## No chain-of-thought exposure

Nothing new in Phase 6 exposes hidden reasoning. `phase_result` items render
only the agent's already-public structured output (the same
`BrainstormSummaryPayload`/`PlanPayload`/... Phase 2 already validated and
stored); `agent_message` items are unchanged `AgentActivity(MESSAGE)`
entries; tool calls remain sanitized Activity-only entries. The redaction
layer (`PathRedactor`) still runs over every piece of text before it reaches
the browser.
