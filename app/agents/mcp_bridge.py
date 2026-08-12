"""MCP client bridge for the LangGraph workflow.

The workflow nodes are synchronous (they ultimately hit a synchronous
SQLAlchemy session), while the MCP client is asynchronous. This module owns
that boundary: it runs a private event loop on a background thread and keeps a
single MCP session open for the whole workflow run, so a nine-node analysis
performs one session handshake rather than nine.

Four execution modes, all selected by `MCP_CLIENT_MODE`:

* ``inmemory`` (default) - a real MCP client against the in-process FastMCP
  server over memory streams. Full protocol: initialise, capability
  negotiation, JSON-RPC tool calls. No socket, no subprocess.
* ``stdio`` - a real MCP client that spawns ``python -m app.mcp.server`` and
  speaks MCP over its stdin/stdout. This is what an external MCP client such as
  Claude Desktop does.
* ``http`` - a real MCP client against a running Streamable-HTTP MCP server.
* ``direct`` - bypasses MCP entirely and calls the tool handler in process.

The tradeoff, stated plainly: ``inmemory`` is the default for the API path
because spawning a subprocess (and a second database pool) per analysis costs
seconds and buys nothing when the tools live in the same deployable. It is
still the MCP protocol end to end. ``stdio`` exercises the out-of-process path
and is what the demo script and the MCP integration tests use, so the
cross-process contract is covered rather than assumed. ``direct`` exists for
one reason: ``run_access_analysis`` is itself an MCP tool, and a tool handler
must not re-enter the transport that is currently serving it.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from concurrent.futures import Future
from typing import Any

from pydantic import TypeAdapter

from app.config import McpClientMode, Settings, get_settings
from app.domain.exceptions import DomainError, McpToolError, domain_error_from_message
from app.logging import get_logger

logger = get_logger(__name__)


class _LoopThread:
    """A background thread owning a private asyncio event loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="mcp-bridge-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Any, timeout: float) -> Any:
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


class McpToolInvoker:
    """Synchronous facade over an MCP session (or a direct in-process call)."""

    def __init__(
        self, mode: McpClientMode | None = None, settings: Settings | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.mode: McpClientMode = mode or self.settings.mcp_client_mode
        self.timeout = float(self.settings.mcp_client_timeout_seconds)
        self._loop_thread: _LoopThread | None = None
        self._client: Any = None
        self._client_cm: Any = None
        self.tool_calls: list[str] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "McpToolInvoker":
        if self.mode != "direct":
            self._connect()
        logger.info("mcp.client.ready", mode=self.mode)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _build_transport(self) -> Any:
        from fastmcp import Client

        match self.mode:
            case "inmemory":
                from app.mcp.server import mcp_server

                return Client(mcp_server, timeout=self.timeout)
            case "stdio":
                import os

                from fastmcp.client.transports import StdioTransport

                # The child is a separate process with its own settings and its
                # own logger, so the parent's environment (database, log level,
                # demo mode) is passed through explicitly rather than left to
                # whatever the transport happens to inherit.
                return Client(
                    StdioTransport(
                        command=sys.executable,
                        args=["-m", "app.mcp.server", "--transport", "stdio"],
                        env={**os.environ, "LOG_LEVEL": self.settings.log_level},
                    ),
                    timeout=self.timeout,
                )
            case "http":
                return Client(self.settings.mcp_client_url, timeout=self.timeout)
            case _:  # pragma: no cover - guarded by the caller
                raise McpToolError(f"Unsupported MCP client mode '{self.mode}'.")

    def _connect(self) -> None:
        self._loop_thread = _LoopThread()
        client = self._build_transport()

        async def _open() -> Any:
            cm = client.__aenter__()
            return await cm

        try:
            self._client = self._loop_thread.submit(_open(), self.timeout)
            self._client_cm = client
        except Exception as exc:
            self.close()
            raise McpToolError(
                f"Could not open an MCP session in '{self.mode}' mode: {exc}"
            ) from exc

    def close(self) -> None:
        if self._client_cm is not None and self._loop_thread is not None:
            try:
                self._loop_thread.submit(
                    self._client_cm.__aexit__(None, None, None), self.timeout
                )
            except Exception as exc:  # pragma: no cover - best-effort teardown
                logger.warning("mcp.client.close_failed", error=str(exc))
        self._client = None
        self._client_cm = None
        if self._loop_thread is not None:
            self._loop_thread.shutdown()
            self._loop_thread = None

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #
    def list_tools(self) -> list[str]:
        if self.mode == "direct":
            from app.mcp.tools import ALL_HANDLERS

            return sorted(ALL_HANDLERS)
        assert self._loop_thread is not None and self._client is not None
        tools = self._loop_thread.submit(self._client.list_tools(), self.timeout)
        return [t.name for t in tools]

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a tool and return its structured result as plain JSON data."""
        arguments = arguments or {}
        self.tool_calls.append(tool_name)
        logger.info("mcp.call", tool=tool_name, mode=self.mode)

        if self.mode == "direct":
            return _call_direct(tool_name, arguments)

        assert self._loop_thread is not None and self._client is not None
        try:
            result = self._loop_thread.submit(
                self._client.call_tool(tool_name, arguments), self.timeout
            )
        except Exception as exc:
            raise _translate_error(tool_name, str(exc)) from exc

        if getattr(result, "is_error", False):
            raise _translate_error(tool_name, _error_text(result))
        return _unwrap(result.structured_content)


def _translate_error(tool_name: str, message: str) -> DomainError:
    """Recover the original domain exception from a flattened MCP error.

    A tool failure that started life as `EmployeeNotFoundError` must still be
    an `EmployeeNotFoundError` when it reaches the API, or the caller sees a
    gateway error where a 404 belongs. Anything unrecognised stays an
    `McpToolError`, which is genuinely a transport-level failure.
    """
    for candidate in _error_candidates(message):
        recovered = domain_error_from_message(candidate)
        if recovered is not None:
            recovered.details.setdefault("tool", tool_name)
            return recovered
    # Some failures arrive with nothing to report - a cancelled task or an
    # expired deadline often stringifies to "". Saying so beats a message that
    # trails off after the colon and leaves the reader with no lead at all.
    detail = message.strip() or (
        "no error detail was returned, which usually means the call timed out "
        "(see MCP_CLIENT_TIMEOUT_SECONDS) or the task was cancelled"
    )
    return McpToolError(
        f"MCP tool '{tool_name}' failed: {detail}", details={"tool": tool_name}
    )


def _error_candidates(message: str) -> list[str]:
    """Yield substrings that might carry the `"<code>: <detail>"` prefix."""
    candidates = [message]
    # Transports prepend their own context, e.g.
    # "Error calling tool 'get_joiner': employee_not_found: ...".
    for marker in ("': ", "\": ", "failed: ", "error: "):
        index = message.find(marker)
        if index != -1:
            candidates.append(message[index + len(marker) :])
    return candidates


def _unwrap(structured: Any) -> Any:
    """MCP wraps non-object return values in {"result": ...}; unwrap that."""
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def _error_text(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    parts = [getattr(b, "text", "") for b in blocks]
    return " ".join(p for p in parts if p) or "unknown error"


def _call_direct(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call the tool handler in process, coercing arguments the way MCP would."""
    from app.mcp.tools import ALL_HANDLERS

    handler = ALL_HANDLERS.get(tool_name)
    if handler is None:
        raise McpToolError(f"No handler registered for tool '{tool_name}'.")

    try:
        coerced = _coerce_arguments(handler, arguments)
        result = handler(**coerced)
    except DomainError:
        raise
    except Exception as exc:
        # The handler wraps domain failures as ToolError to keep one error
        # contract across transports; unwrap it back the same way the MCP path
        # does, so direct mode reports identical exception types.
        raise _translate_error(tool_name, str(exc)) from exc
    return _to_jsonable(result)


def _coerce_arguments(handler: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate raw JSON arguments into the handler's annotated types.

    MCP does this from the generated JSON schema; direct mode does it from the
    same annotations, so both paths reject the same bad input.
    """
    signature = inspect.signature(handler)
    hints = _resolved_hints(handler)
    coerced: dict[str, Any] = {}
    for name, value in arguments.items():
        if name not in signature.parameters:
            continue
        annotation = hints.get(name)
        if annotation is None:
            coerced[name] = value
            continue
        coerced[name] = TypeAdapter(annotation).validate_python(value)
    return coerced


def _resolved_hints(handler: Any) -> dict[str, Any]:
    target = inspect.unwrap(handler)
    try:
        import typing

        return typing.get_type_hints(target)
    except Exception:  # pragma: no cover - exotic annotations
        return {}


def _to_jsonable(value: Any) -> Any:
    """Match the shape MCP would have produced for the same return value."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value
