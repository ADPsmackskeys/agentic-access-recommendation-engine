"""Peer analysis against the real seeded database."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models.identity import Employee
from app.domain.enums import EmploymentStatus, MatchingStrategy
from app.domain.exceptions import EmployeeNotFoundError
from app.services.peer_service import PeerAnalysisService

pytestmark = pytest.mark.integration


def test_exact_match_finds_the_role_cohort(db_session: Session) -> None:
    result = PeerAnalysisService(db_session).find_peers("EMP1001")
    assert result.matching_strategy is MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL
    assert result.peer_count == 8
    assert result.peer_ids == [f"EMP{i:03d}" for i in range(1, 9)]
    assert result.confidence == 0.95
    assert result.sufficient is True


def test_fallback_relaxes_to_department_and_level(db_session: Session) -> None:
    """EMP1006 is a Senior Financial Analyst - a role nobody active holds."""
    result = PeerAnalysisService(db_session).find_peers("EMP1006")
    assert result.matching_strategy is MatchingStrategy.DEPARTMENT_JOB_LEVEL
    assert result.strategies_attempted[:2] == [
        MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        MatchingStrategy.JOB_ROLE_DEPARTMENT,
    ]
    assert result.peer_count == 3
    assert result.confidence < 0.95, "a fallback match must be less confident"
    assert result.notes and "fallback" in result.notes


def test_terminated_employees_are_never_peers(db_session: Session) -> None:
    """EMP045/046 are leavers holding toxic privileged access on purpose."""
    result = PeerAnalysisService(db_session).find_peers("EMP1001")
    assert "EMP045" not in result.peer_ids
    assert "EMP046" not in result.peer_ids


def test_other_joiners_are_never_peers(db_session: Session) -> None:
    """A PENDING_START identity has no access history to learn from."""
    result = PeerAnalysisService(db_session).find_peers("EMP1001")
    joiner_ids = {
        e.employee_id
        for e in db_session.query(Employee).filter(
            Employee.employment_status == EmploymentStatus.PENDING_START.value
        )
    }
    assert not (set(result.peer_ids) & joiner_ids)


def test_the_joiner_is_not_their_own_peer(db_session: Session) -> None:
    result = PeerAnalysisService(db_session).find_peers("EMP1001")
    assert "EMP1001" not in result.peer_ids


def test_no_peers_returns_an_empty_result_rather_than_raising(
    db_session: Session,
) -> None:
    """A department of one must yield nothing, not unrelated employees."""
    db_session.add(
        Employee(
            employee_id="EMP9999",
            name="Solo Founder",
            department="Astrophysics",
            job_role="Astronomer",
            job_level="L7",
            location="Atacama",
            employment_status=EmploymentStatus.PENDING_START.value,
            employment_type="EMPLOYEE",
        )
    )
    db_session.flush()

    result = PeerAnalysisService(db_session).find_peers("EMP9999")
    assert result.matching_strategy is MatchingStrategy.NONE
    assert result.peer_count == 0
    assert result.peer_ids == []
    assert result.confidence == 0.0
    assert result.sufficient is False
    assert len(result.strategies_attempted) == 4, "every strategy must be tried first"


def test_unknown_employee_raises(db_session: Session) -> None:
    with pytest.raises(EmployeeNotFoundError):
        PeerAnalysisService(db_session).find_peers("DOES_NOT_EXIST")


def test_peers_share_the_matched_attributes(db_session: Session) -> None:
    result = PeerAnalysisService(db_session).find_peers("EMP1002")
    assert result.peer_count == 6
    for peer in result.peers:
        assert peer.department == "Finance"
        assert peer.job_role == "Accounts Payable Analyst"
        assert peer.job_level == "L3"
