"""Node: make_decision.

This node calls the decision engine **in process**, not over MCP. That is a
deliberate exception to the "workflow reaches capabilities through MCP" rule.

The decision engine is the governance kernel: it is the one component whose
output is the authorisation outcome itself. Exposing it as a remote tool would
create a seam where a caller could supply hand-made affinity, risk, policy and
SoD inputs and receive an authoritative-looking verdict back. Keeping it
in-process means the verdict can only ever be computed from evidence this
workflow gathered, and `decide()` stays a pure function that the unit tests can
exercise directly.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import logger, node
from app.agents.state import AccessRecommendationState
from app.services.decision_service import DecisionService


@node("make_decision")
def make_decision(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    affinity = state.get("affinity")
    peer_analysis = state.get("peer_analysis")
    if affinity is None or peer_analysis is None or not affinity.candidates:
        logger.info("workflow.decision.skipped", reason="no candidates")
        return {"decisions": []}

    decisions = DecisionService().decide_all(
        peer_analysis=peer_analysis,
        candidates=affinity.candidates,
        risk_results=state.get("risk_results") or [],
        policy_validation=state.get("policy_validation"),
        sod_validation=state.get("sod_validation"),
    )
    return {"decisions": decisions}
