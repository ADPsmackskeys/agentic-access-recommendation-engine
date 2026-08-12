"""Node: generate_explanation."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import AnalysisExplanation


@node("generate_explanation")
def generate_explanation(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Explain the decisions that were already made.

    Non-fatal by construction (see `_common.node`): if this step fails outright
    the workflow continues and the analysis is persisted with its decisions
    intact and no narrative. The explanation service itself also degrades
    internally to a deterministic template when an LLM call fails, so reaching
    the outer failure path at all is unlikely.
    """
    decisions = state.get("decisions") or []
    peer_analysis = state.get("peer_analysis")
    if not decisions or peer_analysis is None:
        return {"explanation": None}

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call(
        "generate_access_explanation",
        {
            "employee_id": state["employee_id"],
            "peer_analysis": peer_analysis.model_dump(mode="json"),
            "decisions": [d.model_dump(mode="json") for d in decisions],
        },
    )
    explanation = AnalysisExplanation.model_validate(raw)

    logger.info(
        "workflow.explanation",
        generator=explanation.generator.value,
        explained=len(explanation.recommendations),
    )
    return {
        "explanation": explanation,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
