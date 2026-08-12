#!/usr/bin/env python
"""Load the client CSV extracts into the internal schema.

The client's files are a *source extract*, not the running system's model, so
this is the adapter between the two. Mapping decisions, all of them deliberate:

* `identities.csv` + `new_joiners.csv` -> `employees`, distinguished by
  `employment_status` (ACTIVE vs PENDING_START). Peer selection filters on that
  column, which is what stops one joiner shaping another joiner's access.
* `identities.entitlements` is a `;`-delimited string -> `employee_entitlements`
  rows. This is the holdings table the client's file list did not name.
* `entitlement_catalog.csv` + `entitlement_risk_scores.csv` -> `entitlements`,
  joined on the entitlement **name**. The catalogue's `ENT0xx` id is not used as
  a key anywhere else in the extract, so it is kept as `external_id` and the
  name becomes the primary key.
* Entitlements with no risk score are loaded as CRITICAL, not skipped. An
  entitlement nobody has scored is not automatically safe, and failing closed is
  the same stance the policy engine takes for a rule it cannot evaluate.
* `policy_rules.csv` carries an expression string; see `parse_policy_rule`.

Usage:
    python scripts/load_client_data.py
    python scripts/load_client_data.py --dir seed/client --purge
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.models.analysis import (  # noqa: E402
    JoinerAnalysis,
    PolicyResult,
    Recommendation,
    RecommendationEvidence,
    RecommendationExplanationRow,
    SailPointRequest,
    SodResult,
)
from app.db.models.governance import Policy, SodRule  # noqa: E402
from app.db.models.identity import Employee, EmployeeEntitlement, Entitlement  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.enums import EmploymentStatus, EmploymentType, PolicyType  # noqa: E402
from app.logging import configure_logging, get_logger  # noqa: E402

logger = get_logger("load_client_data")

# Risk assigned to an entitlement the client has not scored. Deliberately the
# top of the scale: unscored is not the same as safe.
UNSCORED_RISK = 100
UNSCORED_CATEGORY = "UNSCORED"

# --------------------------------------------------------------------------- #
# Policy rule parsing
# --------------------------------------------------------------------------- #
_BIRTHRIGHT = re.compile(r"^\s*(?P<role>[^-]+?)\s*->\s*(?P<entitlement>\S+)\s*$")
_THRESHOLD = re.compile(
    r"^\s*(?P<field>risk_score|affinity_score)\s*>=\s*(?P<value>\d+)\s*$"
)


def parse_policy_rule(policy_id: str, name: str, type_: str, rule: str) -> dict[str, Any] | None:
    """Translate one `policy_rules.csv` row into a typed policy.

    The client's `rule` column is a small expression language with three
    observed forms:

        "<job_role> -> <ENTITLEMENT>"   role birthright grant
        "risk_score >= N"               approval threshold on risk
        "affinity_score >= N"           the recommendation threshold itself

    Returns None for rules that are configuration rather than policy (the
    affinity threshold), or that cannot be interpreted - the caller reports
    those rather than loading something it guessed at.
    """
    rule = rule.strip().strip('"')

    if _BIRTHRIGHT.match(rule):
        # Recognised, but deliberately not loaded. A birthright is a *grant*
        # rule ("this role should receive this entitlement"), and every policy
        # evaluator currently implemented is a restriction. Loading it as one of
        # those would misrepresent it. See docs/client-data-assessment.md.
        return None

    match = _THRESHOLD.match(rule)
    if match and match.group("field") == "risk_score":
        tier = "HUMAN_REVIEW" if type_.strip().upper() == "HUMAN_APPROVAL" else "MANAGER"
        return {
            "policy_id": policy_id,
            "policy_name": name,
            "description": f"Approval threshold: {rule}",
            "policy_type": PolicyType.RISK_THRESHOLD_APPROVAL.value,
            "rule_definition": {
                "min_risk_score": int(match.group("value")),
                "approval_tier": tier,
                "message": f"{name}: entitlements scoring {match.group('value')} or above "
                           f"require {tier.replace('_', ' ').lower()}.",
            },
            "enabled": True,
        }

    if match and match.group("field") == "affinity_score":
        # Not a policy: this is the engine's recommendation threshold, which
        # lives in configuration (AFFINITY_THRESHOLD) so that one number cannot
        # be set in two places and disagree with itself.
        return None

    return None


def classify_unparsed(rule: str) -> str:
    rule = rule.strip().strip('"')
    if _BIRTHRIGHT.match(rule):
        return (
            "role birthright (ALLOW/grant semantics) - needs a new ROLE_BIRTHRIGHT "
            "policy type; every implemented evaluator is a restriction"
        )
    if _THRESHOLD.match(rule):
        return "configuration, not policy (AFFINITY_THRESHOLD setting)"
    return "UNRECOGNISED rule expression"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _read(directory: Path, name: str) -> list[dict[str, str]]:
    path = directory / name
    if not path.exists():
        raise FileNotFoundError(f"Expected client extract not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if any(v.strip() for v in row.values())]


def _upsert(session: Session, model, rows: list[dict], pk: list[str]) -> int:
    if not rows:
        return 0
    stmt = insert(model.__table__).values(rows)
    updatable = {
        col.name: stmt.excluded[col.name]
        for col in model.__table__.columns
        if col.name not in pk and col.name != "created_at"
    }
    session.execute(stmt.on_conflict_do_update(index_elements=pk, set_=updatable))
    return len(rows)


def load(session: Session, directory: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"counts": {}, "warnings": []}

    catalog = _read(directory, "entitlement_catalog.csv")
    risk_rows = _read(directory, "entitlement_risk_scores.csv")
    identities = _read(directory, "identities.csv")
    joiners = _read(directory, "new_joiners.csv")
    sod_rows = _read(directory, "sod_rules.csv")
    policy_rows = _read(directory, "policy_rules.csv")

    # --- entitlements: catalogue + risk, keyed on the name ----------------
    by_name: dict[str, dict[str, Any]] = {}
    for row in catalog:
        name = row["entitlement_name"].strip()
        by_name[name] = {
            "entitlement_id": name,
            "entitlement_name": name,
            "external_id": row["entitlement_id"].strip(),
            "application": row["application"].strip(),
            "owner": row["owner"].strip(),
            "description": None,
            "risk_score": None,
            "risk_category": None,
        }
    for row in risk_rows:
        name = row["entitlement"].strip()
        entry = by_name.setdefault(
            name,
            {
                "entitlement_id": name,
                "entitlement_name": name,
                "external_id": None,
                "application": row["application"].strip(),
                "owner": None,
                "description": None,
                "risk_score": None,
                "risk_category": None,
            },
        )
        entry["risk_score"] = int(row["risk_score"])
        entry["risk_category"] = row["risk_category"].strip().upper()
        entry.setdefault("application", row["application"].strip())
        if not entry.get("application"):
            entry["application"] = row["application"].strip()

    # Entitlements referenced by holdings or SoD but absent from both files.
    referenced = {e for r in identities for e in r["entitlements"].split(";") if e.strip()}
    for r in sod_rows:
        referenced.update({r["entitlement_1"].strip(), r["entitlement_2"].strip()})
    for name in sorted(referenced - set(by_name)):
        by_name[name] = {
            "entitlement_id": name,
            "entitlement_name": name,
            "external_id": None,
            "application": "UNKNOWN",
            "owner": None,
            "description": None,
            "risk_score": None,
            "risk_category": None,
        }
        report["warnings"].append(
            f"{name}: referenced by identities/SoD but present in neither "
            f"entitlement_catalog.csv nor entitlement_risk_scores.csv"
        )

    for name, entry in by_name.items():
        if entry["risk_score"] is None:
            entry["risk_score"] = UNSCORED_RISK
            entry["risk_category"] = UNSCORED_CATEGORY
            report["warnings"].append(
                f"{name}: no risk score in the extract - loaded as {UNSCORED_RISK} "
                f"(CRITICAL) so it fails closed to human review"
            )
        if not entry.get("application"):
            entry["application"] = "UNKNOWN"
        if entry["external_id"] is None:
            report["warnings"].append(f"{name}: scored/held but absent from entitlement_catalog.csv")

    report["counts"]["entitlements"] = _upsert(
        session, Entitlement, list(by_name.values()), ["entitlement_id"]
    )

    # --- employees --------------------------------------------------------
    employees: list[dict[str, Any]] = []
    for row in identities:
        employees.append(
            {
                "employee_id": row["employee_id"].strip(),
                "name": row["name"].strip(),
                "department": row["department"].strip(),
                "job_role": row["job_role"].strip(),
                "job_level": row["job_level"].strip(),
                "location": row["location"].strip(),
                "manager_id": None,
                "cost_center": None,
                "start_date": None,
                "employment_status": EmploymentStatus.ACTIVE.value,
                "employment_type": EmploymentType.EMPLOYEE.value,
            }
        )
    for row in joiners:
        employees.append(
            {
                "employee_id": row["employee_id"].strip(),
                "name": row["name"].strip(),
                "department": row["department"].strip(),
                "job_role": row["job_role"].strip(),
                "job_level": row["job_level"].strip(),
                "location": row["location"].strip(),
                # Manager ids in new_joiners.csv (MGR100...) are not identities
                # in identities.csv, so they cannot satisfy the self-referencing
                # foreign key. Retained in cost_center-adjacent reporting only.
                "manager_id": None,
                "cost_center": (row.get("cost_center") or "").strip() or None,
                "start_date": (
                    date.fromisoformat(row["start_date"].strip())
                    if row.get("start_date", "").strip()
                    else None
                ),
                "employment_status": EmploymentStatus.PENDING_START.value,
                "employment_type": EmploymentType.EMPLOYEE.value,
            }
        )
    unresolved_managers = {
        (r.get("manager_id") or "").strip() for r in joiners if (r.get("manager_id") or "").strip()
    }
    if unresolved_managers:
        report["warnings"].append(
            f"new_joiners.manager_id values {sorted(unresolved_managers)} do not exist in "
            f"identities.csv; stored as NULL"
        )
    report["counts"]["employees"] = _upsert(session, Employee, employees, ["employee_id"])

    # --- holdings ---------------------------------------------------------
    grants = [
        {"employee_id": row["employee_id"].strip(), "entitlement_id": ent.strip(), "source": "IIQ"}
        for row in identities
        for ent in row["entitlements"].split(";")
        if ent.strip()
    ]
    report["counts"]["employee_entitlements"] = _upsert(
        session, EmployeeEntitlement, grants, ["employee_id", "entitlement_id"]
    )

    # --- SoD --------------------------------------------------------------
    sod = [
        {
            "sod_id": row["sod_id"].strip(),
            "name": f"{row['entitlement_1'].strip()} vs {row['entitlement_2'].strip()}",
            "entitlement_1": row["entitlement_1"].strip(),
            "entitlement_2": row["entitlement_2"].strip(),
            "severity": row["severity"].strip().upper(),
            "description": None,
            "enabled": True,
        }
        for row in sod_rows
    ]
    report["counts"]["sod_rules"] = _upsert(session, SodRule, sod, ["sod_id"])

    # --- policies ---------------------------------------------------------
    policies: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    for row in policy_rows:
        parsed = parse_policy_rule(
            row["policy_id"].strip(), row["policy_name"].strip(),
            row["type"].strip(), row["rule"],
        )
        if parsed is None:
            skipped.append((row["policy_id"].strip(), classify_unparsed(row["rule"])))
            continue
        policies.append(parsed)
    report["counts"]["policies"] = _upsert(session, Policy, policies, ["policy_id"])
    report["skipped_policies"] = skipped
    for policy_id, reason in skipped:
        report["warnings"].append(f"{policy_id}: not loaded as a policy - {reason}")

    return report


def purge(session: Session) -> None:
    for model in (
        RecommendationExplanationRow, RecommendationEvidence, PolicyResult, SodResult,
        SailPointRequest, Recommendation, JoinerAnalysis,
        EmployeeEntitlement, SodRule, Policy, Entitlement, Employee,
    ):
        session.execute(delete(model))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="seed/client", help="Directory of client CSV extracts.")
    parser.add_argument("--purge", action="store_true", help="Delete all existing data first.")
    args = parser.parse_args()

    configure_logging()
    directory = Path(args.dir)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parents[1] / directory

    try:
        with session_scope() as session:
            if args.purge:
                purge(session)
            report = load(session, directory)
    except Exception as exc:
        print(f"\nLoad failed: {exc}", file=sys.stderr)
        return 1

    print("\nLoaded from client extracts:")
    for name, count in report["counts"].items():
        print(f"  {name:24s} {count:5d}")

    if report["warnings"]:
        print(f"\nData quality warnings ({len(report['warnings'])}):")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
