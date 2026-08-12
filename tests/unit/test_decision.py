"""The decision engine.

`decide()` is pure, so these tests state the governance rules directly: given
this evidence, this is the only permissible outcome.
"""

from __future__ import annotations

from app.domain.enums import (
    ApprovalTier,
    MatchingStrategy,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    Severity,
    SodStatus,
)
from app.domain.models import (
    EntitlementAffinity,
    EntitlementPolicyResult,
    RiskAssessment,
    SodConflict,
)
from app.services.decision_service import decide


def affinity(score: float = 100.0, threshold: float = 70.0) -> EntitlementAffinity:
    return EntitlementAffinity(
        entitlement_id="SAP_FIN_DISPLAY",
        entitlement_name="SAP Finance Display",
        application="SAP",
        peer_count=int(score / 100 * 8),
        total_peers=8,
        affinity_score=score,
        threshold=threshold,
        meets_threshold=score >= threshold,
        matching_strategy=MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
    )


def risk(score: int, level: RiskLevel, tier: ApprovalTier) -> RiskAssessment:
    return RiskAssessment(
        entitlement_id="SAP_FIN_DISPLAY",
        risk_score=score,
        risk_level=level,
        required_approval_tier=tier,
        band_bounds="test",
        reason=f"score {score}",
    )


def policy(status: PolicyStatus, tier: ApprovalTier) -> EntitlementPolicyResult:
    return EntitlementPolicyResult(
        entitlement_id="SAP_FIN_DISPLAY",
        status=status,
        approval_tier=tier,
        reason=f"policy said {status.value}",
    )


def conflict(severity: Severity = Severity.CRITICAL) -> SodConflict:
    return SodConflict(
        sod_id="SOD-001",
        name="Vendor vs Payment",
        entitlement_1="SAP_FIN_DISPLAY",
        entitlement_2="SAP_AP_APPROVE_PAYMENT",
        severity=severity,
        reason="toxic combination",
    )


LOW = risk(15, RiskLevel.LOW, ApprovalTier.AUTO)
PASS = policy(PolicyStatus.PASS, ApprovalTier.AUTO)


def test_high_affinity_low_risk_clean_is_auto_approved() -> None:
    decision = decide(affinity=affinity(), risk=LOW, policy=PASS, sod_conflicts=[])
    assert decision.recommendation_status is RecommendationStatus.AUTO_APPROVED
    assert decision.approval_tier is ApprovalTier.AUTO
    assert decision.sod_status is SodStatus.PASS


def test_low_affinity_is_not_recommended() -> None:
    decision = decide(affinity=affinity(score=25.0), risk=LOW, policy=None, sod_conflicts=[])
    assert decision.recommendation_status is RecommendationStatus.NOT_RECOMMENDED
    assert decision.approval_tier is ApprovalTier.NONE


def test_low_affinity_wins_over_everything_else() -> None:
    """The affinity gate is first: nothing below threshold is ever requested."""
    decision = decide(
        affinity=affinity(score=10.0),
        risk=risk(95, RiskLevel.CRITICAL, ApprovalTier.HUMAN_REVIEW),
        policy=policy(PolicyStatus.BLOCK, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[conflict()],
    )
    assert decision.recommendation_status is RecommendationStatus.NOT_RECOMMENDED


def test_high_risk_requires_manager_approval() -> None:
    decision = decide(
        affinity=affinity(),
        risk=risk(72, RiskLevel.HIGH, ApprovalTier.MANAGER),
        policy=PASS,
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.MANAGER_APPROVAL
    assert decision.approval_tier is ApprovalTier.MANAGER


def test_critical_risk_requires_human_review() -> None:
    decision = decide(
        affinity=affinity(),
        risk=risk(95, RiskLevel.CRITICAL, ApprovalTier.HUMAN_REVIEW),
        policy=PASS,
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.HUMAN_REVIEW


def test_sod_conflict_blocks() -> None:
    decision = decide(
        affinity=affinity(), risk=LOW, policy=PASS, sod_conflicts=[conflict()]
    )
    assert decision.recommendation_status is RecommendationStatus.BLOCKED
    assert decision.approval_tier is ApprovalTier.HUMAN_REVIEW
    assert decision.sod_status is SodStatus.CONFLICT
    assert decision.sod_severity is Severity.CRITICAL


def test_sod_conflict_outranks_critical_risk_and_policy() -> None:
    """SoD is checked before policy, so the reason names the SoD rule."""
    decision = decide(
        affinity=affinity(),
        risk=risk(95, RiskLevel.CRITICAL, ApprovalTier.HUMAN_REVIEW),
        policy=policy(PolicyStatus.BLOCK, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[conflict()],
    )
    assert decision.recommendation_status is RecommendationStatus.BLOCKED
    assert "SOD-001" in decision.reason


def test_lower_severity_conflict_still_blocks() -> None:
    """Severity informs the reviewer; it never downgrades the outcome."""
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=PASS,
        sod_conflicts=[conflict(Severity.LOW)],
    )
    assert decision.recommendation_status is RecommendationStatus.BLOCKED
    assert decision.sod_severity is Severity.LOW


def test_policy_block_blocks() -> None:
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=policy(PolicyStatus.BLOCK, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.BLOCKED


def test_policy_deny_rejects_with_no_approval_path() -> None:
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=policy(PolicyStatus.DENY, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.REJECTED
    assert decision.approval_tier is ApprovalTier.NONE


def test_policy_error_fails_closed_to_human_review() -> None:
    """A control that could not be evaluated must never look like a pass."""
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=policy(PolicyStatus.ERROR, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.HUMAN_REVIEW


def test_policy_requiring_manager_approval_on_low_risk() -> None:
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=policy(PolicyStatus.REQUIRES_APPROVAL, ApprovalTier.MANAGER),
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.MANAGER_APPROVAL


def test_policy_requiring_human_review_on_low_risk() -> None:
    decision = decide(
        affinity=affinity(),
        risk=LOW,
        policy=policy(PolicyStatus.REQUIRES_APPROVAL, ApprovalTier.HUMAN_REVIEW),
        sod_conflicts=[],
    )
    assert decision.recommendation_status is RecommendationStatus.HUMAN_REVIEW


def test_decision_is_traceable() -> None:
    decision = decide(affinity=affinity(), risk=LOW, policy=PASS, sod_conflicts=[])
    rules = [entry.rule for entry in decision.decision_trace]
    assert rules == ["affinity_threshold", "sod_check", "policy_check", "risk_band"]
    assert decision.reason


def test_decision_is_deterministic() -> None:
    """Same evidence, same verdict - every time."""
    args = dict(affinity=affinity(87.5), risk=LOW, policy=PASS, sod_conflicts=[])
    first = decide(**args)
    for _ in range(5):
        assert decide(**args).model_dump() == first.model_dump()
