"""Claude Agent SDK implementation of the provider-neutral executor."""

import asyncio
import importlib.metadata
import importlib.util
import json
from pathlib import Path
from typing import Any

from ai_platform.claude_auth import ClaudeAuthState, inspect_claude_auth
from ai_platform.config import Settings
from ai_platform.executors.base import (
    AgentActivity,
    AgentActivityType,
    AgentAuthenticationError,
    AgentExecutorError,
    ExecutionRequest,
    ExecutionResult,
    ExecutorPreflight,
)
from ai_platform.workflow import WorkflowPhase

_READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
_IMPLEMENTATION_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
_TOOLS_BY_PHASE = {
    WorkflowPhase.BRAINSTORM: _READ_ONLY_TOOLS,
    WorkflowPhase.PLAN: _READ_ONLY_TOOLS,
    WorkflowPhase.IMPLEMENTATION: _IMPLEMENTATION_TOOLS,
    WorkflowPhase.REVIEW: _READ_ONLY_TOOLS,
}
READ_ONLY_PHASES = frozenset({WorkflowPhase.BRAINSTORM, WorkflowPhase.PLAN, WorkflowPhase.REVIEW})
_MAX_ACTIVITY_TEXT = 4000


class ClaudeAgentExecutor:
    """Run one bounded Claude coding session in an assigned local workspace."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preflight(self) -> ExecutorPreflight:
        """Check SDK availability and identify credential precedence without secrets."""

        if importlib.util.find_spec("claude_agent_sdk") is None:
            raise AgentExecutorError(
                'Claude Agent SDK is unavailable. Run: python -m pip install -e ".[dev]"',
                fatal=True,
            )
        version = importlib.metadata.version("claude-agent-sdk")
        return ExecutorPreflight(
            provider="claude",
            sdk_version=version,
            authentication_method=self._authentication_method(),
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a bounded Agent SDK session and normalize its public messages."""

        workspace = request.workspace_path.resolve()
        if not workspace.is_dir():
            raise AgentExecutorError(f"Workspace does not exist: {workspace}", fatal=True)
        try:
            return asyncio.run(self._execute_async(request, workspace))
        except TimeoutError as error:
            raise AgentExecutorError(
                f"Claude execution timed out after {self.settings.agent_timeout_seconds} seconds"
            ) from error
        except AgentExecutorError:
            raise
        except Exception as error:
            message = self._redact(str(error))[:1000] or type(error).__name__
            fatal = self._looks_fatal(message)
            raise AgentExecutorError(f"Claude Agent SDK failed: {message}", fatal=fatal) from error

    async def _execute_async(self, request: ExecutionRequest, workspace: Path) -> ExecutionResult:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        sdk_env: dict[str, str] = {}
        if self.settings.anthropic_api_key:
            sdk_env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key

        read_only = request.phase in READ_ONLY_PHASES
        tools = _TOOLS_BY_PHASE.get(request.phase, _IMPLEMENTATION_TOOLS)
        system_append = (
            "Work only inside the configured current working directory. "
            "Do not inspect parent directories, credentials, or unrelated "
            "environment data. "
            "Do not commit or push. Make the smallest change required by the task."
        )
        if read_only:
            system_append += (
                " This phase is READ-ONLY: inspect the repository but do not edit, "
                "write, create, delete, or run any command that mutates the workspace. "
                "Only Read, Glob and Grep tools are available to you; no others exist "
                "in this session."
            )
        options = ClaudeAgentOptions(
            tools=tools,
            allowed_tools=tools,
            permission_mode="acceptEdits" if not read_only else "default",
            cwd=workspace,
            model=request.selection.model,
            max_turns=self.settings.agent_max_turns,
            strict_mcp_config=True,
            mcp_servers={},
            setting_sources=[],
            env=sdk_env,
            output_format=(
                {"type": "json_schema", "schema": request.output_schema}
                if request.output_schema
                else None
            ),
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": system_append,
            },
        )
        activities: list[AgentActivity] = []
        final_result: Any = None
        cancellation_observed = False

        async with asyncio.timeout(self.settings.agent_timeout_seconds):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(self._build_prompt(request))
                stop_monitor = asyncio.Event()
                monitor = asyncio.create_task(
                    self._monitor_cancellation(client, request, stop_monitor)
                )
                try:
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage) or (
                            isinstance(message, UserMessage) and isinstance(message.content, list)
                        ):
                            activities.extend(
                                self._normalize_blocks(
                                    message.content,
                                    workspace,
                                    TextBlock,
                                    ToolUseBlock,
                                    ToolResultBlock,
                                )
                            )
                        elif isinstance(message, ResultMessage):
                            final_result = message
                finally:
                    stop_monitor.set()
                    cancellation_observed = await monitor

        if final_result is None:
            if cancellation_observed:
                return ExecutionResult(
                    succeeded=False,
                    summary="Claude execution interrupted after a human pause request",
                    activities=activities,
                    cancelled=True,
                )
            raise AgentExecutorError("Claude Agent SDK returned no final result")

        summary = self._redact(final_result.result or final_result.subtype)[:_MAX_ACTIVITY_TEXT]
        error = summary if final_result.is_error and not cancellation_observed else None
        structured_output = (
            final_result.structured_output
            if isinstance(final_result.structured_output, dict)
            else None
        )
        return ExecutionResult(
            succeeded=not final_result.is_error and not cancellation_observed,
            summary=summary,
            activities=activities,
            usage=self._usage_metadata(final_result),
            session_id=final_result.session_id,
            error=error,
            fatal=self._looks_fatal(error or ""),
            cancelled=cancellation_observed,
            structured_output=structured_output,
        )

    def _build_prompt(self, request: ExecutionRequest) -> str:
        if request.phase is WorkflowPhase.BRAINSTORM:
            return self._build_brainstorm_prompt(request)
        if request.phase is WorkflowPhase.PLAN:
            return self._build_plan_prompt(request)
        if request.phase is WorkflowPhase.REVIEW:
            return self._build_review_prompt(request)
        return self._build_implementation_prompt(request)

    def _build_brainstorm_prompt(self, request: ExecutionRequest) -> str:
        criteria = "\n".join(f"- {item}" for item in request.task.acceptance_criteria)
        feedback = "\n".join(f"- {message}" for message in request.human_messages)
        repair = self._repair_notice(request)
        return f"""Task: {request.task.id}
Title: {request.task.title}

Description:
{request.task.description}

Acceptance criteria:
{criteria}

Human feedback so far (if any):
{feedback or "- None"}
{repair}
Instructions:
- This is the BRAINSTORM phase. You are READ-ONLY: inspect the repository
  (Read, Glob, Grep only) to understand what this request actually requires.
- Do not edit, write, or run any mutating command.
- Identify the relevant code areas, a viable implementation direction, and any
  blocking questions that need a human answer before planning can proceed.
- Return a Brainstorm Summary matching the required JSON schema: a short
  summary, documented assumptions, one or more viable options, and any
  blocking open questions (empty if none).
"""

    def _build_plan_prompt(self, request: ExecutionRequest) -> str:
        criteria = "\n".join(f"- {item}" for item in request.task.acceptance_criteria)
        feedback = "\n".join(f"- {message}" for message in request.human_messages)
        brainstorm = request.upstream_artifacts.get("BRAINSTORM_SUMMARY")
        repair = self._repair_notice(request)
        return f"""Task: {request.task.id}
Title: {request.task.title}

Description:
{request.task.description}

Acceptance criteria:
{criteria}

Latest Brainstorm Summary:
{self._format_artifact(brainstorm)}

Human feedback so far (if any):
{feedback or "- None"}
{repair}
Instructions:
- This is the PLAN phase. You are READ-ONLY: inspect the repository (Read,
  Glob, Grep only) to identify specific files and symbols. Do not edit,
  write, or run any mutating command.
- Only name specific files/paths you actually observed in the repository;
  do not fabricate paths.
- Return a Plan matching the required JSON schema: summary, affected files,
  ordered implementation steps, a test/validation strategy, risks, and any
  open questions blocking implementation (empty if none).
"""

    def _build_review_prompt(self, request: ExecutionRequest) -> str:
        criteria = "\n".join(f"- {item}" for item in request.task.acceptance_criteria)
        brainstorm = request.upstream_artifacts.get("BRAINSTORM_SUMMARY")
        plan = request.upstream_artifacts.get("PLAN")
        implementation_summary = request.upstream_artifacts.get("IMPLEMENTATION_SUMMARY")
        repair = self._repair_notice(request)
        return f"""Task: {request.task.id}
Title: {request.task.title}

Original requirements:
{request.task.description}

Acceptance criteria:
{criteria}

Latest Brainstorm Summary:
{self._format_artifact(brainstorm)}

Latest Plan:
{self._format_artifact(plan)}

Latest Implementation Summary:
{self._format_artifact(implementation_summary)}

Deterministic verification result: {request.current_verification}

Actual current workspace Diff (may be truncated):
{request.workspace_diff[-8000:] or "<no changes>"}
{repair}
Instructions:
- This is the REVIEW phase: a fresh, independent review. You have not seen
  any prior implementation conversation; judge only the requirements, plan,
  summary, diff, and verification result above.
- You are READ-ONLY: inspect the repository (Read, Glob, Grep only) to check
  conventions and correctness. Do not edit, write, or run any mutating
  command.
- Actively look for: requirement misses, regressions, incorrect assumptions,
  unsafe changes, missing tests, unnecessary complexity, repository
  convention violations, and diff changes outside the expected scope.
- Return a Review Report matching the required JSON schema: summary,
  critical/major/minor findings (empty lists if none), and your assessment
  of requirements, tests, and conventions.
"""

    @staticmethod
    def _format_artifact(payload: dict[str, Any] | None) -> str:
        if not payload:
            return "<none available>"
        return json.dumps(payload, indent=2, sort_keys=True)[:6000]

    @staticmethod
    def _repair_notice(request: ExecutionRequest) -> str:
        if not request.format_repair_attempt:
            return ""
        return (
            "\nYour previous response for this phase was not valid JSON matching "
            "the required schema. Return ONLY the JSON object described by the "
            "schema; no prose outside it.\n"
        )

    def _build_implementation_prompt(self, request: ExecutionRequest) -> str:
        criteria = "\n".join(f"- {item}" for item in request.task.acceptance_criteria)
        targets = " ".join(request.task.verification.targets)
        human_context = "\n".join(f"- {message}" for message in request.human_messages)
        agent_context = "\n".join(f"- {message}" for message in request.recent_agent_messages)
        retry = ""
        if request.previous_failure:
            retry = (
                "\nPrevious deterministic verification failed. Continue from the current "
                "workspace and use this failure output:\n\n"
                f"{request.previous_failure[-4000:]}\n"
            )
        continuation = ""
        if request.continuation:
            continuation = f"""
This is a follow-up turn in the existing shared TaskSession. Start a fresh SDK
turn from durable platform context; the current workspace is authoritative.
Current platform verification: {request.current_verification}
Recent human instructions:
{human_context or "- None"}
Recent agent responses:
{agent_context or "- None"}
Current Git diff (may be empty or truncated):
{request.workspace_diff[-6000:] or "<clean>"}
"""
        manual = ""
        if request.human_workspace_changed:
            manual = (
                "\nA human may have edited the workspace while paused. Preserve and review "
                "the current files; do not restore an older agent version over them.\n"
            )
        return f"""Task: {request.task.id}
Execution ID: {request.execution_id}
Title: {request.task.title}

Description:
{request.task.description}

Acceptance criteria:
{criteria}
{continuation}
{manual}
{retry}
Instructions:
- Inspect the repository before editing.
- Make the smallest correct change.
- Do not modify unrelated behavior.
- Run the relevant tests after the change if practical: python -m pytest {targets}
- Do not commit or push.
- Stop after implementation and verification.
- Report what changed and what verification was run, and any known issues,
  as the required Implementation Summary JSON.
"""

    @staticmethod
    async def _monitor_cancellation(
        client: Any,
        request: ExecutionRequest,
        stop_monitor: asyncio.Event,
    ) -> bool:
        """Poll durable pause state and safely interrupt this process's live SDK client."""

        if request.cancellation_requested is None:
            await stop_monitor.wait()
            return False
        while not stop_monitor.is_set():
            if request.cancellation_requested():
                await client.interrupt()
                return True
            try:
                await asyncio.wait_for(stop_monitor.wait(), timeout=0.35)
            except TimeoutError:
                continue
        return False

    def _authentication_method(self) -> str:
        status = inspect_claude_auth(self.settings)
        if status.state in {ClaudeAuthState.AUTHENTICATED, ClaudeAuthState.CONFIGURED}:
            suffix = (
                " (configured, not validated)"
                if status.state is ClaudeAuthState.CONFIGURED
                else ""
            )
            return status.method + suffix
        if status.state is ClaudeAuthState.MISSING:
            raise AgentAuthenticationError(
                "Claude authentication is unavailable or expired. "
                f"{status.detail}. Run the supported command: claude auth login. "
                "Alternatively set ANTHROPIC_API_KEY for Console API billing."
            )
        raise AgentAuthenticationError(
            "Claude authentication status is unknown. "
            f"{status.detail}. Verify with 'claude auth status --json' or run "
            "'claude auth login', then retry."
        )

    def _normalize_blocks(
        self,
        blocks: list[Any],
        workspace: Path,
        text_type: type,
        tool_use_type: type,
        tool_result_type: type,
    ) -> list[AgentActivity]:
        activities: list[AgentActivity] = []
        for block in blocks:
            if isinstance(block, text_type) and block.text.strip():
                activities.append(
                    AgentActivity(
                        activity_type=AgentActivityType.MESSAGE,
                        metadata={"message": self._redact(block.text)[:_MAX_ACTIVITY_TEXT]},
                    )
                )
            elif isinstance(block, tool_use_type):
                activities.append(
                    AgentActivity(
                        activity_type=AgentActivityType.TOOL_CALL,
                        metadata={
                            "tool": block.name,
                            **self._safe_tool_input(block.input, workspace),
                        },
                    )
                )
            elif isinstance(block, tool_result_type):
                content = self._redact(str(block.content or ""))[:1000]
                activities.append(
                    AgentActivity(
                        activity_type=AgentActivityType.TOOL_RESULT,
                        metadata={
                            "tool_use_id": block.tool_use_id,
                            "is_error": bool(block.is_error),
                            "summary": content,
                        },
                    )
                )
        return activities

    def _safe_tool_input(self, tool_input: dict[str, Any], workspace: Path) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key in ("file_path", "path", "pattern", "glob", "command"):
            if key not in tool_input:
                continue
            value = self._redact(str(tool_input[key]))[:1000]
            if key in {"file_path", "path"}:
                value = self._display_path(value, workspace)
            safe[key] = value
        return safe

    @staticmethod
    def _display_path(value: str, workspace: Path) -> str:
        path = Path(value)
        if not path.is_absolute():
            return value
        try:
            return path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return "<outside-workspace>"

    def _usage_metadata(self, result: Any) -> dict[str, Any]:
        usage = dict(result.usage or {})
        metadata: dict[str, Any] = {
            "duration_ms": result.duration_ms,
            "duration_api_ms": result.duration_api_ms,
            "turns": result.num_turns,
        }
        if result.total_cost_usd is not None:
            metadata["total_cost_usd"] = result.total_cost_usd
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if name in usage:
                metadata[name] = usage[name]
        if result.model_usage:
            metadata["models"] = sorted(result.model_usage)
        return metadata

    def _redact(self, value: str) -> str:
        if self.settings.anthropic_api_key:
            return value.replace(self.settings.anthropic_api_key, "<redacted>")
        return value

    @staticmethod
    def _looks_fatal(message: str) -> bool:
        normalized = message.lower()
        authentication_terms = (
            "authentication",
            "authenticate",
            "not logged in",
            "oauth session",
            "api key",
            "unauthorized",
        )
        model_terms = (
            "invalid model",
            "model not found",
            "model_not_found",
            "model unavailable",
            "model is not available",
            "unknown model",
        )
        return any(term in normalized for term in (*authentication_terms, *model_terms))
