"""Risk evaluation (Phase 7).

Risk is a property of the entitlement, read from the catalogue and classified
against configured band bounds. No model is asked what it thinks the risk is;
the same entitlement always produces the same band and the same baseline
approval requirement.

    0  - 30   LOW       auto-approvable
    31 - 69   MEDIUM    auto-approvable unless policy or SoD says otherwise
    70 - 89   HIGH      line-manager approval
    90 - 100  CRITICAL  human governance review
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.catalog_repo import EntitlementRepository
from app.domain.enums import ApprovalTier, RiskLevel
from app.domain.exceptions import EntitlementNotFoundError
from app.domain.models import RiskAssessment
from app.logging import get_logger

logger = get_logger(__name__)

RISK_LEVEL_TO_TIER: dict[RiskLevel, ApprovalTier] = {
    RiskLevel.LOW: ApprovalTier.AUTO,
    RiskLevel.MEDIUM: ApprovalTier.AUTO,
    RiskLevel.HIGH: ApprovalTier.MANAGER,
    RiskLevel.CRITICAL: ApprovalTier.HUMAN_REVIEW,
}


def classify_risk(score: int, settings: Settings | None = None) -> RiskLevel:
    """Map a 0-100 risk score onto its configured band."""
    settings = settings or get_settings()
    if score <= settings.risk_low_max:
        return RiskLevel.LOW
    if score <= settings.risk_medium_max:
        return RiskLevel.MEDIUM
    if score <= settings.risk_high_max:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


def band_bounds(level: RiskLevel, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    match level:
        case RiskLevel.LOW:
            return f"0-{settings.risk_low_max}"
        case RiskLevel.MEDIUM:
            return f"{settings.risk_low_max + 1}-{settings.risk_medium_max}"
        case RiskLevel.HIGH:
            return f"{settings.risk_medium_max + 1}-{settings.risk_high_max}"
        case _:
            return f"{settings.risk_high_max + 1}-100"


class RiskService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.entitlements = EntitlementRepository(session)

    def evaluate(self, entitlement_ids: list[str]) -> list[RiskAssessment]:
        """Assess a batch of entitlements, preserving input order."""
        if not entitlement_ids:
            return []
        catalog = self.entitlements.get_many(entitlement_ids)
        missing = [eid for eid in entitlement_ids if eid not in catalog]
        if missing:
            raise EntitlementNotFoundError(
                f"Unknown entitlement(s): {', '.join(sorted(missing))}",
                details={"entitlement_ids": missing},
            )

        assessments: list[RiskAssessment] = []
        for entitlement_id in entitlement_ids:
            entitlement = catalog[entitlement_id]
            assessments.append(self._assess(entitlement_id, entitlement.risk_score,
                                            entitlement.risk_category))
        logger.info("risk.evaluated", count=len(assessments))
        return assessments

    def evaluate_one(self, entitlement_id: str) -> RiskAssessment:
        return self.evaluate([entitlement_id])[0]

    def _assess(
        self, entitlement_id: str, score: int, category: str | None
    ) -> RiskAssessment:
        level = classify_risk(score, self.settings)
        tier = RISK_LEVEL_TO_TIER[level]
        bounds = band_bounds(level, self.settings)
        if tier is ApprovalTier.AUTO:
            reason = (
                f"Risk score {score} falls in the {level.value} band ({bounds}); "
                f"no risk-driven approval is required."
            )
        else:
            reason = (
                f"Risk score {score} falls in the {level.value} band ({bounds}); "
                f"{tier.value} approval is required."
            )
        return RiskAssessment(
            entitlement_id=entitlement_id,
            risk_score=score,
            risk_level=level,
            risk_category=category,
            required_approval_tier=tier,
            band_bounds=bounds,
            reason=reason,
        )
