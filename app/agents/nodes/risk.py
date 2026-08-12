"""Node: evaluate_risk."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import RiskAssessment


@node("evaluate_risk")
def evaluate_risk(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Assess every candidate, including those below the affinity threshold.

    Risk is a static catalogue lookup, so scoring the full candidate set costs
    nothing and makes the audit trail show the risk of what was *not*
    recommended as well as what was.
    """
    candidate_ids = state.get("candidate_entitlement_ids") or []
    if not candidate_ids:
        return {"risk_results": []}

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call("evaluate_entitlement_risk", {"entitlement_ids": candidate_ids})
    risk_results = [RiskAssessment.model_validate(item) for item in raw]

    bands: dict[str, int] = {}
    for assessment in risk_results:
        bands[assessment.risk_level.value] = bands.get(assessment.risk_level.value, 0) + 1
    logger.info("workflow.risk", evaluated=len(risk_results), **bands)

    return {
        "risk_results": risk_results,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
