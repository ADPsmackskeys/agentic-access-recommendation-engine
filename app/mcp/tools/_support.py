"""Shared plumbing for MCP tools.

MCP tools stay thin: open a session, call the same domain service the REST API
calls, return a typed model. The only tool-specific logic that lives here is
turning domain exceptions into MCP `ToolError`s so that a client receives a
useful message rather than a stack trace.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.domain.exceptions import DomainError
from app.logging import get_logger

logger = get_logger("mcp.tools")

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def tool_session() -> Iterator[Session]:
    """A transactional session for the duration of one tool call."""
    with session_scope() as session:
        yield session


def mcp_tool_handler(func: Callable[P, R]) -> Callable[P, R]:
    """Log invocation and translate domain errors into MCP tool errors."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        tool_name = func.__name__
        logger.info("mcp.tool.invoked", tool=tool_name, arguments=_safe_args(kwargs))
        try:
            result = func(*args, **kwargs)
        except DomainError as exc:
            logger.warning("mcp.tool.domain_error", tool=tool_name, code=exc.code,
                           error=exc.message)
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        except ToolError:
            raise
        except Exception as exc:
            logger.error("mcp.tool.failed", tool=tool_name, error=str(exc))
            raise ToolError(f"Tool '{tool_name}' failed: {exc}") from exc
        logger.info("mcp.tool.completed", tool=tool_name)
        return result

    return wrapper


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Summarise arguments for logging without dumping large payloads."""
    summary: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = f"<list len={len(value)}>"
        else:
            summary[key] = f"<{type(value).__name__}>"
    return summary
