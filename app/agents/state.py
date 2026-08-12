"""Typed LangGraph state.

The state is a TypedDict of *validated domain models*, not loose dictionaries.
Data crossing the MCP boundary arrives as JSON and is re-validated into the
same Pydantic models the services produced, so a malformed tool response fails
at the node that received it rather than three nodes later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, TypedDict

from app.domain.models import (
    AccessDecision,
    AffinityAnalysisResult,
    AnalysisExplanation,
    EmployeeProfile,
    PeerAnalysisResult,
    PolicyValidationResult,
    RiskAssessment,
    SailPointRequestPayload,
    SodValidationResult,
)


def append(existing: list[Any] | None, incoming: list[Any] | None) -> list[Any]:
    """Reducer: accumulate rather than overwrite (used for errors and traces)."""
    return (existing or []) + (incoming or [])


class AccessRecommendationState(TypedDict, total=False):
    """State threaded through the onboarding analysis graph."""

    # --- Inputs -------------------------------------------------------- #
    employee_id: str
    analysis_id: str
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None

    # --- Step outputs -------------------------------------------------- #
    employee_profile: EmployeeProfile | None
    peer_analysis: PeerAnalysisResult | None
    affinity: AffinityAnalysisResult | None
    candidate_entitlement_ids: list[str]
    requested_entitlement_ids: list[str]
    risk_results: list[RiskAssessment]
    policy_validation: PolicyValidationResult | None
    sod_validation: SodValidationResult | None
    decisions: list[AccessDecision]
    explanation: AnalysisExplanation | None
    sailpoint_payload: SailPointRequestPayload | None
    persisted: bool

    # --- Diagnostics --------------------------------------------------- #
    # Accumulated across nodes rather than replaced, so a late failure never
    # erases the record of an earlier one.
    errors: Annotated[list[str], append]
    steps_completed: Annotated[list[str], append]
    mcp_tool_calls: Annotated[list[str], append]


def initial_state(
    employee_id: str, analysis_id: str, correlation_id: str, started_at: datetime
) -> AccessRecommendationState:
    return AccessRecommendationState(
        employee_id=employee_id,
        analysis_id=analysis_id,
        correlation_id=correlation_id,
        started_at=started_at,
        completed_at=None,
        employee_profile=None,
        peer_analysis=None,
        affinity=None,
        candidate_entitlement_ids=[],
        requested_entitlement_ids=[],
        risk_results=[],
        policy_validation=None,
        sod_validation=None,
        decisions=[],
        explanation=None,
        sailpoint_payload=None,
        persisted=False,
        errors=[],
        steps_completed=[],
        mcp_tool_calls=[],
    )
