"""SailPoint payload generation."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.domain.enums import (
    ApprovalTier,
    EmploymentStatus,
    EmploymentType,
    MatchingStrategy,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    SodStatus,
)
from app.domain.models import AccessDecision, EmployeeProfile
from app.services.sailpoint_service import SailPointService


@pytest.fixture
def employee() -> EmployeeProfile:
    return EmployeeProfile(
        employee_id="EMP1001",
        name="Jane Smith",
        department="Finance",
        job_role="Financial Analyst",
        job_level="L3",
        location="London",
        manager_id="EMP015",
        employment_status=EmploymentStatus.PENDING_START,
        employment_type=EmploymentType.EMPLOYEE,
    )


def decision(
    entitlement_id: str,
    status: RecommendationStatus,
    tier: ApprovalTier = ApprovalTier.AUTO,
    application: str = "SAP",
) -> AccessDecision:
    return AccessDecision(
        entitlement_id=entitlement_id,
        entitlement_name=entitlement_id,
        application=application,
        affinity_score=100.0,
        peer_count=8,
        total_peers=8,
        affinity_threshold=70.0,
        matching_strategy=MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        risk_score=15,
        risk_level=RiskLevel.LOW,
        policy_status=PolicyStatus.PASS,
        sod_status=SodStatus.PASS,
        recommendation_status=status,
        approval_tier=tier,
        reason="test",
    )


@pytest.fixture
def service() -> SailPointService:
    return SailPointService(
        Settings(
            postgres_password="",
            sailpoint_included_statuses=["AUTO_APPROVED", "MANAGER_APPROVAL"],
        )
    )


ALL_STATUSES = [
    decision("E_AUTO", RecommendationStatus.AUTO_APPROVED),
    decision("E_MANAGER", RecommendationStatus.MANAGER_APPROVAL, ApprovalTier.MANAGER),
    decision("E_REVIEW", RecommendationStatus.HUMAN_REVIEW, ApprovalTier.HUMAN_REVIEW),
    decision("E_BLOCKED", RecommendationStatus.BLOCKED, ApprovalTier.HUMAN_REVIEW),
    decision("E_REJECTED", RecommendationStatus.REJECTED, ApprovalTier.NONE),
    decision("E_NOTREC", RecommendationStatus.NOT_RECOMMENDED, ApprovalTier.NONE),
]


def test_approved_entitlements_are_included(
    service: SailPointService, employee: EmployeeProfile
) -> None:
    payload = service.generate_request_payload(employee=employee, decisions=ALL_STATUSES)
    included = {e.entitlement for e in payload.requested_entitlements}
    assert included == {"E_AUTO", "E_MANAGER"}


def test_blocked_and_rejected_entitlements_are_excluded(
    service: SailPointService, employee: EmployeeProfile
) -> None:
    """The core safety property: a blocked entitlement must never be requested."""
    payload = service.generate_request_payload(employee=employee, decisions=ALL_STATUSES)
    requested = {e.entitlement for e in payload.requested_entitlements}
    for withheld in ("E_BLOCKED", "E_REJECTED", "E_REVIEW", "E_NOTREC"):
        assert withheld not in requested

    excluded = {e["entitlement"] for e in payload.excluded_entitlements}
    assert excluded == {"E_BLOCKED", "E_REJECTED", "E_REVIEW", "E_NOTREC"}


def test_payload_structure(service: SailPointService, employee: EmployeeProfile) -> None:
    payload = service.generate_request_payload(
        employee=employee, decisions=ALL_STATUSES, analysis_id="A-1", correlation_id="C-1"
    )
    assert payload.identity == "EMP1001"
    assert payload.request_type == "GrantAccess"
    assert payload.source
    assert payload.justification
    assert payload.metadata["analysis_id"] == "A-1"
    assert payload.metadata["correlation_id"] == "C-1"

    item = payload.requested_entitlements[0]
    assert item.application and item.entitlement and item.operation == "Add"

    serialised = payload.model_dump(mode="json")
    assert set(serialised) >= {
        "identity",
        "requested_entitlements",
        "justification",
        "source",
        "status",
    }


def test_payload_is_marked_simulated(
    service: SailPointService, employee: EmployeeProfile
) -> None:
    payload = service.generate_request_payload(employee=employee, decisions=ALL_STATUSES)
    assert payload.status == "SIMULATED"
    assert payload.metadata["simulated"] is True


def test_inclusion_criteria_are_configurable(employee: EmployeeProfile) -> None:
    strict = SailPointService(
        Settings(postgres_password="", sailpoint_included_statuses=["AUTO_APPROVED"])
    )
    payload = strict.generate_request_payload(employee=employee, decisions=ALL_STATUSES)
    assert {e.entitlement for e in payload.requested_entitlements} == {"E_AUTO"}


def test_empty_decision_set_yields_an_empty_request(
    service: SailPointService, employee: EmployeeProfile
) -> None:
    payload = service.generate_request_payload(employee=employee, decisions=[])
    assert payload.requested_entitlements == []
    assert payload.status == "SIMULATED"


def test_submit_is_not_implemented(
    service: SailPointService, employee: EmployeeProfile
) -> None:
    """Better an explicit refusal than a stub that implies provisioning."""
    payload = service.generate_request_payload(employee=employee, decisions=ALL_STATUSES)
    with pytest.raises(NotImplementedError, match="not implemented"):
        service.submit_request(payload)
