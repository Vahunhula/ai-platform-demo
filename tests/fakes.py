"""Deterministic coding executor used only by automated tests.

For the read-only phases (BRAINSTORM/PLAN/REVIEW) this returns a clean,
no-issues structured result by default, so a scripted happy path can flow
through to HUMAN_REVIEW with no human in the loop. Pass
``brainstorm_payload``/``plan_payload``/``review_payload`` to script a
specific phase's structured output for tests that exercise the readiness
gate itself (blocking questions, findings, malformed output, and so on).
"""

from ai_platform.executors.base import (
    AgentActivity,
    AgentActivityType,
    ExecutionRequest,
    ExecutionResult,
    ExecutorPreflight,
)
from ai_platform.models import ModelTier
from ai_platform.workflow import WorkflowPhase

CLEAN_BRAINSTORM_PAYLOAD = {
    "summary": "Understood the requested change and inspected the relevant module.",
    "assumptions": ["The described behavior is the only requirement."],
    "options": ["Make the described change directly."],
    "questions": [],
}
CLEAN_PLAN_PAYLOAD = {
    "summary": "Implement the described change.",
    "files": ["app"],
    "steps": ["Make the described change.", "Run the test suite."],
    "tests": ["pytest"],
    "risks": [],
    "open_questions": [],
}
CLEAN_REVIEW_PAYLOAD = {
    "summary": "No issues found.",
    "critical_findings": [],
    "major_findings": [],
    "minor_findings": [],
    "requirements_assessment": "Requirements are met.",
    "test_assessment": "Deterministic tests cover the change.",
    "convention_assessment": "Consistent with repository conventions.",
}


class FakeAgentExecutor:
    """Optionally make the known tiny fix on a configured IMPLEMENTATION attempt."""

    def __init__(
        self,
        fix_on_attempt: int | None = 1,
        fix_on_tier: ModelTier | None = None,
        *,
        brainstorm_payload: dict | None = CLEAN_BRAINSTORM_PAYLOAD,
        plan_payload: dict | None = CLEAN_PLAN_PAYLOAD,
        review_payload: dict | None = CLEAN_REVIEW_PAYLOAD,
        mutate_during_read_only: bool = False,
    ) -> None:
        self.fix_on_attempt = fix_on_attempt
        self.fix_on_tier = fix_on_tier
        self.brainstorm_payload = brainstorm_payload
        self.plan_payload = plan_payload
        self.review_payload = review_payload
        self.mutate_during_read_only = mutate_during_read_only
        self.requests: list[ExecutionRequest] = []

    def preflight(self) -> ExecutorPreflight:
        return ExecutorPreflight(
            provider="fake",
            sdk_version="test",
            authentication_method="none",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if request.phase is WorkflowPhase.BRAINSTORM:
            return self._structured_result(self.brainstorm_payload, request)
        if request.phase is WorkflowPhase.PLAN:
            return self._structured_result(self.plan_payload, request)
        if request.phase is WorkflowPhase.REVIEW:
            return self._structured_result(self.review_payload, request)
        return self._implementation_result(request)

    def _implementation_result(self, request: ExecutionRequest) -> ExecutionResult:
        tier_matches = self.fix_on_tier is None or request.selection.tier is self.fix_on_tier
        if (
            self.fix_on_attempt is not None
            and request.attempt >= self.fix_on_attempt
            and tier_matches
        ):
            self._apply_known_fix(request)
        return ExecutionResult(
            succeeded=True,
            summary=f"Fake attempt {request.attempt}",
            activities=[
                AgentActivity(
                    activity_type=AgentActivityType.MESSAGE,
                    metadata={"message": "Fake executor completed"},
                )
            ],
            usage={"turns": 1},
            session_id=f"fake-{request.attempt}",
            structured_output={
                "summary": f"Fake attempt {request.attempt}",
                "files_changed": [],
                "tests_run": [],
                "known_issues": [],
            },
        )

    def _structured_result(
        self, payload: dict | None, request: ExecutionRequest
    ) -> ExecutionResult:
        if self.mutate_during_read_only:
            (request.workspace_path / "unexpected-read-only-write.txt").write_text("mutated")
        if payload is None:
            return ExecutionResult(
                succeeded=True,
                summary="Malformed structured result",
                activities=[
                    AgentActivity(
                        activity_type=AgentActivityType.TOOL_CALL,
                        metadata={"tool": "Read", "path": "app"},
                    )
                ],
                usage={"turns": 1},
                session_id=f"fake-{request.phase.value}-{request.attempt}",
                structured_output=None,
            )
        return ExecutionResult(
            succeeded=True,
            summary=str(payload.get("summary", "Fake structured result")),
            activities=[
                AgentActivity(
                    activity_type=AgentActivityType.TOOL_CALL,
                    metadata={"tool": "Read", "path": "app"},
                )
            ],
            usage={"turns": 1},
            session_id=f"fake-{request.phase.value}-{request.attempt}",
            structured_output=payload,
        )

    @staticmethod
    def _apply_known_fix(request: ExecutionRequest) -> None:
        workspace = request.workspace_path
        if request.task.id == "DEMO-1":
            path = workspace / "app" / "messages.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace("Welocme", "Welcome"),
                encoding="utf-8",
            )
        elif request.task.id == "DEMO-2":
            path = workspace / "app" / "discounts.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace("return 0.05", "return 0.10"),
                encoding="utf-8",
            )
        elif request.task.id == "DEMO-3":
            path = workspace / "app" / "users.py"
            old = 'return f"{last_name.strip()}, {first_name.strip()}"'
            new = "return profile_display_name(first_name, last_name)"
            path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
