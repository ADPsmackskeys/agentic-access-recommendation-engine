#!/usr/bin/env python
"""End-to-end demonstration of the access recommendation journey.

Runs the complete workflow for one new joiner and prints every governance step,
then demonstrates live MCP tool discovery and invocation over a real MCP
session against a subprocess server.

Usage:
    python scripts/run_demo.py                      # default joiner (NJ1001)
    python scripts/run_demo.py --employee NJ1007    # human-review scenario
    python scripts/run_demo.py --employee NJ1008    # no peers: recommends nothing
    python scripts/run_demo.py --all                # every seeded joiner
    python scripts/run_demo.py --skip-mcp-demo      # workflow only
    python scripts/run_demo.py --mcp-mode stdio     # run workflow over stdio MCP
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.graph import WORKFLOW_STEPS, run_analysis  # noqa: E402
from app.agents.mcp_bridge import McpToolInvoker  # noqa: E402
from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.db.session import check_database_health, read_session  # noqa: E402
from app.domain.enums import RecommendationStatus  # noqa: E402
from app.domain.models import AnalysisResult  # noqa: E402
from app.logging import configure_logging  # noqa: E402
from app.services.analysis_service import AnalysisService  # noqa: E402

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH

# Reviewer-facing ordering: what needs attention comes first.
STATUS_ORDER = {
    RecommendationStatus.BLOCKED: 0,
    RecommendationStatus.REJECTED: 1,
    RecommendationStatus.HUMAN_REVIEW: 2,
    RecommendationStatus.MANAGER_APPROVAL: 3,
    RecommendationStatus.AUTO_APPROVED: 4,
    RecommendationStatus.NOT_RECOMMENDED: 5,
}


def heading(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def print_banner() -> None:
    settings = get_settings()
    print(RULE)
    print("AGENTIC ACCESS RECOMMENDATION ENGINE".center(WIDTH))
    print(RULE)
    print(f"Environment       : {settings.environment}")
    print(f"Database          : {settings.safe_database_url()}")
    print(f"Demo mode         : {settings.demo_mode}")
    print(f"LLM               : {'enabled' if settings.llm_enabled else 'deterministic (no LLM)'}")
    print(f"MCP client mode   : {settings.mcp_client_mode}")
    print(f"Affinity threshold: {settings.affinity_threshold}%")


def print_employee(result: AnalysisResult) -> None:
    employee = result.employee
    if employee is None:
        return
    heading("NEW JOINER")
    print(f"Employee   : {employee.employee_id} - {employee.name}")
    print(f"Role       : {employee.job_role}")
    print(f"Department : {employee.department}")
    print(f"Level      : {employee.job_level}")
    print(f"Location   : {employee.location}")
    print(f"Type       : {employee.employment_type.value}")
    print(f"Manager    : {employee.manager_id or '-'}")


def print_steps() -> None:
    heading("WORKFLOW")
    total = len(WORKFLOW_STEPS)
    for index, step in enumerate(WORKFLOW_STEPS, start=1):
        print(f"  [{index}/{total}] {step.replace('_', ' ')}")


def print_peer_analysis(result: AnalysisResult) -> None:
    peers = result.peer_analysis
    if peers is None:
        return
    heading("PEER ANALYSIS")
    print(f"Matching strategy : {peers.matching_strategy.value}")
    print(f"Strategies tried  : {', '.join(s.value for s in peers.strategies_attempted)}")
    print(f"Peers matched     : {peers.peer_count}")
    print(f"Confidence        : {peers.confidence}")
    if peers.notes:
        print(f"Note              : {peers.notes}")
    if peers.peers:
        print("\nPeer group:")
        for peer in peers.peers:
            print(
                f"  {peer.employee_id:8s} {peer.name:24s} {peer.job_role:28s} "
                f"{peer.entitlement_count:2d} entitlements"
            )


def print_recommendations(result: AnalysisResult) -> None:
    heading("RECOMMENDATIONS")
    if not result.decisions:
        print("No candidate entitlements were produced.")
        return

    header = (
        f"{'ENTITLEMENT':28s} {'AFFINITY':>9s} {'RISK':>10s} "
        f"{'POLICY':14s} {'SOD':10s} DECISION"
    )
    print(header)
    print(THIN)
    for decision in sorted(
        result.decisions,
        key=lambda d: (STATUS_ORDER[d.recommendation_status], -d.affinity_score),
    ):
        affinity = f"{decision.affinity_score:.1f}%"
        risk = f"{decision.risk_score}/{decision.risk_level.value}"
        print(
            f"{decision.entitlement_id:28s} {affinity:>9s} {risk:>10s} "
            f"{decision.policy_status.value:14s} {decision.sod_status.value:10s} "
            f"{decision.recommendation_status.value}"
        )

    summary: dict[str, int] = {}
    for decision in result.decisions:
        key = decision.recommendation_status.value
        summary[key] = summary.get(key, 0) + 1
    print(THIN)
    print("Summary: " + ", ".join(f"{count} {status}" for status, count in sorted(summary.items())))


def print_controls(result: AnalysisResult) -> None:
    blocked = [
        d
        for d in result.decisions
        if d.recommendation_status
        in (
            RecommendationStatus.BLOCKED,
            RecommendationStatus.REJECTED,
            RecommendationStatus.HUMAN_REVIEW,
        )
    ]
    if not blocked:
        return
    heading("CONTROLS THAT FIRED")
    for decision in blocked:
        print(f"\n{decision.entitlement_id} -> {decision.recommendation_status.value}")
        print(f"  {decision.reason}")
        for conflict in decision.sod_conflicts:
            print(f"  SoD  {conflict.sod_id} [{conflict.severity.value}] {conflict.name}")
        if decision.policy_result:
            for policy in decision.policy_result.failed_policies:
                print(f"  Pol  {policy.policy_id} [{policy.status.value}] {policy.policy_name}")


def print_explanation(result: AnalysisResult) -> None:
    if result.explanation is None:
        return
    heading("EXPLANATION")
    print(f"Generator: {result.explanation.generator.value}")
    if result.explanation.model:
        print(f"Model    : {result.explanation.model}")
    print(f"\n{result.explanation.summary}\n")
    for explanation in result.explanation.recommendations[:3]:
        print(THIN)
        print(explanation.narrative)
    if len(result.explanation.recommendations) > 3:
        remaining = len(result.explanation.recommendations) - 3
        print(THIN)
        print(f"... {remaining} further explanation(s) persisted with the analysis.")


def print_sailpoint(result: AnalysisResult) -> None:
    payload = result.sailpoint_payload
    if payload is None:
        return
    heading("SAILPOINT REQUEST PAYLOAD (SIMULATED)")
    print(json.dumps(payload.model_dump(mode="json"), indent=2, default=str))
    print(
        f"\n{len(payload.excluded_entitlements)} entitlement(s) were withheld and are NOT "
        f"present in the request above."
    )


def demo_mcp(employee_id: str) -> None:
    """Discover and invoke MCP tools over a real out-of-process session."""
    heading("MCP TOOL DEMONSTRATION (stdio transport, subprocess server)")
    print("Client: fastmcp.Client   Server: python -m app.mcp.server --transport stdio\n")

    try:
        with McpToolInvoker(mode="stdio") as invoker:
            tools = invoker.list_tools()
            print(f"[discovery] tools/list returned {len(tools)} tools:")
            for name in tools:
                print(f"             - {name}")

            print(f"\n[call] find_peer_employees(employee_id='{employee_id}')")
            peers = invoker.call("find_peer_employees", {"employee_id": employee_id})
            print(
                f"       -> strategy={peers['matching_strategy']} "
                f"peers={peers['peer_count']} confidence={peers['confidence']}"
            )

            print(f"\n[call] calculate_entitlement_affinity(peer_ids=[{peers['peer_count']} ids])")
            affinity = invoker.call(
                "calculate_entitlement_affinity",
                {
                    "employee_id": employee_id,
                    "peer_ids": peers["peer_ids"],
                    "matching_strategy": peers["matching_strategy"],
                },
            )
            above = [c for c in affinity["candidates"] if c["meets_threshold"]]
            print(f"       -> {len(affinity['candidates'])} candidates, {len(above)} above threshold")
            for candidate in above[:5]:
                print(
                    f"          {candidate['entitlement_id']:28s} "
                    f"{candidate['affinity_score']:6.2f}% "
                    f"({candidate['peer_count']}/{candidate['total_peers']})"
                )

            entitlement_ids = [c["entitlement_id"] for c in above]
            print(f"\n[call] evaluate_entitlement_risk({len(entitlement_ids)} entitlements)")
            risks = invoker.call(
                "evaluate_entitlement_risk", {"entitlement_ids": entitlement_ids}
            )
            for assessment in risks[:5]:
                print(
                    f"       -> {assessment['entitlement_id']:28s} "
                    f"{assessment['risk_score']:3d} {assessment['risk_level']}"
                )

            print("\n[call] check_sod_conflicts(...)")
            sod = invoker.call(
                "check_sod_conflicts",
                {"employee_id": employee_id, "entitlement_ids": entitlement_ids},
            )
            print(f"       -> status={sod['status']} severity={sod['severity']}")
            for conflict in sod["conflicts"]:
                print(
                    f"          {conflict['sod_id']} [{conflict['severity']}] "
                    f"{conflict['entitlement_1']} + {conflict['entitlement_2']}"
                )

            print("\n[call] validate_entitlement_policy(...)")
            policy = invoker.call(
                "validate_entitlement_policy",
                {"employee_id": employee_id, "entitlement_ids": entitlement_ids},
            )
            print(
                f"       -> status={policy['status']} "
                f"evaluated={len(policy['evaluated_policy_ids'])} "
                f"skipped(disabled)={policy['skipped_policy_ids']}"
            )
            print(f"\n[total] {len(invoker.tool_calls)} MCP tool calls over one MCP session.")
    except Exception as exc:
        print(f"\nMCP demonstration failed: {exc}")
        print("The workflow results above are unaffected.")


def run_for(employee_id: str, mcp_mode: str | None) -> AnalysisResult:
    print_steps()
    result = run_analysis(employee_id, mcp_client_mode=mcp_mode)  # type: ignore[arg-type]
    print_employee(result)
    print_peer_analysis(result)
    print_recommendations(result)
    print_controls(result)
    print_explanation(result)
    print_sailpoint(result)

    heading("PERSISTENCE")
    print(f"Analysis id   : {result.analysis_id}")
    print(f"Correlation id: {result.correlation_id}")
    print(f"Status        : {result.status.value}")
    if result.errors:
        print("Warnings      :")
        for error in result.errors:
            print(f"  - {error}")
    print(f"\nRetrieve with : GET /api/v1/analyses/{result.analysis_id}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--employee", default="NJ1001", help="Joiner to analyse.")
    parser.add_argument("--all", action="store_true", help="Analyse every seeded joiner.")
    parser.add_argument("--skip-mcp-demo", action="store_true", help="Skip the MCP walkthrough.")
    parser.add_argument(
        "--mcp-mode",
        choices=["inmemory", "stdio", "http", "direct"],
        default=None,
        help="How the workflow reaches the MCP tools (defaults to MCP_CLIENT_MODE).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress structured logs.")
    args = parser.parse_args()

    if args.quiet:
        # Set the variable, not just the local logger: the MCP demonstration
        # spawns a server subprocess that reads its own log level from the
        # environment, and its logs would otherwise interleave with the output.
        os.environ["LOG_LEVEL"] = "ERROR"
        reset_settings_cache()
    configure_logging(level="ERROR" if args.quiet else None, json_output=False)
    print_banner()

    db_ok, db_error = check_database_health()
    if not db_ok:
        print(f"\nDatabase is not reachable: {db_error}")
        print("Run `alembic upgrade head` and `python scripts/seed_database.py` first.")
        return 1

    if args.all:
        with read_session() as session:
            employee_ids = [j.employee_id for j in AnalysisService(session).list_joiners()]
    else:
        employee_ids = [args.employee]

    last_id = employee_ids[-1]
    for employee_id in employee_ids:
        try:
            run_for(employee_id, args.mcp_mode)
        except Exception as exc:
            print(f"\nAnalysis failed for {employee_id}: {exc}")
            if not args.all:
                return 1

    if not args.skip_mcp_demo:
        demo_mcp(last_id)

    heading("DEMO COMPLETE")
    print("Next:")
    print("  uvicorn app.main:app --reload        # REST API + MCP at /mcp")
    print("  python -m app.mcp.server             # standalone MCP server (stdio)")
    print("  pytest                               # full test suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
