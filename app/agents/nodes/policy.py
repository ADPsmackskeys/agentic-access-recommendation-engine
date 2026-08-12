"""Node: validate_policies."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import PolicyValidationResult


@node("validate_policies")
def validate_policies(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Validate only the entitlements that cleared the affinity threshold.

    Policies answer "may this identity receive this entitlement"; asking that of
    something the engine is not proposing to grant would put misleading BLOCK
    rows in the audit trail.
    """
    requested_ids = state.get("requested_entitlement_ids") or []
    if not requested_ids:
        return {"policy_validation": None}

    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call(
        "validate_entitlement_policy",
        {"employee_id": state["employee_id"], "entitlement_ids": requested_ids},
    )
    validation = PolicyValidationResult.model_validate(raw)

    logger.info(
        "workflow.policy",
        status=validation.status.value,
        approval_tier=validation.approval_tier.value,
        evaluated=len(validation.evaluated_policy_ids),
        skipped=len(validation.skipped_policy_ids),
    )
    return {
        "policy_validation": validation,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }
