"""Policy rule evaluators.

These are the controls themselves, so the tests assert on the outcome an
auditor would care about, not just that a function returns something.
"""

from __future__ import annotations

import pytest

from app.domain.enums import (
    ApprovalTier,
    EmploymentStatus,
    EmploymentType,
    PolicyStatus,
    PolicyType,
)
from app.domain.exceptions import InvalidPolicyDefinitionError
from app.domain.models import EmployeeProfile
from app.domain.rules.policy_rules import (
    DepartmentEvaluator,
    EmploymentTypeEvaluator,
    JobLevelEvaluator,
    LocationEvaluator,
    MutuallyExclusiveEvaluator,
    PolicyContext,
    RiskThresholdEvaluator,
    get_evaluator,
    job_level_rank,
)


def profile(**overrides) -> EmployeeProfile:
    defaults = dict(
        employee_id="EMP9001",
        name="Test Joiner",
        department="Finance",
        job_role="Financial Analyst",
        job_level="L3",
        location="London",
        employment_status=EmploymentStatus.PENDING_START,
        employment_type=EmploymentType.EMPLOYEE,
        existing_entitlement_ids=[],
    )
    defaults.update(overrides)
    return EmployeeProfile(**defaults)


def context(**overrides) -> PolicyContext:
    defaults = dict(
        employee=profile(),
        requested_entitlement_ids=[],
        existing_entitlement_ids=[],
        entitlement_risk={},
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


# --------------------------------------------------------------------------- #
# Mutually exclusive entitlements
# --------------------------------------------------------------------------- #
class TestMutuallyExclusive:
    evaluator = MutuallyExclusiveEvaluator()
    params = {"entitlements": ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"]}

    def test_blocks_when_both_are_requested(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            requested_entitlement_ids=["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"]
        )
        outcome = self.evaluator.evaluate("SAP_AP_CREATE_VENDOR", parsed, ctx)
        assert outcome is not None
        assert outcome.status is PolicyStatus.BLOCK
        assert outcome.approval_tier is ApprovalTier.HUMAN_REVIEW
        assert "SAP_AP_APPROVE_PAYMENT" in outcome.reason

    def test_silent_when_only_one_is_requested(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(requested_entitlement_ids=["SAP_AP_CREATE_VENDOR"])
        assert self.evaluator.evaluate("SAP_AP_CREATE_VENDOR", parsed, ctx) is None

    def test_counterpart_already_held_still_conflicts(self) -> None:
        """The most common real conflict: new access colliding with old access."""
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            requested_entitlement_ids=["SAP_AP_CREATE_VENDOR"],
            existing_entitlement_ids=["SAP_AP_APPROVE_PAYMENT"],
        )
        outcome = self.evaluator.evaluate("SAP_AP_CREATE_VENDOR", parsed, ctx)
        assert outcome is not None and outcome.status is PolicyStatus.BLOCK

    def test_silent_for_unrelated_entitlement(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            requested_entitlement_ids=["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"]
        )
        assert self.evaluator.evaluate("SAP_FIN_DISPLAY", parsed, ctx) is None


# --------------------------------------------------------------------------- #
# Risk threshold
# --------------------------------------------------------------------------- #
class TestRiskThreshold:
    evaluator = RiskThresholdEvaluator()

    def test_requires_manager_at_or_above_threshold(self) -> None:
        parsed = self.evaluator.parse({"min_risk_score": 70, "approval_tier": "MANAGER"})
        ctx = context(entitlement_risk={"E1": 70})
        outcome = self.evaluator.evaluate("E1", parsed, ctx)
        assert outcome is not None
        assert outcome.status is PolicyStatus.REQUIRES_APPROVAL
        assert outcome.approval_tier is ApprovalTier.MANAGER

    def test_silent_below_threshold(self) -> None:
        parsed = self.evaluator.parse({"min_risk_score": 70, "approval_tier": "MANAGER"})
        assert self.evaluator.evaluate("E1", parsed, context(entitlement_risk={"E1": 69})) is None

    def test_critical_threshold_demands_human_review(self) -> None:
        parsed = self.evaluator.parse({"min_risk_score": 90, "approval_tier": "HUMAN_REVIEW"})
        outcome = self.evaluator.evaluate("E1", parsed, context(entitlement_risk={"E1": 91}))
        assert outcome is not None and outcome.approval_tier is ApprovalTier.HUMAN_REVIEW


# --------------------------------------------------------------------------- #
# Employment type
# --------------------------------------------------------------------------- #
class TestEmploymentType:
    evaluator = EmploymentTypeEvaluator()
    params = {
        "employment_types": ["CONTRACTOR"],
        "min_risk_score": 70,
        "effect": "DENY",
    }

    def test_denies_risky_access_for_contractor(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            employee=profile(employment_type=EmploymentType.CONTRACTOR),
            entitlement_risk={"E1": 82},
        )
        outcome = self.evaluator.evaluate("E1", parsed, ctx)
        assert outcome is not None and outcome.status is PolicyStatus.DENY

    def test_silent_for_permanent_employee(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            employee=profile(employment_type=EmploymentType.EMPLOYEE),
            entitlement_risk={"E1": 82},
        )
        assert self.evaluator.evaluate("E1", parsed, ctx) is None

    def test_silent_for_low_risk_access(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(
            employee=profile(employment_type=EmploymentType.CONTRACTOR),
            entitlement_risk={"E1": 10},
        )
        assert self.evaluator.evaluate("E1", parsed, ctx) is None

    def test_requires_a_selector(self) -> None:
        with pytest.raises(InvalidPolicyDefinitionError):
            self.evaluator.parse({"employment_types": ["CONTRACTOR"]})


# --------------------------------------------------------------------------- #
# Location / job level / department
# --------------------------------------------------------------------------- #
class TestLocation:
    evaluator = LocationEvaluator()
    params = {
        "entitlements": ["SNOWFLAKE_PII_READ"],
        "allowed_locations": ["London", "Frankfurt"],
    }

    def test_blocks_outside_approved_location(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(employee=profile(location="Bangalore"))
        outcome = self.evaluator.evaluate("SNOWFLAKE_PII_READ", parsed, ctx)
        assert outcome is not None and outcome.status is PolicyStatus.BLOCK
        assert "Bangalore" in outcome.reason

    def test_silent_inside_approved_location(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(employee=profile(location="London"))
        assert self.evaluator.evaluate("SNOWFLAKE_PII_READ", parsed, ctx) is None


class TestJobLevel:
    evaluator = JobLevelEvaluator()
    params = {"entitlements": ["SAP_GL_CLOSE_PERIOD"], "min_job_level": "L4"}

    def test_blocks_below_required_level(self) -> None:
        parsed = self.evaluator.parse(self.params)
        outcome = self.evaluator.evaluate(
            "SAP_GL_CLOSE_PERIOD", parsed, context(employee=profile(job_level="L3"))
        )
        assert outcome is not None and outcome.status is PolicyStatus.BLOCK

    def test_silent_at_or_above_required_level(self) -> None:
        parsed = self.evaluator.parse(self.params)
        for level in ("L4", "L5"):
            ctx = context(employee=profile(job_level=level))
            assert self.evaluator.evaluate("SAP_GL_CLOSE_PERIOD", parsed, ctx) is None

    def test_unparseable_level_fails_closed(self) -> None:
        """An unreadable level must not sail past a seniority requirement."""
        assert job_level_rank("SENIOR") == 0
        parsed = self.evaluator.parse(self.params)
        ctx = context(employee=profile(job_level="SENIOR"))
        outcome = self.evaluator.evaluate("SAP_GL_CLOSE_PERIOD", parsed, ctx)
        assert outcome is not None and outcome.status is PolicyStatus.BLOCK


class TestDepartment:
    evaluator = DepartmentEvaluator()
    params = {
        "entitlements": ["WORKDAY_HR_ADMIN"],
        "allowed_departments": ["Human Resources"],
    }

    def test_blocks_outside_allowed_department(self) -> None:
        parsed = self.evaluator.parse(self.params)
        outcome = self.evaluator.evaluate(
            "WORKDAY_HR_ADMIN", parsed, context(employee=profile(department="Finance"))
        )
        assert outcome is not None and outcome.status is PolicyStatus.BLOCK

    def test_silent_inside_allowed_department(self) -> None:
        parsed = self.evaluator.parse(self.params)
        ctx = context(employee=profile(department="Human Resources"))
        assert self.evaluator.evaluate("WORKDAY_HR_ADMIN", parsed, ctx) is None


# --------------------------------------------------------------------------- #
# Registry and failure modes
# --------------------------------------------------------------------------- #
def test_every_policy_type_has_an_evaluator() -> None:
    for policy_type in PolicyType:
        assert get_evaluator(policy_type.value) is not None


def test_unknown_policy_type_raises_rather_than_passing() -> None:
    with pytest.raises(InvalidPolicyDefinitionError):
        get_evaluator("ARBITRARY_PYTHON_EXPRESSION")


def test_malformed_parameters_raise() -> None:
    with pytest.raises(InvalidPolicyDefinitionError):
        MutuallyExclusiveEvaluator().parse({"entitlements": ["only_one"]})


def test_rule_definitions_reject_unknown_keys() -> None:
    """No smuggling extra directives into a rule definition."""
    with pytest.raises(InvalidPolicyDefinitionError):
        RiskThresholdEvaluator().parse(
            {"min_risk_score": 70, "approval_tier": "MANAGER", "exec": "os.system('x')"}
        )
