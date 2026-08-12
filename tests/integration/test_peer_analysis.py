"""Peer analysis against the real seeded database.

The corpus is the client's extract (`seed/*.json`): ten active identities in
Finance, Technology, Risk and Audit, and ten new joiners. The figures asserted
here are properties of that corpus, so a change to the extract that moves them
should fail loudly rather than quietly re-baseline.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models.identity import Employee
from app.domain.enums import EmploymentStatus, MatchingStrategy
from app.domain.exceptions import EmployeeNotFoundError
from app.services.peer_service import PeerAnalysisService

pytestmark = pytest.mark.integration


def test_exact_match_finds_the_role_cohort(db_session: Session) -> None:
    """NJ1001 is a Financial Analyst L2 - five identities match exactly."""
    result = PeerAnalysisService(db_session).find_peers("NJ1001")
    assert result.matching_strategy is MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL
    assert result.peer_count == 5
    assert result.peer_ids == [f"EMP{i:03d}" for i in range(1, 6)]
    assert result.confidence == 0.8075
    assert result.sufficient is True
    assert result.strategies_attempted == [MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL]


def test_fallback_relaxes_to_department(db_session: Session) -> None:
    """NJ1010 is a Senior Financial Analyst L3 - a role and level nobody holds.

    Every more precise strategy is tried and fails: no Senior Financial Analyst
    exists, and no Finance identity is L3. The match lands on department alone.
    """
    result = PeerAnalysisService(db_session).find_peers("NJ1010")
    assert result.matching_strategy is MatchingStrategy.DEPARTMENT
    assert result.strategies_attempted == [
        MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        MatchingStrategy.JOB_ROLE_DEPARTMENT,
        MatchingStrategy.DEPARTMENT_JOB_LEVEL,
        MatchingStrategy.DEPARTMENT,
    ]
    assert result.peer_count == 5
    assert result.confidence < 0.8075, "a fallback match must be less confident"
    assert result.notes and "fallback" in result.notes


def test_a_weaker_strategy_yields_a_weaker_claim(db_session: Session) -> None:
    """NJ1009 (Cloud Engineer) and NJ1004 (Software Engineer) share a peer group.

    Both end up with the three Technology identities, but NJ1004 reached them by
    exact role match and NJ1009 only by department. Same evidence, different
    strength of claim - and the confidence has to say so.
    """
    exact = PeerAnalysisService(db_session).find_peers("NJ1004")
    fallback = PeerAnalysisService(db_session).find_peers("NJ1009")

    assert exact.peer_ids == fallback.peer_ids
    assert exact.matching_strategy is MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL
    assert fallback.matching_strategy is MatchingStrategy.DEPARTMENT
    assert fallback.confidence < exact.confidence


def test_terminated_employees_are_never_peers(db_session: Session) -> None:
    """A leaver's access must not shape a joiner's recommendation.

    The client's extract has no lifecycle column, so every identity loads as
    ACTIVE and no leaver exists to exclude. The guarantee is still the system's,
    so the test creates one rather than leaving the path uncovered.
    """
    db_session.add(
        Employee(
            employee_id="EMP900",
            name="Departed Analyst",
            department="Finance",
            job_role="Financial Analyst",
            job_level="L2",
            location="Bangalore",
            employment_status=EmploymentStatus.TERMINATED.value,
            employment_type="EMPLOYEE",
        )
    )
    db_session.flush()

    result = PeerAnalysisService(db_session).find_peers("NJ1001")
    assert "EMP900" not in result.peer_ids
    assert result.peer_count == 5


def test_other_joiners_are_never_peers(db_session: Session) -> None:
    """A PENDING_START identity has no access history to learn from.

    NJ1002 and NJ1003 are Financial Analysts too; if they were eligible peers,
    one joiner's recommendation would start feeding another's.
    """
    result = PeerAnalysisService(db_session).find_peers("NJ1001")
    joiner_ids = {
        e.employee_id
        for e in db_session.query(Employee).filter(
            Employee.employment_status == EmploymentStatus.PENDING_START.value
        )
    }
    assert {"NJ1002", "NJ1003"} <= joiner_ids
    assert not (set(result.peer_ids) & joiner_ids)


def test_the_joiner_is_not_their_own_peer(db_session: Session) -> None:
    result = PeerAnalysisService(db_session).find_peers("NJ1001")
    assert "NJ1001" not in result.peer_ids


def test_no_peers_returns_an_empty_result_rather_than_raising(
    db_session: Session,
) -> None:
    """NJ1008 is an HR Specialist and the corpus contains no HR identities.

    This is the honest case, and it is real rather than contrived: every
    strategy is tried, none matches, and nothing unrelated is substituted.
    """
    result = PeerAnalysisService(db_session).find_peers("NJ1008")
    assert result.matching_strategy is MatchingStrategy.NONE
    assert result.peer_count == 0
    assert result.peer_ids == []
    assert result.confidence == 0.0
    assert result.sufficient is False
    assert len(result.strategies_attempted) == 4, "every strategy must be tried first"


def test_a_lone_peer_is_not_a_sufficient_cohort(db_session: Session) -> None:
    """NJ1006 matches exactly, but against a single Risk Analyst.

    One peer is an exact match and still weak evidence; the result must say it
    is insufficient even though a strategy succeeded.
    """
    result = PeerAnalysisService(db_session).find_peers("NJ1006")
    assert result.matching_strategy is MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL
    assert result.peer_ids == ["EMP009"]
    assert result.sufficient is False


def test_unknown_employee_raises(db_session: Session) -> None:
    with pytest.raises(EmployeeNotFoundError):
        PeerAnalysisService(db_session).find_peers("DOES_NOT_EXIST")


def test_peers_share_the_matched_attributes(db_session: Session) -> None:
    result = PeerAnalysisService(db_session).find_peers("NJ1004")
    assert result.peer_count == 3
    for peer in result.peers:
        assert peer.department == "Technology"
        assert peer.job_role == "Software Engineer"
        assert peer.job_level == "L2"
