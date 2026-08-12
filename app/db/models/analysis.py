"""Analysis, recommendation and audit-trail tables.

Everything the governance engine decided is persisted here so that a decision
can be reconstructed and defended months later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import JSONB, Base, StrUUID, created_at_col, uuid_pk


class JoinerAnalysis(Base):
    """One run of the onboarding analysis workflow."""

    __tablename__ = "joiner_analyses"

    analysis_id: Mapped[str] = uuid_pk()
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # Peer-analysis provenance: which strategy produced the evidence base.
    # `peer_ids` and `strategies_attempted` are ordered scalar lists that are
    # only ever read back as a whole, which is what JSONB is good at; the
    # queryable facts (strategy, count, confidence) stay as real columns.
    matching_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategies_attempted: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    peer_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    peer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peer_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    affinity_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    errors: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan", lazy="selectin"
    )
    sailpoint_requests: Mapped[list["SailPointRequest"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_joiner_analyses_employee_id", "employee_id"),
        Index("ix_joiner_analyses_status", "status"),
    )


class Recommendation(Base):
    """A single candidate entitlement and its final governance verdict."""

    __tablename__ = "recommendations"

    recommendation_id: Mapped[str] = uuid_pk()
    # NOTE: the original specification wrote this column as `analysis_idx`;
    # it is a foreign key to joiner_analyses.analysis_id, so it is named
    # `analysis_id` here.
    analysis_id: Mapped[str] = mapped_column(
        StrUUID, ForeignKey("joiner_analyses.analysis_id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False
    )
    entitlement_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("entitlements.entitlement_id", ondelete="CASCADE"), nullable=False
    )

    affinity_score: Mapped[float] = mapped_column(Float, nullable=False)
    peer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_peers: Mapped[int] = mapped_column(Integer, nullable=False)
    affinity_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    matching_strategy: Mapped[str] = mapped_column(String(64), nullable=False)

    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)

    policy_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sod_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sod_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)

    recommendation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_trace: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = created_at_col()

    analysis: Mapped[JoinerAnalysis] = relationship(back_populates="recommendations")
    evidence: Mapped[list["RecommendationEvidence"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", lazy="selectin"
    )
    policy_results: Mapped[list["PolicyResult"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", lazy="selectin"
    )
    sod_results: Mapped[list["SodResult"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", lazy="selectin"
    )
    explanation: Mapped["RecommendationExplanationRow | None"] = relationship(
        back_populates="recommendation",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("analysis_id", "entitlement_id", name="uq_recommendation_per_analysis"),
        Index("ix_recommendations_analysis_id", "analysis_id"),
        Index("ix_recommendations_status", "recommendation_status"),
    )


class RecommendationEvidence(Base):
    """Which peer contributed which piece of evidence."""

    __tablename__ = "recommendation_evidence"

    evidence_id: Mapped[str] = uuid_pk()
    recommendation_id: Mapped[str] = mapped_column(
        StrUUID,
        ForeignKey("recommendations.recommendation_id", ondelete="CASCADE"),
        nullable=False,
    )
    peer_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entitlement_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    recommendation: Mapped[Recommendation] = relationship(back_populates="evidence")

    __table_args__ = (Index("ix_recommendation_evidence_rec_id", "recommendation_id"),)


class PolicyResult(Base):
    """Outcome of one policy against one recommendation."""

    __tablename__ = "policy_results"

    result_id: Mapped[str] = uuid_pk()
    recommendation_id: Mapped[str] = mapped_column(
        StrUUID,
        ForeignKey("recommendations.recommendation_id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    recommendation: Mapped[Recommendation] = relationship(back_populates="policy_results")

    __table_args__ = (Index("ix_policy_results_rec_id", "recommendation_id"),)


class SodResult(Base):
    """Outcome of one SoD rule against one recommendation."""

    __tablename__ = "sod_results"

    result_id: Mapped[str] = uuid_pk()
    recommendation_id: Mapped[str] = mapped_column(
        StrUUID,
        ForeignKey("recommendations.recommendation_id", ondelete="CASCADE"),
        nullable=False,
    )
    sod_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conflicting_entitlement_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    recommendation: Mapped[Recommendation] = relationship(back_populates="sod_results")

    __table_args__ = (Index("ix_sod_results_rec_id", "recommendation_id"),)


class RecommendationExplanationRow(Base):
    """Both halves of explainability: the structured evidence and the prose.

    Persisting the structured form alongside the narrative is what makes the
    narrative auditable - you can always check the prose against the facts it
    was generated from.
    """

    __tablename__ = "recommendation_explanations"

    explanation_id: Mapped[str] = uuid_pk()
    recommendation_id: Mapped[str] = mapped_column(
        StrUUID,
        ForeignKey("recommendations.recommendation_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    structured_explanation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    generator: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    recommendation: Mapped[Recommendation] = relationship(back_populates="explanation")


class SailPointRequest(Base):
    """A generated (simulated) SailPoint IdentityIQ access request."""

    __tablename__ = "sailpoint_requests"

    request_id: Mapped[str] = uuid_pk()
    analysis_id: Mapped[str] = mapped_column(
        StrUUID, ForeignKey("joiner_analyses.analysis_id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    entitlement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()

    analysis: Mapped[JoinerAnalysis] = relationship(back_populates="sailpoint_requests")

    __table_args__ = (
        Index("ix_sailpoint_requests_analysis_id", "analysis_id"),
        Index("ix_sailpoint_requests_employee_id", "employee_id"),
    )
