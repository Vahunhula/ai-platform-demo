# Demo 2.5 Phase 3 workflow

Phase 3 connects the Phase 2/2.1 domain model (workflow phases, artifacts,
checklists, model routing) into the real LangGraph turn: one durable
`TaskSession` maps to one LangGraph thread (`thread_id = task_id`), and a
single turn walks forward through as many workflow phases as their readiness
gates allow, starting from the task's current durable phase:

    prepare -> brainstorm -> [gate] -> plan -> [gate] -> implementation ->
    verify -> [gate] -> review -> [gate] -> human_review

Lifecycle status (`READY`, `RUNNING`-equivalents, `WAITING_FOR_HUMAN`,
`PAUSED_BY_HUMAN`, `COMPLETED`, `FAILED`) and workflow phase (`BRAINSTORM`,
`PLAN`, `IMPLEMENTATION`, `REVIEW`, `HUMAN_REVIEW`) remain independent axes,
exactly as introduced in Phase 2: `RUNNING + PLAN`, `WAITING_FOR_HUMAN +
REVIEW`, and `PAUSED_BY_HUMAN + IMPLEMENTATION` are all valid combinations.

## Phase execution

Each phase resolves its own model through the Phase 2.1 `ModelRouter` at the
moment that phase starts (`model_router.resolve(task_id, phase)`), builds a
bounded context from durable artifacts (never a raw prior-phase transcript),
and is a fresh executor invocation (a new `ClaudeSDKClient`, no session
resume). A phase never carries another phase's hidden chain-of-thought.

- **Brainstorm** and **Plan** are read-only: only `Read`/`Glob`/`Grep` are
  offered to the agent at the tool layer (`executors/claude.py`), and the
  platform independently diffs a workspace snapshot taken immediately before
  and after the call. An unexpected mutation fails that phase's gate closed
  (all its checklist items become blocking `FAIL`) rather than being trusted.
- **Implementation** may edit the workspace and run tests. It reuses the
  existing bounded retry/escalation loop unchanged (`analyze_implement` /
  `verify` / `escalate_model`); a new `implementation_gate` node runs once
  that loop concludes, whether by passing or by exhausting retries.
- **Review** is a fresh, independent invocation, also read-only. It sees the
  original requirements, the latest Brainstorm Summary, Plan, and
  Implementation Summary, the actual current workspace diff, and the
  deterministic verification result -- never the implementation agent's own
  conversation.
- **Human Review** runs no agent. Entering it only sets
  `workflow_phase = HUMAN_REVIEW` and `status = WAITING_FOR_HUMAN`.

Each agent phase returns Claude Agent SDK native structured output
(`output_format={"type": "json_schema", ...}`, validated again by the
platform's own pydantic schema). A payload that still fails validation after
one bounded repair attempt does not crash the phase: it becomes deterministic
`NEEDS_HUMAN` checklist evidence, and the task waits.

## Readiness gates

Score, blockers, and eligibility are always computed by the platform
(`workflow.calculate_readiness`), never self-declared by the model. Checklist
evidence combines validated structured output, actual tool-call observations,
the actual diff, and actual test results. The platform-owned weights (each
phase sums to 100; automatic progression needs a score >= 98 and no blocking
`FAIL`/`NEEDS_HUMAN`):

| Phase | Items (weight, blocking) |
| --- | --- |
| Brainstorm | requirements_understood (35, ✓), repository_context_inspected (25, ✓), viable_direction_identified (20, ✓), blocking_questions_resolved (18, ✓), assumptions_documented (2) |
| Plan | implementation_steps_complete (30, ✓), affected_files_identified (20, ✓), validation_plan_defined (25, ✓), risks_addressed (13, ✓), open_questions_resolved (10, ✓), plan_clarity (2) |
| Implementation | required_changes_present (30, ✓), deterministic_verification_passed (40, ✓), implementation_matches_plan (18, ✓), no_known_blocking_issues (10, ✓), cleanup_quality (2) |
| Review | no_critical_findings (35, ✓), no_major_blocking_findings (30, ✓), requirements_satisfied (20, ✓), tests_sufficient (13, ✓), no_minor_findings (2) |

If eligible, the phase transitions automatically (`WORKFLOW_PHASE_CHANGED`,
`transition_mode=AUTOMATIC`, actor `workflow-gate`) and the turn continues
into the next phase's node in the same graph invocation. If not, the task
remains in that phase with `status=WAITING_FOR_HUMAN`
(`WORKFLOW_PHASE_WAITING_FOR_HUMAN`); this is a **business/gate stop**, never
a `FAILED` task. Exhausting Implementation's bounded retries and AUTO
escalation is the same kind of gate stop, not a system failure. Only genuine
executor/system failures (a fatal `AgentExecutorError`, an unavailable
explicitly-selected model, an unhandled exception) use `FAILED`.

## Human intervention

- A human message while a gate-stopped phase waits reruns that same phase
  with the feedback, producing a new artifact version and a new checklist
  snapshot; earlier versions remain historical.
- A human message that arrives while the task rests in **Human Review**
  starts no turn (`terminal=True` on the outcome): it is durably recorded and
  a queued browser message still completes, but only Approve or Reject move
  the task forward from there.
- **Reject** from Human Review is the default correction path: it records
  feedback, transitions `HUMAN_REVIEW -> IMPLEMENTATION` as `MANUAL` (a human
  decision, never an automatic gate transition), and the next turn resumes
  Implementation with that feedback.
- **Approve** requires `status=WAITING_FOR_HUMAN`, `workflow_phase=
  HUMAN_REVIEW`, and passing verification; it completes the task exactly as
  before (no commit, push, or merge).
- Developers can still manually transition to any phase (`/phase`, audited as
  `MANUAL`) regardless of score, e.g. `REVIEW -> PLAN` to backtrack further
  than the default Reject target. The next Start/Resume/message execution
  runs whatever phase is currently durable.
- Reset preserves workflow phase, artifacts, checklists, and events exactly
  as in Phase 2; only status and workspace are reset.

## Provenance

Agent-produced artifacts are attributable: `workflow_artifacts` gained
additive columns (`created_by_type`, `execution_id`, `logical_model`,
`concrete_model`, `provider`), backfilled `HUMAN` for pre-Phase-3 rows created
only through the authenticated API. Nothing existing was migrated destructively.

## Deferred

No slash commands (`/brainstorm`, `/plan`, ...), no Claude command bridge, no
GitHub/Jira integration, no rich workflow UI. The frontend gained only a
"Last gate" fact (phase, score, eligibility) alongside the existing Phase
fact; everything else is unchanged.
