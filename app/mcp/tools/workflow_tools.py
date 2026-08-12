"""MCP tools: whole-workflow entry points and reporting.

`run_access_analysis` runs the LangGraph workflow, so an MCP client gets the
same end-to-end journey the REST API exposes. The graph import is deferred to
call time - the graph builds an MCP client of its own, and importing it at
module scope would close a cycle between the server and its own consumer.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from app.domain.models import AnalysisResult, DashboardMetrics
from app.mcp.tools._support import mcp_tool_handler, tool_session
from app.services.analysis_service import AnalysisService


@mcp_tool_handler
def run_access_analysis(employee_id: str) -> AnalysisResult:
    from app.agents.graph import run_analysis

    # Executing in `direct` mode keeps the workflow from re-entering the MCP
    # transport from inside a tool handler. Every other entry point (REST,
    # demo script, tests) drives the graph through a real MCP client.
    return run_analysis(employee_id, mcp_client_mode="direct")


@mcp_tool_handler
def get_analysis_result(analysis_id: str) -> AnalysisResult:
    with tool_session() as session:
        return AnalysisService(session).get_analysis(analysis_id)


@mcp_tool_handler
def get_dashboard_metrics() -> DashboardMetrics:
    with tool_session() as session:
        return AnalysisService(session).dashboard()


DESCRIPTIONS: dict[str, str] = {
    "run_access_analysis": (
        "Run the complete onboarding analysis for a new joiner: profile the identity, find "
        "peers, score entitlement affinity, evaluate risk, validate policies, check SoD, decide, "
        "explain, generate the simulated SailPoint payload and persist everything. Returns the "
        "full analysis record including its analysis_id."
    ),
    "get_analysis_result": (
        "Retrieve a previously persisted analysis by id, including recommendations, peer "
        "evidence, policy and SoD results, explanations and the SailPoint payload."
    ),
    "get_dashboard_metrics": (
        "Summary governance metrics across all analyses: joiner and analysis counts, and the "
        "distribution of recommendation outcomes and risk levels."
    ),
}

TAGS: dict[str, set[str]] = {
    "run_access_analysis": {"workflow"},
    "get_analysis_result": {"workflow", "read"},
    "get_dashboard_metrics": {"reporting", "read"},
}

HANDLERS: dict[str, Callable] = {
    "run_access_analysis": run_access_analysis,
    "get_analysis_result": get_analysis_result,
    "get_dashboard_metrics": get_dashboard_metrics,
}


def register(mcp: FastMCP) -> None:
    for name, handler in HANDLERS.items():
        kwargs = {"timeout": 300.0} if name == "run_access_analysis" else {}
        mcp.tool(handler, name=name, description=DESCRIPTIONS[name], tags=TAGS[name], **kwargs)
