"""Joiner endpoints: listing, profile lookup and running the analysis."""

from __future__ import annotations

from fastapi import APIRouter, Body, Path, Query

from app.agents.graph import run_analysis
from app.api.dependencies import AnalysisSvc
from app.domain.models import EmployeeProfile
from app.logging import get_logger
from app.schemas.api import (
    AnalyzeRequest,
    AnalysisResponse,
    ErrorResponse,
    JoinerListResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/joiners", tags=["joiners"])


@router.get(
    "",
    response_model=JoinerListResponse,
    summary="List new joiners",
    description=(
        "Identities with employment status PENDING_START, i.e. those eligible for onboarding "
        "access analysis."
    ),
)
def list_joiners(
    service: AnalysisSvc,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> JoinerListResponse:
    joiners = service.list_joiners(limit=limit, offset=offset)
    return JoinerListResponse(count=len(joiners), joiners=joiners)


@router.get(
    "/{employee_id}",
    response_model=EmployeeProfile,
    responses={404: {"model": ErrorResponse}},
    summary="Get employee details",
    description="Full identity profile including current entitlement holdings.",
)
def get_joiner(
    service: AnalysisSvc,
    employee_id: str = Path(max_length=64, description="Employee identifier, e.g. EMP1001."),
) -> EmployeeProfile:
    return service.get_employee_profile(employee_id)


@router.post(
    "/{employee_id}/analyze",
    response_model=AnalysisResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Run the complete access-recommendation workflow",
    description=(
        "Executes the LangGraph workflow end to end: profile the identity, find peers, score "
        "entitlement affinity, evaluate risk, validate policies, check segregation of duties, "
        "decide, explain, generate the simulated SailPoint payload and persist the whole audit "
        "trail. Returns the complete analysis."
    ),
)
def analyze_joiner(
    employee_id: str = Path(max_length=64, description="Employee identifier, e.g. EMP1001."),
    request: AnalyzeRequest | None = Body(default=None),
) -> AnalysisResponse:
    request = request or AnalyzeRequest()
    logger.info("api.analyze.request", employee_id=employee_id)
    result = run_analysis(
        employee_id,
        correlation_id=request.correlation_id,
        mcp_client_mode=request.mcp_client_mode,  # type: ignore[arg-type]
    )
    return AnalysisResponse.from_domain(result)
