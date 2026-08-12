"""Health endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response, status

from app.api.dependencies import AppSettings
from app.db.session import check_database_health
from app.schemas.api import HealthResponse

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    description=(
        "Liveness and readiness probe. Reports 200 when the database is reachable and 503 when "
        "it is not, so an orchestrator can use it directly as a readiness check."
    ),
)
def health(settings: AppSettings, response: Response) -> HealthResponse:
    db_ok, db_error = check_database_health()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        service=settings.app_name,
        environment=settings.environment,
        version=VERSION,
        database="up" if db_ok else "down",
        database_error=db_error,
        demo_mode=settings.demo_mode,
        llm_enabled=settings.llm_enabled,
        mcp_client_mode=settings.mcp_client_mode,
        timestamp=datetime.now(timezone.utc),
    )
