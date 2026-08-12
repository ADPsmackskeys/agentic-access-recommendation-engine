"""The onboarding analysis graph.

    START -> load_joiner -> profile_joiner -> find_peers -> calculate_affinity
          -> evaluate_risk -> validate_policies -> check_sod -> make_decision
          -> generate_explanation -> generate_sailpoint_payload
          -> persist_analysis -> END

The graph is linear because the governance sequence is linear: you cannot score
affinity before you have peers, and you cannot decide before risk, policy and
SoD have all reported. Branching here would buy nothing and would make the
audit trail harder to reason about.

Every node except `make_decision` and `persist_analysis` reaches its capability
through an MCP tool call; those two are in-process for reasons documented in
their own modules.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from app.agents.mcp_bridge import McpToolInvoker
from app.agents.nodes import (
    build_result,
    calculate_affinity,
    check_sod,
    evaluate_risk,
    find_peers,
    generate_explanation,
    generate_sailpoint_payload,
    load_joiner,
    make_decision,
    persist_analysis,
    profile_joiner,
    validate_policies,
)
from app.agents.state import AccessRecommendationState, initial_state
from app.config import McpClientMode, Settings, get_settings
from app.domain.enums import AnalysisStatus
from app.domain.exceptions import DomainError, WorkflowError
from app.domain.models import AnalysisResult
from app.logging import analysis_context, get_logger

logger = get_logger("workflow")

# The workflow sequence, in order. Also used by the tests that assert the graph
# executes the expected steps.
WORKFLOW_STEPS: tuple[str, ...] = (
    "load_joiner",
    "profile_joiner",
    "find_peers",
    "calculate_affinity",
    "evaluate_risk",
    "validate_policies",
    "check_sod",
    "make_decision",
    "generate_explanation",
    "generate_sailpoint_payload",
    "persist_analysis",
)

_NODES = {
    "load_joiner": load_joiner,
    "profile_joiner": profile_joiner,
    "find_peers": find_peers,
    "calculate_affinity": calculate_affinity,
    "evaluate_risk": evaluate_risk,
    "validate_policies": validate_policies,
    "check_sod": check_sod,
    "make_decision": make_decision,
    "generate_explanation": generate_explanation,
    "generate_sailpoint_payload": generate_sailpoint_payload,
    "persist_analysis": persist_analysis,
}


def build_graph():
    """Compile the analysis graph."""
    builder = StateGraph(AccessRecommendationState)
    for name, fn in _NODES.items():
        builder.add_node(name, fn)

    builder.add_edge(START, WORKFLOW_STEPS[0])
    for current, following in zip(WORKFLOW_STEPS, WORKFLOW_STEPS[1:]):
        builder.add_edge(current, following)
    builder.add_edge(WORKFLOW_STEPS[-1], END)

    return builder.compile()


_compiled_graph = None


def get_graph():
    """Compiled-graph singleton (compilation is pure and reusable)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def run_analysis(
    employee_id: str,
    *,
    correlation_id: str | None = None,
    mcp_client_mode: McpClientMode | None = None,
    settings: Settings | None = None,
) -> AnalysisResult:
    """Run the full workflow for one joiner and return the persisted result.

    One MCP session is opened for the whole run and closed on the way out, so
    the eleven steps share a single protocol handshake.
    """
    settings = settings or get_settings()
    analysis_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    with analysis_context(
        correlation_id=correlation_id, analysis_id=analysis_id, employee_id=employee_id
    ) as cid:
        logger.info(
            "workflow.start",
            employee_id=employee_id,
            analysis_id=analysis_id,
            mcp_mode=mcp_client_mode or settings.mcp_client_mode,
        )
        state = initial_state(employee_id, analysis_id, cid, started_at)

        try:
            with McpToolInvoker(mode=mcp_client_mode, settings=settings) as invoker:
                final_state = get_graph().invoke(
                    state,
                    config={
                        "configurable": {"mcp_invoker": invoker},
                        "recursion_limit": len(WORKFLOW_STEPS) + 10,
                    },
                )
        except DomainError:
            logger.error("workflow.failed", employee_id=employee_id, analysis_id=analysis_id)
            raise
        except Exception as exc:
            logger.error(
                "workflow.failed",
                employee_id=employee_id,
                analysis_id=analysis_id,
                error=str(exc),
            )
            raise WorkflowError(
                f"Analysis workflow failed for '{employee_id}': {exc}",
                details={"employee_id": employee_id, "analysis_id": analysis_id},
            ) from exc

        result = build_result(final_state)
        logger.info(
            "workflow.complete",
            employee_id=employee_id,
            analysis_id=analysis_id,
            status=result.status.value,
            recommendations=len(result.decisions),
            mcp_tool_calls=len(final_state.get("mcp_tool_calls") or []),
            persisted=final_state.get("persisted", False),
        )
        if not final_state.get("persisted", False):
            # The analysis ran but could not be written; say so rather than
            # handing back a result that cannot be retrieved later.
            result.status = AnalysisStatus.FAILED
            result.errors = list(result.errors) + [
                "persist_analysis: the analysis was not written to the database."
            ]
        return result
