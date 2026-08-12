"""Affinity, policy and SoD engines against the real seeded database."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models.governance import Policy, SodRule
from app.domain.enums import PolicyStatus, Severity, SodStatus
from app.services.affinity_service import AffinityService
from app.services.analysis_service import AnalysisService
from app.services.peer_service import PeerAnalysisService
from app.services.policy_service import PolicyService
from app.services.sod_service import SodService

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Affinity
# --------------------------------------------------------------------------- #
def test_affinity_reproduces_the_documented_percentages(db_session: Session) -> None:
    """The canonical worked example from the specification."""
    peers = PeerAnalysisService(db_session).find_peers("EMP1001")
    result = AffinityService(db_session).calculate("EMP1001", peers)
    scores = {c.entitlement_id: c.affinity_score for c in result.candidates}

    assert result.total_peers == 8
    assert scores["SAP_FIN_DISPLAY"] == 100.0
    assert scores["POWERBI_FINANCE_VIEW"] == 87.5
    assert scores["SNOWFLAKE_FIN_READ"] == 75.0
    assert scores["SAP_FIN_POST_JOURNAL"] == 25.0


def test_threshold_partitions_candidates(db_session: Session) -> None:
    peers = PeerAnalysisService(db_session).find_peers("EMP1001")
    result = AffinityService(db_session).calculate("EMP1001", peers, threshold=70.0)
    above = {c.entitlement_id for c in result.above_threshold()}
    assert "SNOWFLAKE_FIN_READ" in above          # 75.0
    assert "JIRA_PROJECT_USER" not in above        # 62.5


def test_threshold_is_configurable_per_call(db_session: Session) -> None:
    peers = PeerAnalysisService(db_session).find_peers("EMP1001")
    service = AffinityService(db_session)
    strict = service.calculate("EMP1001", peers, threshold=90.0)
    lenient = service.calculate("EMP1001", peers, threshold=20.0)
    assert len(strict.above_threshold()) < len(lenient.above_threshold())


def test_every_candidate_carries_its_peer_evidence(db_session: Session) -> None:
    peers = PeerAnalysisService(db_session).find_peers("EMP1001")
    result = AffinityService(db_session).calculate("EMP1001", peers)
    for candidate in result.candidates:
        assert len(candidate.evidence) == candidate.peer_count
        assert all(e.peer_employee_id in peers.peer_ids for e in candidate.evidence)


def test_no_peers_yields_no_candidates(db_session: Session) -> None:
    from app.domain.enums import MatchingStrategy
    from app.domain.models import PeerAnalysisResult

    empty = PeerAnalysisResult(
        employee_id="EMP1001", matching_strategy=MatchingStrategy.NONE, peer_count=0
    )
    result = AffinityService(db_session).calculate("EMP1001", empty)
    assert result.candidates == []


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_clean_request_passes_every_policy(db_session: Session) -> None:
    result = PolicyService(db_session).validate(
        "EMP1001", ["SAP_FIN_DISPLAY", "WORKDAY_SELF_SERVICE"]
    )
    assert result.status is PolicyStatus.PASS
    assert all(r.status is PolicyStatus.PASS for r in result.results)


def test_toxic_pair_is_blocked_by_policy(db_session: Session) -> None:
    result = PolicyService(db_session).validate(
        "EMP1002", ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"]
    )
    assert result.status is PolicyStatus.BLOCK
    per_entitlement = result.by_entitlement("SAP_AP_CREATE_VENDOR")
    assert per_entitlement is not None
    assert per_entitlement.status is PolicyStatus.BLOCK
    assert "POL-001" in {p.policy_id for p in per_entitlement.failed_policies}


def test_contractor_is_denied_sensitive_access(db_session: Session) -> None:
    result = PolicyService(db_session).validate("EMP1004", ["SNOWFLAKE_PII_READ"])
    entitlement = result.by_entitlement("SNOWFLAKE_PII_READ")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.DENY
    assert "POL-004" in {p.policy_id for p in entitlement.failed_policies}


def test_location_restriction_blocks_outside_approved_locations(
    db_session: Session,
) -> None:
    result = PolicyService(db_session).validate("EMP1005", ["WORKDAY_COMP_VIEW"])
    entitlement = result.by_entitlement("WORKDAY_COMP_VIEW")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.BLOCK
    assert "POL-005" in {p.policy_id for p in entitlement.failed_policies}


def test_disabled_policies_are_skipped(db_session: Session) -> None:
    """POL-008 would block CRM read outside Sales - it is disabled."""
    result = PolicyService(db_session).validate("EMP1001", ["SALESFORCE_CRM_READ"])
    assert "POL-008" in result.skipped_policy_ids
    assert "POL-008" not in result.evaluated_policy_ids
    entitlement = result.by_entitlement("SALESFORCE_CRM_READ")
    assert entitlement is not None and entitlement.status is PolicyStatus.PASS


def test_enabling_a_policy_changes_the_outcome(db_session: Session) -> None:
    """Proves the skip above is the policy's `enabled` flag doing the work."""
    policy = db_session.get(Policy, "POL-008")
    assert policy is not None
    policy.enabled = True
    db_session.flush()

    result = PolicyService(db_session).validate("EMP1001", ["SALESFORCE_CRM_READ"])
    entitlement = result.by_entitlement("SALESFORCE_CRM_READ")
    assert entitlement is not None
    assert entitlement.status is PolicyStatus.BLOCK


def test_unevaluable_policy_fails_closed(db_session: Session) -> None:
    """A corrupt rule definition must produce ERROR, never a silent PASS."""
    db_session.add(
        Policy(
            policy_id="POL-BROKEN",
            policy_name="Corrupt Control",
            policy_type="MUTUALLY_EXCLUSIVE_ENTITLEMENTS",
            rule_definition={"entitlements": ["only_one_item"]},  # needs >= 2
            enabled=True,
        )
    )
    db_session.flush()

    result = PolicyService(db_session).validate("EMP1001", ["SAP_FIN_DISPLAY"])
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

    result = PolicyService(db_session).validate("EMP1001", ["SAP_FIN_DISPLAY"])
    entitlement = result.by_entitlement("SAP_FIN_DISPLAY")
    assert entitlement is not None and entitlement.status is PolicyStatus.ERROR


# --------------------------------------------------------------------------- #
# Segregation of duties
# --------------------------------------------------------------------------- #
def test_no_conflict_for_a_clean_set(db_session: Session) -> None:
    result = SodService(db_session).check(
        "EMP1001", ["SAP_FIN_DISPLAY", "POWERBI_FINANCE_VIEW"]
    )
    assert result.status is SodStatus.PASS
    assert result.conflicts == []
    assert result.severity is None


def test_toxic_pair_is_detected(db_session: Session) -> None:
    result = SodService(db_session).check(
        "EMP1002", ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"]
    )
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.CRITICAL
    assert [c.sod_id for c in result.conflicts] == ["SOD-001"]


def test_highest_severity_is_reported(db_session: Session) -> None:
    result = SodService(db_session).check(
        "EMP1003",
        [
            "CONCUR_EXPENSE_SUBMIT",
            "CONCUR_EXPENSE_APPROVE",   # SOD-003, MEDIUM
            "SAP_AP_CREATE_VENDOR",
            "SAP_AP_APPROVE_PAYMENT",   # SOD-001, CRITICAL
        ],
    )
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.CRITICAL
    assert len(result.conflicts) >= 2


def test_conflict_against_previously_held_access(db_session: Session) -> None:
    """A single new entitlement colliding with existing access still conflicts."""
    result = SodService(db_session).check(
        "EMP1002",
        ["SAP_AP_APPROVE_PAYMENT"],
        existing_entitlement_ids=["SAP_AP_CREATE_VENDOR"],
    )
    assert result.status is SodStatus.CONFLICT
    assert result.conflicts[0].conflicts_with_existing_access is True


def test_pre_existing_conflict_is_not_attributed_to_this_request(
    db_session: Session,
) -> None:
    """Both sides already held: a remediation matter, not a joiner decision."""
    result = SodService(db_session).check(
        "EMP1002",
        ["SAP_FIN_DISPLAY"],
        existing_entitlement_ids=["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"],
    )
    assert result.status is SodStatus.PASS


def test_disabled_sod_rules_are_skipped(db_session: Session) -> None:
    """SOD-008 is disabled and must never fire."""
    result = SodService(db_session).check(
        "EMP1001", ["SALESFORCE_CRM_ADMIN", "SALESFORCE_CRM_READ"]
    )
    assert "SOD-008" not in result.evaluated_rule_ids
    assert result.status is SodStatus.PASS


def test_enabling_an_sod_rule_changes_the_outcome(db_session: Session) -> None:
    rule = db_session.get(SodRule, "SOD-008")
    assert rule is not None
    rule.enabled = True
    db_session.flush()

    result = SodService(db_session).check(
        "EMP1001", ["SALESFORCE_CRM_ADMIN", "SALESFORCE_CRM_READ"]
    )
    assert result.status is SodStatus.CONFLICT
    assert result.severity is Severity.LOW


def test_existing_access_is_loaded_when_not_supplied(db_session: Session) -> None:
    """The service must not assume the caller passed current holdings."""
    profile = AnalysisService(db_session).get_employee_profile("EMP009")
    assert "SAP_AP_CREATE_VENDOR" in profile.existing_entitlement_ids
    result = SodService(db_session).check("EMP009", ["SAP_AP_APPROVE_PAYMENT"])
    assert result.status is SodStatus.CONFLICT
