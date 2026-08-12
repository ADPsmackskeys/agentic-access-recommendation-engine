"""Decision engine (Phase 10).

The governance kernel. It folds affinity, risk, policy and SoD into exactly one
terminal status per candidate entitlement, in a fixed precedence order, and
records a step-by-step trace of how it got there.

Precedence (first match wins):

    1. affinity below threshold            -> NOT_RECOMMENDED
    2. SoD conflict                        -> BLOCKED          (human review)
    3. policy DENY                         -> REJECTED         (not overridable)
    4. policy BLOCK                        -> BLOCKED          (human review)
    5. policy ERROR                        -> HUMAN_REVIEW     (fail closed)
    6. risk CRITICAL                       -> HUMAN_REVIEW
    7. policy REQUIRES_APPROVAL(HUMAN)     -> HUMAN_REVIEW
    8. risk HIGH or policy REQUIRES(MGR)   -> MANAGER_APPROVAL
    9. otherwise                           -> AUTO_APPROVED

This function is pure: same inputs, same output, no I/O, no model calls. An LLM
never reaches it.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.domain.enums import (
    ApprovalTier,
    MatchingStrategy,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    SodStatus,
)
from app.domain.models import (
    AccessDecision,
    DecisionTraceEntry,
    EntitlementAffinity,
    EntitlementPolicyResult,
    PeerAnalysisResult,
    RiskAssessment,
    SodConflict,
    SodValidationResult,
)
from app.logging import get_logger

logger = get_logger(__name__)


def decide(
    *,
    affinity: EntitlementAffinity,
    risk: RiskAssessment,
    policy: EntitlementPolicyResult | None,
    sod_conflicts: list[SodConflict],
) -> AccessDecision:
    """Produce the terminal decision for one candidate entitlement."""
    trace: list[DecisionTraceEntry] = [
        DecisionTraceEntry(
            rule="affinity_threshold",
            outcome="MEETS" if affinity.meets_threshold else "BELOW",
            detail=(
                f"{affinity.peer_count} of {affinity.total_peers} matched peers hold "
                f"{affinity.entitlement_id} ({affinity.affinity_score}% affinity) against a "
                f"threshold of {affinity.threshold}%."
            ),
        )
    ]

    policy_status = policy.status if policy else PolicyStatus.NOT_EVALUATED
    policy_tier = policy.approval_tier if policy else ApprovalTier.AUTO
    sod_status = SodStatus.CONFLICT if sod_conflicts else (
        SodStatus.PASS if affinity.meets_threshold else SodStatus.NOT_EVALUATED
    )
    sod_severity = (
        max(sod_conflicts, key=lambda c: _SEVERITY_RANK[c.severity.value]).severity
        if sod_conflicts
        else None
    )

    def build(
        status: RecommendationStatus, tier: ApprovalTier, reason: str
    ) -> AccessDecision:
        return AccessDecision(
            entitlement_id=affinity.entitlement_id,
            entitlement_name=affinity.entitlement_name,
            application=affinity.application,
            affinity_score=affinity.affinity_score,
            peer_count=affinity.peer_count,
            total_peers=affinity.total_peers,
            affinity_threshold=affinity.threshold,
            matching_strategy=affinity.matching_strategy,
            risk_score=risk.risk_score,
            risk_level=risk.risk_level,
            policy_status=policy_status,
            sod_status=sod_status,
            sod_severity=sod_severity,
            recommendation_status=status,
            approval_tier=tier,
            reason=reason,
            decision_trace=trace,
            policy_result=policy,
            sod_conflicts=sod_conflicts,
            evidence=affinity.evidence,
        )

    # 1 - affinity gate
    if not affinity.meets_threshold:
        return build(
            RecommendationStatus.NOT_RECOMMENDED,
            ApprovalTier.NONE,
            (
                f"Not recommended: peer affinity of {affinity.affinity_score}% is below the "
                f"{affinity.threshold}% threshold ({affinity.peer_count} of "
                f"{affinity.total_peers} peers)."
            ),
        )

    # 2 - segregation of duties
    if sod_conflicts:
        ids = ", ".join(sorted({c.sod_id for c in sod_conflicts}))
        trace.append(
            DecisionTraceEntry(
                rule="sod_check",
                outcome="CONFLICT",
                detail=f"Conflicting SoD rule(s): {ids}.",
            )
        )
        return build(
            RecommendationStatus.BLOCKED,
            ApprovalTier.HUMAN_REVIEW,
            (
                f"Blocked: segregation-of-duties conflict ({ids}) at "
                f"{sod_severity.value if sod_severity else 'UNKNOWN'} severity. "
                f"{sod_conflicts[0].reason}"
            ),
        )
    trace.append(
        DecisionTraceEntry(rule="sod_check", outcome="PASS", detail="No SoD conflicts detected.")
    )

    # 3-5 - policy outcomes
    if policy_status is PolicyStatus.DENY:
        trace.append(
            DecisionTraceEntry(rule="policy_check", outcome="DENY", detail=policy.reason)  # type: ignore[union-attr]
        )
        return build(
            RecommendationStatus.REJECTED,
            ApprovalTier.NONE,
            f"Rejected by policy: {policy.reason}",  # type: ignore[union-attr]
        )

    if policy_status is PolicyStatus.BLOCK:
        trace.append(
            DecisionTraceEntry(rule="policy_check", outcome="BLOCK", detail=policy.reason)  # type: ignore[union-attr]
        )
        return build(
            RecommendationStatus.BLOCKED,
            ApprovalTier.HUMAN_REVIEW,
            f"Blocked by policy: {policy.reason}",  # type: ignore[union-attr]
        )

    if policy_status is PolicyStatus.ERROR:
        trace.append(
            DecisionTraceEntry(
                rule="policy_check",
                outcome="ERROR",
                detail="A policy could not be evaluated; failing closed to human review.",
            )
        )
        return build(
            RecommendationStatus.HUMAN_REVIEW,
            ApprovalTier.HUMAN_REVIEW,
            (
                "Human review required: one or more policies could not be evaluated and were "
                f"failed closed. {policy.reason if policy else ''}".strip()
            ),
        )

    trace.append(
        DecisionTraceEntry(
            rule="policy_check",
            outcome=policy_status.value,
            detail=policy.reason if policy else "No policy result available.",
        )
    )

    # 6 - critical risk
    trace.append(
        DecisionTraceEntry(
            rule="risk_band",
            outcome=risk.risk_level.value,
            detail=risk.reason,
        )
    )
    if risk.risk_level is RiskLevel.CRITICAL:
        return build(
            RecommendationStatus.HUMAN_REVIEW,
            ApprovalTier.HUMAN_REVIEW,
            (
                f"Human review required: risk score {risk.risk_score} is classified as "
                f"CRITICAL ({risk.band_bounds})."
            ),
        )

    # 7 - policy demands human review
    if policy_status is PolicyStatus.REQUIRES_APPROVAL and policy_tier is ApprovalTier.HUMAN_REVIEW:
        return build(
            RecommendationStatus.HUMAN_REVIEW,
            ApprovalTier.HUMAN_REVIEW,
            f"Human review required by policy: {policy.reason}",  # type: ignore[union-attr]
        )

    # 8 - manager approval
    if risk.risk_level is RiskLevel.HIGH or (
        policy_status is PolicyStatus.REQUIRES_APPROVAL and policy_tier is ApprovalTier.MANAGER
    ):
        driver = (
            f"risk score {risk.risk_score} is classified as HIGH ({risk.band_bounds})"
            if risk.risk_level is RiskLevel.HIGH
            else "policy requires line-manager approval"
        )
        return build(
            RecommendationStatus.MANAGER_APPROVAL,
            ApprovalTier.MANAGER,
            (
                f"Manager approval required: {driver}. Peer affinity "
                f"{affinity.affinity_score}% ({affinity.peer_count}/{affinity.total_peers})."
            ),
        )

    # 9 - clean
    return build(
        RecommendationStatus.AUTO_APPROVED,
        ApprovalTier.AUTO,
        (
            f"Auto-approved: {affinity.peer_count} of {affinity.total_peers} matched peers hold "
            f"this entitlement ({affinity.affinity_score}% affinity), risk score "
            f"{risk.risk_score} is {risk.risk_level.value}, all policies passed and no SoD "
            f"conflicts were detected."
        ),
    )


class DecisionService:
    """Assembles per-entitlement decisions for a whole analysis."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def decide_all(
        self,
        *,
        peer_analysis: PeerAnalysisResult,
        candidates: list[EntitlementAffinity],
        risk_results: list[RiskAssessment],
        policy_validation,
        sod_validation: SodValidationResult | None,
    ) -> list[AccessDecision]:
        risk_by_id = {r.entitlement_id: r for r in risk_results}
        decisions: list[AccessDecision] = []

        for candidate in candidates:
            risk = risk_by_id.get(candidate.entitlement_id)
            if risk is None:
                # Should not happen: risk is evaluated for every candidate.
                logger.warning(
                    "decision.missing_risk", entitlement_id=candidate.entitlement_id
                )
                continue

            policy_result = (
                policy_validation.by_entitlement(candidate.entitlement_id)
                if policy_validation is not None
                else None
            )
            conflicts = (
                sod_validation.conflicts_for(candidate.entitlement_id)
                if sod_validation is not None and candidate.meets_threshold
                else []
            )
            decisions.append(
                decide(
                    affinity=candidate,
                    risk=risk,
                    policy=policy_result,
                    sod_conflicts=conflicts,
                )
            )

        decisions.sort(key=lambda d: (_STATUS_ORDER[d.recommendation_status], -d.affinity_score,
                                      d.entitlement_id))
        summary: dict[str, int] = {}
        for decision in decisions:
            summary[decision.recommendation_status.value] = (
                summary.get(decision.recommendation_status.value, 0) + 1
            )
        logger.info(
            "decision.completed",
            employee_id=peer_analysis.employee_id,
            total=len(decisions),
            **summary,
        )
        return decisions


_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

# Presentation order: what a reviewer needs to look at first.
_STATUS_ORDER = {
    RecommendationStatus.BLOCKED: 0,
    RecommendationStatus.REJECTED: 1,
    RecommendationStatus.HUMAN_REVIEW: 2,
    RecommendationStatus.MANAGER_APPROVAL: 3,
    RecommendationStatus.AUTO_APPROVED: 4,
    RecommendationStatus.NOT_RECOMMENDED: 5,
}
