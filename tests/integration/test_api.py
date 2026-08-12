"""REST API tests, driven through the real ASGI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.integration

PREFIX = "/api/v1"


@pytest.fixture
def client(app_session_factory) -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Health and documentation
# --------------------------------------------------------------------------- #
def test_health_reports_the_database(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert "demo_mode" in body and "llm_enabled" in body


def test_openapi_documentation_is_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200

    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in (
        f"{PREFIX}/health",
        f"{PREFIX}/joiners",
        f"{PREFIX}/joiners/{{employee_id}}",
        f"{PREFIX}/joiners/{{employee_id}}/analyze",
        f"{PREFIX}/analyses/{{analysis_id}}",
        f"{PREFIX}/access-requests",
        f"{PREFIX}/dashboard",
    ):
        assert path in paths, f"undocumented endpoint: {path}"


def test_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/health", headers={"X-Correlation-Id": "abc-123"})
    assert response.headers["X-Correlation-Id"] == "abc-123"


# --------------------------------------------------------------------------- #
# Joiners
# --------------------------------------------------------------------------- #
def test_list_joiners(client: TestClient) -> None:
    body = client.get(f"{PREFIX}/joiners").json()
    assert body["count"] == 10
    assert all(j["employment_status"] == "PENDING_START" for j in body["joiners"])


def test_get_joiner(client: TestClient) -> None:
    body = client.get(f"{PREFIX}/joiners/NJ1001").json()
    assert body["name"] == "Rahul Sharma"
    assert body["job_role"] == "Financial Analyst"


def test_unknown_joiner_returns_404_with_a_structured_error(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/joiners/NOPE")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "employee_not_found"
    assert body["details"]["employee_id"] == "NOPE"


def test_analyzing_an_unknown_joiner_returns_404(client: TestClient) -> None:
    """The domain error must survive the workflow and the MCP hop intact."""
    response = client.post(f"{PREFIX}/joiners/NOPE/analyze", json={})
    assert response.status_code == 404
    assert response.json()["error"] == "employee_not_found"


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def test_analyze_returns_the_complete_journey(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/joiners/NJ1001/analyze", json={})
    assert response.status_code == 200
    body = response.json()

    for field in (
        "analysis_id",
        "employee",
        "peer_analysis",
        "recommendations",
        "risk_results",
        "policy_results",
        "sod_results",
        "explanation",
        "sailpoint_payload",
    ):
        assert field in body, f"missing response section: {field}"

    assert body["status"] == "COMPLETED"
    assert body["peer_analysis"]["peer_count"] == 5
    assert body["recommendations"]
    assert body["sailpoint_payload"]["status"] == "SIMULATED"
    assert body["summary"]["AUTO_APPROVED"] > 0


def test_analysis_can_be_retrieved_after_the_fact(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/joiners/NJ1007/analyze", json={}).json()
    fetched = client.get(f"{PREFIX}/analyses/{created['analysis_id']}").json()

    assert fetched["analysis_id"] == created["analysis_id"]
    assert len(fetched["recommendations"]) == len(created["recommendations"])
    assert {r["entitlement_id"]: r["recommendation_status"] for r in fetched["recommendations"]} == {
        r["entitlement_id"]: r["recommendation_status"] for r in created["recommendations"]
    }


def test_unknown_analysis_returns_404(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/analyses/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"] == "analysis_not_found"


def test_analysis_list(client: TestClient) -> None:
    client.post(f"{PREFIX}/joiners/NJ1004/analyze", json={})
    body = client.get(f"{PREFIX}/analyses?limit=5").json()
    assert body["count"] >= 1
    assert "matching_strategy" in body["analyses"][0]


# --------------------------------------------------------------------------- #
# Access requests
# --------------------------------------------------------------------------- #
def test_access_request_excludes_withheld_entitlements(client: TestClient) -> None:
    """NJ1007 has both an auto-approval and two human-review holds in one run."""
    analysis = client.post(f"{PREFIX}/joiners/NJ1007/analyze", json={}).json()
    response = client.post(
        f"{PREFIX}/access-requests", json={"analysis_id": analysis["analysis_id"]}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SIMULATED"

    requested = {e["entitlement"] for e in body["payload"]["requested_entitlements"]}
    withheld = {
        r["entitlement_id"]
        for r in analysis["recommendations"]
        if r["recommendation_status"]
        in ("BLOCKED", "REJECTED", "HUMAN_REVIEW", "NOT_RECOMMENDED")
    }
    assert withheld, "this fixture is only meaningful if something was withheld"
    assert requested, "...and only meaningful if something else got through"
    assert not (requested & withheld)


def test_access_request_for_unknown_analysis_returns_404(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/access-requests",
        json={"analysis_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 404


def test_malformed_access_request_is_rejected(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/access-requests", json={})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_reports_every_required_metric(client: TestClient) -> None:
    client.post(f"{PREFIX}/joiners/NJ1007/analyze", json={})
    body = client.get(f"{PREFIX}/dashboard").json()

    for metric in (
        "total_joiners",
        "total_analyses",
        "total_recommendations",
        "auto_approved",
        "manager_approval",
        "human_review",
        "blocked",
        "high_risk",
        "critical_risk",
    ):
        assert metric in body, f"missing dashboard metric: {metric}"

    assert body["total_joiners"] == 10
    assert body["total_analyses"] >= 1
    # Nothing in the client's extract can produce BLOCKED: their only policies
    # are risk thresholds, and no identity holds either side of an SoD pair.
    # Human review is the strongest outcome this corpus reaches.
    assert body["blocked"] == 0
    assert body["human_review"] >= 1


def test_request_size_is_bounded(client: TestClient) -> None:
    oversized = {"analysis_id": "x" * 2_000_000}
    response = client.post(f"{PREFIX}/access-requests", json=oversized)
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
