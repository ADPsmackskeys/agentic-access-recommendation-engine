"""API request and response schemas.

These are the documented HTTP contract. They wrap the domain models rather than
re-declaring them, so the OpenAPI schema and the governance engine can never
drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    AccessDecision,
    AffinityAnalysisResult,
    AnalysisExplanation,
    EmployeeProfile,
    EmployeeSummary,
    EntitlementPolicyResult,
    PeerAnalysisResult,
    RiskAssessment,
    SailPointRequestPayload,
    SodConflict,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ErrorResponse(ApiModel):
    error: str = Field(description="Machine-readable error code.")
    message: str = Field(description="Human-readable description.")
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
class HealthResponse(ApiModel):
    status: str = Field(description="'ok' or 'degraded'.")
    service: str
    environment: str
    version: str
    database: str = Field(description="'up' or 'down'.")
    database_error: str | None = None
    demo_mode: bool
    llm_enabled: bool
    mcp_client_mode: str
    timestamp: datetime


# --------------------------------------------------------------------------- #
# Joiners
# --------------------------------------------------------------------------- #
class JoinerListResponse(ApiModel):
    count: int
    joiners: list[EmployeeSummary]


class AnalyzeRequest(ApiModel):
    """Optional overrides for a single analysis run."""

    correlation_id: str | None = Field(
        default=None,
        max_length=64,
        description="Client-supplied correlation id; generated when omitted.",
    )
    mcp_client_mode: str | None = Field(
        default=None,
        description=(
            "Override how the workflow reaches the MCP tools for this run: "
            "'inmemory', 'stdio', 'http' or 'direct'."
        ),
    )


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #
class AnalysisResponse(ApiModel):
    """The complete result of an onboarding analysis."""

    analysis_id: str
    correlation_id: str
    employee_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None

    employee: EmployeeProfile | None
    peer_analysis: PeerAnalysisResult | None
    affinity: AffinityAnalysisResult | None
    recommendations: list[AccessDecision]
    risk_results: list[RiskAssessment]
    policy_results: list[EntitlementPolicyResult]
    sod_results: list[SodConflict]
    explanation: AnalysisExplanation | None
    sailpoint_payload: SailPointRequestPayload | None
    errors: list[str]
    summary: dict[str, int] = Field(
        default_factory=dict, description="Recommendation status counts."
    )

    @classmethod
    def from_domain(cls, result) -> "AnalysisResponse":
        summary: dict[str, int] = {}
        for decision in result.decisions:
            key = decision.recommendation_status.value
            summary[key] = summary.get(key, 0) + 1
        return cls(
            analysis_id=result.analysis_id,
            correlation_id=result.correlation_id,
            employee_id=result.employee_id,
            status=result.status.value,
            started_at=result.started_at,
            completed_at=result.completed_at,
            employee=result.employee,
            peer_analysis=result.peer_analysis,
            affinity=result.affinity,
            recommendations=result.decisions,
            risk_results=result.risk_results,
            policy_results=(
                result.policy_validation.results if result.policy_validation else []
            ),
            sod_results=(result.sod_validation.conflicts if result.sod_validation else []),
            explanation=result.explanation,
            sailpoint_payload=result.sailpoint_payload,
            errors=result.errors,
            summary=summary,
        )


class AnalysisSummary(ApiModel):
    analysis_id: str
    employee_id: str
    status: str
    matching_strategy: str | None
    peer_count: int
    candidate_count: int
    started_at: datetime
    completed_at: datetime | None


class AnalysisListResponse(ApiModel):
    count: int
    analyses: list[AnalysisSummary]


# --------------------------------------------------------------------------- #
# Access requests
# --------------------------------------------------------------------------- #
class AccessRequestCreate(ApiModel):
    analysis_id: str = Field(
        description="Analysis whose approved recommendations should be requested."
    )


class AccessRequestResponse(ApiModel):
    request_id: str
    analysis_id: str
    employee_id: str
    status: str = Field(description="Always SIMULATED in this MVP.")
    entitlement_count: int
    payload: SailPointRequestPayload
