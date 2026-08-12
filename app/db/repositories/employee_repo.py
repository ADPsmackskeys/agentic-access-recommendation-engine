"""Data access for identities and their access holdings.

All queries go through SQLAlchemy's expression language, so every literal is
sent as a bound parameter - there is no string interpolation of user input
anywhere in this layer.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models.identity import Employee, EmployeeEntitlement
from app.domain.enums import EmploymentStatus


class EmployeeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, employee_id: str) -> Employee | None:
        return self.session.get(Employee, employee_id)

    def list_all(self, limit: int = 500, offset: int = 0) -> list[Employee]:
        stmt = select(Employee).order_by(Employee.employee_id).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def list_joiners(self, limit: int = 200, offset: int = 0) -> list[Employee]:
        """Identities eligible for onboarding analysis."""
        stmt = (
            select(Employee)
            .where(Employee.employment_status == EmploymentStatus.PENDING_START.value)
            .order_by(Employee.employee_id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def count_by_status(self, status: EmploymentStatus) -> int:
        stmt = select(func.count()).select_from(Employee).where(
            Employee.employment_status == status.value
        )
        return int(self.session.scalar(stmt) or 0)

    def count_all(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(Employee)) or 0)

    # ------------------------------------------------------------------ #
    # Peer matching
    # ------------------------------------------------------------------ #
    def find_peers(
        self,
        *,
        exclude_employee_id: str,
        department: str,
        job_role: str | None = None,
        job_level: str | None = None,
    ) -> list[Employee]:
        """Find candidate peers.

        Only ACTIVE identities are ever considered: leavers and identities that
        have not started yet must not shape somebody else's access.
        """
        stmt: Select[tuple[Employee]] = select(Employee).where(
            Employee.employment_status == EmploymentStatus.ACTIVE.value,
            Employee.employee_id != exclude_employee_id,
            Employee.department == department,
        )
        if job_role is not None:
            stmt = stmt.where(Employee.job_role == job_role)
        if job_level is not None:
            stmt = stmt.where(Employee.job_level == job_level)
        return list(self.session.scalars(stmt.order_by(Employee.employee_id)))

    # ------------------------------------------------------------------ #
    # Access holdings
    # ------------------------------------------------------------------ #
    def get_entitlement_ids(self, employee_id: str) -> list[str]:
        stmt = (
            select(EmployeeEntitlement.entitlement_id)
            .where(EmployeeEntitlement.employee_id == employee_id)
            .order_by(EmployeeEntitlement.entitlement_id)
        )
        return list(self.session.scalars(stmt))

    def get_entitlements_for(self, employee_ids: list[str]) -> dict[str, list[str]]:
        """Map employee_id -> entitlement ids for a batch of identities."""
        if not employee_ids:
            return {}
        stmt = (
            select(EmployeeEntitlement.employee_id, EmployeeEntitlement.entitlement_id)
            .where(EmployeeEntitlement.employee_id.in_(employee_ids))
            .order_by(EmployeeEntitlement.employee_id, EmployeeEntitlement.entitlement_id)
        )
        holdings: dict[str, list[str]] = {eid: [] for eid in employee_ids}
        for emp_id, ent_id in self.session.execute(stmt):
            holdings.setdefault(emp_id, []).append(ent_id)
        return holdings

    def count_entitlements_for(self, employee_ids: list[str]) -> dict[str, int]:
        if not employee_ids:
            return {}
        stmt = (
            select(EmployeeEntitlement.employee_id, func.count())
            .where(EmployeeEntitlement.employee_id.in_(employee_ids))
            .group_by(EmployeeEntitlement.employee_id)
        )
        counts = {eid: 0 for eid in employee_ids}
        for emp_id, count in self.session.execute(stmt):
            counts[emp_id] = int(count)
        return counts
