"""Identity and entitlement tables."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_col, updated_at_col


class Employee(Base):
    __tablename__ = "employees"

    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    job_role: Mapped[str] = mapped_column(String(128), nullable=False)
    job_level: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False)
    manager_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("employees.employee_id", ondelete="SET NULL"), nullable=True
    )
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    employment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Not in the original table sketch, but required by the contractor policy:
    # employment *status* is a lifecycle state, employment *type* is contract form.
    employment_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="EMPLOYEE"
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    entitlements: Mapped[list["EmployeeEntitlement"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # Peer matching filters on these columns in every strategy.
        Index("ix_employees_peer_match", "department", "job_role", "job_level"),
        Index("ix_employees_employment_status", "employment_status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Employee {self.employee_id} {self.job_role}/{self.department}>"


class Entitlement(Base):
    __tablename__ = "entitlements"

    entitlement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entitlement_name: Mapped[str] = mapped_column(String(200), nullable=False)
    application: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_entitlements_application", "application"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Entitlement {self.entitlement_id} risk={self.risk_score}>"


class EmployeeEntitlement(Base):
    """Current access holdings - the raw material for peer affinity."""

    __tablename__ = "employee_entitlements"

    employee_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("employees.employee_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entitlement_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("entitlements.entitlement_id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, server_default="SEED")

    employee: Mapped[Employee] = relationship(back_populates="entitlements")
    entitlement: Mapped[Entitlement] = relationship(lazy="joined")

    __table_args__ = (Index("ix_employee_entitlements_entitlement_id", "entitlement_id"),)
