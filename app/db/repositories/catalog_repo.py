"""Data access for the entitlement catalogue and the governance rule sets."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.governance import Policy, SodRule
from app.db.models.identity import Entitlement


class EntitlementRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entitlement_id: str) -> Entitlement | None:
        return self.session.get(Entitlement, entitlement_id)

    def get_many(self, entitlement_ids: list[str]) -> dict[str, Entitlement]:
        """Fetch a batch of entitlements keyed by id (missing ids are absent)."""
        if not entitlement_ids:
            return {}
        stmt = select(Entitlement).where(Entitlement.entitlement_id.in_(entitlement_ids))
        return {e.entitlement_id: e for e in self.session.scalars(stmt)}

    def list_all(self) -> list[Entitlement]:
        return list(self.session.scalars(select(Entitlement).order_by(Entitlement.entitlement_id)))

    def count_all(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(Entitlement)) or 0)


class PolicyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_enabled(self) -> list[Policy]:
        stmt = select(Policy).where(Policy.enabled.is_(True)).order_by(Policy.policy_id)
        return list(self.session.scalars(stmt))

    def list_all(self) -> list[Policy]:
        return list(self.session.scalars(select(Policy).order_by(Policy.policy_id)))

    def get(self, policy_id: str) -> Policy | None:
        return self.session.get(Policy, policy_id)


class SodRuleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_enabled(self) -> list[SodRule]:
        stmt = select(SodRule).where(SodRule.enabled.is_(True)).order_by(SodRule.sod_id)
        return list(self.session.scalars(stmt))

    def list_all(self) -> list[SodRule]:
        return list(self.session.scalars(select(SodRule).order_by(SodRule.sod_id)))

    def get(self, sod_id: str) -> SodRule | None:
        return self.session.get(SodRule, sod_id)
