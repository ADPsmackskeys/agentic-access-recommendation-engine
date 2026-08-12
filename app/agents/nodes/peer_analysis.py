"""Node: find_peers."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import PeerAnalysisResult


@node("find_peers")
def find_peers(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call("find_peer_employees", {"employee_id": state["employee_id"]})
    peer_analysis = PeerAnalysisResult.model_validate(raw)

    errors: list[str] = []
    if peer_analysis.peer_count == 0:
        # Not an exception: "nobody comparable exists" is a legitimate finding,
        # and the analysis should still be persisted saying exactly that.
        errors.append(
            "find_peers: no peer group could be established under any matching strategy; "
            "no entitlements can be recommended from peer evidence."
        )
    elif not peer_analysis.sufficient:
        errors.append(
            f"find_peers: peer group of {peer_analysis.peer_count} is below the configured "
            f"minimum; recommendations carry reduced confidence "
            f"({peer_analysis.confidence})."
        )

    logger.info(
        "workflow.peers",
        strategy=peer_analysis.matching_strategy.value,
        peer_count=peer_analysis.peer_count,
        confidence=peer_analysis.confidence,
    )
    return {
        "peer_analysis": peer_analysis,
        "errors": errors,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
