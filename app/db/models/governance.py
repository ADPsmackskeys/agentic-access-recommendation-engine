"""Policy and Segregation-of-Duties rule tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONB, Base, created_at_col


class Policy(Base):
    """A governance policy.

    `rule_definition` is JSONB holding *parameters only*. The behaviour is
    selected by `policy_type` and implemented by a hand-written evaluator; the
    JSON is never executed or eval'd.
    """

    __tablename__ = "policies"

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("ix_policies_enabled", "enabled"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Policy {self.policy_id} {self.policy_type} enabled={self.enabled}>"


class SodRule(Base):
    """A toxic-combination rule between two entitlements."""

    __tablename__ = "sod_rules"

    sod_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    entitlement_1: Mapped[str] = mapped_column(
        String(64), ForeignKey("entitlements.entitlement_id", ondelete="CASCADE"), nullable=False
    )
    entitlement_2: Mapped[str] = mapped_column(
        String(64), ForeignKey("entitlements.entitlement_id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        Index("ix_sod_rules_enabled", "enabled"),
        Index("ix_sod_rules_entitlement_1", "entitlement_1"),
        Index("ix_sod_rules_entitlement_2", "entitlement_2"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SodRule {self.sod_id} {self.entitlement_1}+{self.entitlement_2}>"
