# Demo 2 runtime environment. Source it; do not execute it:
#
#     . scripts/demo2-env.sh
#
# Points every AI_PLATFORM_* runtime path of this Demo 2 checkout at the
# dedicated Demo 2 runtime, /var/lib/ai-platform-demo2. Variables that are
# already set win (explicit overrides, e.g. a copied test runtime), but a path
# inside the frozen Demo 1 runtime, /var/lib/ai-platform, is refused unless
# AI_PLATFORM_ALLOW_DEMO1_RUNTIME=1 is set deliberately.
#
# /var/lib/ai-platform        frozen Demo 1 runtime (used by /usr/local/bin/ai-platform)
# /var/lib/ai-platform-demo2  mutable Demo 2 / showcase runtime (users, auth, resets)

# Locate this checkout: the launcher passes its own path; bash exposes BASH_SOURCE.
_demo2_self="${AI_PLATFORM_DEMO2_ENV_SOURCE:-${BASH_SOURCE:-$0}}"
_demo2_root=$(CDPATH= cd -- "$(dirname -- "$_demo2_self")/.." 2>/dev/null && pwd)
unset _demo2_self
if [ ! -f "$_demo2_root/tasks.json" ]; then
    # Sourced interactively ($0 is the shell): fall back to the current directory.
    _demo2_root=$(pwd)
fi

AI_PLATFORM_DEMO2_RUNTIME=/var/lib/ai-platform-demo2
AI_PLATFORM_DEMO1_RUNTIME=/var/lib/ai-platform

export AI_PLATFORM_TASK_FILE="${AI_PLATFORM_TASK_FILE:-$_demo2_root/tasks.json}"
export AI_PLATFORM_DEMO_REPO="${AI_PLATFORM_DEMO_REPO:-$_demo2_root/demo_repo}"
export AI_PLATFORM_DATA_DIR="${AI_PLATFORM_DATA_DIR:-$AI_PLATFORM_DEMO2_RUNTIME}"
export AI_PLATFORM_WORKSPACE_ROOT="${AI_PLATFORM_WORKSPACE_ROOT:-$AI_PLATFORM_DATA_DIR/workspaces}"
export AI_PLATFORM_DB_PATH="${AI_PLATFORM_DB_PATH:-$AI_PLATFORM_DATA_DIR/platform.db}"
export AI_PLATFORM_CHECKPOINT_DB_PATH="${AI_PLATFORM_CHECKPOINT_DB_PATH:-$AI_PLATFORM_DATA_DIR/langgraph-checkpoints.db}"

_demo2_in_demo1() {
    case "$1" in
        "$AI_PLATFORM_DEMO1_RUNTIME" | "$AI_PLATFORM_DEMO1_RUNTIME"/*) return 0 ;;
    esac
    return 1
}

_demo2_refused=""
for _demo2_path in "$AI_PLATFORM_DATA_DIR" "$AI_PLATFORM_WORKSPACE_ROOT" \
    "$AI_PLATFORM_DB_PATH" "$AI_PLATFORM_CHECKPOINT_DB_PATH"; do
    if _demo2_in_demo1 "$_demo2_path"; then
        _demo2_refused="$_demo2_path"
    fi
done
unset _demo2_path _demo2_root

if [ -n "$_demo2_refused" ] && [ "${AI_PLATFORM_ALLOW_DEMO1_RUNTIME:-}" != "1" ]; then
    echo "demo2-env: refusing $_demo2_refused — it is inside the frozen Demo 1 runtime" \
        "($AI_PLATFORM_DEMO1_RUNTIME)." >&2
    echo "demo2-env: unset the AI_PLATFORM_* path variables to use" \
        "$AI_PLATFORM_DEMO2_RUNTIME, or set AI_PLATFORM_ALLOW_DEMO1_RUNTIME=1 deliberately." >&2
    unset _demo2_refused
    return 1 2>/dev/null || exit 1
fi
unset _demo2_refused

echo "demo2-env: runtime $AI_PLATFORM_DATA_DIR (db $AI_PLATFORM_DB_PATH)" >&2
