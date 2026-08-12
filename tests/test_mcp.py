"""MCP protocol integration tests.

These drive a real MCP client against the FastMCP server: session
initialisation, tool discovery and JSON-RPC tool invocation. They assert on the
protocol surface an external client actually sees - names, input schemas,
structured results and error propagation - rather than calling the underlying
Python functions.

The in-memory transport is used for speed; `test_stdio_transport_works` covers
the out-of-process path so the cross-process contract is exercised too.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.mcp.server import build_mcp_server, mcp_server

pytestmark = [pytest.mark.integration, pytest.mark.mcp]

# Every capability the specification requires to be reachable over MCP.
REQUIRED_TOOLS = {
    "get_joiner",
    "find_peer_employees",
    "calculate_entitlement_affinity",
    "evaluate_entitlement_risk",
    "validate_entitlement_policy",
    "check_sod_conflicts",
    "generate_access_explanation",
    "generate_sailpoint_request",
    "run_access_analysis",
    "get_analysis_result",
}


@pytest.fixture
def mcp_client(app_session_factory) -> Client:
    """A real MCP client bound to the in-process server over memory streams."""
    return Client(mcp_server)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
async def test_client_can_discover_tools(mcp_client: Client) -> None:
    async with mcp_client as client:
        tools = await client.list_tools()
    assert REQUIRED_TOOLS <= {t.name for t in tools}


async def test_tools_expose_usable_schemas_and_descriptions(mcp_client: Client) -> None:
    async with mcp_client as client:
        tools = {t.name: t for t in await client.list_tools()}

    peer_tool = tools["find_peer_employees"]
    assert peer_tool.description and len(peer_tool.description) > 40
    assert peer_tool.inputSchema["properties"]["employee_id"]["type"] == "string"
    assert "employee_id" in peer_tool.inputSchema["required"]

    sod_tool = tools["check_sod_conflicts"]
    assert sod_tool.inputSchema["properties"]["entitlement_ids"]["type"] == "array"


def test_server_can_be_rebuilt_independently() -> None:
    """Server construction is a pure function of the registered tools."""
    rebuilt = build_mcp_server()
    assert rebuilt.name == mcp_server.name


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #
async def test_get_joiner_returns_a_profile(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await client.call_tool("get_joiner", {"employee_id": "NJ1001"})
    data = result.structured_content
    assert data["employee_id"] == "NJ1001"
    assert data["job_role"] == "Financial Analyst"
    assert data["employment_status"] == "PENDING_START"


async def test_find_peer_employees_returns_the_cohort(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await client.call_tool("find_peer_employees", {"employee_id": "NJ1001"})
    data = result.structured_content
    assert data["peer_count"] == 5
    assert data["matching_strategy"] == "job_role_department_job_level"
    assert data["confidence"] == 0.8075
    assert len(data["peer_ids"]) == 5


async def test_affinity_over_mcp_matches_the_documented_scores(
    mcp_client: Client,
) -> None:
    async with mcp_client as client:
        peers = (
            await client.call_tool("find_peer_employees", {"employee_id": "NJ1001"})
        ).structured_content
        affinity = (
            await client.call_tool(
                "calculate_entitlement_affinity",
                {
                    "employee_id": "NJ1001",
                    "peer_ids": peers["peer_ids"],
                    "matching_strategy": peers["matching_strategy"],
                },
            )
        ).structured_content

    scores = {c["entitlement_id"]: c["affinity_score"] for c in affinity["candidates"]}
    assert scores["SAP_FIN_DISPLAY"] == 100.0
    assert scores["POWERBI_FINANCE"] == 100.0
    assert scores["SAP_AP_INVOICE"] == 80.0
    assert scores["FIN_SHAREPOINT"] == 20.0


async def test_risk_evaluation_over_mcp(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await client.call_tool(
            "evaluate_entitlement_risk",
            {"entitlement_ids": ["SAP_FIN_DISPLAY", "AD_DOMAIN_ADMIN"]},
        )
    assessments = {a["entitlement_id"]: a for a in result.structured_content["result"]}
    assert assessments["SAP_FIN_DISPLAY"]["risk_level"] == "LOW"          # 15
    assert assessments["AD_DOMAIN_ADMIN"]["risk_level"] == "CRITICAL"     # 100
    assert assessments["AD_DOMAIN_ADMIN"]["required_approval_tier"] == "HUMAN_REVIEW"


async def test_policy_validation_over_mcp(mcp_client: Client) -> None:
    """SAP_PAYMENT_APPROVER scores 95, clearing POL005 (70) and POL006 (90)."""
    async with mcp_client as client:
        result = await client.call_tool(
            "validate_entitlement_policy",
            {
                "employee_id": "NJ1001",
                "entitlement_ids": ["SAP_PAYMENT_APPROVER", "SAP_FIN_DISPLAY"],
            },
        )
    data = result.structured_content
    assert data["status"] == "REQUIRES_APPROVAL"
    assert {"POL005", "POL006"} <= set(data["evaluated_policy_ids"])

    by_entitlement = {r["entitlement_id"]: r for r in data["results"]}
    assert by_entitlement["SAP_PAYMENT_APPROVER"]["status"] == "REQUIRES_APPROVAL"
    assert by_entitlement["SAP_FIN_DISPLAY"]["status"] == "PASS"


async def test_sod_check_over_mcp(mcp_client: Client) -> None:
    """SOD001: vendor creation and payment approval held by one identity."""
    async with mcp_client as client:
        result = await client.call_tool(
            "check_sod_conflicts",
            {
                "employee_id": "NJ1001",
                "entitlement_ids": ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"],
            },
        )
    data = result.structured_content
    assert data["status"] == "CONFLICT"
    assert data["severity"] == "CRITICAL"
    assert data["conflicts"][0]["sod_id"] == "SOD001"


async def test_dashboard_metrics_over_mcp(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await client.call_tool("get_dashboard_metrics", {})
    assert result.structured_content["total_joiners"] == 10


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
async def test_domain_errors_surface_as_tool_errors(mcp_client: Client) -> None:
    async with mcp_client as client:
        with pytest.raises(ToolError, match="employee_not_found"):
            await client.call_tool("get_joiner", {"employee_id": "NOPE"})


async def test_unknown_entitlement_is_reported(mcp_client: Client) -> None:
    async with mcp_client as client:
        with pytest.raises(ToolError, match="entitlement_not_found"):
            await client.call_tool(
                "evaluate_entitlement_risk", {"entitlement_ids": ["NOT_A_REAL_ENTITLEMENT"]}
            )


async def test_schema_violations_are_rejected_by_the_protocol(mcp_client: Client) -> None:
    async with mcp_client as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_joiner", {})  # employee_id is required


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
async def test_results_are_json_serialisable(mcp_client: Client) -> None:
    """Whatever a tool returns has to survive the wire."""
    async with mcp_client as client:
        result = await client.call_tool("find_peer_employees", {"employee_id": "NJ1001"})
    assert json.loads(json.dumps(result.structured_content))


@pytest.mark.slow
def test_stdio_transport_works(app_session_factory) -> None:
    """The out-of-process path: a subprocess server speaking MCP over stdio.

    This is what an external client (Claude Desktop, an agent runtime) does, so
    it is covered rather than assumed.
    """
    from app.agents.mcp_bridge import McpToolInvoker

    with McpToolInvoker(mode="stdio") as invoker:
        assert REQUIRED_TOOLS <= set(invoker.list_tools())
        peers = invoker.call("find_peer_employees", {"employee_id": "NJ1001"})
        assert peers["peer_count"] == 5


def test_direct_mode_calls_the_same_handlers(app_session_factory) -> None:
    """`direct` mode must be the same code path, minus the transport."""
    from app.agents.mcp_bridge import McpToolInvoker
    from app.mcp.tools import ALL_HANDLERS

    with McpToolInvoker(mode="direct") as invoker:
        assert set(invoker.list_tools()) == set(ALL_HANDLERS)
        peers = invoker.call("find_peer_employees", {"employee_id": "NJ1001"})
    assert peers["peer_count"] == 5
    assert peers["matching_strategy"] == "job_role_department_job_level"
