"""LangGraph workflow: node execution, persistence and failure behaviour.

Driven against the client's corpus. NJ1001 is the client's own worked example
and is used wherever a clean, fully-recommended run is needed; NJ1007 is the
one joiner whose access reaches human review, and NJ1008 the one with no peers
at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.agents.graph import WORKFLOW_STEPS, build_graph, get_graph, run_analysis
from app.agents.mcp_bridge import McpToolInvoker
from app.agents.state import initial_state
from app.db.repositories.analysis_repo import AnalysisRepository
from app.domain.enums import AnalysisStatus, ApprovalTier, RecommendationStatus
from app.domain.exceptions import EmployeeNotFoundError
from app.services.analysis_service import AnalysisService

pytestmark = pytest.mark.integration

# Statuses that must never appear in a provisioning payload.
NOT_PROVISIONABLE = {
    RecommendationStatus.BLOCKED,
    RecommendationStatus.REJECTED,
    RecommendationStatus.NOT_RECOMMENDED,
    RecommendationStatus.HUMAN_REVIEW,
}


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
    state = initial_state("NJ1001", str(uuid.uuid4()), "corr-test", datetime.now(timezone.utc))
    with McpToolInvoker(mode="inmemory") as invoker:
        final = get_graph().invoke(state, config={"configurable": {"mcp_invoker": invoker}})

    assert [s for s in final["steps_completed"] if not s.endswith(":FAILED")] == list(
        WORKFLOW_STEPS
    )
    assert final["persisted"] is True
    assert final["errors"] == []


def test_workflow_reaches_its_capabilities_over_mcp(app_session_factory) -> None:
    """The workflow must actually go through MCP, not around it."""
    state = initial_state("NJ1001", str(uuid.uuid4()), "corr-mcp", datetime.now(timezone.utc))
    with McpToolInvoker(mode="inmemory") as invoker:
        final = get_graph().invoke(state, config={"configurable": {"mcp_invoker": invoker}})

    assert set(final["mcp_tool_calls"]) == {
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
    result = run_analysis("NJ1001", mcp_client_mode="inmemory")

    assert result.status is AnalysisStatus.COMPLETED
    assert result.employee is not None
    assert result.peer_analysis is not None and result.peer_analysis.peer_count == 5
    assert result.affinity is not None
    assert result.decisions
    assert result.explanation is not None
    assert result.sailpoint_payload is not None
    assert result.errors == []

    with app_session_factory() as session:
        stored = AnalysisService(session).get_analysis(result.analysis_id)
    assert stored.analysis_id == result.analysis_id
    assert len(stored.decisions) == len(result.decisions)


def test_the_clients_worked_example_reproduces(app_session_factory) -> None:
    """NJ1001 is the walkthrough in the client's own POC document.

    It expects SAP_FIN_DISPLAY, SAP_AP_INVOICE and POWERBI_FINANCE, with
    FIN_SHAREPOINT excluded at 20% affinity.
    """
    result = run_analysis("NJ1001", mcp_client_mode="inmemory")
    recommended = {
        d.entitlement_id
        for d in result.decisions
        if d.recommendation_status is RecommendationStatus.AUTO_APPROVED
    }
    assert recommended == {"SAP_FIN_DISPLAY", "SAP_AP_INVOICE", "POWERBI_FINANCE"}

    excluded = next(d for d in result.decisions if d.entitlement_id == "FIN_SHAREPOINT")
    assert excluded.recommendation_status is RecommendationStatus.NOT_RECOMMENDED
    assert excluded.affinity_score == 20.0

    payload = {e.entitlement for e in result.sailpoint_payload.requested_entitlements}
    assert payload == recommended


def test_persisted_audit_trail_is_complete(app_session_factory) -> None:
    result = run_analysis("NJ1007", mcp_client_mode="inmemory")

    with app_session_factory() as session:
        row = AnalysisRepository(session).get_row(result.analysis_id)
        assert row is not None
        assert row.matching_strategy == "job_role_department_job_level"
        assert row.peer_count == 1
        assert row.peer_ids == ["EMP010"]
        assert row.correlation_id

        held = [
            r
            for r in row.recommendations
            if r.recommendation_status == RecommendationStatus.HUMAN_REVIEW.value
        ]
        assert held, "NJ1007's peer holds high-risk audit tooling"
        for rec in held:
            assert rec.policy_results, "a held recommendation must record which control held it"
            assert rec.decision_trace

        for rec in row.recommendations:
            assert rec.explanation is not None
            assert rec.explanation.narrative
            assert rec.explanation.structured_explanation


def test_unprovisionable_entitlements_never_reach_the_sailpoint_payload(
    app_session_factory,
) -> None:
    """NJ1007 has both a human-review hold and an auto-approval in one run."""
    result = run_analysis("NJ1007", mcp_client_mode="inmemory")
    requested = {e.entitlement for e in result.sailpoint_payload.requested_entitlements}
    withheld = {
        d.entitlement_id
        for d in result.decisions
        if d.recommendation_status in NOT_PROVISIONABLE
    }
    assert withheld, "this fixture is only meaningful if something was withheld"
    assert requested, "...and only meaningful if something else got through"
    assert not (requested & withheld)
    assert result.sailpoint_payload.status == "SIMULATED"


def test_an_unscored_entitlement_fails_closed_end_to_end(app_session_factory) -> None:
    """SHAREPOINT_AUDIT is in nobody's risk file, so it loads as 100/CRITICAL.

    It has 100% peer affinity and would otherwise sail through. The whole point
    of failing closed is that an unscored entitlement is not a safe one, and
    that has to survive the entire workflow, not just the loader.
    """
    result = run_analysis("NJ1007", mcp_client_mode="inmemory")
    unscored = next(d for d in result.decisions if d.entitlement_id == "SHAREPOINT_AUDIT")

    assert unscored.affinity_score == 100.0, "held back on risk, not on affinity"
    assert unscored.risk_score == 100
    assert unscored.recommendation_status is RecommendationStatus.HUMAN_REVIEW


def test_risk_threshold_policy_flows_through_the_whole_workflow(
    app_session_factory,
) -> None:
    """RSA_GRC scores exactly 70, the inclusive bottom of the HIGH band.

    POL005 fires (`risk_score >= 70`) and routes to the line manager rather than
    to governance review - 70 sits inside HIGH, not CRITICAL.
    """
    result = run_analysis("NJ1006", mcp_client_mode="inmemory")
    grc = next(d for d in result.decisions if d.entitlement_id == "RSA_GRC")

    assert grc.affinity_score == 100.0, "routed on risk, not on affinity"
    assert grc.risk_score == 70
    assert grc.recommendation_status is RecommendationStatus.MANAGER_APPROVAL
    assert grc.approval_tier is ApprovalTier.MANAGER
    assert grc.policy_result is not None
    assert "POL005" in {p.policy_id for p in grc.policy_result.failed_policies}


def test_fallback_peer_matching_flows_through(app_session_factory) -> None:
    """NJ1010 is a Senior Financial Analyst; only the department matches."""
    result = run_analysis("NJ1010", mcp_client_mode="inmemory")
    assert result.peer_analysis.matching_strategy.value == "department"
    assert all(
        d.matching_strategy.value == "department" for d in result.decisions
    ), "every recommendation must record the strategy it came from"


def test_a_joiner_with_no_peers_recommends_nothing(app_session_factory) -> None:
    """NJ1008 is an HR Specialist and the corpus has no HR identities.

    Nothing unrelated may be substituted, and the reason has to be recorded
    rather than the run simply producing an empty result.
    """
    result = run_analysis("NJ1008", mcp_client_mode="inmemory")
    assert result.status is AnalysisStatus.FAILED
    assert result.decisions == []
    assert any("no peer group" in e for e in result.errors)


def test_a_thin_peer_group_is_flagged_but_still_analysed(app_session_factory) -> None:
    """NJ1006 matches exactly, but against a single Risk Analyst."""
    result = run_analysis("NJ1006", mcp_client_mode="inmemory")
    assert result.status is AnalysisStatus.COMPLETED_WITH_WARNINGS
    assert result.decisions, "a thin peer group still yields an analysis"
    assert any("below the configured minimum" in e for e in result.errors)


def test_unknown_employee_fails_fast(app_session_factory) -> None:
    """No identity means no analysis - and nothing written."""
    with pytest.raises(EmployeeNotFoundError):
        run_analysis("NO_SUCH_EMPLOYEE", mcp_client_mode="inmemory")


def test_workflow_runs_identically_in_direct_mode(app_session_factory) -> None:
    """Same decisions whether or not the MCP transport is in the path."""
    via_mcp = run_analysis("NJ1001", mcp_client_mode="inmemory")
    via_direct = run_analysis("NJ1001", mcp_client_mode="direct")

    def fingerprint(result):
        return sorted(
            (d.entitlement_id, d.recommendation_status.value, d.affinity_score)
            for d in result.decisions
        )

    assert fingerprint(via_mcp) == fingerprint(via_direct)


def test_repeated_analyses_are_deterministic(app_session_factory) -> None:
    first = run_analysis("NJ1004", mcp_client_mode="inmemory")
    second = run_analysis("NJ1004", mcp_client_mode="inmemory")
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

    result = run_analysis("NJ1001", mcp_client_mode="direct")

    assert result.decisions, "decisions must survive an explanation failure"
    assert result.explanation is None
    assert result.status is AnalysisStatus.COMPLETED_WITH_WARNINGS
    assert any("generate_explanation" in e for e in result.errors)

    # And the surviving decisions were still written to the database.
    with app_session_factory() as session:
        stored = AnalysisService(session).get_analysis(result.analysis_id)
    assert len(stored.decisions) == len(result.decisions)


def test_high_risk_routes_to_the_manager_and_low_risk_auto_approves(
    app_session_factory,
) -> None:
    """The joiner routing rule, end to end.

    Low and medium risk are ticketed automatically; high risk is ticketed but
    flagged for the line manager; critical is withheld for human review. NJ1007
    is the one joiner whose three entitlements span all three outcomes.
    """
    result = run_analysis("NJ1007", mcp_client_mode="inmemory")
    by_id = {d.entitlement_id: d for d in result.decisions}

    low = by_id["POWERBI_AUDIT"]                   # risk 10
    assert low.recommendation_status is RecommendationStatus.AUTO_APPROVED
    assert low.approval_tier is ApprovalTier.AUTO

    high = by_id["AUDIT_TOOL"]                     # risk 75, HIGH band
    assert high.recommendation_status is RecommendationStatus.MANAGER_APPROVAL
    assert high.approval_tier is ApprovalTier.MANAGER

    critical = by_id["SHAREPOINT_AUDIT"]           # risk 100, CRITICAL band
    assert critical.recommendation_status is RecommendationStatus.HUMAN_REVIEW

    # Both the auto-approved and the manager-tier entitlement are raised on the
    # ticket; only the critical one is withheld.
    metadata = result.sailpoint_payload.metadata
    requested = {e.entitlement for e in result.sailpoint_payload.requested_entitlements}
    assert requested == {"POWERBI_AUDIT", "AUDIT_TOOL"}
    assert metadata["manager_approval_required"] == ["AUDIT_TOOL"]


def test_a_manager_tier_request_names_an_approver(app_session_factory) -> None:
    """`MGR400` is not an identity in the extract, so the FK cannot hold it.

    Routing to a manager is meaningless without one, so the source-system id is
    preserved separately and reported on the payload.
    """
    result = run_analysis("NJ1007", mcp_client_mode="inmemory")
    metadata = result.sailpoint_payload.metadata

    assert metadata["manager_id"] is None, "MGR400 is not an identity in the corpus"
    assert metadata["manager_external_id"] == "MGR400"
    assert metadata["manager_approval_required"], "an approver is only needed if something needs it"
