"""Deterministic coding executor used only by automated tests."""

from ai_platform.executors.base import (
    AgentActivity,
    AgentActivityType,
    ExecutionRequest,
    ExecutionResult,
    ExecutorPreflight,
)


class FakeAgentExecutor:
    """Optionally make the known tiny fix on a configured attempt."""

    def __init__(self, fix_on_attempt: int | None = 1) -> None:
        self.fix_on_attempt = fix_on_attempt
        self.requests: list[ExecutionRequest] = []

    def preflight(self) -> ExecutorPreflight:
        return ExecutorPreflight(
            provider="fake",
            sdk_version="test",
            authentication_method="none",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if self.fix_on_attempt is not None and request.attempt >= self.fix_on_attempt:
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
