"""Shared node plumbing: MCP access, logging and failure policy.

Failure policy is deliberate. Most nodes are *non-fatal*: if one fails it
records the error on the state and returns an empty result, and the workflow
keeps going so that everything already established still reaches the database
and the audit trail. A node marked *fatal* (only `load_joiner`) re-raises,
because there is no meaningful analysis to persist when the identity itself
cannot be resolved.

That is what keeps the guarantee in the specification: an explanation failure
degrades the prose, never the decision.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig

from app.agents.mcp_bridge import McpToolInvoker
from app.agents.state import AccessRecommendationState
from app.logging import get_logger, workflow_step

logger = get_logger("workflow")

# LangGraph decides whether to pass `config` by comparing the parameter's raw
# annotation against the `RunnableConfig` type object. Because these modules use
# `from __future__ import annotations`, every annotation is a *string* at
# runtime, which never matches - so LangGraph would silently call each node with
# state only and the MCP invoker would never arrive. Stamping an explicit,
# already-resolved signature onto the wrapper fixes that at one site instead of
# forcing every node module to drop PEP 563.
_NODE_SIGNATURE = inspect.Signature(
    parameters=[
        inspect.Parameter(
            "state", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=AccessRecommendationState
        ),
        inspect.Parameter(
            "config",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=Optional[RunnableConfig],
        ),
    ],
    return_annotation=dict,
)


def get_invoker(config: RunnableConfig | None) -> McpToolInvoker:
    """Pull the MCP invoker the graph runner attached to this invocation."""
    configurable = (config or {}).get("configurable", {})
    invoker = configurable.get("mcp_invoker")
    if invoker is None:
        raise RuntimeError(
            "No MCP invoker on the runnable config. Drive the graph through "
            "`app.agents.graph.run_analysis`, which attaches one."
        )
    return invoker


def node(
    name: str, *, fatal: bool = False
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Wrap a node with step logging, timing and the failure policy."""

    def decorator(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(func)
        def wrapper(
            state: AccessRecommendationState, config: RunnableConfig | None = None
        ) -> dict[str, Any]:
            with workflow_step(name):
                logger.info("workflow.step.start", step=name)
                try:
                    result = func(state, config)
                except Exception as exc:
                    logger.error("workflow.step.failed", step=name, error=str(exc))
                    if fatal:
                        raise
                    return {
                        "errors": [f"{name}: {exc}"],
                        "steps_completed": [f"{name}:FAILED"],
                    }
                logger.info("workflow.step.complete", step=name)
                result.setdefault("steps_completed", [name])
                return result

        # `functools.wraps` set `__wrapped__`, which makes `inspect.signature`
        # follow through to the stringified annotations again; drop it so the
        # explicit signature above is what LangGraph inspects.
        wrapper.__signature__ = _NODE_SIGNATURE  # type: ignore[attr-defined]
        wrapper.__annotations__ = {
            "state": AccessRecommendationState,
            "config": Optional[RunnableConfig],
            "return": dict,
        }
        wrapper.__dict__.pop("__wrapped__", None)
        return wrapper

    return decorator


def record_tool_calls(invoker: McpToolInvoker, before: int) -> list[str]:
    """Tool names invoked since `before`, for the workflow's own audit trail."""
    return invoker.tool_calls[before:]
