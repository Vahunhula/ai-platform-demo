#!/bin/sh
set -eu

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (version 3.12 or newer)." >&2
    exit 1
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required.")'

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

python - <<'PY'
from ai_platform.config import Settings

settings = Settings.from_env()
runtime_directories = {
    settings.data_dir,
    settings.workspace_root,
    settings.db_path.parent,
    settings.checkpoint_db_path.parent,
}
for directory in runtime_directories:
    directory.mkdir(parents=True, exist_ok=True)
PY

echo "Runtime directories are ready. Doctor may request 'claude auth login'."
ai-platform doctor
