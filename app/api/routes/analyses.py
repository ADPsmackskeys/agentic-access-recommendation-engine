"""Analysis retrieval endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Path, Query

from app.api.dependencies import AnalysisSvc
from app.schemas.api import (
    AnalysisListResponse,
    AnalysisResponse,
    AnalysisSummary,
    ErrorResponse,
)

router = APIRouter(prefix="/analyses", tags=["analyses"])


@router.get(
    "",
    response_model=AnalysisListResponse,
    summary="List analyses",
    description="Most recent analyses first.",
)
def list_analyses(
    service: AnalysisSvc,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AnalysisListResponse:
    rows = service.list_analyses(limit=limit, offset=offset)
    return AnalysisListResponse(
        count=len(rows), analyses=[AnalysisSummary(**row) for row in rows]
    )


@router.get(
    "/{analysis_id}",
    response_model=AnalysisResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get a persisted analysis",
    description=(
        "Reconstructs the complete analysis from the audit trail: recommendations, peer "
        "evidence, policy results, SoD results, explanations and the SailPoint payload."
    ),
)
def get_analysis(
    service: AnalysisSvc,
    analysis_id: str = Path(max_length=64, description="Analysis UUID."),
) -> AnalysisResponse:
    return AnalysisResponse.from_domain(service.get_analysis(analysis_id))
