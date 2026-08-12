"""MCP tools: risk, policy, SoD, explanation and SailPoint payload generation."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from app.domain.models import (
    AccessDecision,
    AnalysisExplanation,
    PeerAnalysisResult,
    PolicyValidationResult,
    RiskAssessment,
    SailPointRequestPayload,
    SodValidationResult,
)
from app.mcp.tools._support import mcp_tool_handler, tool_session
from app.services.analysis_service import AnalysisService
from app.services.explanation_service import ExplanationService
from app.services.policy_service import PolicyService
from app.services.risk_service import RiskService
from app.services.sailpoint_service import SailPointService
from app.services.sod_service import SodService


@mcp_tool_handler
def evaluate_entitlement_risk(entitlement_ids: list[str]) -> list[RiskAssessment]:
    with tool_session() as session:
        return RiskService(session).evaluate(entitlement_ids)


@mcp_tool_handler
def validate_entitlement_policy(
    employee_id: str, entitlement_ids: list[str]
) -> PolicyValidationResult:
    with tool_session() as session:
        return PolicyService(session).validate(employee_id, entitlement_ids)


@mcp_tool_handler
def check_sod_conflicts(employee_id: str, entitlement_ids: list[str]) -> SodValidationResult:
    with tool_session() as session:
        return SodService(session).check(employee_id, entitlement_ids)


@mcp_tool_handler
def generate_access_explanation(
    employee_id: str,
    peer_analysis: PeerAnalysisResult,
    decisions: list[AccessDecision],
) -> AnalysisExplanation:
    with tool_session() as session:
        employee = AnalysisService(session).get_employee_profile(employee_id)
    return ExplanationService().explain(
        employee=employee, peer_analysis=peer_analysis, decisions=decisions
    )


@mcp_tool_handler
def generate_sailpoint_request(
    employee_id: str,
    decisions: list[AccessDecision],
    analysis_id: str | None = None,
) -> SailPointRequestPayload:
    with tool_session() as session:
        employee = AnalysisService(session).get_employee_profile(employee_id)
    return SailPointService().generate_request_payload(
        employee=employee, decisions=decisions, analysis_id=analysis_id
    )


DESCRIPTIONS: dict[str, str] = {
    "evaluate_entitlement_risk": (
        "Classify entitlements against the configured risk bands (0-30 LOW, 31-69 MEDIUM, "
        "70-89 HIGH, 90-100 CRITICAL) and return the baseline approval tier each band demands. "
        "Risk is read from the entitlement catalogue; it is never inferred."
    ),
    "validate_entitlement_policy": (
        "Run every enabled governance policy against the requested entitlements for an identity. "
        "Returns a per-entitlement status (PASS / REQUIRES_APPROVAL / BLOCK / DENY / ERROR), the "
        "policies that matched, and the approval tier demanded. Policies are evaluated by "
        "registered Python evaluators - rule definitions supply parameters, never executable "
        "expressions."
    ),
    "check_sod_conflicts": (
        "Check the requested entitlements, combined with the identity's existing access, against "
        "every enabled segregation-of-duties rule. Returns the conflicting pairs and the highest "
        "severity found."
    ),
    "generate_access_explanation": (
        "Produce the structured evidence bundle and the natural-language narrative for decisions "
        "that have already been made. The explainer cannot change a decision - it only describes "
        "one - and falls back to a deterministic template if no LLM is configured or the model "
        "call fails."
    ),
    "generate_sailpoint_request": (
        "Build a SailPoint IdentityIQ-style access-request payload from a set of decisions. Only "
        "entitlements whose decision meets the configured approval criteria are included; "
        "blocked, rejected and review-pending items are listed separately as excluded. The "
        "payload is marked SIMULATED - no SailPoint environment is contacted."
    ),
}

TAGS: dict[str, set[str]] = {
    "evaluate_entitlement_risk": {"risk", "read"},
    "validate_entitlement_policy": {"policy", "read"},
    "check_sod_conflicts": {"sod", "read"},
    "generate_access_explanation": {"explainability"},
    "generate_sailpoint_request": {"sailpoint"},
}

HANDLERS: dict[str, Callable] = {
    "evaluate_entitlement_risk": evaluate_entitlement_risk,
    "validate_entitlement_policy": validate_entitlement_policy,
    "check_sod_conflicts": check_sod_conflicts,
    "generate_access_explanation": generate_access_explanation,
    "generate_sailpoint_request": generate_sailpoint_request,
}


def register(mcp: FastMCP) -> None:
    for name, handler in HANDLERS.items():
        mcp.tool(handler, name=name, description=DESCRIPTIONS[name], tags=TAGS[name])
