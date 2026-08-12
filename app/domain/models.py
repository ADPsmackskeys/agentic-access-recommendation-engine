"""Domain models.

Framework-free Pydantic models that form the contract between the deterministic
governance services, the LangGraph workflow, the MCP tools and the REST layer.
Nothing here imports FastAPI, SQLAlchemy or LangGraph.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    AnalysisStatus,
    ApprovalTier,
    EmploymentStatus,
    EmploymentType,
    EvidenceType,
    ExplanationGenerator,
    MatchingStrategy,
    PolicyStatus,
    PolicyType,
    RecommendationStatus,
    RiskLevel,
    SodStatus,
    Severity,
)


class DomainModel(BaseModel):
    """Base model: strict-ish, serialisable, safe to send over MCP."""

    model_config = ConfigDict(from_attributes=True, use_enum_values=False, extra="forbid")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class EmployeeProfile(DomainModel):
    employee_id: str
    name: str
    department: str
    job_role: str
    job_level: str
    location: str
    manager_id: str | None = None
    cost_center: str | None = None
    start_date: date | None = None
    employment_status: EmploymentStatus
    employment_type: EmploymentType = EmploymentType.EMPLOYEE
    existing_entitlement_ids: list[str] = Field(default_factory=list)


class EmployeeSummary(DomainModel):
    employee_id: str
    name: str
    department: str
    job_role: str
    job_level: str
    location: str
    employment_status: EmploymentStatus
    employment_type: EmploymentType
    start_date: date | None = None


# --------------------------------------------------------------------------- #
# Entitlements
# --------------------------------------------------------------------------- #
class Entitlement(DomainModel):
    entitlement_id: str
    entitlement_name: str
    application: str
    description: str | None = None
    owner: str | None = None
    risk_score: int = Field(ge=0, le=100)
    risk_category: str | None = None


# --------------------------------------------------------------------------- #
# Peer analysis
# --------------------------------------------------------------------------- #
class PeerEmployee(DomainModel):
    employee_id: str
    name: str
    department: str
    job_role: str
    job_level: str
    location: str
    entitlement_count: int = 0


class PeerAnalysisResult(DomainModel):
    employee_id: str
    matching_strategy: MatchingStrategy
    strategies_attempted: list[MatchingStrategy] = Field(default_factory=list)
    peer_count: int = 0
    peer_ids: list[str] = Field(default_factory=list)
    peers: list[PeerEmployee] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sufficient: bool = False
    notes: str | None = None


# --------------------------------------------------------------------------- #
# Affinity
# --------------------------------------------------------------------------- #
class PeerEntitlementEvidence(DomainModel):
    peer_employee_id: str
    peer_name: str
    evidence_type: EvidenceType = EvidenceType.PEER_HOLDS_ENTITLEMENT
    evidence_value: str | None = None


class EntitlementAffinity(DomainModel):
    entitlement_id: str
    entitlement_name: str
    application: str
    peer_count: int  # peers holding this entitlement
    total_peers: int  # size of the matched peer group
    affinity_score: float = Field(ge=0.0, le=100.0)
    threshold: float
    meets_threshold: bool
    matching_strategy: MatchingStrategy
    already_held: bool = False
    evidence: list[PeerEntitlementEvidence] = Field(default_factory=list)


class AffinityAnalysisResult(DomainModel):
    employee_id: str
    threshold: float
    total_peers: int
    matching_strategy: MatchingStrategy
    candidates: list[EntitlementAffinity] = Field(default_factory=list)

    def above_threshold(self) -> list[EntitlementAffinity]:
        return [c for c in self.candidates if c.meets_threshold]


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
class RiskAssessment(DomainModel):
    entitlement_id: str
    risk_score: int = Field(ge=0, le=100)
    risk_level: RiskLevel
    risk_category: str | None = None
    required_approval_tier: ApprovalTier
    band_bounds: str
    reason: str


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class PolicyMatch(DomainModel):
    policy_id: str
    policy_name: str
    policy_type: PolicyType
    status: PolicyStatus
    required_approval_tier: ApprovalTier
    reason: str


class EntitlementPolicyResult(DomainModel):
    entitlement_id: str
    status: PolicyStatus
    approval_tier: ApprovalTier
    matched_policies: list[PolicyMatch] = Field(default_factory=list)
    failed_policies: list[PolicyMatch] = Field(default_factory=list)
    reason: str


class PolicyValidationResult(DomainModel):
    employee_id: str
    status: PolicyStatus  # worst status across all entitlements
    approval_tier: ApprovalTier  # highest tier demanded by any policy
    results: list[EntitlementPolicyResult] = Field(default_factory=list)
    evaluated_policy_ids: list[str] = Field(default_factory=list)
    skipped_policy_ids: list[str] = Field(default_factory=list)

    def by_entitlement(self, entitlement_id: str) -> EntitlementPolicyResult | None:
        return next((r for r in self.results if r.entitlement_id == entitlement_id), None)


# --------------------------------------------------------------------------- #
# Segregation of Duties
# --------------------------------------------------------------------------- #
class SodConflict(DomainModel):
    sod_id: str
    name: str
    entitlement_1: str
    entitlement_2: str
    severity: Severity
    reason: str
    conflicts_with_existing_access: bool = False


class SodValidationResult(DomainModel):
    employee_id: str
    status: SodStatus
    severity: Severity | None = None
    conflicts: list[SodConflict] = Field(default_factory=list)
    evaluated_entitlement_ids: list[str] = Field(default_factory=list)
    evaluated_rule_ids: list[str] = Field(default_factory=list)

    def conflicts_for(self, entitlement_id: str) -> list[SodConflict]:
        return [
            c
            for c in self.conflicts
            if entitlement_id in (c.entitlement_1, c.entitlement_2)
        ]


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
class DecisionTraceEntry(DomainModel):
    rule: str
    outcome: str
    detail: str


class AccessDecision(DomainModel):
    """The authoritative, deterministic outcome for one candidate entitlement."""

    entitlement_id: str
    entitlement_name: str
    application: str

    affinity_score: float
    peer_count: int
    total_peers: int
    affinity_threshold: float
    matching_strategy: MatchingStrategy

    risk_score: int
    risk_level: RiskLevel

    policy_status: PolicyStatus
    sod_status: SodStatus
    sod_severity: Severity | None = None

    recommendation_status: RecommendationStatus
    approval_tier: ApprovalTier
    reason: str
    decision_trace: list[DecisionTraceEntry] = Field(default_factory=list)

    policy_result: EntitlementPolicyResult | None = None
    sod_conflicts: list[SodConflict] = Field(default_factory=list)
    evidence: list[PeerEntitlementEvidence] = Field(default_factory=list)

    @property
    def is_requestable(self) -> bool:
        return self.recommendation_status in (
            RecommendationStatus.AUTO_APPROVED,
            RecommendationStatus.MANAGER_APPROVAL,
        )


# --------------------------------------------------------------------------- #
# Explanation
# --------------------------------------------------------------------------- #
class StructuredExplanation(DomainModel):
    """Machine-readable evidence bundle. The only input an LLM ever sees."""

    recommendation: str  # entitlement id
    entitlement_name: str
    application: str
    why_recommended: str
    peer_evidence: list[str] = Field(default_factory=list)
    peer_summary: str
    affinity: float
    risk: int
    risk_level: RiskLevel
    policy_results: list[str] = Field(default_factory=list)
    sod_results: list[str] = Field(default_factory=list)
    final_decision: RecommendationStatus
    approval_tier: ApprovalTier


class RecommendationExplanation(DomainModel):
    entitlement_id: str
    structured: StructuredExplanation
    narrative: str
    generator: ExplanationGenerator
    model: str | None = None
    error: str | None = None


class AnalysisExplanation(DomainModel):
    employee_id: str
    summary: str
    generator: ExplanationGenerator
    model: str | None = None
    recommendations: list[RecommendationExplanation] = Field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------- #
# SailPoint (simulated)
# --------------------------------------------------------------------------- #
class SailPointRequestedEntitlement(DomainModel):
    application: str
    entitlement: str
    entitlement_name: str
    operation: str = "Add"
    approval_tier: ApprovalTier
    risk_level: RiskLevel
    affinity_score: float


class SailPointRequestPayload(DomainModel):
    identity: str
    request_type: str
    requested_entitlements: list[SailPointRequestedEntitlement] = Field(default_factory=list)
    justification: str
    source: str
    status: str
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    excluded_entitlements: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Analysis aggregate
# --------------------------------------------------------------------------- #
class AnalysisResult(DomainModel):
    analysis_id: str
    correlation_id: str
    employee_id: str
    status: AnalysisStatus
    started_at: datetime
    completed_at: datetime | None = None

    employee: EmployeeProfile | None = None
    peer_analysis: PeerAnalysisResult | None = None
    affinity: AffinityAnalysisResult | None = None
    risk_results: list[RiskAssessment] = Field(default_factory=list)
    policy_validation: PolicyValidationResult | None = None
    sod_validation: SodValidationResult | None = None
    decisions: list[AccessDecision] = Field(default_factory=list)
    explanation: AnalysisExplanation | None = None
    sailpoint_payload: SailPointRequestPayload | None = None
    errors: list[str] = Field(default_factory=list)


class DashboardMetrics(DomainModel):
    total_joiners: int
    total_employees: int
    total_analyses: int
    total_recommendations: int
    auto_approved: int
    manager_approval: int
    human_review: int
    blocked: int
    rejected: int
    not_recommended: int
    high_risk: int
    critical_risk: int
    sailpoint_requests: int
