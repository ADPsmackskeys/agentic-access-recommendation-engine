"""Entitlement affinity (Phase 6).

    affinity_score = (peers holding the entitlement / total matched peers) * 100

That is the whole formula, and it is deliberately the whole formula: an auditor
can recompute any score by counting rows. Everything that makes the score
*meaningful* - which peers, which strategy, what threshold - is carried
alongside it rather than folded into it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.catalog_repo import EntitlementRepository
from app.db.repositories.employee_repo import EmployeeRepository
from app.domain.enums import EvidenceType, MatchingStrategy
from app.domain.models import (
    AffinityAnalysisResult,
    EntitlementAffinity,
    PeerAnalysisResult,
    PeerEntitlementEvidence,
)
from app.logging import get_logger

logger = get_logger(__name__)


def calculate_affinity_score(peers_with_entitlement: int, total_peers: int) -> float:
    """Percentage of the peer group holding an entitlement, rounded to 2dp."""
    if total_peers <= 0:
        return 0.0
    return round((peers_with_entitlement / total_peers) * 100, 2)


class AffinityService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.employees = EmployeeRepository(session)
        self.entitlements = EntitlementRepository(session)

    def calculate(
        self,
        employee_id: str,
        peer_analysis: PeerAnalysisResult,
        threshold: float | None = None,
    ) -> AffinityAnalysisResult:
        threshold = self.settings.affinity_threshold if threshold is None else threshold
        total_peers = peer_analysis.peer_count

        if total_peers == 0:
            logger.info("affinity.no_peers", employee_id=employee_id)
            return AffinityAnalysisResult(
                employee_id=employee_id,
                threshold=threshold,
                total_peers=0,
                matching_strategy=peer_analysis.matching_strategy,
                candidates=[],
            )

        holdings = self.employees.get_entitlements_for(peer_analysis.peer_ids)
        peer_names = {p.employee_id: p.name for p in peer_analysis.peers}
        already_held = set(self.employees.get_entitlement_ids(employee_id))

        # entitlement_id -> ordered list of peers holding it
        holders: dict[str, list[str]] = {}
        for peer_id in peer_analysis.peer_ids:
            for entitlement_id in holdings.get(peer_id, []):
                holders.setdefault(entitlement_id, []).append(peer_id)

        catalog = self.entitlements.get_many(list(holders))
        candidates: list[EntitlementAffinity] = []

        for entitlement_id, holder_ids in holders.items():
            entitlement = catalog.get(entitlement_id)
            if entitlement is None:
                # A grant pointing at an entitlement that is not in the
                # catalogue is a data-quality problem, not a recommendation.
                logger.warning("affinity.entitlement_missing", entitlement_id=entitlement_id)
                continue

            score = calculate_affinity_score(len(holder_ids), total_peers)
            candidates.append(
                EntitlementAffinity(
                    entitlement_id=entitlement_id,
                    entitlement_name=entitlement.entitlement_name,
                    application=entitlement.application,
                    peer_count=len(holder_ids),
                    total_peers=total_peers,
                    affinity_score=score,
                    threshold=threshold,
                    meets_threshold=score >= threshold,
                    matching_strategy=peer_analysis.matching_strategy,
                    already_held=entitlement_id in already_held,
                    evidence=[
                        PeerEntitlementEvidence(
                            peer_employee_id=peer_id,
                            peer_name=peer_names.get(peer_id, peer_id),
                            evidence_type=EvidenceType.PEER_HOLDS_ENTITLEMENT,
                            evidence_value=(
                                f"{peer_names.get(peer_id, peer_id)} ({peer_id}) holds "
                                f"{entitlement_id}"
                            ),
                        )
                        for peer_id in holder_ids
                    ],
                )
            )

        candidates.sort(key=lambda c: (-c.affinity_score, c.entitlement_id))
        above = sum(1 for c in candidates if c.meets_threshold)
        logger.info(
            "affinity.calculated",
            employee_id=employee_id,
            total_peers=total_peers,
            candidates=len(candidates),
            above_threshold=above,
            threshold=threshold,
        )
        return AffinityAnalysisResult(
            employee_id=employee_id,
            threshold=threshold,
            total_peers=total_peers,
            matching_strategy=peer_analysis.matching_strategy or MatchingStrategy.NONE,
            candidates=candidates,
        )
