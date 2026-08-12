#!/usr/bin/env python
"""Load the deterministic seed corpus into PostgreSQL.

Idempotent: re-running upserts the reference data rather than duplicating it.
Analysis output (analyses, recommendations, evidence, SailPoint requests) is
left untouched unless `--reset` is passed.

Usage:
    python scripts/seed_database.py
    python scripts/seed_database.py --reset      # wipe analyses and reseed
    python scripts/seed_database.py --purge-all  # wipe everything and reseed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

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
from app.logging import configure_logging, get_logger  # noqa: E402

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
logger = get_logger("seed")


def _load(name: str) -> list[dict]:
    path = SEED_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Seed file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


def seed(session: Session) -> dict[str, int]:
    entitlements = _load("entitlements.json")
    employees = _load("employees.json")
    grants = _load("employee_entitlements.json")
    policies = _load("policies.json")
    sod_rules = _load("sod_rules.json")

    counts: dict[str, int] = {}

    counts["entitlements"] = _upsert(session, Entitlement, entitlements, ["entitlement_id"])

    # Managers are themselves employees, so insert without the FK target first
    # and let the self-referencing manager_id settle in a second pass.
    employee_rows = []
    for row in employees:
        row = dict(row)
        if row.get("start_date"):
            row["start_date"] = date.fromisoformat(row["start_date"])
        employee_rows.append(row)

    known_ids = {r["employee_id"] for r in employee_rows}
    for row in employee_rows:
        if row.get("manager_id") not in known_ids:
            row["manager_id"] = None

    without_manager = [dict(r, manager_id=None) for r in employee_rows]
    counts["employees"] = _upsert(session, Employee, without_manager, ["employee_id"])
    session.flush()
    _upsert(session, Employee, employee_rows, ["employee_id"])

    counts["employee_entitlements"] = _upsert(
        session, EmployeeEntitlement, grants, ["employee_id", "entitlement_id"]
    )
    counts["policies"] = _upsert(session, Policy, policies, ["policy_id"])
    counts["sod_rules"] = _upsert(session, SodRule, sod_rules, ["sod_id"])
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

    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
            if args.purge_all:
                purge_all(session)
            elif args.reset:
                purge_analyses(session)
            counts = seed(session)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
