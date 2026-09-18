#!/bin/sh
# Serve the Demo 2 HTTP API from this checkout against the shared hosted-demo
# runtime (/var/lib/ai-platform), mirroring /usr/local/bin/ai-platform.
# Any AI_PLATFORM_* variable already set in the environment takes precedence.
#
# Browser messaging needs both (see docs/demo2-ui.md):
#   AI_PLATFORM_WEB_ACTOR=<name>     server-configured web actor (not authentication)
#   AI_PLATFORM_ENABLE_RUNNER=1      execute queued browser messages in this process
# Without them the API is read-only.
set -eu

umask 0002

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUNTIME=${AI_PLATFORM_DATA_DIR:-/var/lib/ai-platform}

export PYTHONDONTWRITEBYTECODE=1
export AI_PLATFORM_TASK_FILE="${AI_PLATFORM_TASK_FILE:-$ROOT/tasks.json}"
export AI_PLATFORM_DEMO_REPO="${AI_PLATFORM_DEMO_REPO:-$ROOT/demo_repo}"
export AI_PLATFORM_DATA_DIR="$RUNTIME"
export AI_PLATFORM_WORKSPACE_ROOT="${AI_PLATFORM_WORKSPACE_ROOT:-$RUNTIME/workspaces}"
export AI_PLATFORM_DB_PATH="${AI_PLATFORM_DB_PATH:-$RUNTIME/platform.db}"
export AI_PLATFORM_CHECKPOINT_DB_PATH="${AI_PLATFORM_CHECKPOINT_DB_PATH:-$RUNTIME/langgraph-checkpoints.db}"

exec "$ROOT/.venv/bin/ai-platform" serve --host 127.0.0.1 --port "${AI_PLATFORM_API_PORT:-8765}"
