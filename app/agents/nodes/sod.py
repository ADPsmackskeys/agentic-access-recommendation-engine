"""Node: check_sod."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import SodValidationResult


@node("check_sod")
def check_sod(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    requested_ids = state.get("requested_entitlement_ids") or []
    if not requested_ids:
        return {"sod_validation": None}

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call(
        "check_sod_conflicts",
        {"employee_id": state["employee_id"], "entitlement_ids": requested_ids},
    )
    validation = SodValidationResult.model_validate(raw)

    logger.info(
        "workflow.sod",
        status=validation.status.value,
        conflicts=len(validation.conflicts),
        severity=validation.severity.value if validation.severity else None,
    )
    return {
        "sod_validation": validation,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
