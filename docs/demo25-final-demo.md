# Demo 2.5 Final Demo

Use a disposable isolated runtime, FakeAgentExecutor, registered disposable Git
repository, and two tasks. Do not use production data or paid model traffic.

1. Log in as a developer. The header shows the authenticated name and role. Create
   Task A from a repository ID, branch, and assignee; create Task B as the isolation
   control. Both receive distinct workspaces and appear in the sidebar.
2. Open Task A and Start. Chat shows Claude · Brainstorm and Claude · Plan results;
   Activity is not required to understand the workflow. Changes remain empty in the
   read-only phases.
3. Script Plan v1 with an unresolved question. The workflow remains at Plan and Chat
   shows “Needs your input · Plan.” Restart the application now. Reopen Task A: the
   same question and prior phase result remain visible and the task remains waiting.
4. Answer in Chat using a unique client message ID. The instruction appears once.
   Plan reruns as version 2 and the readiness gate advances to Implementation.
5. Implementation changes only Task A's workspace. Tests show baseline-aware
   verification: known baseline failures are warnings while a new task-caused failure
   blocks. Fix the scripted failure and continue. Task B and the source repository
   remain byte-for-byte unchanged.
6. Review runs as a fresh read-only invocation and its report appears in Chat. The
   workflow reaches Human Review. Send a normal message and show that no new model
   request runs.
7. Run `/status`, `/tests`, `claude/help`, and `claude/status`. Show that unregistered
   Claude text and global configuration/auth capabilities are unavailable.
8. Inspect Changes, Tests, Activity, Summary, and Workspace. Activity begins with at
   most 50 newest events and “Load older events” obtains the next exclusive sequence
   page. An SSE event appears once and survives reconnect from Last-Event-ID.
9. Human Review visibly offers Confirm, Reject, and Defer. Before a decision, Remove
   task remains visible but disabled with “Confirm or Defer … first.” Choose Confirm.
   The sidebar shows `Completed · Confirmed`; Remove task becomes enabled.
10. Open Remove task, read the permanent-cleanup list, and first Cancel. Reopen it and
    choose Remove permanently. Task A leaves the list, its page clears, its stream
    closes, and navigation returns to a safe unselected/list state. Reload and verify
    it stays absent. Task B, its events, checkpoints, and workspace remain intact.

Repeat step 9 with a second disposable task using Defer; it must show
`Completed · Deferred` and be removable under the same confirmation.

Expected final checks: no active writers, no queued messages, `PRAGMA integrity_check`
returns `ok`, source repository unchanged, and no production path was accessed.
