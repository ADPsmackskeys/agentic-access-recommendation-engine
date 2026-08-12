"""LangGraph workflow: node execution, persistence and failure behaviour."""

from __future__ import annotations

import pytest

from app.agents.graph import WORKFLOW_STEPS, build_graph, get_graph, run_analysis
from app.agents.mcp_bridge import McpToolInvoker
from app.agents.state import initial_state
from app.db.repositories.analysis_repo import AnalysisRepository
from app.domain.enums import AnalysisStatus, RecommendationStatus
from app.domain.exceptions import EmployeeNotFoundError
from app.services.analysis_service import AnalysisService

pytestmark = pytest.mark.integration


def test_graph_declares_the_expected_steps() -> None:
    assert WORKFLOW_STEPS == (
        "load_joiner",
        "profile_joiner",
        "find_peers",
        "calculate_affinity",
        "evaluate_risk",
        "validate_policies",
        "check_sod",
        "make_decision",
        "generate_explanation",
        "generate_sailpoint_payload",
        "persist_analysis",
    )
    assert set(build_graph().get_graph().nodes) >= set(WORKFLOW_STEPS)


def test_graph_compilation_is_cached() -> None:
    assert get_graph() is get_graph()


def test_workflow_executes_every_node(app_session_factory) -> None:
    """Drive the compiled graph directly to inspect the terminal state."""
    import uuid
    from datetime import datetime, timezone

    state = initial_state("EMP1001", str(uuid.uuid4()), "corr-test",
                          datetime.now(timezone.utc))
    with McpToolInvoker(mode="inmemory") as invoker:
        final = get_graph().invoke(
            state, config={"configurable": {"mcp_invoker": invoker}}
        )

    assert [s for s in final["steps_completed"] if not s.endswith(":FAILED")] == list(
        WORKFLOW_STEPS
    )
    assert final["persisted"] is True
    assert final["errors"] == []


def test_workflow_reaches_its_capabilities_over_mcp(app_session_factory) -> None:
    """The workflow must actually go through MCP, not around it."""
    import uuid
    from datetime import datetime, timezone

    state = initial_state("EMP1001", str(uuid.uuid4()), "corr-mcp",
                          datetime.now(timezone.utc))
    with McpToolInvoker(mode="inmemory") as invoker:
        final = get_graph().invoke(
            state, config={"configurable": {"mcp_invoker": invoker}}
        )

    called = set(final["mcp_tool_calls"])
    assert called == {
        "get_joiner",
        "find_peer_employees",
        "calculate_entitlement_affinity",
        "evaluate_entitlement_risk",
        "validate_entitlement_policy",
        "check_sod_conflicts",
        "generate_access_explanation",
        "generate_sailpoint_request",
    }


def test_run_analysis_produces_and_persists_a_complete_result(
    app_session_factory,
) -> None:
    result = run_analysis("EMP1001", mcp_client_mode="inmemory")

    assert result.status is AnalysisStatus.COMPLETED
    assert result.employee is not None
    assert result.peer_analysis is not None and result.peer_analysis.peer_count == 8
    assert result.affinity is not None
    assert result.decisions
    assert result.explanation is not None
    assert result.sailpoint_payload is not None
    assert result.errors == []

    with app_session_factory() as session:
        stored = AnalysisService(session).get_analysis(result.analysis_id)
    assert stored.analysis_id == result.analysis_id
    assert len(stored.decisions) == len(result.decisions)


def test_persisted_audit_trail_is_complete(app_session_factory) -> None:
    result = run_analysis("EMP1002", mcp_client_mode="inmemory")

    with app_session_factory() as session:
        row = AnalysisRepository(session).get_row(result.analysis_id)
        assert row is not None
        assert row.matching_strategy == "job_role_department_job_level"
        assert row.peer_count == 6
        assert row.peer_ids and len(row.peer_ids) == 6
        assert row.correlation_id

        blocked = [
            r for r in row.recommendations
            if r.recommendation_status == RecommendationStatus.BLOCKED.value
        ]
        assert blocked, "EMP1002's peer group carries a toxic pair"
        for rec in blocked:
            assert rec.sod_results, "a blocked recommendation must record why"
            assert rec.decision_trace

        for rec in row.recommendations:
            assert rec.explanation is not None
            assert rec.explanation.narrative
            assert rec.explanation.structured_explanation


def test_blocked_entitlements_never_reach_the_sailpoint_payload(
    app_session_factory,
) -> None:
    result = run_analysis("EMP1002", mcp_client_mode="inmemory")
    requested = {e.entitlement for e in result.sailpoint_payload.requested_entitlements}
    blocked = {
        d.entitlement_id
        for d in result.decisions
        if d.recommendation_status
        in (RecommendationStatus.BLOCKED, RecommendationStatus.REJECTED)
    }
    assert blocked, "this fixture is only meaningful if something was blocked"
    assert not (requested & blocked)
    assert result.sailpoint_payload.status == "SIMULATED"


def test_contractor_restriction_flows_through_the_whole_workflow(
    app_session_factory,
) -> None:
    result = run_analysis("EMP1004", mcp_client_mode="inmemory")
    pii = next(
        d for d in result.decisions if d.entitlement_id == "SNOWFLAKE_PII_READ"
    )
    assert pii.recommendation_status is RecommendationStatus.REJECTED
    assert pii.affinity_score >= 70.0, "rejected on policy, not on affinity"


def test_fallback_peer_matching_flows_through(app_session_factory) -> None:
    result = run_analysis("EMP1006", mcp_client_mode="inmemory")
    assert result.peer_analysis.matching_strategy.value == "department_job_level"
    assert all(
        d.matching_strategy.value == "department_job_level" for d in result.decisions
    ), "every recommendation must record the strategy it came from"


def test_unknown_employee_fails_fast(app_session_factory) -> None:
    """No identity means no analysis - and nothing written."""
    with pytest.raises(EmployeeNotFoundError):
        run_analysis("NO_SUCH_EMPLOYEE", mcp_client_mode="inmemory")


def test_workflow_runs_identically_in_direct_mode(app_session_factory) -> None:
    """Same decisions whether or not the MCP transport is in the path."""
    via_mcp = run_analysis("EMP1001", mcp_client_mode="inmemory")
    via_direct = run_analysis("EMP1001", mcp_client_mode="direct")

    def fingerprint(result):
        return sorted(
            (d.entitlement_id, d.recommendation_status.value, d.affinity_score)
            for d in result.decisions
        )

    assert fingerprint(via_mcp) == fingerprint(via_direct)


def test_repeated_analyses_are_deterministic(app_session_factory) -> None:
    first = run_analysis("EMP1003", mcp_client_mode="inmemory")
    second = run_analysis("EMP1003", mcp_client_mode="inmemory")
    assert first.analysis_id != second.analysis_id
    assert sorted(
        (d.entitlement_id, d.recommendation_status.value) for d in first.decisions
    ) == sorted(
        (d.entitlement_id, d.recommendation_status.value) for d in second.decisions
    )


def test_explanation_failure_does_not_lose_the_decisions(
    app_session_factory, monkeypatch
) -> None:
    """The specification's hard requirement, exercised end to end."""
    from app.mcp.tools import ALL_HANDLERS

    def explode(*args, **kwargs):
        raise RuntimeError("explanation subsystem is down")

    monkeypatch.setitem(ALL_HANDLERS, "generate_access_explanation", explode)

    result = run_analysis("EMP1001", mcp_client_mode="direct")

    assert result.decisions, "decisions must survive an explanation failure"
    assert result.explanation is None
    assert result.status is AnalysisStatus.COMPLETED_WITH_WARNINGS
    assert any("generate_explanation" in e for e in result.errors)

    # And the surviving decisions were still written to the database.
    with app_session_factory() as session:
        stored = AnalysisService(session).get_analysis(result.analysis_id)
    assert len(stored.decisions) == len(result.decisions)
