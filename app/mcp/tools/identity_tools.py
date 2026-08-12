"""MCP tools: identity lookup, peer analysis and affinity.

The handlers are module-level functions, not closures created inside
`register()`. That is what lets the workflow's `direct` execution mode call the
*same* function object the MCP transport calls, so "MCP tools are thin wrappers
over the domain services" stays literally true rather than aspirational.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from sqlalchemy.orm import Session

from app.domain.models import (
    AffinityAnalysisResult,
    EmployeeProfile,
    EmployeeSummary,
    PeerAnalysisResult,
)
from app.mcp.tools._support import mcp_tool_handler, tool_session
from app.services.affinity_service import AffinityService
from app.services.analysis_service import AnalysisService
from app.services.peer_service import PeerAnalysisService


@mcp_tool_handler
def get_joiner(employee_id: str) -> EmployeeProfile:
    with tool_session() as session:
        return AnalysisService(session).get_employee_profile(employee_id)


@mcp_tool_handler
def list_joiners(limit: int = 200, offset: int = 0) -> list[EmployeeSummary]:
    with tool_session() as session:
        return AnalysisService(session).list_joiners(limit=limit, offset=offset)


@mcp_tool_handler
def find_peer_employees(employee_id: str) -> PeerAnalysisResult:
    with tool_session() as session:
        return PeerAnalysisService(session).find_peers(employee_id)


@mcp_tool_handler
def calculate_entitlement_affinity(
    employee_id: str,
    peer_ids: list[str],
    matching_strategy: str = "job_role_department_job_level",
    threshold: float | None = None,
) -> AffinityAnalysisResult:
    with tool_session() as session:
        peer_analysis = _rebuild_peer_analysis(session, employee_id, peer_ids, matching_strategy)
        return AffinityService(session).calculate(employee_id, peer_analysis, threshold)


DESCRIPTIONS: dict[str, str] = {
    "get_joiner": (
        "Fetch the identity profile of an employee, including their current entitlement "
        "holdings. Works for new joiners and existing employees alike."
    ),
    "list_joiners": (
        "List identities awaiting onboarding (employment status PENDING_START), i.e. those "
        "eligible for access-recommendation analysis."
    ),
    "find_peer_employees": (
        "Find the peer group whose access predicts what this identity needs. Strategies are "
        "tried in order of decreasing precision (job_role+department+job_level, then "
        "job_role+department, then department+job_level, then department) and the strategy that "
        "produced the group is returned alongside a confidence score. Only ACTIVE identities are "
        "ever selected as peers."
    ),
    "calculate_entitlement_affinity": (
        "Calculate, for every entitlement held by the given peer group, the percentage of peers "
        "holding it: (peers_with_entitlement / total_peers) * 100. Returns each candidate with "
        "its peer evidence and whether it meets the recommendation threshold."
    ),
}

TAGS: dict[str, set[str]] = {
    "get_joiner": {"identity", "read"},
    "list_joiners": {"identity", "read"},
    "find_peer_employees": {"peer-analysis", "read"},
    "calculate_entitlement_affinity": {"affinity", "read"},
}

HANDLERS: dict[str, Callable] = {
    "get_joiner": get_joiner,
    "list_joiners": list_joiners,
    "find_peer_employees": find_peer_employees,
    "calculate_entitlement_affinity": calculate_entitlement_affinity,
}


def register(mcp: FastMCP) -> None:
    for name, handler in HANDLERS.items():
        mcp.tool(handler, name=name, description=DESCRIPTIONS[name], tags=TAGS[name])


def _rebuild_peer_analysis(
    session: Session, employee_id: str, peer_ids: list[str], matching_strategy: str
) -> PeerAnalysisResult:
    """Reconstruct a peer group from ids so the tool stays stateless.

    MCP tools share no memory with their caller: the caller passes back the peer
    ids it received from `find_peer_employees`. Confidence is recomputed here
    from the same deterministic formula rather than trusted from the wire.
    """
    from app.db.repositories.employee_repo import EmployeeRepository
    from app.domain.enums import MatchingStrategy
    from app.services.mappers import employee_to_peer
    from app.services.peer_service import compute_confidence

    try:
        strategy = MatchingStrategy(matching_strategy)
    except ValueError:
        strategy = MatchingStrategy.NONE

    repo = EmployeeRepository(session)
    counts = repo.count_entitlements_for(peer_ids)
    peers = []
    resolved_ids: list[str] = []
    for peer_id in peer_ids:
        row = repo.get(peer_id)
        if row is None:
            continue
        resolved_ids.append(peer_id)
        peers.append(employee_to_peer(row, counts.get(peer_id, 0)))

    return PeerAnalysisResult(
        employee_id=employee_id,
        matching_strategy=strategy,
        strategies_attempted=[strategy],
        peer_count=len(resolved_ids),
        peer_ids=resolved_ids,
        peers=peers,
        confidence=compute_confidence(strategy, len(resolved_ids)),
        sufficient=bool(resolved_ids),
    )
