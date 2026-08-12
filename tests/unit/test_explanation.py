"""Explainability, and the guarantee that prose never affects a decision."""

from __future__ import annotations

import pytest

from app.domain.enums import (
    ApprovalTier,
    EmploymentStatus,
    EmploymentType,
    ExplanationGenerator,
    MatchingStrategy,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    SodStatus,
)
from app.domain.exceptions import LlmError
from app.domain.models import (
    AccessDecision,
    EmployeeProfile,
    PeerAnalysisResult,
    PeerEntitlementEvidence,
)
from app.services.explanation_service import ExplanationService
from app.services.llm_service import DeterministicLLMService, LLMService


@pytest.fixture
def employee() -> EmployeeProfile:
    return EmployeeProfile(
        employee_id="EMP1001",
        name="Jane Smith",
        department="Finance",
        job_role="Financial Analyst",
        job_level="L3",
        location="London",
        employment_status=EmploymentStatus.PENDING_START,
        employment_type=EmploymentType.EMPLOYEE,
    )


@pytest.fixture
def peer_analysis() -> PeerAnalysisResult:
    return PeerAnalysisResult(
        employee_id="EMP1001",
        matching_strategy=MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        peer_count=8,
        peer_ids=[f"EMP{i:03d}" for i in range(1, 9)],
        confidence=0.95,
        sufficient=True,
    )


@pytest.fixture
def decision() -> AccessDecision:
    return AccessDecision(
        entitlement_id="SAP_FIN_DISPLAY",
        entitlement_name="SAP Finance Display",
        application="SAP",
        affinity_score=100.0,
        peer_count=8,
        total_peers=8,
        affinity_threshold=70.0,
        matching_strategy=MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        risk_score=15,
        risk_level=RiskLevel.LOW,
        policy_status=PolicyStatus.PASS,
        sod_status=SodStatus.PASS,
        recommendation_status=RecommendationStatus.AUTO_APPROVED,
        approval_tier=ApprovalTier.AUTO,
        reason="clean",
        evidence=[
            PeerEntitlementEvidence(
                peer_employee_id="EMP001",
                peer_name="Amelia Hart",
                evidence_value="Amelia Hart (EMP001) holds SAP_FIN_DISPLAY",
            )
        ],
    )


class ExplodingLLM(LLMService):
    name = "exploding"
    model = "test-model"

    @property
    def available(self) -> bool:
        return True

    def generate_narrative(self, **kwargs) -> str:
        raise LlmError("model unavailable")


class FabricatingLLM(LLMService):
    name = "fabricating"
    model = "test-model"

    @property
    def available(self) -> bool:
        return True

    def generate_narrative(self, **kwargs) -> str:
        return "This entitlement was BLOCKED due to an SoD conflict with SAP_BASIS_ADMIN."


def test_structured_explanation_contains_every_required_field(
    decision, peer_analysis
) -> None:
    structured = ExplanationService(llm=DeterministicLLMService()).build_structured(
        decision, peer_analysis
    )
    dumped = structured.model_dump()
    for field in (
        "recommendation",
        "why_recommended",
        "peer_evidence",
        "affinity",
        "risk",
        "risk_level",
        "policy_results",
        "sod_results",
        "final_decision",
        "approval_tier",
    ):
        assert field in dumped, f"missing explainability field: {field}"


def test_deterministic_narrative_states_the_evidence(decision, peer_analysis) -> None:
    service = ExplanationService(llm=DeterministicLLMService())
    structured = service.build_structured(decision, peer_analysis)
    narrative = service.render_template(structured)
    assert "8 of 8" in narrative
    assert "100.0%" in narrative
    assert "risk score of 15" in narrative
    assert "AUTO_APPROVED" in narrative


def test_no_llm_configured_uses_the_deterministic_generator(
    employee, peer_analysis, decision
) -> None:
    result = ExplanationService(llm=DeterministicLLMService()).explain(
        employee=employee, peer_analysis=peer_analysis, decisions=[decision]
    )
    assert result.generator is ExplanationGenerator.DETERMINISTIC
    assert result.recommendations[0].narrative
    assert result.error is None


def test_llm_failure_falls_back_and_preserves_the_decision(
    employee, peer_analysis, decision
) -> None:
    """The load-bearing guarantee: a broken model must not lose a decision."""
    result = ExplanationService(llm=ExplodingLLM()).explain(
        employee=employee, peer_analysis=peer_analysis, decisions=[decision]
    )
    explanation = result.recommendations[0]
    assert explanation.generator is ExplanationGenerator.DETERMINISTIC_FALLBACK
    assert explanation.error == "model unavailable"
    assert explanation.narrative, "a fallback narrative must still be produced"
    # The decision the explanation describes is untouched.
    assert explanation.structured.final_decision is RecommendationStatus.AUTO_APPROVED
    assert explanation.structured.approval_tier is ApprovalTier.AUTO


def test_llm_prose_cannot_change_the_decision(employee, peer_analysis, decision) -> None:
    """Even a model that fabricates a different verdict changes nothing."""
    result = ExplanationService(llm=FabricatingLLM()).explain(
        employee=employee, peer_analysis=peer_analysis, decisions=[decision]
    )
    explanation = result.recommendations[0]
    assert explanation.generator is ExplanationGenerator.LLM
    assert "BLOCKED" in explanation.narrative  # the model said so...
    # ...and it made no difference whatsoever.
    assert explanation.structured.final_decision is RecommendationStatus.AUTO_APPROVED
    assert decision.recommendation_status is RecommendationStatus.AUTO_APPROVED


def test_not_recommended_explanation_says_why(employee, peer_analysis, decision) -> None:
    low = decision.model_copy(
        update={
            "affinity_score": 25.0,
            "peer_count": 2,
            "recommendation_status": RecommendationStatus.NOT_RECOMMENDED,
            "approval_tier": ApprovalTier.NONE,
        }
    )
    result = ExplanationService(llm=DeterministicLLMService()).explain(
        employee=employee, peer_analysis=peer_analysis, decisions=[low]
    )
    narrative = result.recommendations[0].narrative
    assert "below the 70.0% recommendation threshold" in narrative
    assert "NOT_RECOMMENDED" in narrative
