# Hosted SSH demo runbook

## Pre-demo checks

Run as the deployment administrator:

```sh
cd /opt/ai-platform-demo
git status --short --branch
getent group ai-platform
id vakho
id alex
id david
df -h /var/lib/ai-platform
free -h
sudo -u vakho -H ai-platform doctor
sudo -u alex -H ai-platform doctor
sudo -u david -H ai-platform doctor
sudo -u vakho -H ai-platform tasks
```

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

## Terminal 1 - Vakho

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

After Alex and David finish their parts:

```sh
ai-platform diff DEMO-1
ai-platform approve DEMO-1
ai-platform trace DEMO-1
```

## Terminal 2 - Alex

```sh
ssh alex@HOST
unset AI_PLATFORM_USER
ai-platform attach DEMO-1
ai-platform message DEMO-1 \
  "Check whether the same typo appears anywhere else in the repository."
```

The message is attributed to `alex` and continues in the same workspace. Alex
must have a valid personal Claude login for the continuation to execute. If not,
stop after the FakeAgent rehearsal; do not borrow another user's credentials.

## Terminal 3 - David

```sh
ssh david@HOST
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

David needs a valid personal Claude login for `resume`. The trace must contain
`HUMAN_SHELL_OPENED`, `HUMAN_WORKSPACE_CHANGED`, and `HUMAN_SHELL_CLOSED`, all
with actor `david`.

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
david   HUMAN_PAUSED
david   HUMAN_SHELL_OPENED
david   HUMAN_WORKSPACE_CHANGED
david   HUMAN_SHELL_CLOSED
david   HUMAN_RESUMED
agent   AGENT_STARTED
system  TEST_PASSED
vakho   HUMAN_APPROVED
system  TASK_COMPLETED
```

Do not claim events that are absent from `ai-platform trace DEMO-1`.

## Model routing demonstration

Use the deterministic suite; do not spend provider usage forcing failures:

```sh
cd /opt/ai-platform-demo
.venv/bin/python -m pytest \
  tests/test_model_router.py tests/test_workflow.py tests/test_reliability.py
```

This demonstrates `DEMO-1 LOW -> cheap`, `DEMO-2 MEDIUM -> default`,
`DEMO-3 HIGH -> strong`, and two cheap-tier failures followed by
`MODEL_ESCALATED cheap -> default` and a pass.

## Recovery

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
