"""Policy rule evaluators (Phase 8).

Every policy type is a hand-written evaluator with a Pydantic parameter model.
`rule_definition` JSON supplies *parameters only* - it is never compiled,
`eval`-ed or otherwise executed. A policy row whose type is unknown, or whose
parameters fail validation, does not silently pass: it raises, and the policy
service converts that into an ERROR status that forces human review.

Adding a new policy type means writing an evaluator and registering it. That is
intentional friction: a governance control should be reviewable code, not a
string in a database.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import ApprovalTier, EmploymentType, PolicyStatus, PolicyType
from app.domain.exceptions import InvalidPolicyDefinitionError
from app.domain.models import EmployeeProfile

_JOB_LEVEL_RE = re.compile(r"^[Ll](\d+)$")


def job_level_rank(level: str) -> int:
    """Rank an `L<n>` job level. Unparseable levels rank lowest (0).

    Ranking low is the safe direction: an identity whose level cannot be read
    fails a minimum-seniority check rather than passing it.
    """
    match = _JOB_LEVEL_RE.match(level.strip())
    return int(match.group(1)) if match else 0


class PolicyContext(BaseModel):
    """Everything an evaluator is allowed to look at."""

    model_config = ConfigDict(extra="forbid")

    employee: EmployeeProfile
    requested_entitlement_ids: list[str] = Field(default_factory=list)
    existing_entitlement_ids: list[str] = Field(default_factory=list)
    entitlement_risk: dict[str, int] = Field(default_factory=dict)

    def effective_access(self) -> set[str]:
        """Access the identity would hold if the request were fulfilled."""
        return set(self.requested_entitlement_ids) | set(self.existing_entitlement_ids)


class RuleOutcome(BaseModel):
    """What a policy says about one entitlement. `None` means 'silent'."""

    model_config = ConfigDict(extra="forbid")

    status: PolicyStatus
    approval_tier: ApprovalTier
    reason: str


# --------------------------------------------------------------------------- #
# Parameter models
# --------------------------------------------------------------------------- #
class BaseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = None


class MutuallyExclusiveParams(BaseParams):
    entitlements: list[str] = Field(min_length=2)
    effect: Literal["BLOCK", "DENY"] = "BLOCK"


class RiskThresholdParams(BaseParams):
    min_risk_score: int = Field(ge=0, le=100)
    approval_tier: Literal["MANAGER", "HUMAN_REVIEW"] = "MANAGER"


class EmploymentTypeParams(BaseParams):
    employment_types: list[EmploymentType] = Field(min_length=1)
    entitlements: list[str] | None = None
    min_risk_score: int | None = Field(default=None, ge=0, le=100)
    effect: Literal["BLOCK", "DENY"] = "DENY"

    @model_validator(mode="after")
    def _need_a_selector(self) -> "EmploymentTypeParams":
        """Without a selector the rule would restrict every entitlement.

        Validated at the model level, not the field level, so it still fires
        when `min_risk_score` is simply absent from the rule definition.
        """
        if self.min_risk_score is None and not self.entitlements:
            raise ValueError(
                "EMPLOYMENT_TYPE_RESTRICTION requires 'entitlements' and/or 'min_risk_score'"
            )
        return self


class LocationParams(BaseParams):
    entitlements: list[str] = Field(min_length=1)
    allowed_locations: list[str] = Field(min_length=1)
    effect: Literal["BLOCK", "DENY"] = "BLOCK"


class JobLevelParams(BaseParams):
    entitlements: list[str] = Field(min_length=1)
    min_job_level: str
    effect: Literal["BLOCK", "DENY", "REQUIRES_APPROVAL"] = "BLOCK"


class DepartmentParams(BaseParams):
    entitlements: list[str] = Field(min_length=1)
    allowed_departments: list[str] = Field(min_length=1)
    effect: Literal["BLOCK", "DENY"] = "BLOCK"


def _effect_status(effect: str) -> PolicyStatus:
    return {
        "BLOCK": PolicyStatus.BLOCK,
        "DENY": PolicyStatus.DENY,
        "REQUIRES_APPROVAL": PolicyStatus.REQUIRES_APPROVAL,
    }[effect]


# --------------------------------------------------------------------------- #
# Evaluators
# --------------------------------------------------------------------------- #
class PolicyRuleEvaluator(ABC):
    policy_type: ClassVar[PolicyType]
    params_model: ClassVar[type[BaseParams]]

    def parse(self, rule_definition: dict[str, Any]) -> BaseParams:
        try:
            return self.params_model.model_validate(rule_definition)
        except Exception as exc:
            raise InvalidPolicyDefinitionError(
                f"Invalid parameters for {self.policy_type.value}: {exc}",
                details={"policy_type": self.policy_type.value},
            ) from exc

    @abstractmethod
    def evaluate(
        self, entitlement_id: str, params: BaseParams, context: PolicyContext
    ) -> RuleOutcome | None:
        """Return an outcome, or None when the policy has nothing to say."""


class MutuallyExclusiveEvaluator(PolicyRuleEvaluator):
    """Two or more named entitlements must not be held together."""

    policy_type = PolicyType.MUTUALLY_EXCLUSIVE_ENTITLEMENTS
    params_model = MutuallyExclusiveParams

    def evaluate(
        self, entitlement_id: str, params: MutuallyExclusiveParams, context: PolicyContext
    ) -> RuleOutcome | None:
        if entitlement_id not in params.entitlements:
            return None
        counterparts = sorted(
            (set(params.entitlements) & context.effective_access()) - {entitlement_id}
        )
        if not counterparts:
            return None
        detail = (
            params.message
            or "This entitlement must not be held together with the conflicting entitlement."
        )
        return RuleOutcome(
            status=_effect_status(params.effect),
            approval_tier=ApprovalTier.HUMAN_REVIEW,
            reason=(
                f"{detail} Conflicting entitlement(s) also in scope: {', '.join(counterparts)}."
            ),
        )


class RiskThresholdEvaluator(PolicyRuleEvaluator):
    """Entitlements at or above a risk score need a specific approval tier."""

    policy_type = PolicyType.RISK_THRESHOLD_APPROVAL
    params_model = RiskThresholdParams

    def evaluate(
        self, entitlement_id: str, params: RiskThresholdParams, context: PolicyContext
    ) -> RuleOutcome | None:
        score = context.entitlement_risk.get(entitlement_id)
        if score is None or score < params.min_risk_score:
            return None
        tier = ApprovalTier(params.approval_tier)
        detail = params.message or "Elevated-risk entitlement requires additional approval."
        return RuleOutcome(
            status=PolicyStatus.REQUIRES_APPROVAL,
            approval_tier=tier,
            reason=(
                f"{detail} Risk score {score} meets the policy threshold of "
                f"{params.min_risk_score}."
            ),
        )


class EmploymentTypeEvaluator(PolicyRuleEvaluator):
    """Certain contract types may not receive certain (or risky) entitlements."""

    policy_type = PolicyType.EMPLOYMENT_TYPE_RESTRICTION
    params_model = EmploymentTypeParams

    def evaluate(
        self, entitlement_id: str, params: EmploymentTypeParams, context: PolicyContext
    ) -> RuleOutcome | None:
        if context.employee.employment_type not in params.employment_types:
            return None

        by_name = params.entitlements is not None and entitlement_id in params.entitlements
        score = context.entitlement_risk.get(entitlement_id)
        by_risk = (
            params.min_risk_score is not None
            and score is not None
            and score >= params.min_risk_score
        )
        if not (by_name or by_risk):
            return None

        detail = params.message or "Employment type is not eligible for this entitlement."
        qualifier = (
            f"risk score {score} meets the restricted threshold of {params.min_risk_score}"
            if by_risk
            else "the entitlement is explicitly restricted"
        )
        return RuleOutcome(
            status=_effect_status(params.effect),
            approval_tier=ApprovalTier.HUMAN_REVIEW,
            reason=(
                f"{detail} Identity employment type is "
                f"{context.employee.employment_type.value} and {qualifier}."
            ),
        )


class LocationEvaluator(PolicyRuleEvaluator):
    """Named entitlements may only be granted in approved locations."""

    policy_type = PolicyType.LOCATION_RESTRICTION
    params_model = LocationParams

    def evaluate(
        self, entitlement_id: str, params: LocationParams, context: PolicyContext
    ) -> RuleOutcome | None:
        if entitlement_id not in params.entitlements:
            return None
        if context.employee.location in params.allowed_locations:
            return None
        detail = params.message or "Entitlement is restricted to approved locations."
        return RuleOutcome(
            status=_effect_status(params.effect),
            approval_tier=ApprovalTier.HUMAN_REVIEW,
            reason=(
                f"{detail} Identity location '{context.employee.location}' is not in the "
                f"approved list ({', '.join(params.allowed_locations)})."
            ),
        )


class JobLevelEvaluator(PolicyRuleEvaluator):
    """Named entitlements require a minimum seniority."""

    policy_type = PolicyType.JOB_LEVEL_RESTRICTION
    params_model = JobLevelParams

    def evaluate(
        self, entitlement_id: str, params: JobLevelParams, context: PolicyContext
    ) -> RuleOutcome | None:
        if entitlement_id not in params.entitlements:
            return None
        required = job_level_rank(params.min_job_level)
        actual = job_level_rank(context.employee.job_level)
        if actual >= required:
            return None
        detail = params.message or "Entitlement requires a higher job level."
        return RuleOutcome(
            status=_effect_status(params.effect),
            approval_tier=ApprovalTier.HUMAN_REVIEW,
            reason=(
                f"{detail} Identity job level {context.employee.job_level} is below the "
                f"required minimum of {params.min_job_level}."
            ),
        )


class DepartmentEvaluator(PolicyRuleEvaluator):
    """Named entitlements are confined to particular departments."""

    policy_type = PolicyType.DEPARTMENT_RESTRICTION
    params_model = DepartmentParams

    def evaluate(
        self, entitlement_id: str, params: DepartmentParams, context: PolicyContext
    ) -> RuleOutcome | None:
        if entitlement_id not in params.entitlements:
            return None
        if context.employee.department in params.allowed_departments:
            return None
        detail = params.message or "Entitlement is restricted to specific departments."
        return RuleOutcome(
            status=_effect_status(params.effect),
            approval_tier=ApprovalTier.HUMAN_REVIEW,
            reason=(
                f"{detail} Identity department '{context.employee.department}' is not in the "
                f"approved list ({', '.join(params.allowed_departments)})."
            ),
        )


EVALUATORS: dict[PolicyType, PolicyRuleEvaluator] = {
    evaluator.policy_type: evaluator
    for evaluator in (
        MutuallyExclusiveEvaluator(),
        RiskThresholdEvaluator(),
        EmploymentTypeEvaluator(),
        LocationEvaluator(),
        JobLevelEvaluator(),
        DepartmentEvaluator(),
    )
}


def get_evaluator(policy_type: str) -> PolicyRuleEvaluator:
    try:
        return EVALUATORS[PolicyType(policy_type)]
    except (KeyError, ValueError) as exc:
        raise InvalidPolicyDefinitionError(
            f"No evaluator is registered for policy type '{policy_type}'.",
            details={"policy_type": policy_type},
        ) from exc
