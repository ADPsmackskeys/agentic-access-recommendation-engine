"""Domain exceptions.

These are transport-agnostic: the REST layer maps them to status codes and the
MCP layer maps them to tool errors, but the services themselves never know
about HTTP or MCP.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every expected, business-meaningful failure."""

    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class EmployeeNotFoundError(DomainError):
    code = "employee_not_found"


class AnalysisNotFoundError(DomainError):
    code = "analysis_not_found"


class EntitlementNotFoundError(DomainError):
    code = "entitlement_not_found"


class NoPeersFoundError(DomainError):
    """No peer group could be established under any matching strategy."""

    code = "no_peers_found"


class InvalidPolicyDefinitionError(DomainError):
    """A policy row could not be interpreted; the engine fails closed."""

    code = "invalid_policy_definition"


class WorkflowError(DomainError):
    code = "workflow_error"


class McpToolError(DomainError):
    """An MCP tool invocation failed at the protocol or handler level."""

    code = "mcp_tool_error"


class LlmError(DomainError):
    """The LLM could not produce an explanation. Never fatal to a decision."""

    code = "llm_error"


# MCP flattens exceptions to strings on the wire. The tool handler prefixes
# every domain failure with its code (`"employee_not_found: ..."`), and this
# registry turns that prefix back into the original exception type on the
# client side. Without it, a missing employee reaching the API through the
# workflow would be reported as a gateway failure rather than a 404.
DOMAIN_ERROR_CODES: dict[str, type[DomainError]] = {
    cls.code: cls
    for cls in (
        EmployeeNotFoundError,
        AnalysisNotFoundError,
        EntitlementNotFoundError,
        NoPeersFoundError,
        InvalidPolicyDefinitionError,
        WorkflowError,
        McpToolError,
        LlmError,
    )
}


def domain_error_from_message(message: str) -> DomainError | None:
    """Rebuild a domain exception from a `"<code>: <message>"` string."""
    code, separator, detail = message.partition(": ")
    if not separator:
        return None
    error_class = DOMAIN_ERROR_CODES.get(code.strip())
    if error_class is None:
        return None
    return error_class(detail.strip())
