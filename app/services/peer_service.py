"""Peer analysis (Phase 5).

Finds the population of existing identities whose access legitimately predicts
what a new joiner needs. The matching strategies are tried strictly in order of
decreasing precision, and the strategy that produced the peer group is recorded
on the analysis - a recommendation derived from a department-wide match is a
much weaker claim than one derived from an exact role match, and the audit
trail has to say which one it was.

Unrelated employees are never silently substituted: if no strategy yields a
peer group, the result says so and the workflow stops recommending.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.employee_repo import EmployeeRepository
from app.domain.enums import MatchingStrategy
from app.domain.exceptions import EmployeeNotFoundError
from app.domain.models import PeerAnalysisResult
from app.logging import get_logger
from app.services.mappers import employee_to_peer

logger = get_logger(__name__)

# Ordered from most to least precise. Relaxation stops at the first strategy
# that yields at least one peer.
STRATEGY_ORDER: tuple[MatchingStrategy, ...] = (
    MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
    MatchingStrategy.JOB_ROLE_DEPARTMENT,
    MatchingStrategy.DEPARTMENT_JOB_LEVEL,
    MatchingStrategy.DEPARTMENT,
)

# How much a peer group found by each strategy can be trusted, before the size
# of the group is taken into account.
STRATEGY_BASE_CONFIDENCE: dict[MatchingStrategy, float] = {
    MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL: 0.95,
    MatchingStrategy.JOB_ROLE_DEPARTMENT: 0.85,
    MatchingStrategy.DEPARTMENT_JOB_LEVEL: 0.70,
    MatchingStrategy.DEPARTMENT: 0.55,
}

# A single peer is evidence of almost nothing; a large group adds little beyond
# the saturation point. This floor/ceiling encodes that.
_CONFIDENCE_FLOOR = 0.6


def compute_confidence(
    strategy: MatchingStrategy, peer_count: int, saturation: int = 8
) -> float:
    """Deterministic confidence in [0, 1] for a peer group.

        confidence = base(strategy) * (0.6 + 0.4 * min(1, peer_count / saturation))

    so an exact-role match over 8+ peers scores 0.95, while the same strategy
    over 2 peers scores 0.665.
    """
    if peer_count <= 0 or strategy is MatchingStrategy.NONE:
        return 0.0
    base = STRATEGY_BASE_CONFIDENCE.get(strategy, 0.5)
    size_factor = _CONFIDENCE_FLOOR + (1 - _CONFIDENCE_FLOOR) * min(
        1.0, peer_count / max(saturation, 1)
    )
    return round(base * size_factor, 4)


class PeerAnalysisService:
    """Deterministic peer selection."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.employees = EmployeeRepository(session)

    def find_peers(self, employee_id: str) -> PeerAnalysisResult:
        employee = self.employees.get(employee_id)
        if employee is None:
            raise EmployeeNotFoundError(
                f"Employee '{employee_id}' does not exist.",
                details={"employee_id": employee_id},
            )

        attempted: list[MatchingStrategy] = []
        for strategy in STRATEGY_ORDER:
            attempted.append(strategy)
            criteria = self._criteria_for(strategy, employee)
            peers = self.employees.find_peers(
                exclude_employee_id=employee_id,
                department=employee.department,
                job_role=criteria["job_role"],
                job_level=criteria["job_level"],
            )
            if not peers:
                logger.info(
                    "peer_analysis.strategy.empty",
                    strategy=strategy.value,
                    employee_id=employee_id,
                )
                continue

            peer_ids = [p.employee_id for p in peers]
            counts = self.employees.count_entitlements_for(peer_ids)
            confidence = compute_confidence(
                strategy, len(peers), self.settings.peer_confidence_saturation
            )
            sufficient = len(peers) >= self.settings.min_peer_count

            logger.info(
                "peer_analysis.matched",
                strategy=strategy.value,
                peer_count=len(peers),
                confidence=confidence,
                sufficient=sufficient,
            )
            return PeerAnalysisResult(
                employee_id=employee_id,
                matching_strategy=strategy,
                strategies_attempted=attempted,
                peer_count=len(peers),
                peer_ids=peer_ids,
                peers=[employee_to_peer(p, counts.get(p.employee_id, 0)) for p in peers],
                confidence=confidence,
                sufficient=sufficient,
                notes=(
                    None
                    if strategy is STRATEGY_ORDER[0]
                    else (
                        f"Exact match was unavailable; peer group derived using the "
                        f"'{strategy.value}' fallback strategy."
                    )
                ),
            )

        logger.warning("peer_analysis.no_peers", employee_id=employee_id)
        return PeerAnalysisResult(
            employee_id=employee_id,
            matching_strategy=MatchingStrategy.NONE,
            strategies_attempted=attempted,
            peer_count=0,
            peer_ids=[],
            peers=[],
            confidence=0.0,
            sufficient=False,
            notes=(
                "No active peer group could be established under any matching strategy. "
                "No entitlements can be recommended from peer evidence."
            ),
        )

    @staticmethod
    def _criteria_for(strategy: MatchingStrategy, employee) -> dict[str, str | None]:
        match strategy:
            case MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL:
                return {"job_role": employee.job_role, "job_level": employee.job_level}
            case MatchingStrategy.JOB_ROLE_DEPARTMENT:
                return {"job_role": employee.job_role, "job_level": None}
            case MatchingStrategy.DEPARTMENT_JOB_LEVEL:
                return {"job_role": None, "job_level": employee.job_level}
            case MatchingStrategy.DEPARTMENT:
                return {"job_role": None, "job_level": None}
            case _:  # pragma: no cover - STRATEGY_ORDER never contains NONE
                return {"job_role": None, "job_level": None}
