"""Persistence and reconstruction of the analysis audit trail.

An analysis is written once, at the end of the workflow, in a single
transaction: either the full decision record lands or none of it does. There is
no partially-persisted analysis for an auditor to misread.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.analysis import (
    JoinerAnalysis,
    PolicyResult,
    Recommendation,
    RecommendationEvidence,
    RecommendationExplanationRow,
    SailPointRequest,
    SodResult,
)
from app.db.models.identity import Employee
from app.domain.enums import (
    AnalysisStatus,
    ApprovalTier,
    EmploymentStatus,
    EvidenceType,
    MatchingStrategy,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    Severity,
    SodStatus,
)
from app.domain.models import (
    AccessDecision,
    AffinityAnalysisResult,
    AnalysisExplanation,
    AnalysisResult,
    DashboardMetrics,
    DecisionTraceEntry,
    EntitlementAffinity,
    EntitlementPolicyResult,
    PeerAnalysisResult,
    PeerEmployee,
    PeerEntitlementEvidence,
    PolicyMatch,
    PolicyValidationResult,
    RecommendationExplanation,
    RiskAssessment,
    SailPointRequestPayload,
    SodConflict,
    SodValidationResult,
    StructuredExplanation,
)


class AnalysisRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def persist(self, result: AnalysisResult) -> str:
        """Write a completed analysis and its full evidence trail.

        The caller owns the transaction (see `session_scope`), so this method
        flushes but never commits.
        """
        analysis = JoinerAnalysis(
            analysis_id=result.analysis_id,
            employee_id=result.employee_id,
            correlation_id=result.correlation_id,
            status=result.status.value,
            matching_strategy=(
                result.peer_analysis.matching_strategy.value if result.peer_analysis else None
            ),
            strategies_attempted=(
                [s.value for s in result.peer_analysis.strategies_attempted]
                if result.peer_analysis
                else None
            ),
            peer_ids=result.peer_analysis.peer_ids if result.peer_analysis else None,
            peer_count=result.peer_analysis.peer_count if result.peer_analysis else 0,
            peer_confidence=result.peer_analysis.confidence if result.peer_analysis else None,
            affinity_threshold=result.affinity.threshold if result.affinity else None,
            candidate_count=len(result.decisions),
            errors=result.errors or None,
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
        self.session.add(analysis)
        self.session.flush()

        explanations_by_entitlement = {
            e.entitlement_id: e
            for e in (result.explanation.recommendations if result.explanation else [])
        }

        for decision in result.decisions:
            rec = Recommendation(
                analysis_id=analysis.analysis_id,
                employee_id=result.employee_id,
                entitlement_id=decision.entitlement_id,
                affinity_score=decision.affinity_score,
                peer_count=decision.peer_count,
                total_peers=decision.total_peers,
                affinity_threshold=decision.affinity_threshold,
                matching_strategy=decision.matching_strategy.value,
                risk_score=decision.risk_score,
                risk_level=decision.risk_level.value,
                policy_status=decision.policy_status.value,
                sod_status=decision.sod_status.value,
                sod_severity=decision.sod_severity.value if decision.sod_severity else None,
                recommendation_status=decision.recommendation_status.value,
                approval_tier=decision.approval_tier.value,
                reason=decision.reason,
                decision_trace=[t.model_dump() for t in decision.decision_trace],
            )
            self.session.add(rec)
            self.session.flush()

            for ev in decision.evidence:
                self.session.add(
                    RecommendationEvidence(
                        recommendation_id=rec.recommendation_id,
                        peer_employee_id=ev.peer_employee_id,
                        entitlement_id=decision.entitlement_id,
                        evidence_type=ev.evidence_type.value,
                        evidence_value=ev.evidence_value,
                    )
                )

            if decision.policy_result is not None:
                seen: set[str] = set()
                for match in (
                    decision.policy_result.matched_policies
                    + decision.policy_result.failed_policies
                ):
                    if match.policy_id in seen:
                        continue
                    seen.add(match.policy_id)
                    self.session.add(
                        PolicyResult(
                            recommendation_id=rec.recommendation_id,
                            policy_id=match.policy_id,
                            policy_name=match.policy_name,
                            status=match.status.value,
                            approval_tier=match.required_approval_tier.value,
                            reason=match.reason,
                        )
                    )

            for conflict in decision.sod_conflicts:
                other = (
                    conflict.entitlement_2
                    if conflict.entitlement_1 == decision.entitlement_id
                    else conflict.entitlement_1
                )
                self.session.add(
                    SodResult(
                        recommendation_id=rec.recommendation_id,
                        sod_id=conflict.sod_id,
                        status=SodStatus.CONFLICT.value,
                        severity=conflict.severity.value,
                        conflicting_entitlement_id=other,
                        reason=conflict.reason,
                    )
                )

            explanation = explanations_by_entitlement.get(decision.entitlement_id)
            if explanation is not None:
                self.session.add(
                    RecommendationExplanationRow(
                        recommendation_id=rec.recommendation_id,
                        structured_explanation=explanation.structured.model_dump(mode="json"),
                        narrative=explanation.narrative,
                        generator=explanation.generator.value,
                        model=explanation.model,
                        error=explanation.error,
                    )
                )

        if result.sailpoint_payload is not None:
            self.session.add(
                SailPointRequest(
                    analysis_id=analysis.analysis_id,
                    employee_id=result.employee_id,
                    payload=result.sailpoint_payload.model_dump(mode="json"),
                    status=result.sailpoint_payload.status,
                    entitlement_count=len(result.sailpoint_payload.requested_entitlements),
                )
            )

        self.session.flush()
        return analysis.analysis_id

    def add_sailpoint_request(
        self, *, analysis_id: str, employee_id: str, payload: SailPointRequestPayload
    ) -> str:
        row = SailPointRequest(
            analysis_id=analysis_id,
            employee_id=employee_id,
            payload=payload.model_dump(mode="json"),
            status=payload.status,
            entitlement_count=len(payload.requested_entitlements),
        )
        self.session.add(row)
        self.session.flush()
        return row.request_id

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def get_row(self, analysis_id: str) -> JoinerAnalysis | None:
        return self.session.get(JoinerAnalysis, analysis_id)

    def list_rows(self, limit: int = 50, offset: int = 0) -> list[JoinerAnalysis]:
        stmt = (
            select(JoinerAnalysis)
            .order_by(JoinerAnalysis.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt))

    def latest_for_employee(self, employee_id: str) -> JoinerAnalysis | None:
        stmt = (
            select(JoinerAnalysis)
            .where(JoinerAnalysis.employee_id == employee_id)
            .order_by(JoinerAnalysis.started_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def get(self, analysis_id: str) -> AnalysisResult | None:
        """Rebuild the full domain result from the persisted audit trail."""
        row = self.get_row(analysis_id)
        if row is None:
            return None
        return self._to_domain(row)

    def _to_domain(self, row: JoinerAnalysis) -> AnalysisResult:
        from app.services.mappers import employee_to_profile  # local import: avoids a cycle

        employee = self.session.get(Employee, row.employee_id)
        strategy = MatchingStrategy(row.matching_strategy) if row.matching_strategy else (
            MatchingStrategy.NONE
        )

        peer_ids = row.peer_ids or []
        peers: list[PeerEmployee] = []
        if peer_ids:
            peer_rows = self.session.scalars(
                select(Employee).where(Employee.employee_id.in_(peer_ids))
            )
            peers = [
                PeerEmployee(
                    employee_id=p.employee_id,
                    name=p.name,
                    department=p.department,
                    job_role=p.job_role,
                    job_level=p.job_level,
                    location=p.location,
                )
                for p in sorted(peer_rows, key=lambda p: p.employee_id)
            ]

        peer_analysis = PeerAnalysisResult(
            employee_id=row.employee_id,
            matching_strategy=strategy,
            strategies_attempted=[
                MatchingStrategy(s) for s in (row.strategies_attempted or [])
            ],
            peer_count=row.peer_count,
            peer_ids=peer_ids,
            peers=peers,
            confidence=row.peer_confidence or 0.0,
            sufficient=row.peer_count > 0,
        )

        decisions: list[AccessDecision] = []
        risk_results: list[RiskAssessment] = []
        policy_results: list[EntitlementPolicyResult] = []
        sod_conflicts: list[SodConflict] = []
        affinity_candidates: list[EntitlementAffinity] = []
        explanations: list[RecommendationExplanation] = []

        for rec in sorted(
            row.recommendations, key=lambda r: (-r.affinity_score, r.entitlement_id)
        ):
            matched = [
                PolicyMatch(
                    policy_id=pr.policy_id,
                    policy_name=pr.policy_name or pr.policy_id,
                    policy_type=_safe_policy_type(pr),
                    status=PolicyStatus(pr.status),
                    required_approval_tier=ApprovalTier(pr.approval_tier or ApprovalTier.AUTO),
                    reason=pr.reason or "",
                )
                for pr in rec.policy_results
            ]
            failed = [m for m in matched if m.status != PolicyStatus.PASS]
            policy_result = EntitlementPolicyResult(
                entitlement_id=rec.entitlement_id,
                status=PolicyStatus(rec.policy_status),
                approval_tier=ApprovalTier(rec.approval_tier),
                matched_policies=matched,
                failed_policies=failed,
                reason=rec.reason,
            )
            policy_results.append(policy_result)

            rec_conflicts = [
                SodConflict(
                    sod_id=sr.sod_id,
                    name=sr.sod_id,
                    entitlement_1=rec.entitlement_id,
                    entitlement_2=sr.conflicting_entitlement_id or "",
                    severity=Severity(sr.severity or Severity.MEDIUM),
                    reason=sr.reason or "",
                )
                for sr in rec.sod_results
            ]
            sod_conflicts.extend(rec_conflicts)

            evidence = [
                PeerEntitlementEvidence(
                    peer_employee_id=ev.peer_employee_id or "",
                    peer_name=ev.evidence_value or "",
                    evidence_type=EvidenceType(ev.evidence_type),
                    evidence_value=ev.evidence_value,
                )
                for ev in sorted(rec.evidence, key=lambda e: e.peer_employee_id or "")
            ]

            risk_results.append(
                RiskAssessment(
                    entitlement_id=rec.entitlement_id,
                    risk_score=rec.risk_score,
                    risk_level=RiskLevel(rec.risk_level),
                    required_approval_tier=ApprovalTier(rec.approval_tier),
                    band_bounds="persisted",
                    reason=rec.reason,
                )
            )

            affinity_candidates.append(
                EntitlementAffinity(
                    entitlement_id=rec.entitlement_id,
                    entitlement_name=rec.entitlement_id,
                    application="",
                    peer_count=rec.peer_count,
                    total_peers=rec.total_peers,
                    affinity_score=rec.affinity_score,
                    threshold=rec.affinity_threshold,
                    meets_threshold=rec.affinity_score >= rec.affinity_threshold,
                    matching_strategy=MatchingStrategy(rec.matching_strategy),
                    evidence=evidence,
                )
            )

            decisions.append(
                AccessDecision(
                    entitlement_id=rec.entitlement_id,
                    entitlement_name=rec.entitlement_id,
                    application="",
                    affinity_score=rec.affinity_score,
                    peer_count=rec.peer_count,
                    total_peers=rec.total_peers,
                    affinity_threshold=rec.affinity_threshold,
                    matching_strategy=MatchingStrategy(rec.matching_strategy),
                    risk_score=rec.risk_score,
                    risk_level=RiskLevel(rec.risk_level),
                    policy_status=PolicyStatus(rec.policy_status),
                    sod_status=SodStatus(rec.sod_status),
                    sod_severity=Severity(rec.sod_severity) if rec.sod_severity else None,
                    recommendation_status=RecommendationStatus(rec.recommendation_status),
                    approval_tier=ApprovalTier(rec.approval_tier),
                    reason=rec.reason,
                    decision_trace=[
                        DecisionTraceEntry(**t) for t in (rec.decision_trace or [])
                    ],
                    policy_result=policy_result,
                    sod_conflicts=rec_conflicts,
                    evidence=evidence,
                )
            )

            if rec.explanation is not None:
                explanations.append(
                    RecommendationExplanation(
                        entitlement_id=rec.entitlement_id,
                        structured=StructuredExplanation(**rec.explanation.structured_explanation),
                        narrative=rec.explanation.narrative,
                        generator=rec.explanation.generator,  # type: ignore[arg-type]
                        model=rec.explanation.model,
                        error=rec.explanation.error,
                    )
                )

        sailpoint_payload = None
        if row.sailpoint_requests:
            latest = sorted(row.sailpoint_requests, key=lambda r: r.created_at)[-1]
            sailpoint_payload = SailPointRequestPayload(**latest.payload)

        explanation = None
        if explanations:
            first = explanations[0]
            explanation = AnalysisExplanation(
                employee_id=row.employee_id,
                summary=(
                    f"{len(explanations)} explained recommendation(s) for {row.employee_id}."
                ),
                generator=first.generator,
                model=first.model,
                recommendations=explanations,
            )

        return AnalysisResult(
            analysis_id=row.analysis_id,
            correlation_id=row.correlation_id or "",
            employee_id=row.employee_id,
            status=AnalysisStatus(row.status),
            started_at=row.started_at,
            completed_at=row.completed_at,
            employee=employee_to_profile(employee, []) if employee else None,
            peer_analysis=peer_analysis,
            affinity=AffinityAnalysisResult(
                employee_id=row.employee_id,
                threshold=row.affinity_threshold or 0.0,
                total_peers=row.peer_count,
                matching_strategy=strategy,
                candidates=affinity_candidates,
            ),
            risk_results=risk_results,
            policy_validation=PolicyValidationResult(
                employee_id=row.employee_id,
                status=_worst_policy_status([p.status for p in policy_results]),
                approval_tier=ApprovalTier.AUTO,
                results=policy_results,
            ),
            sod_validation=SodValidationResult(
                employee_id=row.employee_id,
                status=SodStatus.CONFLICT if sod_conflicts else SodStatus.PASS,
                severity=(
                    max((c.severity for c in sod_conflicts), key=_severity_rank)
                    if sod_conflicts
                    else None
                ),
                conflicts=sod_conflicts,
            ),
            decisions=decisions,
            explanation=explanation,
            sailpoint_payload=sailpoint_payload,
            errors=row.errors or [],
        )

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    def dashboard_metrics(self) -> DashboardMetrics:
        def count_status(status: RecommendationStatus) -> int:
            stmt = (
                select(func.count())
                .select_from(Recommendation)
                .where(Recommendation.recommendation_status == status.value)
            )
            return int(self.session.scalar(stmt) or 0)

        def count_risk(level: RiskLevel) -> int:
            stmt = (
                select(func.count())
                .select_from(Recommendation)
                .where(Recommendation.risk_level == level.value)
            )
            return int(self.session.scalar(stmt) or 0)

        total_joiners = int(
            self.session.scalar(
                select(func.count())
                .select_from(Employee)
                .where(Employee.employment_status == EmploymentStatus.PENDING_START.value)
            )
            or 0
        )
        return DashboardMetrics(
            total_joiners=total_joiners,
            total_employees=int(
                self.session.scalar(select(func.count()).select_from(Employee)) or 0
            ),
            total_analyses=int(
                self.session.scalar(select(func.count()).select_from(JoinerAnalysis)) or 0
            ),
            total_recommendations=int(
                self.session.scalar(select(func.count()).select_from(Recommendation)) or 0
            ),
            auto_approved=count_status(RecommendationStatus.AUTO_APPROVED),
            manager_approval=count_status(RecommendationStatus.MANAGER_APPROVAL),
            human_review=count_status(RecommendationStatus.HUMAN_REVIEW),
            blocked=count_status(RecommendationStatus.BLOCKED),
            rejected=count_status(RecommendationStatus.REJECTED),
            not_recommended=count_status(RecommendationStatus.NOT_RECOMMENDED),
            high_risk=count_risk(RiskLevel.HIGH),
            critical_risk=count_risk(RiskLevel.CRITICAL),
            sailpoint_requests=int(
                self.session.scalar(select(func.count()).select_from(SailPointRequest)) or 0
            ),
        )


def _severity_rank(severity: Severity) -> int:
    return {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[severity.value]


def _worst_policy_status(statuses: list[PolicyStatus]) -> PolicyStatus:
    order = [
        PolicyStatus.DENY,
        PolicyStatus.BLOCK,
        PolicyStatus.ERROR,
        PolicyStatus.REQUIRES_APPROVAL,
        PolicyStatus.PASS,
        PolicyStatus.NOT_EVALUATED,
    ]
    for candidate in order:
        if candidate in statuses:
            return candidate
    return PolicyStatus.PASS


def _safe_policy_type(row: PolicyResult):
    """Policy type is not stored on the result row; look it up, defaulting safely."""
    from app.db.models.governance import Policy
    from app.domain.enums import PolicyType

    session = Session.object_session(row)
    if session is not None:
        policy = session.get(Policy, row.policy_id)
        if policy is not None:
            try:
                return PolicyType(policy.policy_type)
            except ValueError:
                pass
    return PolicyType.RISK_THRESHOLD_APPROVAL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
