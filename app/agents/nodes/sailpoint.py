"""Node: generate_sailpoint_payload."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import SailPointRequestPayload


@node("generate_sailpoint_payload")
def generate_sailpoint_payload(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    decisions = state.get("decisions") or []
    if not decisions:
        return {"sailpoint_payload": None}

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call(
        "generate_sailpoint_request",
        {
            "employee_id": state["employee_id"],
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "analysis_id": state.get("analysis_id"),
        },
    )
    payload = SailPointRequestPayload.model_validate(raw)

    logger.info(
        "workflow.sailpoint",
        included=len(payload.requested_entitlements),
        excluded=len(payload.excluded_entitlements),
        status=payload.status,
    )
    return {
        "sailpoint_payload": payload,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
