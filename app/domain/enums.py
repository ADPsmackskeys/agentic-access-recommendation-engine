"""Domain enumerations.

These are the vocabulary of the governance engine. They are deliberately
closed sets: an LLM can never introduce a new decision value, because every
decision path in the system terminates in one of these members.
"""

from __future__ import annotations

from enum import StrEnum


class EmploymentStatus(StrEnum):
    """Lifecycle state of an identity."""

    ACTIVE = "ACTIVE"
    PENDING_START = "PENDING_START"  # the "new joiner" population
    ON_LEAVE = "ON_LEAVE"
    TERMINATED = "TERMINATED"


class EmploymentType(StrEnum):
    """Contract type - drives contractor-restriction policies."""

    EMPLOYEE = "EMPLOYEE"
    CONTRACTOR = "CONTRACTOR"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ApprovalTier(StrEnum):
    """Who must approve before provisioning may occur."""

    AUTO = "AUTO"
    MANAGER = "MANAGER"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    NONE = "NONE"  # nothing to approve (entitlement not requested)


class PolicyStatus(StrEnum):
    PASS = "PASS"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    BLOCK = "BLOCK"
    DENY = "DENY"  # hard denial, not overridable by an approver
    ERROR = "ERROR"  # rule could not be evaluated -> fail closed
    NOT_EVALUATED = "NOT_EVALUATED"


class SodStatus(StrEnum):
    PASS = "PASS"
    CONFLICT = "CONFLICT"
    NOT_EVALUATED = "NOT_EVALUATED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RecommendationStatus(StrEnum):
    """Terminal outcome for a single candidate entitlement."""

    AUTO_APPROVED = "AUTO_APPROVED"
    MANAGER_APPROVAL = "MANAGER_APPROVAL"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    NOT_RECOMMENDED = "NOT_RECOMMENDED"


class MatchingStrategy(StrEnum):
    """Peer-matching strategies, in order of decreasing precision."""

    JOB_ROLE_DEPARTMENT_JOB_LEVEL = "job_role_department_job_level"
    JOB_ROLE_DEPARTMENT = "job_role_department"
    DEPARTMENT_JOB_LEVEL = "department_job_level"
    DEPARTMENT = "department"
    NONE = "none"


class AnalysisStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"


class PolicyType(StrEnum):
    """Supported, explicitly-implemented policy rule types.

    There is no generic expression evaluator: each type maps to a hand-written,
    parameter-validated evaluator in `app.domain.rules.policy_rules`.
    """

    MUTUALLY_EXCLUSIVE_ENTITLEMENTS = "MUTUALLY_EXCLUSIVE_ENTITLEMENTS"
    RISK_THRESHOLD_APPROVAL = "RISK_THRESHOLD_APPROVAL"
    EMPLOYMENT_TYPE_RESTRICTION = "EMPLOYMENT_TYPE_RESTRICTION"
    LOCATION_RESTRICTION = "LOCATION_RESTRICTION"
    JOB_LEVEL_RESTRICTION = "JOB_LEVEL_RESTRICTION"
    DEPARTMENT_RESTRICTION = "DEPARTMENT_RESTRICTION"


class EvidenceType(StrEnum):
    PEER_HOLDS_ENTITLEMENT = "PEER_HOLDS_ENTITLEMENT"
    PEER_MATCH = "PEER_MATCH"
    AFFINITY = "AFFINITY"


class ExplanationGenerator(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    LLM = "LLM"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


class SailPointRequestStatus(StrEnum):
    SIMULATED = "SIMULATED"
    SUBMITTED = "SUBMITTED"  # reserved for a future real connector
    FAILED = "FAILED"


# Ordering helper used when aggregating severities / risk levels.
SEVERITY_ORDER: dict[str, int] = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def max_severity(values: list[str]) -> Severity | None:
    """Return the highest severity in `values`, or None when empty."""
    if not values:
        return None
    return Severity(max(values, key=lambda v: SEVERITY_ORDER.get(v, 0)))
