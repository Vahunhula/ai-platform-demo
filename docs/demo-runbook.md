# Hosted SSH demo runbook

Current access limitation: Vakho has a public key and supported Claude login.
Alex and An have distinct platform-ready Unix accounts, but their external SSH
access and real Claude continuation remain pending public-key installation and
per-user `claude auth login`. Until then, their collaboration is rehearsed with
server-side `sudo -u` identity simulation and the test-only `FakeAgentExecutor`.

## PRE-DEMO — 10 MINUTES BEFORE

Run as the deployment administrator:

```sh
cd /opt/ai-platform-demo
git status --short --branch
getent group ai-platform
id vakho
id alex
id an
df -h /var/lib/ai-platform
free -h
sudo -u vakho -H ai-platform doctor
sudo -u vakho -H claude auth status --json
sudo -u vakho -H ai-platform tasks
sudo -u alex -H ai-platform tasks
sudo -u an -H ai-platform tasks
```

Verify external SSH from the presentation laptop:

```sh
ssh vakho@HOST whoami
```

The expected result is `vakho`. Alex/An external SSH checks remain pending
their public keys.

On every account that will start or resume Claude:

```sh
claude --version
claude auth status --json
```

If logged out, authenticate interactively as that same Unix user:

```sh
claude auth login
```

Never copy or link Claude credential directories between accounts. Ensure the
development identity override is absent:

```sh
test -z "${AI_PLATFORM_USER:-}"
```

Prepare a clean task as Vakho:

```sh
ai-platform reset DEMO-1
```

Confirm the displayed deletion target is exactly
`/var/lib/ai-platform/workspaces/DEMO-1` before answering yes.

## VAKHO TERMINAL

```sh
ssh vakho@HOST
unset AI_PLATFORM_USER
ai-platform attach DEMO-1 --follow
```

In a second Vakho terminal, start the task if it is `READY`:

```sh
ssh vakho@HOST
unset AI_PLATFORM_USER
ai-platform start DEMO-1
```

Expected initial route: `LOW -> cheap`. Deterministic verification should emit
`TEST_PASSED`, followed by `WAITING_FOR_HUMAN`.

After Alex and An finish their parts:

```sh
ai-platform diff DEMO-1
ai-platform approve DEMO-1
ai-platform trace DEMO-1
```

## ALEX TERMINAL

```sh
ssh alex@HOST
unset AI_PLATFORM_USER
ai-platform attach DEMO-1
ai-platform message DEMO-1 \
  "Check whether the same typo appears anywhere else in the repository."
```

The message is attributed to `alex` and continues in the same workspace. This
exact external command requires Alex's public key and personal Claude login.
Until those are installed, demonstrate Alex's identity and shared read access
from the administrator terminal:

```sh
sudo -u alex -H env -u AI_PLATFORM_USER whoami
sudo -u alex -H env -u AI_PLATFORM_USER ai-platform attach DEMO-1
sudo -u alex -H env -u AI_PLATFORM_USER ai-platform diff DEMO-1
```

Use the already validated FakeAgent rehearsal/trace for Alex's continuation; do
not borrow Vakho's credentials.

## AN TERMINAL

```sh
ssh an@HOST
unset AI_PLATFORM_USER
ai-platform pause DEMO-1
ai-platform shell DEMO-1
```

Inside the task shell, make only the planned harmless demonstration edit, then:

```sh
git diff
exit
```

Resume from the current human-edited files:

```sh
ai-platform resume DEMO-1 \
  --message "I changed the current workspace manually. Review it and continue."
```

An needs a valid personal Claude login for a real `resume`. Until An's external
key and login are installed, demonstrate the distinct OS identity and shared
workspace access from the administrator terminal:

```sh
sudo -u an -H env -u AI_PLATFORM_USER whoami
sudo -u an -H env -u AI_PLATFORM_USER ai-platform attach DEMO-1
sudo -u an -H env -u AI_PLATFORM_USER ai-platform diff DEMO-1
```

The validated simulated takeover trace contains
`HUMAN_SHELL_OPENED`, `HUMAN_WORKSPACE_CHANGED`, and `HUMAN_SHELL_CLOSED`, all
with actor `an`.

## Expected events

The exact sequence can include tool and status details, but must contain:

```text
vakho   TASK_STARTED
system  MODEL_SELECTED (cheap)
agent   AGENT_STARTED
system  TEST_PASSED
alex    HUMAN_MESSAGE
agent   AGENT_STARTED
system  TEST_PASSED
an      HUMAN_PAUSED
an      HUMAN_SHELL_OPENED
an      HUMAN_WORKSPACE_CHANGED
an      HUMAN_SHELL_CLOSED
an      HUMAN_RESUMED
agent   AGENT_STARTED
system  TEST_PASSED
vakho   HUMAN_APPROVED
system  TASK_COMPLETED
```

Do not claim events that are absent from `ai-platform trace DEMO-1`.

## WHAT TO EXPLAIN WHILE IT RUNS

- One Jira-like task becomes one shared TaskSession.
- Difficulty selects the initial model tier, keeping easy work inexpensive.
- Claude edits a task-owned workspace, never the clean source template.
- Platform-owned pytest independently verifies the result.
- Alex can continue the same durable task and workspace.
- An can pause the agent and take over the workspace manually.
- Linux usernames attribute every human action.
- The event history is append-only.
- Deterministic failures drive bounded model escalation.
- A future UI will call the same platform core.

## MODEL ROUTING DEMONSTRATION

Use the deterministic suite; do not spend provider usage forcing failures:

```sh
cd /opt/ai-platform-demo
.venv/bin/python -m pytest \
  tests/test_model_router.py tests/test_workflow.py tests/test_reliability.py
```

This demonstrates `DEMO-1 LOW -> cheap`, `DEMO-2 MEDIUM -> default`,
`DEMO-3 HIGH -> strong`, and two cheap-tier failures followed by
`MODEL_ESCALATED cheap -> default` and a pass.

## FAILURE RECOVERY

Claude logged out:

```sh
claude auth login
ai-platform doctor
```

Existing task state:

```sh
ai-platform attach DEMO-1
ai-platform trace DEMO-1
ai-platform reset DEMO-1
```

Only confirm reset after checking its exact workspace target.

Stale lock:

```sh
ai-platform attach DEMO-1
ai-platform trace DEMO-1
```

Normal CLI startup performs supported same-host dead-process recovery. Expect
`STALE_LOCK_RECOVERED` and `PAUSED_BY_HUMAN`; inspect the diff, then explicitly
resume. Never kill an unrelated PID or edit lock rows manually.

Tests fail:

```sh
ai-platform attach DEMO-1
ai-platform diff DEMO-1
ai-platform trace DEMO-1
```

Do not delete runtime files, modify SQLite directly, or improvise broad
permission changes during the presentation.
