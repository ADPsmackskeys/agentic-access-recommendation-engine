#!/usr/bin/env python
"""Load the seed corpus in `seed/*.json` into PostgreSQL.

`seed/*.json` is the ground truth: a faithful transliteration of the client's
CSV extracts, one file per extract, produced by `scripts/convert_client_csv.py`.
This script is the adapter between that source shape and the internal schema,
and it is the only place where the client's file layout is interpreted.

Mapping decisions, all deliberate:

* Entitlements are keyed on the entitlement **name**. The catalogue's `ENT0xx`
  id is not a join key anywhere else in the extract - identities, risk scores,
  SoD rules and the affinity table all reference the name - so the name is the
  primary key and `ENT001` is kept as `external_id` for traceability.
* `identities.entitlements` is a `;`-delimited string; splitting it yields the
  holdings table the client's file list never named.
* An entitlement nobody has scored is loaded as risk 100, not skipped and not
  assumed safe. Failing closed is the same stance the policy engine takes for a
  rule it cannot evaluate.
* `new_joiners` become `PENDING_START` employees, `identities` become `ACTIVE`
  ones. Peer selection filters on that column, which is what stops one joiner
  from shaping another joiner's access.
* `policy_rules.rule` is a small expression language. Rules with no implemented
  evaluator are reported, not mangled into one that inverts their meaning.

`peer_affinity_scores.json` is deliberately **not** loaded. It is the client's
own affinity output, and the engine recomputing it from `identities` is what
demonstrates the two agree - see tests/integration/test_client_affinity.py.

Idempotent: re-running upserts the reference data rather than duplicating it.

Usage:
    python scripts/seed_database.py
    python scripts/seed_database.py --reset      # wipe analyses and reseed
    python scripts/seed_database.py --purge-all  # wipe everything and reseed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Allow `python scripts/seed_database.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
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

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
logger = get_logger("seed")

# Risk assigned to an entitlement the client has not scored. Deliberately the
# top of the scale: unscored is not the same as safe.
UNSCORED_RISK = 100
UNSCORED_CATEGORY = "UNSCORED"

# The client's `rule` column has three observed forms. Anything else is
# reported rather than guessed at.
_BIRTHRIGHT = re.compile(r"^\s*(?P<role>[^-]+?)\s*->\s*(?P<entitlement>\S+)\s*$")
_THRESHOLD = re.compile(r"^\s*(?P<field>risk_score|affinity_score)\s*>=\s*(?P<value>\d+)\s*$")


def _load(name: str) -> list[dict[str, Any]]:
    path = SEED_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Seed file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _upsert(session: Session, model, rows: list[dict], pk: list[str]) -> int:
    """PostgreSQL INSERT ... ON CONFLICT DO UPDATE for a batch of rows."""
    if not rows:
        return 0
    stmt = insert(model.__table__).values(rows)
    updatable = {
        col.name: stmt.excluded[col.name]
        for col in model.__table__.columns
        if col.name not in pk and col.name != "created_at"
    }
    stmt = stmt.on_conflict_do_update(index_elements=pk, set_=updatable)
    session.execute(stmt)
    return len(rows)


# --------------------------------------------------------------------------- #
# Builders - pure, so they can be unit-tested without a database
# --------------------------------------------------------------------------- #
def build_entitlements(
    catalog: list[dict[str, Any]],
    risk_rows: list[dict[str, Any]],
    identities: list[dict[str, Any]],
    sod_rows: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Merge the catalogue and the risk file, keyed on entitlement name."""
    by_name: dict[str, dict[str, Any]] = {}

    for row in catalog:
        name = _text(row, "entitlement_name")
        by_name[name] = {
            "entitlement_id": name,
            "entitlement_name": name,
            "external_id": _text(row, "entitlement_id") or None,
            "application": _text(row, "application"),
            "description": None,
            "owner": _text(row, "owner") or None,
            "risk_score": None,
            "risk_category": None,
        }

    for row in risk_rows:
        name = _text(row, "entitlement")
        entry = by_name.setdefault(
            name,
            {
                "entitlement_id": name,
                "entitlement_name": name,
                "external_id": None,
                "application": _text(row, "application"),
                "description": None,
                "owner": None,
                "risk_score": None,
                "risk_category": None,
            },
        )
        entry["risk_score"] = int(row["risk_score"])
        entry["risk_category"] = _text(row, "risk_category").upper()
        if not entry["application"]:
            entry["application"] = _text(row, "application")
        if entry["external_id"] is None:
            warnings.append(f"{name}: scored but absent from entitlement_catalog")

    # Entitlements referenced by holdings or SoD rules but present in neither file.
    referenced: set[str] = {
        ent.strip()
        for row in identities
        for ent in _text(row, "entitlements").split(";")
        if ent.strip()
    }
    for row in sod_rows:
        referenced.update({_text(row, "entitlement_1"), _text(row, "entitlement_2")})

    for name in sorted(referenced - set(by_name)):
        by_name[name] = {
            "entitlement_id": name,
            "entitlement_name": name,
            "external_id": None,
            "application": "UNKNOWN",
            "description": None,
            "owner": None,
            "risk_score": None,
            "risk_category": None,
        }
        warnings.append(
            f"{name}: referenced by identities/SoD but present in neither "
            f"entitlement_catalog nor entitlement_risk_scores"
        )

    for name in sorted(by_name):
        entry = by_name[name]
        if entry["risk_score"] is None:
            entry["risk_score"] = UNSCORED_RISK
            entry["risk_category"] = UNSCORED_CATEGORY
            warnings.append(
                f"{name}: no risk score in the extract - loaded as {UNSCORED_RISK} "
                f"so it fails closed to human review"
            )
        if not entry["application"]:
            entry["application"] = "UNKNOWN"

    return [by_name[name] for name in sorted(by_name)]


def build_employees(
    identities: list[dict[str, Any]],
    joiners: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    employees: list[dict[str, Any]] = []

    for row in identities:
        employees.append(
            {
                "employee_id": _text(row, "employee_id"),
                "name": _text(row, "name"),
                "department": _text(row, "department"),
                "job_role": _text(row, "job_role"),
                "job_level": _text(row, "job_level"),
                "location": _text(row, "location"),
                "manager_id": None,
                "manager_external_id": None,
                "cost_center": None,
                "start_date": None,
                # `identities` carries no lifecycle column, so every identity is
                # loaded as ACTIVE. If the extract contains leavers, their access
                # is currently shaping joiner recommendations - question 2 in
                # docs/client-data-assessment.md.
                "employment_status": EmploymentStatus.ACTIVE.value,
                "employment_type": EmploymentType.EMPLOYEE.value,
            }
        )

    for row in joiners:
        start = _text(row, "start_date")
        employees.append(
            {
                "employee_id": _text(row, "employee_id"),
                "name": _text(row, "name"),
                "department": _text(row, "department"),
                "job_role": _text(row, "job_role"),
                "job_level": _text(row, "job_level"),
                "location": _text(row, "location"),
                "manager_id": _text(row, "manager_id") or None,
                # Kept whether or not it resolves to an identity - manager-tier
                # approvals need a name to route to.
                "manager_external_id": _text(row, "manager_id") or None,
                "cost_center": _text(row, "cost_center") or None,
                "start_date": date.fromisoformat(start) if start else None,
                "employment_status": EmploymentStatus.PENDING_START.value,
                "employment_type": EmploymentType.EMPLOYEE.value,
            }
        )

    # Managers are themselves employees, so a manager_id that is not in the
    # corpus cannot satisfy the self-referencing foreign key.
    known = {row["employee_id"] for row in employees}
    unresolved = sorted(
        {e["manager_id"] for e in employees if e["manager_id"] and e["manager_id"] not in known}
    )
    if unresolved:
        warnings.append(
            f"manager_id values {unresolved} are not identities in the corpus; the foreign key "
            f"is stored as NULL and the source value is kept in manager_external_id"
        )
    for row in employees:
        if row["manager_id"] not in known:
            row["manager_id"] = None

    return employees


def build_holdings(identities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "employee_id": _text(row, "employee_id"),
            "entitlement_id": ent.strip(),
            "source": "IIQ",
        }
        for row in identities
        for ent in _text(row, "entitlements").split(";")
        if ent.strip()
    ]


def build_sod_rules(sod_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sod_id": _text(row, "sod_id"),
            "name": f"{_text(row, 'entitlement_1')} vs {_text(row, 'entitlement_2')}",
            "entitlement_1": _text(row, "entitlement_1"),
            "entitlement_2": _text(row, "entitlement_2"),
            "severity": _text(row, "severity").upper(),
            "description": None,
            "enabled": True,
        }
        for row in sod_rows
    ]


def build_policies(
    policy_rows: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any]]:
    """Translate the client's rule expressions into typed policies.

    Returns only rules an implemented evaluator can represent. The rest are
    reported: there is no generic expression evaluator in this system, and
    forcing a grant rule into a restriction would invert its meaning.
    """
    policies: list[dict[str, Any]] = []

    for row in policy_rows:
        policy_id = _text(row, "policy_id")
        name = _text(row, "policy_name")
        type_ = _text(row, "type").upper()
        rule = _text(row, "rule").strip('"')

        threshold = _THRESHOLD.match(rule)
        if threshold and threshold.group("field") == "risk_score":
            value = int(threshold.group("value"))
            # The client's `type` column says HUMAN_APPROVAL for every threshold
            # rule, which cannot distinguish "a manager signs this off" from
            # "governance must look at it". The risk band does distinguish them,
            # so the tier is derived from where the threshold sits: a rule
            # firing inside the HIGH band routes to the line manager, one
            # reaching CRITICAL routes to human review. On the supplied rules
            # that makes POL005 (>=70) a manager approval and POL006 (>=90) a
            # human review, which is the intended joiner routing.
            settings = get_settings()
            tier = "HUMAN_REVIEW" if value > settings.risk_high_max else "MANAGER"
            policies.append(
                {
                    "policy_id": policy_id,
                    "policy_name": name,
                    "description": f"Approval threshold: {rule}",
                    "policy_type": PolicyType.RISK_THRESHOLD_APPROVAL.value,
                    "rule_definition": {
                        "min_risk_score": value,
                        "approval_tier": tier,
                        "message": (
                            f"{name}: entitlements scoring {value} or above require "
                            f"{tier.replace('_', ' ').lower()}."
                        ),
                    },
                    "enabled": True,
                }
            )
            continue

        if threshold and threshold.group("field") == "affinity_score":
            reason = (
                "configuration, not policy - this is the engine's recommendation "
                "threshold and lives in the AFFINITY_THRESHOLD setting so that one "
                "number cannot be set in two places and disagree with itself"
            )
        elif _BIRTHRIGHT.match(rule):
            reason = (
                "role birthright (grant semantics) - every implemented policy "
                "evaluator is a restriction, so loading this as one would invert "
                "its meaning; needs a ROLE_BIRTHRIGHT policy type"
            )
        else:
            reason = "unrecognised rule expression"
        warnings.append(f"{policy_id} '{rule}': not loaded as a policy - {reason}")

    return policies


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def purge_analyses(session: Session) -> None:
    """Delete analysis output only; reference data survives."""
    for model in (
        RecommendationExplanationRow,
        RecommendationEvidence,
        PolicyResult,
        SodResult,
        SailPointRequest,
        Recommendation,
        JoinerAnalysis,
    ):
        session.execute(delete(model))
    logger.info("seed.purge.analyses")


def purge_all(session: Session) -> None:
    purge_analyses(session)
    for model in (EmployeeEntitlement, SodRule, Policy, Entitlement, Employee):
        session.execute(delete(model))
    logger.info("seed.purge.all")


def seed(session: Session, warnings: list[str] | None = None) -> dict[str, int]:
    """Upsert the reference corpus. `warnings` collects data-quality findings."""
    warnings = [] if warnings is None else warnings

    catalog = _load("entitlement_catalog.json")
    risk_rows = _load("entitlement_risk_scores.json")
    identities = _load("identities.json")
    joiners = _load("new_joiners.json")
    sod_rows = _load("sod_rules.json")
    policy_rows = _load("policy_rules.json")

    counts: dict[str, int] = {}

    counts["entitlements"] = _upsert(
        session,
        Entitlement,
        build_entitlements(catalog, risk_rows, identities, sod_rows, warnings),
        ["entitlement_id"],
    )

    employees = build_employees(identities, joiners, warnings)
    counts["employees"] = _upsert(session, Employee, employees, ["employee_id"])
    session.flush()

    counts["employee_entitlements"] = _upsert(
        session, EmployeeEntitlement, build_holdings(identities), ["employee_id", "entitlement_id"]
    )
    counts["policies"] = _upsert(
        session, Policy, build_policies(policy_rows, warnings), ["policy_id"]
    )
    counts["sod_rules"] = _upsert(session, SodRule, build_sod_rules(sod_rows), ["sod_id"])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="delete existing analyses before seeding"
    )
    parser.add_argument(
        "--purge-all",
        action="store_true",
        help="delete ALL data (reference + analyses) before seeding",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    logger.info("seed.start", database=settings.safe_database_url())

    warnings: list[str] = []
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
            if args.purge_all:
                purge_all(session)
            elif args.reset:
                purge_analyses(session)
            counts = seed(session, warnings)
    except Exception as exc:
        logger.error("seed.failed", error=str(exc))
        print(f"\nSeeding failed: {exc}", file=sys.stderr)
        print(
            "Check that PostgreSQL is reachable and `alembic upgrade head` has been run.",
            file=sys.stderr,
        )
        return 1

    logger.info("seed.complete", **counts)
    print("\nSeed complete:")
    for name, count in counts.items():
        print(f"  {name:24s} {count:5d}")

    if warnings:
        print(f"\nData quality warnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
