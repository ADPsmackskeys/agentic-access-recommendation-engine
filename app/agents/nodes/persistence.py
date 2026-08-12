"""Node: persist_analysis.

Persistence runs in process, not over MCP. The whole audit trail - analysis,
recommendations, evidence, policy results, SoD results, explanations and the
SailPoint payload - has to land in one database transaction, and a tool call
that opens its own session cannot be enrolled in the caller's transaction. An
analysis that is half-written is worse than one that failed cleanly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import logger, node
from app.agents.state import AccessRecommendationState
from app.db.session import session_scope
from app.domain.enums import AnalysisStatus
from app.domain.models import AnalysisResult
from app.services.analysis_service import AnalysisService


def build_result(
    state: AccessRecommendationState, status: AnalysisStatus | None = None
) -> AnalysisResult:
    """Assemble the domain result from the accumulated workflow state."""
    errors = state.get("errors") or []
    decisions = state.get("decisions") or []
    if status is None:
        if errors and not decisions:
            status = AnalysisStatus.FAILED
        elif errors:
            status = AnalysisStatus.COMPLETED_WITH_WARNINGS
        else:
            status = AnalysisStatus.COMPLETED

    return AnalysisResult(
        analysis_id=state["analysis_id"],
        correlation_id=state.get("correlation_id", ""),
        employee_id=state["employee_id"],
        status=status,
        started_at=state["started_at"],
        completed_at=state.get("completed_at") or datetime.now(timezone.utc),
        employee=state.get("employee_profile"),
        peer_analysis=state.get("peer_analysis"),
        affinity=state.get("affinity"),
        risk_results=state.get("risk_results") or [],
        policy_validation=state.get("policy_validation"),
        sod_validation=state.get("sod_validation"),
        decisions=decisions,
        explanation=state.get("explanation"),
        sailpoint_payload=state.get("sailpoint_payload"),
        errors=errors,
    )


@node("persist_analysis")
def persist_analysis(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    completed_at = datetime.now(timezone.utc)
    result = build_result({**state, "completed_at": completed_at})

    with session_scope() as session:
        AnalysisService(session).persist(result)

    logger.info(
        "workflow.persisted",
        analysis_id=result.analysis_id,
        status=result.status.value,
        recommendations=len(result.decisions),
    )
    return {"persisted": True, "completed_at": completed_at}
