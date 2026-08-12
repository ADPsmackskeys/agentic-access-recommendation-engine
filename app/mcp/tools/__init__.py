"""MCP tool registrations, grouped by capability area.

`ALL_HANDLERS` is the single registry of tool-name -> handler used by both the
MCP server (which wraps them in the protocol) and the workflow's `direct`
execution mode (which calls them in process). One definition, two transports.
"""

from collections.abc import Callable

from app.mcp.tools import governance_tools, identity_tools, workflow_tools

ALL_HANDLERS: dict[str, Callable] = {
    **identity_tools.HANDLERS,
    **governance_tools.HANDLERS,
    **workflow_tools.HANDLERS,
}

__all__ = ["ALL_HANDLERS", "governance_tools", "identity_tools", "workflow_tools"]
