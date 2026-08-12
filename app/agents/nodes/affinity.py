"""Node: calculate_affinity."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import AffinityAnalysisResult


@node("calculate_affinity")
def calculate_affinity(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    peer_analysis = state.get("peer_analysis")
    if peer_analysis is None or peer_analysis.peer_count == 0:
        logger.info("workflow.affinity.skipped", reason="no peers")
        return {
            "affinity": None,
            "candidate_entitlement_ids": [],
            "requested_entitlement_ids": [],
        }

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call(
        "calculate_entitlement_affinity",
        {
            "employee_id": state["employee_id"],
            "peer_ids": peer_analysis.peer_ids,
            "matching_strategy": peer_analysis.matching_strategy.value,
        },
    )
    affinity = AffinityAnalysisResult.model_validate(raw)

    candidate_ids = [c.entitlement_id for c in affinity.candidates]
    # Only entitlements that clear the threshold are "requested"; the rest are
    # kept as candidates so the audit trail shows what was considered and why
    # it was not recommended.
    requested_ids = [c.entitlement_id for c in affinity.above_threshold()]

    logger.info(
        "workflow.affinity",
        candidates=len(candidate_ids),
        requested=len(requested_ids),
        threshold=affinity.threshold,
    )
    return {
        "affinity": affinity,
        "candidate_entitlement_ids": candidate_ids,
        "requested_entitlement_ids": requested_ids,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
