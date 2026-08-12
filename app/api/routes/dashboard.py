"""Dashboard metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import AnalysisSvc
from app.domain.models import DashboardMetrics

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "",
    response_model=DashboardMetrics,
    summary="Governance summary metrics",
    description=(
        "Counts across all persisted analyses: joiners, analyses, recommendations, the "
        "distribution of recommendation outcomes and the number of high and critical risk "
        "recommendations."
    ),
)
def dashboard(service: AnalysisSvc) -> DashboardMetrics:
    return service.dashboard()
