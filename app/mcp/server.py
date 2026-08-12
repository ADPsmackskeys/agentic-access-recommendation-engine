"""FastMCP server.

Exposes the governance capabilities as MCP tools over a real MCP session. The
same server object is used three ways:

* `python -m app.mcp.server`  - stdio (or HTTP) transport for external clients;
* mounted on the FastAPI app   - Streamable HTTP at `/mcp`;
* in-memory                    - the LangGraph workflow connects a real MCP
                                 client to this instance over memory streams.

All three are the MCP protocol. The in-memory transport is not a shortcut past
it: the client still initialises a session, negotiates capabilities and
exchanges JSON-RPC messages - it simply does so without a socket.

Tools delegate straight to the domain services in `app.services`, which are the
same objects the REST layer uses. No business logic lives in this module.
"""

from __future__ import annotations

import argparse

from fastmcp import FastMCP

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.mcp.tools import governance_tools, identity_tools, workflow_tools

logger = get_logger(__name__)

INSTRUCTIONS = """\
Agentic Access Recommendation Engine - identity governance tools.

This server answers the question "what application entitlements should this new
joiner receive?" using deterministic governance logic over a PostgreSQL identity
warehouse.

Typical sequence:
  1. get_joiner(employee_id)
  2. find_peer_employees(employee_id)                    -> peer_ids, strategy
  3. calculate_entitlement_affinity(employee_id, peer_ids)
  4. evaluate_entitlement_risk(entitlement_ids)
  5. validate_entitlement_policy(employee_id, entitlement_ids)
  6. check_sod_conflicts(employee_id, entitlement_ids)
  7. generate_access_explanation(...) / generate_sailpoint_request(...)

Or call run_access_analysis(employee_id) to execute the whole workflow at once.

Important: affinity, risk classification, policy outcomes, SoD detection and the
final recommendation status are computed by deterministic rules. These tools
report those decisions; they do not accept overrides of them.
"""


def build_mcp_server() -> FastMCP:
    """Construct the server and register every tool."""
    settings = get_settings()
    mcp = FastMCP(name=settings.mcp_server_name, instructions=INSTRUCTIONS)

    identity_tools.register(mcp)
    governance_tools.register(mcp)
    workflow_tools.register(mcp)

    logger.info("mcp.server.built", name=settings.mcp_server_name)
    return mcp


# Module-level singleton: the in-memory transport and the FastAPI mount both
# need to talk to the *same* server object.
mcp_server: FastMCP = build_mcp_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="Transport to serve on (defaults to MCP_TRANSPORT).",
    )
    parser.add_argument("--host", default=None, help="Bind host for HTTP transport.")
    parser.add_argument("--port", type=int, default=None, help="Bind port for HTTP transport.")
    args = parser.parse_args()

    settings = get_settings()
    # stdio transport speaks JSON-RPC on stdout, so logs must go to stderr -
    # which is exactly where `configure_logging` sends them.
    configure_logging()

    transport = args.transport or settings.mcp_transport
    if transport == "http":
        host = args.host or settings.mcp_host
        port = args.port or settings.mcp_port
        logger.info("mcp.server.start", transport="http", host=host, port=port)
        mcp_server.run(transport="http", host=host, port=port, show_banner=False)
    else:
        logger.info("mcp.server.start", transport="stdio")
        mcp_server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
