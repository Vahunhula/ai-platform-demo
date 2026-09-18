"""The Demo 2 runtime helper never points this checkout at the frozen Demo 1 runtime."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts" / "demo2-env.sh"
LAUNCHER = ROOT / "scripts" / "serve_api_dev.sh"
PATHS = (
    "AI_PLATFORM_DATA_DIR",
    "AI_PLATFORM_WORKSPACE_ROOT",
    "AI_PLATFORM_DB_PATH",
    "AI_PLATFORM_CHECKPOINT_DB_PATH",
)


def _source(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = f'. "{HELPER}" && env | grep "^AI_PLATFORM_" | sort'
    return subprocess.run(
        ["sh", "-c", script],
        env={"PATH": "/usr/bin:/bin", "AI_PLATFORM_DEMO2_ENV_SOURCE": str(HELPER), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _values(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_defaults_to_the_dedicated_demo2_runtime() -> None:
    result = _source({})
    values = _values(result)

    assert result.returncode == 0
    assert values["AI_PLATFORM_DATA_DIR"] == "/var/lib/ai-platform-demo2"
    assert values["AI_PLATFORM_WORKSPACE_ROOT"] == "/var/lib/ai-platform-demo2/workspaces"
    assert values["AI_PLATFORM_DB_PATH"] == "/var/lib/ai-platform-demo2/platform.db"
    assert values["AI_PLATFORM_CHECKPOINT_DB_PATH"] == (
        "/var/lib/ai-platform-demo2/langgraph-checkpoints.db"
    )
    assert values["AI_PLATFORM_TASK_FILE"] == str(ROOT / "tasks.json")


def test_explicit_paths_override_the_default(tmp_path: Path) -> None:
    values = _values(_source({"AI_PLATFORM_DATA_DIR": str(tmp_path)}))

    assert values["AI_PLATFORM_DB_PATH"] == f"{tmp_path}/platform.db"
    assert values["AI_PLATFORM_WORKSPACE_ROOT"] == f"{tmp_path}/workspaces"


def test_frozen_demo1_runtime_is_refused_unless_explicitly_allowed() -> None:
    for name in PATHS:
        refused = _source({name: "/var/lib/ai-platform/platform.db"})
        assert refused.returncode == 1, name
        assert "frozen Demo 1 runtime" in refused.stderr
        assert refused.stdout == ""

    allowed = _source(
        {"AI_PLATFORM_DATA_DIR": "/var/lib/ai-platform", "AI_PLATFORM_ALLOW_DEMO1_RUNTIME": "1"}
    )
    assert allowed.returncode == 0
    assert _values(allowed)["AI_PLATFORM_DB_PATH"] == "/var/lib/ai-platform/platform.db"
    # A sibling directory with a shared prefix is not the Demo 1 runtime.
    assert _source({"AI_PLATFORM_DATA_DIR": "/var/lib/ai-platform-other"}).returncode == 0


def test_launcher_refuses_demo1_before_starting_anything() -> None:
    result = subprocess.run(
        [str(LAUNCHER)],
        env={"PATH": "/usr/bin:/bin", "AI_PLATFORM_DB_PATH": "/var/lib/ai-platform/platform.db"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert "frozen Demo 1 runtime" in result.stderr
    assert "Started server" not in result.stderr + result.stdout
