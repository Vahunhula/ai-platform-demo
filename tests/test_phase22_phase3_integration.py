"""Integration coverage for baseline-aware verification in the Phase 3 graph."""

import subprocess
from pathlib import Path

import pytest

from ai_platform.api.presenters import _render_artifact_body
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors.base import ExecutionRequest, ExecutionResult
from ai_platform.executors.claude import ClaudeAgentExecutor
from ai_platform.graph import run_task_graph
from ai_platform.models import (
    ModelTier,
    TaskDefinition,
    TaskDifficulty,
    TaskStatus,
    VerificationConfig,
    VerificationStatus,
    VerificationType,
)
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.verification import BaselineContext
from ai_platform.workflow import ArtifactKind, WorkflowPhase
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _baseline_repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    (repository / "app").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repository / "app" / "__init__.py").write_text("", encoding="utf-8")
    for number in range(1, 4):
        (repository / "tests" / f"test_unrelated_{number}.py").write_text(
            f"def test_known_baseline_failure_{number}(): assert False\n",
            encoding="utf-8",
        )
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline with known failure")
    return repository, _git(repository, "rev-parse", "HEAD")


def _task() -> TaskDefinition:
    return TaskDefinition(
        id="BASELINE-PHASE3",
        title="Integrate baseline verification",
        description="Add a scoped feature without regressing unrelated behavior.",
        difficulty=TaskDifficulty.MEDIUM,
        acceptance_criteria=["The scoped feature test passes"],
        verification=VerificationConfig(type=VerificationType.PYTEST, targets=["tests"]),
    )


def _router() -> ModelRouter:
    return ModelRouter(
        {
            ModelTier.CHEAP: "haiku-test",
            ModelTier.DEFAULT: "sonnet-test",
            ModelTier.STRONG: "opus-test",
        }
    )


class ScopedChangeExecutor(FakeAgentExecutor):
    def __init__(
        self, *, passing: bool, narrative: str = "Implemented the scoped feature."
    ) -> None:
        super().__init__()
        self.passing = passing
        self.narrative = narrative

    def _implementation_result(self, request: ExecutionRequest):
        (request.workspace_path / "app" / "feature.py").write_text(
            "def value(): return 42\n", encoding="utf-8"
        )
        expected = "42" if self.passing else "99"
        (request.workspace_path / "tests" / "test_feature.py").write_text(
            "from app.feature import value\n"
            f"def test_feature(): assert value() == {expected}\n",
            encoding="utf-8",
        )
        result = super()._implementation_result(request)
        return ExecutionResult(
            **result.model_dump(exclude={"summary", "structured_output"}),
            summary=self.narrative,
            structured_output={
                "summary": self.narrative,
                "files_changed": ["provider/invented.py"],
                "tests_run": ["Provider claims an authoritative result"],
                "known_issues": [],
            },
        )


def _run(tmp_path: Path, *, passing: bool, narrative: str = "Implemented the scoped feature."):
    repository, commit = _baseline_repository(tmp_path)
    task = _task()
    storage = SQLiteStorage(tmp_path / "data" / "platform.db")
    storage.initialize()
    storage.create_task(task)
    storage.transition_workflow_phase(
        task.id,
        WorkflowPhase.IMPLEMENTATION,
        WorkflowPhase.BRAINSTORM,
        Event(
            task_id=task.id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="test-setup",
            metadata={
                "from_phase": "IMPLEMENTATION",
                "to_phase": "BRAINSTORM",
                "transition_mode": "MANUAL",
            },
        ),
    )
    workspaces = LocalWorkspaceProvider(tmp_path / "workspaces", repository)
    workspace = workspaces.create(task.id, repository, commit)
    storage.update_workspace_path(task.id, workspace)
    executor = ScopedChangeExecutor(passing=passing, narrative=narrative)
    state = run_task_graph(
        task,
        _router(),
        storage,
        workspaces,
        executor,
        tmp_path / "data" / "checkpoints.db",
        verification_timeout_seconds=20,
        max_attempts_per_tier=1,
        actor_id="test",
        continuation=True,
        baseline_context=BaselineContext(
            repository_id="baseline-phase3",
            source_repository=repository,
            source_commit=commit,
        ),
    )
    return state, storage, executor


def _verification_events(storage: SQLiteStorage) -> list[Event]:
    return [
        event
        for event in storage.get_events("BASELINE-PHASE3")
        if event.event_type in {EventType.TEST_PASSED, EventType.TEST_FAILED}
    ]


def test_known_baseline_failure_is_non_blocking_in_phase3(tmp_path: Path) -> None:
    state, storage, executor = _run(tmp_path, passing=True)

    record = storage.get_task("BASELINE-PHASE3")
    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert record.workflow_phase is WorkflowPhase.HUMAN_REVIEW
    assert record.verification_status is VerificationStatus.PASSED
    event = _verification_events(storage)[-1]
    assert event.event_type is EventType.TEST_PASSED
    assert event.metadata["verification_mode"] == "BASELINE_AWARE"
    assert event.metadata["baseline_warning_count"] == 3
    assert event.metadata["new_regression_count"] == 0
    assert WorkflowPhase.REVIEW in [request.phase for request in executor.requests]


def test_new_task_failure_blocks_phase3_and_is_not_baselined(tmp_path: Path) -> None:
    state, storage, executor = _run(tmp_path, passing=False)

    record = storage.get_task("BASELINE-PHASE3")
    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert record.verification_status is VerificationStatus.FAILED
    event = _verification_events(storage)[-1]
    assert event.event_type is EventType.TEST_FAILED
    assert event.metadata["baseline_warning_count"] == 3
    assert event.metadata["new_regression_count"] == 1
    assert event.metadata["new_failures"] == ["tests/test_feature.py::test_feature"]
    assert WorkflowPhase.REVIEW not in [request.phase for request in executor.requests]


@pytest.mark.parametrize("narrative", ["All tests pass.", "Tests are still failing."])
def test_canonical_verification_overrides_provider_narrative(
    tmp_path: Path, narrative: str
) -> None:
    state, storage, executor = _run(tmp_path, passing=True, narrative=narrative)

    record = storage.get_task("BASELINE-PHASE3")
    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert record.workflow_phase is WorkflowPhase.HUMAN_REVIEW
    assert record.verification_status is VerificationStatus.PASSED

    artifacts = storage.list_workflow_artifacts("BASELINE-PHASE3", current_only=True)
    implementation = next(
        artifact for artifact in artifacts if artifact.kind is ArtifactKind.IMPLEMENTATION_SUMMARY
    )
    payload = implementation.payload
    canonical = payload["canonical_verification"]
    assert payload["summary"] == narrative
    assert payload["files_changed"] == ["app/feature.py", "tests/test_feature.py"]
    assert "provider/invented.py" not in payload["files_changed"]
    assert payload["source_commit"]
    assert canonical["status"] == "PASS"
    assert canonical["task_specific_passed"] is True
    assert canonical["task_specific_passed_tests"] == 1
    assert canonical["known_baseline_failures"] == 3
    assert canonical["new_regressions"] == 0
    assert payload["tests_run"] == [
        "Platform verification: PASS",
        "Focused/task verification: PASS (1 passed)",
        "Known baseline failures: 3 unchanged",
        "New regressions: 0",
    ]

    review_request = next(
        request for request in executor.requests if request.phase is WorkflowPhase.REVIEW
    )
    assert review_request.canonical_changed_files == [
        "app/feature.py",
        "tests/test_feature.py",
    ]
    assert review_request.canonical_verification == canonical
    assert review_request.source_commit == payload["source_commit"]

    review_prompt = ClaudeAgentExecutor._build_review_prompt(  # noqa: SLF001
        object.__new__(ClaudeAgentExecutor), review_request
    )
    assert "NON-AUTHORITATIVE implementation narrative" in review_prompt
    assert narrative in review_prompt
    assert "AUTHORITATIVE persisted verification evidence" in review_prompt
    assert '"known_baseline_failures": 3' in review_prompt
    assert '"new_regressions": 0' in review_prompt
    assert "Never create a finding solely" in review_prompt

    legacy_request = review_request.model_copy(
        update={
            "upstream_artifacts": {
                **review_request.upstream_artifacts,
                "IMPLEMENTATION_SUMMARY": {
                    "summary": "Legacy claim: all tests pass.",
                    "files_changed": ["legacy/provider-claim.py"],
                    "tests_run": ["all pass"],
                    "known_issues": [],
                },
            }
        }
    )
    legacy_prompt = ClaudeAgentExecutor._build_review_prompt(  # noqa: SLF001
        object.__new__(ClaudeAgentExecutor), legacy_request
    )
    assert "Legacy claim: all tests pass." in legacy_prompt
    assert "legacy/provider-claim.py" not in legacy_prompt
    assert '"files": [' in legacy_prompt
    assert '"app/feature.py"' in legacy_prompt
    assert '"known_baseline_failures": 3' in legacy_prompt

    rendered = _render_artifact_body(ArtifactKind.IMPLEMENTATION_SUMMARY, payload)
    assert "Implementation notes (descriptive)" in rendered
    assert "Provider-authored narrative; canonical facts follow." in rendered
    assert "Changed files (platform-generated)" in rendered
    assert "Verification (platform-generated)" in rendered
    assert "Known baseline failures: 3 unchanged" in rendered
    assert "New regressions: 0" in rendered


def test_provider_success_claim_cannot_override_real_new_regression(tmp_path: Path) -> None:
    state, storage, executor = _run(
        tmp_path,
        passing=False,
        narrative="Everything passes.",
    )

    record = storage.get_task("BASELINE-PHASE3")
    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert record.verification_status is VerificationStatus.FAILED
    assert WorkflowPhase.REVIEW not in [request.phase for request in executor.requests]

    implementation = next(
        artifact
        for artifact in storage.list_workflow_artifacts(
            "BASELINE-PHASE3", current_only=True
        )
        if artifact.kind is ArtifactKind.IMPLEMENTATION_SUMMARY
    )
    canonical = implementation.payload["canonical_verification"]
    assert canonical["status"] == "FAIL"
    assert canonical["known_baseline_failures"] == 3
    assert canonical["new_regressions"] == 1
    assert implementation.payload["tests_run"][0] == "Platform verification: FAIL"


def test_verifier_infrastructure_failure_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("verifier unavailable")

    monkeypatch.setattr("ai_platform.graph.verify_registered_task", unavailable)
    state, storage, executor = _run(
        tmp_path,
        passing=True,
        narrative="Everything passes.",
    )

    record = storage.get_task("BASELINE-PHASE3")
    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert record.verification_status is VerificationStatus.FAILED
    assert WorkflowPhase.REVIEW not in [request.phase for request in executor.requests]

    implementation = next(
        artifact
        for artifact in storage.list_workflow_artifacts(
            "BASELINE-PHASE3", current_only=True
        )
        if artifact.kind is ArtifactKind.IMPLEMENTATION_SUMMARY
    )
    canonical = implementation.payload["canonical_verification"]
    assert canonical["status"] == "FAIL"
    assert canonical["infrastructure_error"] == "verifier unavailable"
    assert "Verification infrastructure: verifier unavailable" in implementation.payload[
        "tests_run"
    ]
