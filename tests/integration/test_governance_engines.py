"""Affinity, policy and SoD engines against the real seeded database.

The seeded corpus is the client's extract, which supplies only two policies
(both risk thresholds) and three SoD rules. The engine implements more policy
types than the client happens to use, so tests for those build the policy row
they need rather than leaving an implemented control uncovered. Anything the
client's data *can* demonstrate is asserted against the client's data.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models.governance import Policy, SodRule
from app.db.models.identity import Employee
from app.domain.enums import (
    ApprovalTier,
    EmploymentType,
    PolicyStatus,
    PolicyType,
    Severity,
    SodStatus,
)
from app.services.affinity_service import AffinityService
from app.services.analysis_service import AnalysisService
from app.services.peer_service import PeerAnalysisService
from app.services.policy_service import PolicyService
from app.services.sod_service import SodService

pytestmark = pytest.mark.integration


def _affinity(session: Session, employee_id: str, **kwargs):
    peers = PeerAnalysisService(session).find_peers(employee_id)
    return AffinityService(session).calculate(employee_id, peers, **kwargs)


# --------------------------------------------------------------------------- #
# Affinity
# --------------------------------------------------------------------------- #
def test_affinity_reproduces_the_clients_own_figures(db_session: Session) -> None:
    """NJ1001 against five Financial Analyst peers.

    These four percentages are exactly the Finance rows of the client's
    `peer_affinity_scores` extract, recomputed from the identities rather than
    read back from it.
    """
    result = _affinity(db_session, "NJ1001")
    scores = {c.entitlement_id: c.affinity_score for c in result.candidates}

    assert result.total_peers == 5
    assert scores["SAP_FIN_DISPLAY"] == 100.0
    assert scores["POWERBI_FINANCE"] == 100.0
    assert scores["SAP_AP_INVOICE"] == 80.0
    assert scores["FIN_SHAREPOINT"] == 20.0


def test_affinity_keeps_a_fractional_score_precise(db_session: Session) -> None:
    """Two of three Software Engineers hold CONFLUENCE_USER.

    The client's extract rounds this to 67; the engine keeps 66.67. Same value,
    and the precision is retained here so the threshold comparison is made on
    the real number rather than a rounded one.
    """
    scores = {c.entitlement_id: c.affinity_score for c in _affinity(db_session, "NJ1004").candidates}
    assert scores["JIRA_USER"] == 100.0
    assert scores["GITHUB_DEV"] == 100.0
    assert scores["CONFLUENCE_USER"] == pytest.approx(66.67, abs=0.01)


def test_threshold_partitions_candidates(db_session: Session) -> None:
    result = _affinity(db_session, "NJ1001", threshold=70.0)
    above = {c.entitlement_id for c in result.above_threshold()}
    assert "SAP_AP_INVOICE" in above       # 80.0
    assert "FIN_SHAREPOINT" not in above   # 20.0


def test_threshold_is_configurable_per_call(db_session: Session) -> None:
    strict = _affinity(db_session, "NJ1001", threshold=90.0)
    lenient = _affinity(db_session, "NJ1001", threshold=20.0)
    assert len(strict.above_threshold()) < len(lenient.above_threshold())


def test_every_candidate_carries_its_peer_evidence(db_session: Session) -> None:
    peers = PeerAnalysisService(db_session).find_peers("NJ1001")
    result = AffinityService(db_session).calculate("NJ1001", peers)
    for candidate in result.candidates:
        assert len(candidate.evidence) == candidate.peer_count
        assert all(e.peer_employee_id in peers.peer_ids for e in candidate.evidence)


def test_no_peers_yields_no_candidates(db_session: Session) -> None:
    """NJ1008 is an HR Specialist with no peers - nothing may be invented."""
    result = _affinity(db_session, "NJ1008")
    assert result.total_peers == 0
    assert result.candidates == []


# --------------------------------------------------------------------------- #
# Policy - using the client's own rules
# --------------------------------------------------------------------------- #
def test_clean_request_passes_every_policy(db_session: Session) -> None:
    """SAP_FIN_DISPLAY (15) and POWERBI_FINANCE (10) are below every threshold."""
    result = PolicyService(db_session).validate(
        "NJ1001", ["SAP_FIN_DISPLAY", "POWERBI_FINANCE"]
    )
    assert result.status is PolicyStatus.PASS
    assert all(r.status is PolicyStatus.PASS for r in result.results)


def test_risk_threshold_policy_demands_approval(db_session: Session) -> None:
    """POL005 fires at 70; AUDIT_TOOL scores 75."""
    result = PolicyService(db_session).validate("NJ1007", ["AUDIT_TOOL"])
    entitlement = result.by_entitlement("AUDIT_TOOL")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.REQUIRES_APPROVAL
    assert "POL005" in {p.policy_id for p in entitlement.failed_policies}


def test_the_threshold_is_inclusive_at_its_boundary(db_session: Session) -> None:
    """RSA_GRC scores exactly 70, and POL005 reads `risk_score >= 70`."""
    result = PolicyService(db_session).validate("NJ1006", ["RSA_GRC"])
    entitlement = result.by_entitlement("RSA_GRC")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.REQUIRES_APPROVAL


def test_both_thresholds_fire_on_critical_access(db_session: Session) -> None:
    """SAP_PAYMENT_APPROVER scores 95, clearing POL005 (70) and POL006 (90)."""
    result = PolicyService(db_session).validate("NJ1001", ["SAP_PAYMENT_APPROVER"])
    entitlement = result.by_entitlement("SAP_PAYMENT_APPROVER")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.REQUIRES_APPROVAL
    assert {"POL005", "POL006"} <= {p.policy_id for p in entitlement.failed_policies}
    assert entitlement.approval_tier is ApprovalTier.HUMAN_REVIEW


def test_a_low_risk_entitlement_clears_both_thresholds(db_session: Session) -> None:
    result = PolicyService(db_session).validate("NJ1004", ["JIRA_USER"])
    entitlement = result.by_entitlement("JIRA_USER")
    assert entitlement is not None and entitlement.status is PolicyStatus.PASS


# --------------------------------------------------------------------------- #
# Policy - evaluators the client's extract does not exercise
# --------------------------------------------------------------------------- #
def test_toxic_pair_is_blocked_by_policy(db_session: Session) -> None:
    db_session.add(
        Policy(
            policy_id="POL-EXCL",
            policy_name="Vendor and Payment Segregation",
            policy_type=PolicyType.MUTUALLY_EXCLUSIVE_ENTITLEMENTS.value,
            rule_definition={
                "entitlements": ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"],
                "effect": "BLOCK",
            },
            enabled=True,
        )
    )
    db_session.flush()

    result = PolicyService(db_session).validate(
        "NJ1001", ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"]
    )
    assert result.status is PolicyStatus.BLOCK
    per_entitlement = result.by_entitlement("SAP_VENDOR_CREATE")
    assert per_entitlement is not None
    assert per_entitlement.status is PolicyStatus.BLOCK
    assert "POL-EXCL" in {p.policy_id for p in per_entitlement.failed_policies}


def test_contractor_is_denied_sensitive_access(db_session: Session) -> None:
    db_session.add(
        Policy(
            policy_id="POL-CONTRACTOR",
            policy_name="Contractor Privileged Restriction",
            policy_type=PolicyType.EMPLOYMENT_TYPE_RESTRICTION.value,
            rule_definition={
                "employment_types": ["CONTRACTOR"],
                "min_risk_score": 70,
                "effect": "DENY",
            },
            enabled=True,
        )
    )
    # The client's extract has no employment_type column, so every identity
    # loads as EMPLOYEE. The restriction is still the system's, so the test
    # makes one identity a contractor rather than leaving the path uncovered.
    employee = db_session.get(Employee, "NJ1001")
    assert employee is not None
    employee.employment_type = EmploymentType.CONTRACTOR.value
    db_session.flush()

    result = PolicyService(db_session).validate("NJ1001", ["AUDIT_TOOL"])
    entitlement = result.by_entitlement("AUDIT_TOOL")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.DENY
    assert "POL-CONTRACTOR" in {p.policy_id for p in entitlement.failed_policies}


def test_location_restriction_blocks_outside_approved_locations(
    db_session: Session,
) -> None:
    db_session.add(
        Policy(
            policy_id="POL-LOCATION",
            policy_name="Audit Tooling Location Restriction",
            policy_type=PolicyType.LOCATION_RESTRICTION.value,
            rule_definition={
                "entitlements": ["AUDIT_TOOL"],
                "allowed_locations": ["Mumbai"],
                "effect": "BLOCK",
            },
            enabled=True,
        )
    )
    db_session.flush()

    # NJ1001 is in Bangalore; NJ1007 is in Mumbai and must be unaffected.
    blocked = PolicyService(db_session).validate("NJ1001", ["AUDIT_TOOL"])
    entitlement = blocked.by_entitlement("AUDIT_TOOL")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.BLOCK
    assert "POL-LOCATION" in {p.policy_id for p in entitlement.failed_policies}

    allowed = PolicyService(db_session).validate("NJ1007", ["AUDIT_TOOL"])
    permitted = allowed.by_entitlement("AUDIT_TOOL")
    assert permitted is not None
    assert "POL-LOCATION" not in {p.policy_id for p in permitted.failed_policies}


def test_disabled_policies_are_skipped(db_session: Session) -> None:
    db_session.add(
        Policy(
            policy_id="POL-OFF",
            policy_name="Disabled Control",
            policy_type=PolicyType.DEPARTMENT_RESTRICTION.value,
            rule_definition={
                "entitlements": ["SAP_FIN_DISPLAY"],
                "allowed_departments": ["Audit"],
                "effect": "BLOCK",
            },
            enabled=False,
        )
    )
    db_session.flush()

    result = PolicyService(db_session).validate("NJ1001", ["SAP_FIN_DISPLAY"])
    assert "POL-OFF" in result.skipped_policy_ids
    assert "POL-OFF" not in result.evaluated_policy_ids
    entitlement = result.by_entitlement("SAP_FIN_DISPLAY")
    assert entitlement is not None and entitlement.status is PolicyStatus.PASS


def test_enabling_a_policy_changes_the_outcome(db_session: Session) -> None:
    """Proves the skip above is the policy's `enabled` flag doing the work."""
    policy = Policy(
        policy_id="POL-OFF",
        policy_name="Disabled Control",
        policy_type=PolicyType.DEPARTMENT_RESTRICTION.value,
        rule_definition={
            "entitlements": ["SAP_FIN_DISPLAY"],
            "allowed_departments": ["Audit"],
            "effect": "BLOCK",
        },
        enabled=True,
    )
    db_session.add(policy)
    db_session.flush()

    result = PolicyService(db_session).validate("NJ1001", ["SAP_FIN_DISPLAY"])
    entitlement = result.by_entitlement("SAP_FIN_DISPLAY")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.BLOCK


def test_unevaluable_policy_fails_closed(db_session: Session) -> None:
    """A corrupt rule definition must produce ERROR, never a silent PASS."""
    db_session.add(
        Policy(
            policy_id="POL-BROKEN",
            policy_name="Corrupt Control",
            policy_type=PolicyType.MUTUALLY_EXCLUSIVE_ENTITLEMENTS.value,
            rule_definition={"entitlements": ["only_one_item"]},  # needs >= 2
            enabled=True,
        )
    )
    db_session.flush()

    result = PolicyService(db_session).validate("NJ1001", ["SAP_FIN_DISPLAY"])
    entitlement = result.by_entitlement("SAP_FIN_DISPLAY")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.ERROR
    assert "POL-BROKEN" in {p.policy_id for p in entitlement.failed_policies}


def test_unknown_policy_type_fails_closed(db_session: Session) -> None:
    db_session.add(
        Policy(
            policy_id="POL-ALIEN",
            policy_name="Unregistered Type",
            policy_type="EVALUATE_ARBITRARY_PYTHON",
            rule_definition={"expr": "True"},
            enabled=True,
        )
    )
    db_session.flush()

    result = PolicyService(db_session).validate("NJ1001", ["SAP_FIN_DISPLAY"])
    entitlement = result.by_entitlement("SAP_FIN_DISPLAY")
    assert entitlement is not None and entitlement.status is PolicyStatus.ERROR


# --------------------------------------------------------------------------- #
# Segregation of duties - the client's own three rules
# --------------------------------------------------------------------------- #
def test_no_conflict_for_a_clean_set(db_session: Session) -> None:
    result = SodService(db_session).check("NJ1001", ["SAP_FIN_DISPLAY", "POWERBI_FINANCE"])
    assert result.status is SodStatus.PASS
    assert result.conflicts == []
    assert result.severity is None


def test_toxic_pair_is_detected(db_session: Session) -> None:
    """SOD001: vendor creation and payment approval in one identity."""
    result = SodService(db_session).check(
        "NJ1001", ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"]
    )
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.CRITICAL
    assert [c.sod_id for c in result.conflicts] == ["SOD001"]


def test_highest_severity_is_reported(db_session: Session) -> None:
    """SOD001 (CRITICAL) and SOD003 (HIGH) both fire; CRITICAL must win."""
    result = SodService(db_session).check(
        "NJ1001",
        ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER", "SAP_AP_INVOICE"],
    )
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.CRITICAL
    assert {c.sod_id for c in result.conflicts} == {"SOD001", "SOD003"}


def test_conflict_against_previously_held_access(db_session: Session) -> None:
    """A single new entitlement colliding with existing access still conflicts."""
    result = SodService(db_session).check(
        "NJ1001",
        ["SAP_PAYMENT_APPROVER"],
        existing_entitlement_ids=["SAP_VENDOR_CREATE"],
    )
    assert result.status is SodStatus.CONFLICT
    assert result.conflicts[0].conflicts_with_existing_access is True


def test_pre_existing_conflict_is_not_attributed_to_this_request(
    db_session: Session,
) -> None:
    """Both sides already held: a remediation matter, not a joiner decision."""
    result = SodService(db_session).check(
        "NJ1001",
        ["SAP_FIN_DISPLAY"],
        existing_entitlement_ids=["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"],
    )
    assert result.status is SodStatus.PASS


def test_disabled_sod_rules_are_skipped(db_session: Session) -> None:
    rule = db_session.get(SodRule, "SOD002")
    assert rule is not None
    rule.enabled = False
    db_session.flush()

    result = SodService(db_session).check("NJ1006", ["AD_DOMAIN_ADMIN", "RSA_GRC"])
    assert "SOD002" not in result.evaluated_rule_ids
    assert result.status is SodStatus.PASS


def test_an_enabled_sod_rule_fires_on_the_same_pair(db_session: Session) -> None:
    """The counterpart to the skip above: SOD002 ships enabled and does fire."""
    result = SodService(db_session).check("NJ1006", ["AD_DOMAIN_ADMIN", "RSA_GRC"])
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.HIGH
    assert [c.sod_id for c in result.conflicts] == ["SOD002"]


def test_existing_access_is_loaded_when_not_supplied(db_session: Session) -> None:
    """The service must not assume the caller passed current holdings.

    EMP009 holds RSA_GRC in the client's extract, so requesting
    AD_DOMAIN_ADMIN alone is enough to trip SOD002.
    """
    profile = AnalysisService(db_session).get_employee_profile("EMP009")
    assert "RSA_GRC" in profile.existing_entitlement_ids

    result = SodService(db_session).check("EMP009", ["AD_DOMAIN_ADMIN"])
    assert result.status is SodStatus.CONFLICT
    assert [c.sod_id for c in result.conflicts] == ["SOD002"]
