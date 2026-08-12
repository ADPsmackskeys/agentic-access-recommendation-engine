"""Policy validation (Phase 8).

Runs every enabled policy against every requested entitlement and aggregates
the outcomes. Disabled policies are never loaded, let alone evaluated.

Failure behaviour is deliberate: a policy that cannot be evaluated produces
ERROR rather than PASS, and ERROR forces human review downstream. A broken
control must never look like a satisfied control.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.catalog_repo import EntitlementRepository, PolicyRepository
from app.db.repositories.employee_repo import EmployeeRepository
from app.domain.enums import ApprovalTier, PolicyStatus, PolicyType
from app.domain.exceptions import EmployeeNotFoundError, InvalidPolicyDefinitionError
from app.domain.models import (
    EmployeeProfile,
    EntitlementPolicyResult,
    PolicyMatch,
    PolicyValidationResult,
)
from app.domain.rules.policy_rules import PolicyContext, get_evaluator
from app.logging import get_logger
from app.services.mappers import employee_to_profile

logger = get_logger(__name__)

# Worst-first. The first status present in a set wins the aggregate.
_STATUS_PRECEDENCE: tuple[PolicyStatus, ...] = (
    PolicyStatus.DENY,
    PolicyStatus.BLOCK,
    PolicyStatus.ERROR,
    PolicyStatus.REQUIRES_APPROVAL,
    PolicyStatus.PASS,
    PolicyStatus.NOT_EVALUATED,
)

_TIER_PRECEDENCE: tuple[ApprovalTier, ...] = (
    ApprovalTier.HUMAN_REVIEW,
    ApprovalTier.MANAGER,
    ApprovalTier.AUTO,
    ApprovalTier.NONE,
)


def worst_status(statuses: list[PolicyStatus]) -> PolicyStatus:
    for candidate in _STATUS_PRECEDENCE:
        if candidate in statuses:
            return candidate
    return PolicyStatus.PASS


def highest_tier(tiers: list[ApprovalTier]) -> ApprovalTier:
    for candidate in _TIER_PRECEDENCE:
        if candidate in tiers:
            return candidate
    return ApprovalTier.AUTO


class PolicyService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.policies = PolicyRepository(session)
        self.entitlements = EntitlementRepository(session)
        self.employees = EmployeeRepository(session)

    def validate(
        self,
        employee_id: str,
        entitlement_ids: list[str],
        *,
        employee: EmployeeProfile | None = None,
    ) -> PolicyValidationResult:
        profile = employee or self._load_profile(employee_id)

        if not entitlement_ids:
            return PolicyValidationResult(
                employee_id=employee_id,
                status=PolicyStatus.PASS,
                approval_tier=ApprovalTier.AUTO,
                results=[],
            )

        catalog = self.entitlements.get_many(entitlement_ids)
        context = PolicyContext(
            employee=profile,
            requested_entitlement_ids=list(entitlement_ids),
            existing_entitlement_ids=list(profile.existing_entitlement_ids),
            entitlement_risk={
                eid: ent.risk_score for eid, ent in catalog.items()
            },
        )

        all_policies = self.policies.list_all()
        enabled = [p for p in all_policies if p.enabled]
        skipped = [p.policy_id for p in all_policies if not p.enabled]
        if skipped:
            logger.info("policy.skipped_disabled", policy_ids=skipped)

        results: list[EntitlementPolicyResult] = []
        for entitlement_id in entitlement_ids:
            results.append(self._validate_one(entitlement_id, enabled, context))

        aggregate_status = worst_status([r.status for r in results])
        aggregate_tier = highest_tier([r.approval_tier for r in results])

        logger.info(
            "policy.validated",
            employee_id=employee_id,
            entitlements=len(entitlement_ids),
            policies_evaluated=len(enabled),
            status=aggregate_status.value,
        )
        return PolicyValidationResult(
            employee_id=employee_id,
            status=aggregate_status,
            approval_tier=aggregate_tier,
            results=results,
            evaluated_policy_ids=[p.policy_id for p in enabled],
            skipped_policy_ids=skipped,
        )

    # ------------------------------------------------------------------ #
    def _validate_one(
        self, entitlement_id: str, policies, context: PolicyContext
    ) -> EntitlementPolicyResult:
        matched: list[PolicyMatch] = []
        failed: list[PolicyMatch] = []

        for policy in policies:
            try:
                evaluator = get_evaluator(policy.policy_type)
                params = evaluator.parse(policy.rule_definition)
                outcome = evaluator.evaluate(entitlement_id, params, context)
            except InvalidPolicyDefinitionError as exc:
                # Fail closed: an uninterpretable control becomes an ERROR that
                # forces a human to look, never a silent pass.
                logger.error(
                    "policy.invalid_definition",
                    policy_id=policy.policy_id,
                    error=exc.message,
                )
                match = PolicyMatch(
                    policy_id=policy.policy_id,
                    policy_name=policy.policy_name,
                    policy_type=_coerce_type(policy.policy_type),
                    status=PolicyStatus.ERROR,
                    required_approval_tier=ApprovalTier.HUMAN_REVIEW,
                    reason=(
                        f"Policy could not be evaluated and has been failed closed: {exc.message}"
                    ),
                )
                matched.append(match)
                failed.append(match)
                continue

            if outcome is None:
                continue

            match = PolicyMatch(
                policy_id=policy.policy_id,
                policy_name=policy.policy_name,
                policy_type=_coerce_type(policy.policy_type),
                status=outcome.status,
                required_approval_tier=outcome.approval_tier,
                reason=outcome.reason,
            )
            matched.append(match)
            if outcome.status is not PolicyStatus.PASS:
                failed.append(match)

        status = worst_status([m.status for m in matched]) if matched else PolicyStatus.PASS
        tier = (
            highest_tier([m.required_approval_tier for m in failed])
            if failed
            else ApprovalTier.AUTO
        )

        if status is PolicyStatus.PASS:
            reason = (
                f"All {len(policies)} enabled policies passed for {entitlement_id}."
                if policies
                else f"No enabled policies applied to {entitlement_id}."
            )
        else:
            reason = " ".join(m.reason for m in failed)

        return EntitlementPolicyResult(
            entitlement_id=entitlement_id,
            status=status,
            approval_tier=tier,
            matched_policies=matched,
            failed_policies=failed,
            reason=reason,
        )

    def _load_profile(self, employee_id: str) -> EmployeeProfile:
        row = self.employees.get(employee_id)
        if row is None:
            raise EmployeeNotFoundError(
                f"Employee '{employee_id}' does not exist.",
                details={"employee_id": employee_id},
            )
        return employee_to_profile(row, self.employees.get_entitlement_ids(employee_id))


def _coerce_type(value: str) -> PolicyType:
    try:
        return PolicyType(value)
    except ValueError:
        return PolicyType.RISK_THRESHOLD_APPROVAL
