#!/bin/sh
# Serve the Demo 2 HTTP API from this checkout on 127.0.0.1.
#
# Runtime: the dedicated Demo 2 runtime /var/lib/ai-platform-demo2 by default
# (see scripts/demo2-env.sh). Explicit AI_PLATFORM_* variables still override
# it, but the frozen Demo 1 runtime (/var/lib/ai-platform) is refused unless
# AI_PLATFORM_ALLOW_DEMO1_RUNTIME=1 is set deliberately.
#
# Web users sign in with CLI-provisioned access tokens (see docs/demo2-ui.md):
#   . scripts/demo2-env.sh
#   .venv/bin/ai-platform users add --username vakho --display-name Vakho --role developer
#   .venv/bin/ai-platform auth-token create vakho
# Optional:
#   AI_PLATFORM_ENABLE_RUNNER=1      run browser-initiated agent turns in this process
#   AI_PLATFORM_API_PORT=8765        listen port (always 127.0.0.1)
#   AI_PLATFORM_SESSION_HOURS=12     web session lifetime
#   AI_PLATFORM_COOKIE_SECURE=1      required behind HTTPS (production)
set -eu

umask 0002

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AI_PLATFORM_DEMO2_ENV_SOURCE="$ROOT/scripts/demo2-env.sh"
# shellcheck source=scripts/demo2-env.sh
. "$ROOT/scripts/demo2-env.sh"

export PYTHONDONTWRITEBYTECODE=1

exec "$ROOT/.venv/bin/ai-platform" serve --host 127.0.0.1 --port "${AI_PLATFORM_API_PORT:-8765}"
