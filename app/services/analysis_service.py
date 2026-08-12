"""Identity lookup, analysis persistence and retrieval.

This is the thin service the REST routes, the MCP tools and the workflow's
persistence node all share, so there is exactly one definition of "what a
joiner is" and one place an analysis gets written.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.analysis_repo import AnalysisRepository
from app.db.repositories.employee_repo import EmployeeRepository
from app.domain.exceptions import AnalysisNotFoundError, EmployeeNotFoundError
from app.domain.models import (
    AnalysisResult,
    DashboardMetrics,
    EmployeeProfile,
    EmployeeSummary,
)
from app.logging import get_logger
from app.services.mappers import employee_to_profile, employee_to_summary

logger = get_logger(__name__)


class AnalysisService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.employees = EmployeeRepository(session)
        self.analyses = AnalysisRepository(session)

    # ------------------------------------------------------------------ #
    # Identities
    # ------------------------------------------------------------------ #
    def get_employee_profile(self, employee_id: str) -> EmployeeProfile:
        row = self.employees.get(employee_id)
        if row is None:
            raise EmployeeNotFoundError(
                f"Employee '{employee_id}' does not exist.",
                details={"employee_id": employee_id},
            )
        return employee_to_profile(row, self.employees.get_entitlement_ids(employee_id))

    def list_joiners(self, limit: int = 200, offset: int = 0) -> list[EmployeeSummary]:
        return [employee_to_summary(r) for r in self.employees.list_joiners(limit, offset)]

    def list_employees(self, limit: int = 500, offset: int = 0) -> list[EmployeeSummary]:
        return [employee_to_summary(r) for r in self.employees.list_all(limit, offset)]

    # ------------------------------------------------------------------ #
    # Analyses
    # ------------------------------------------------------------------ #
    def persist(self, result: AnalysisResult) -> str:
        analysis_id = self.analyses.persist(result)
        logger.info(
            "analysis.persisted",
            analysis_id=analysis_id,
            employee_id=result.employee_id,
            recommendations=len(result.decisions),
        )
        return analysis_id

    def get_analysis(self, analysis_id: str) -> AnalysisResult:
        result = self.analyses.get(analysis_id)
        if result is None:
            raise AnalysisNotFoundError(
                f"Analysis '{analysis_id}' does not exist.",
                details={"analysis_id": analysis_id},
            )
        return result

    def latest_analysis_for(self, employee_id: str) -> AnalysisResult:
        row = self.analyses.latest_for_employee(employee_id)
        if row is None:
            raise AnalysisNotFoundError(
                f"No analysis has been run for employee '{employee_id}'.",
                details={"employee_id": employee_id},
            )
        return self.get_analysis(row.analysis_id)

    def list_analyses(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return [
            {
                "analysis_id": row.analysis_id,
                "employee_id": row.employee_id,
                "status": row.status,
                "matching_strategy": row.matching_strategy,
                "peer_count": row.peer_count,
                "candidate_count": row.candidate_count,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
            }
            for row in self.analyses.list_rows(limit, offset)
        ]

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    def dashboard(self) -> DashboardMetrics:
        return self.analyses.dashboard_metrics()
