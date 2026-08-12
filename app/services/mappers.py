"""ORM row -> domain model conversions.

Kept in one place so that no service quietly invents a different projection of
the same table.
"""

from __future__ import annotations

from app.db.models.identity import Employee, Entitlement as EntitlementRow
from app.domain.enums import EmploymentStatus, EmploymentType
from app.domain.models import EmployeeProfile, EmployeeSummary, Entitlement, PeerEmployee


def employee_to_profile(row: Employee, entitlement_ids: list[str]) -> EmployeeProfile:
    return EmployeeProfile(
        employee_id=row.employee_id,
        name=row.name,
        department=row.department,
        job_role=row.job_role,
        job_level=row.job_level,
        location=row.location,
        manager_id=row.manager_id,
        cost_center=row.cost_center,
        start_date=row.start_date,
        employment_status=EmploymentStatus(row.employment_status),
        employment_type=EmploymentType(row.employment_type),
        existing_entitlement_ids=entitlement_ids,
    )


def employee_to_summary(row: Employee) -> EmployeeSummary:
    return EmployeeSummary(
        employee_id=row.employee_id,
        name=row.name,
        department=row.department,
        job_role=row.job_role,
        job_level=row.job_level,
        location=row.location,
        employment_status=EmploymentStatus(row.employment_status),
        employment_type=EmploymentType(row.employment_type),
        start_date=row.start_date,
    )


def employee_to_peer(row: Employee, entitlement_count: int = 0) -> PeerEmployee:
    return PeerEmployee(
        employee_id=row.employee_id,
        name=row.name,
        department=row.department,
        job_role=row.job_role,
        job_level=row.job_level,
        location=row.location,
        entitlement_count=entitlement_count,
    )


def entitlement_to_domain(row: EntitlementRow) -> Entitlement:
    return Entitlement(
        entitlement_id=row.entitlement_id,
        entitlement_name=row.entitlement_name,
        application=row.application,
        description=row.description,
        owner=row.owner,
        risk_score=row.risk_score,
        risk_category=row.risk_category,
    )
