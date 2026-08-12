"""Structured logging setup and correlation-context helpers.

Every log line emitted during an analysis carries the workflow context that an
IGA auditor needs: correlation_id, analysis_id, employee_id and workflow_step.
Secrets are never logged - the only place a credential could leak is the
database URL, and that is redacted at the source (`Settings.safe_database_url`).
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import structlog

from app.config import get_settings

# Context that is automatically merged into every log record.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_analysis_id: ContextVar[str | None] = ContextVar("analysis_id", default=None)
_employee_id: ContextVar[str | None] = ContextVar("employee_id", default=None)
_workflow_step: ContextVar[str | None] = ContextVar("workflow_step", default=None)

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "llm_api_key",
    "authorization",
    "postgres_password",
}

_configured = False


def _context_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Merge the ambient analysis context into the event."""
    for key, var in (
        ("correlation_id", _correlation_id),
        ("analysis_id", _analysis_id),
        ("employee_id", _employee_id),
        ("workflow_step", _workflow_step),
    ):
        value = var.get()
        if value is not None and key not in event_dict:
            event_dict[key] = value
    return event_dict


def _redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Defensively redact anything that looks like a secret."""
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(level: str | None = None, json_output: bool | None = None) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _configured
    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    resolved_json = settings.log_json if json_output is None else json_output

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, resolved_level, logging.INFO),
        force=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if resolved_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _context_processor,
            _redact_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, resolved_level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[return-value]


def new_correlation_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def analysis_context(
    *,
    correlation_id: str | None = None,
    analysis_id: str | None = None,
    employee_id: str | None = None,
) -> Iterator[str]:
    """Bind analysis-scoped logging context for the duration of the block."""
    cid = correlation_id or new_correlation_id()
    tokens = [
        _correlation_id.set(cid),
        _analysis_id.set(analysis_id),
        _employee_id.set(employee_id),
    ]
    try:
        yield cid
    finally:
        _correlation_id.reset(tokens[0])
        _analysis_id.reset(tokens[1])
        _employee_id.reset(tokens[2])


@contextmanager
def workflow_step(step: str) -> Iterator[None]:
    """Bind the current workflow step for the duration of the block."""
    token = _workflow_step.set(step)
    try:
        yield
    finally:
        _workflow_step.reset(token)


def bind_analysis_id(analysis_id: str) -> None:
    """Attach the analysis id once it has been generated mid-workflow."""
    _analysis_id.set(analysis_id)


def current_correlation_id() -> str | None:
    return _correlation_id.get()
