"""FastAPI application.

Wires the REST API, mounts the MCP server over Streamable HTTP, and installs
the middleware and exception handlers that make failures observable and
requests bounded.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import access_requests, analyses, dashboard, health, joiners
from app.config import get_settings
from app.db.session import check_database_health, dispose_engine
from app.domain.exceptions import (
    AnalysisNotFoundError,
    DomainError,
    EmployeeNotFoundError,
    EntitlementNotFoundError,
    InvalidPolicyDefinitionError,
    LlmError,
    McpToolError,
    NoPeersFoundError,
    WorkflowError,
)
from app.logging import analysis_context, configure_logging, get_logger, new_correlation_id
from app.schemas.api import ErrorResponse

logger = get_logger(__name__)

DESCRIPTION = """
AI-assisted onboarding access recommendations with deterministic identity governance.

For a new joiner the service identifies comparable peers, scores entitlement affinity,
evaluates risk, validates policies, checks segregation of duties, produces an explainable
decision and generates a SailPoint IdentityIQ-style access request.

**Governance is deterministic.** Peer selection, affinity, risk classification, policy
evaluation, SoD detection, approval tier and the final recommendation status are computed by
explicit rules over database state. The LLM layer only turns that structured evidence into
prose, and cannot alter a decision.

**SailPoint is simulated.** No SailPoint environment is contacted; generated requests are
persisted with status `SIMULATED`.

The same domain services are exposed over MCP - see the `/mcp` endpoint and `app/mcp/server.py`.
"""

# HTTP status for each domain failure. Anything not listed is a 500.
_ERROR_STATUS: dict[type[DomainError], int] = {
    EmployeeNotFoundError: status.HTTP_404_NOT_FOUND,
    AnalysisNotFoundError: status.HTTP_404_NOT_FOUND,
    EntitlementNotFoundError: status.HTTP_404_NOT_FOUND,
    NoPeersFoundError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidPolicyDefinitionError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    McpToolError: status.HTTP_502_BAD_GATEWAY,
    LlmError: status.HTTP_503_SERVICE_UNAVAILABLE,
    WorkflowError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def build_lifespan(mcp_app):
    """Application lifespan, chaining the mounted MCP app's own lifespan.

    FastMCP's Streamable HTTP transport starts a session-manager task group in
    its lifespan. Mounting the ASGI app without running that lifespan yields a
    500 on the first MCP request, so the child lifespan is entered here rather
    than left to chance.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging()
        logger.info(
            "app.startup",
            environment=settings.environment,
            database=settings.safe_database_url(),
            demo_mode=settings.demo_mode,
            llm_enabled=settings.llm_enabled,
            mcp_client_mode=settings.mcp_client_mode,
        )
        db_ok, db_error = check_database_health()
        if not db_ok:
            # Not fatal: the container should start and report unhealthy rather
            # than crash-loop while the database is still coming up.
            logger.error("app.startup.database_unavailable", error=db_error)

        if mcp_app is not None:
            async with mcp_app.router.lifespan_context(app):
                yield
        else:
            yield

        dispose_engine()
        logger.info("app.shutdown")

    return lifespan


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    # Built before the FastAPI app so its lifespan can be chained in.
    mcp_app = None
    try:
        from app.mcp.server import mcp_server

        mcp_app = mcp_server.http_app(path="/")
    except Exception as exc:  # pragma: no cover - REST must survive this
        logger.error("app.mcp_build_failed", error=str(exc))

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=build_lifespan(mcp_app),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Correlation-Id"],
        max_age=600,
    )

    @app.middleware("http")
    async def correlation_and_limits(request: Request, call_next):
        """Bind a correlation id to the request and bound the request body."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > settings.max_request_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content=ErrorResponse(
                            error="request_too_large",
                            message=(
                                f"Request body exceeds the {settings.max_request_bytes} byte "
                                f"limit."
                            ),
                        ).model_dump(mode="json"),
                    )
            except ValueError:
                pass

        correlation_id = request.headers.get("X-Correlation-Id") or new_correlation_id()
        with analysis_context(correlation_id=correlation_id):
            response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response

    # --- Exception handlers ------------------------------------------- #
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        http_status = _ERROR_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST)
        log = logger.warning if http_status < 500 else logger.error
        log("api.domain_error", code=exc.code, error=exc.message, path=request.url.path)
        return JSONResponse(
            status_code=http_status,
            content=ErrorResponse(
                error=exc.code,
                message=exc.message,
                details=exc.details,
                correlation_id=request.headers.get("X-Correlation-Id"),
            ).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorResponse(
                error="validation_error",
                message="Request validation failed.",
                details={"errors": exc.errors()},
            ).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("api.unhandled_error", error=str(exc), path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                error="internal_error",
                # Deliberately generic: internals never leak to the client.
                message="An unexpected error occurred while processing the request.",
            ).model_dump(mode="json"),
        )

    # --- Routes -------------------------------------------------------- #
    prefix = settings.api_prefix
    app.include_router(health.router, prefix=prefix)
    app.include_router(joiners.router, prefix=prefix)
    app.include_router(analyses.router, prefix=prefix)
    app.include_router(access_requests.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": f"{prefix}/health",
            "mcp": "/mcp",
        }

    # --- MCP over Streamable HTTP -------------------------------------- #
    # The same FastMCP server object used by the workflow and by
    # `python -m app.mcp.server`, so an external MCP client can reach the tools
    # at http://<host>:8000/mcp without running a second process.
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        logger.info("app.mcp_mounted", path="/mcp")

    return app


app = create_app()
