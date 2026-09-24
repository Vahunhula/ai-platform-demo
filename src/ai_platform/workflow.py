"""Workflow-phase, artifact, and deterministic readiness domain models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class WorkflowPhase(StrEnum):
    """Durable workflow position, independent from task lifecycle status."""

    BRAINSTORM = "BRAINSTORM"
    PLAN = "PLAN"
    IMPLEMENTATION = "IMPLEMENTATION"
    REVIEW = "REVIEW"
    HUMAN_REVIEW = "HUMAN_REVIEW"


PHASE_SEQUENCE = tuple(WorkflowPhase)


class TransitionMode(StrEnum):
    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


class ArtifactKind(StrEnum):
    BRAINSTORM_SUMMARY = "BRAINSTORM_SUMMARY"
    PLAN = "PLAN"
    IMPLEMENTATION_SUMMARY = "IMPLEMENTATION_SUMMARY"
    REVIEW_REPORT = "REVIEW_REPORT"
    HUMAN_REVIEW_DECISION = "HUMAN_REVIEW_DECISION"


ARTIFACT_PHASES = {
    ArtifactKind.BRAINSTORM_SUMMARY: WorkflowPhase.BRAINSTORM,
    ArtifactKind.PLAN: WorkflowPhase.PLAN,
    ArtifactKind.IMPLEMENTATION_SUMMARY: WorkflowPhase.IMPLEMENTATION,
    ArtifactKind.REVIEW_REPORT: WorkflowPhase.REVIEW,
    ArtifactKind.HUMAN_REVIEW_DECISION: WorkflowPhase.HUMAN_REVIEW,
}


class _ArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


NonBlank = Annotated[str, Field(min_length=1, max_length=20_000)]


class BrainstormSummaryPayload(_ArtifactPayload):
    summary: NonBlank
    assumptions: list[NonBlank]
    options: list[NonBlank]
    questions: list[NonBlank]


class PlanPayload(_ArtifactPayload):
    summary: NonBlank
    files: list[NonBlank]
    steps: list[NonBlank]
    tests: list[NonBlank]
    risks: list[NonBlank]
    open_questions: list[NonBlank]


class ImplementationSummaryPayload(_ArtifactPayload):
    summary: NonBlank
    files_changed: list[NonBlank]
    tests_run: list[NonBlank]
    known_issues: list[NonBlank]


class ReviewReportPayload(_ArtifactPayload):
    summary: NonBlank
    critical_findings: list[NonBlank]
    major_findings: list[NonBlank]
    minor_findings: list[NonBlank]
    requirements_assessment: NonBlank
    test_assessment: NonBlank
    convention_assessment: NonBlank


class HumanReviewDecisionPayload(_ArtifactPayload):
    decision: Literal["APPROVED", "CHANGES_REQUESTED", "REJECTED"]
    feedback: str = Field(max_length=20_000)
    target_phase: WorkflowPhase | None = None


_PAYLOAD_ADAPTERS = {
    ArtifactKind.BRAINSTORM_SUMMARY: TypeAdapter(BrainstormSummaryPayload),
    ArtifactKind.PLAN: TypeAdapter(PlanPayload),
    ArtifactKind.IMPLEMENTATION_SUMMARY: TypeAdapter(ImplementationSummaryPayload),
    ArtifactKind.REVIEW_REPORT: TypeAdapter(ReviewReportPayload),
    ArtifactKind.HUMAN_REVIEW_DECISION: TypeAdapter(HumanReviewDecisionPayload),
}


def validate_artifact_payload(kind: ArtifactKind, payload: object) -> dict[str, object]:
    """Validate and normalize one kind-specific JSON payload."""

    validated = _PAYLOAD_ADAPTERS[kind].validate_python(payload)
    return validated.model_dump(mode="json")


class WorkflowArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    phase: WorkflowPhase
    kind: ArtifactKind
    version: int = Field(ge=1)
    payload: dict[str, object]
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    supersedes_artifact_id: str | None = None


class ChecklistStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class ChecklistDefinition(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    weight: int = Field(ge=0)
    blocking: bool


class ChecklistResult(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: ChecklistStatus
    evidence: str = Field(min_length=1, max_length=20_000)


class ChecklistItem(ChecklistDefinition):
    status: ChecklistStatus
    evidence: str = Field(min_length=1, max_length=20_000)


class ReadinessDecision(BaseModel):
    score: float = Field(ge=0, le=100)
    blocking_failures: list[str]
    blocking_needs_human: list[str]
    eligible_for_auto_progression: bool


class ChecklistEvaluation(BaseModel):
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    phase: WorkflowPhase
    evaluation_number: int = Field(ge=1)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    items: list[ChecklistItem]
    readiness: ReadinessDecision


CHECKLISTS: dict[WorkflowPhase, tuple[ChecklistDefinition, ...]] = {
    WorkflowPhase.BRAINSTORM: (
        ChecklistDefinition(
            key="problem_defined", label="Problem is defined", weight=40, blocking=True
        ),
        ChecklistDefinition(
            key="assumptions_recorded", label="Assumptions are recorded", weight=25, blocking=False
        ),
        ChecklistDefinition(
            key="options_considered", label="Options are considered", weight=35, blocking=False
        ),
    ),
    WorkflowPhase.PLAN: (
        ChecklistDefinition(
            key="scope_complete", label="Plan scope is complete", weight=40, blocking=True
        ),
        ChecklistDefinition(
            key="tests_defined", label="Tests are defined", weight=30, blocking=True
        ),
        ChecklistDefinition(
            key="risks_addressed", label="Risks are addressed", weight=30, blocking=False
        ),
    ),
    WorkflowPhase.IMPLEMENTATION: (
        ChecklistDefinition(
            key="requirements_met", label="Requirements are implemented", weight=40, blocking=True
        ),
        ChecklistDefinition(
            key="tests_pass", label="Required tests pass", weight=40, blocking=True
        ),
        ChecklistDefinition(
            key="known_issues_recorded",
            label="Known issues are recorded",
            weight=20,
            blocking=False,
        ),
    ),
    WorkflowPhase.REVIEW: (
        ChecklistDefinition(
            key="no_critical_findings",
            label="No critical findings remain",
            weight=50,
            blocking=True,
        ),
        ChecklistDefinition(
            key="requirements_reviewed", label="Requirements are reviewed", weight=30, blocking=True
        ),
        ChecklistDefinition(
            key="conventions_reviewed", label="Conventions are reviewed", weight=20, blocking=False
        ),
    ),
    WorkflowPhase.HUMAN_REVIEW: (
        ChecklistDefinition(
            key="human_decision", label="Human decision is recorded", weight=100, blocking=True
        ),
    ),
}


def calculate_readiness(items: list[ChecklistItem]) -> ReadinessDecision:
    """Calculate the platform-owned weighted score and locked 98/no-blocker gate."""

    total_weight = sum(item.weight for item in items)
    passed_weight = sum(item.weight for item in items if item.status is ChecklistStatus.PASS)
    score = (passed_weight / total_weight * 100) if total_weight else 0.0
    failures = [item.key for item in items if item.blocking and item.status is ChecklistStatus.FAIL]
    needs_human = [
        item.key for item in items if item.blocking and item.status is ChecklistStatus.NEEDS_HUMAN
    ]
    return ReadinessDecision(
        score=score,
        blocking_failures=failures,
        blocking_needs_human=needs_human,
        eligible_for_auto_progression=(
            total_weight > 0 and score >= 98 and not failures and not needs_human
        ),
    )


def resolve_checklist(phase: WorkflowPhase, results: list[ChecklistResult]) -> list[ChecklistItem]:
    """Join submitted results to platform-owned labels, weights, and blocker flags."""

    definitions = {item.key: item for item in CHECKLISTS[phase]}
    submitted: dict[str, ChecklistResult] = {}
    for result in results:
        if result.key not in definitions:
            raise ValueError(f"Unknown checklist key for {phase.value}: {result.key}")
        if result.key in submitted:
            raise ValueError(f"Duplicate checklist key: {result.key}")
        submitted[result.key] = result
    return [
        ChecklistItem(
            **definition.model_dump(),
            status=(
                submitted[definition.key].status
                if definition.key in submitted
                else ChecklistStatus.NEEDS_HUMAN
            ),
            evidence=(
                submitted[definition.key].evidence
                if definition.key in submitted
                else "No evaluation result supplied"
            ),
        )
        for definition in CHECKLISTS[phase]
    ]
