"""Nodes: load_joiner and profile_joiner."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agents.nodes._common import get_invoker, logger, node, record_tool_calls
from app.agents.state import AccessRecommendationState
from app.domain.models import EmployeeProfile


@node("load_joiner", fatal=True)
def load_joiner(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Resolve the identity through the MCP `get_joiner` tool.

    Fatal on failure: without an identity there is nothing to analyse and
    nothing that could legally be persisted against a foreign key.
    """
    invoker = get_invoker(config)
    mark = len(invoker.tool_calls)
    raw = invoker.call("get_joiner", {"employee_id": state["employee_id"]})
    profile = EmployeeProfile.model_validate(raw)
    return {
        "employee_profile": profile,
        "mcp_tool_calls": record_tool_calls(invoker, mark),
    }


@node("profile_joiner")
def profile_joiner(
    state: AccessRecommendationState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """Derive the matching attributes that drive peer selection.

    No I/O: this step exists to make the profiling decision explicit and
    logged, rather than an implicit side effect of the peer query.
    """
    profile = state.get("employee_profile")
    if profile is None:
        return {"errors": ["profile_joiner: no employee profile was loaded."]}

    logger.info(
        "workflow.profile",
        employee_id=profile.employee_id,
        department=profile.department,
        job_role=profile.job_role,
        job_level=profile.job_level,
        location=profile.location,
        employment_type=profile.employment_type.value,
        employment_status=profile.employment_status.value,
        existing_entitlements=len(profile.existing_entitlement_ids),
    )
    return {}
