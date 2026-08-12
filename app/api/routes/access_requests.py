"""Access-request endpoints (simulated SailPoint)."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import AnalysisSvc, AppSettings
from app.db.repositories.analysis_repo import AnalysisRepository
from app.logging import current_correlation_id, get_logger
from app.schemas.api import AccessRequestCreate, AccessRequestResponse, ErrorResponse
from app.services.sailpoint_service import SailPointService

logger = get_logger(__name__)

router = APIRouter(prefix="/access-requests", tags=["access-requests"])


@router.post(
    "",
    response_model=AccessRequestResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"model": ErrorResponse}},
    summary="Generate a SailPoint request from an analysis",
    description=(
        "Regenerates the SailPoint IdentityIQ-style access-request payload from a persisted "
        "analysis and stores it as a new request record. Only recommendations whose decision "
        "meets the configured approval criteria are included; blocked, rejected and "
        "review-pending entitlements are listed as excluded. The request is marked SIMULATED - "
        "no SailPoint environment is contacted and nothing is provisioned."
    ),
)
def create_access_request(
    payload: AccessRequestCreate,
    service: AnalysisSvc,
    settings: AppSettings,
) -> AccessRequestResponse:
    result = service.get_analysis(payload.analysis_id)
    employee = result.employee or service.get_employee_profile(result.employee_id)

    request_payload = SailPointService(settings).generate_request_payload(
        employee=employee,
        decisions=result.decisions,
        analysis_id=result.analysis_id,
        correlation_id=current_correlation_id(),
    )
    request_id = AnalysisRepository(service.session).add_sailpoint_request(
        analysis_id=result.analysis_id,
        employee_id=result.employee_id,
        payload=request_payload,
    )
    logger.info(
        "api.access_request.created",
        request_id=request_id,
        analysis_id=result.analysis_id,
        entitlements=len(request_payload.requested_entitlements),
    )
    return AccessRequestResponse(
        request_id=request_id,
        analysis_id=result.analysis_id,
        employee_id=result.employee_id,
        status=request_payload.status,
        entitlement_count=len(request_payload.requested_entitlements),
        payload=request_payload,
    )
