"""The engine must reproduce the client's own affinity output.

`seed/peer_affinity_scores.json` is labelled by the client as their Affinity
Engine's result. It is deliberately never loaded into the database: it is the
expected answer, and recomputing it from `identities` is what demonstrates the
two agree.

This is the strongest single check in the suite. It is also the argument for
computing affinity rather than consuming their table - a precomputed lookup has
no row for a role it has never seen, which is exactly the new-joiner case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.services.affinity_service import AffinityService
from app.services.peer_service import PeerAnalysisService

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[2] / "seed" / "peer_affinity_scores.json"

# One joiner standing in for each (job_role, department) group the client's
# table covers. The joiner is not a peer of itself, so the peer group it sees is
# exactly the set of identities the client aggregated.
PROBES = {
    ("Financial Analyst", "Finance"): "NJ1001",
    ("Software Engineer", "Technology"): "NJ1004",
    ("Risk Analyst", "Risk"): "NJ1006",
    ("Internal Auditor", "Audit"): "NJ1007",
}


def _expected() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_covers_every_group_we_probe() -> None:
    """Guards against the extract shrinking without anyone noticing."""
    groups = {(row["job_role"], row["department"]) for row in _expected()}
    assert groups == set(PROBES), (
        "peer_affinity_scores.json no longer matches the probes this test uses; "
        "re-run scripts/convert_client_csv.py and update PROBES"
    )
    assert len(_expected()) == 13


@pytest.mark.parametrize("row", _expected(), ids=lambda r: f"{r['job_role']}-{r['entitlement']}")
def test_engine_reproduces_the_clients_affinity_row(row: dict, db_session: Session) -> None:
    employee_id = PROBES[(row["job_role"], row["department"])]

    peers = PeerAnalysisService(db_session).find_peers(employee_id)
    affinity = AffinityService(db_session).calculate(employee_id, peers)
    candidates = {c.entitlement_id: c for c in affinity.candidates}

    candidate = candidates.get(row["entitlement"])
    assert candidate is not None, (
        f"{row['entitlement']} is in the client's affinity table but the engine "
        f"produced no candidate for {employee_id}"
    )
    assert candidate.peer_count == row["peer_count"]
    assert candidate.total_peers == row["total_peers"]
    # The client rounds to whole percentages (CONFLUENCE_USER is written as 67
    # where the engine holds 66.67), so agreement is asserted to within half a
    # point rather than exactly.
    assert candidate.affinity_score == pytest.approx(row["affinity_score"], abs=0.5)
