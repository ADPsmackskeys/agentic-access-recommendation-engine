"""A description of the readable schema, for the SQL-generating model.

Derived from the SQLAlchemy metadata rather than hand-written, so it cannot
drift from the real tables: a column added to a model appears here on the next
call, and a table removed from the allow-list disappears.

The hand-written part is the per-table and per-column notes. Those carry the
domain meaning that column names alone do not - that `entitlement_id` is the
entitlement *name* rather than the catalogue's `ENT001`, that `PENDING_START`
identifies new joiners, that an unscored entitlement is stored as risk 100.
Without them a model writes syntactically valid SQL that answers the wrong
question.
"""

from __future__ import annotations

from functools import lru_cache

from app.db.models import Base
from app.services.sql_guard import READABLE_TABLES

# Why a table exists, in the terms someone asking a question would use.
_TABLE_NOTES: dict[str, str] = {
    "employees": (
        "All identities, both existing staff and new joiners. "
        "employment_status='ACTIVE' is existing staff; 'PENDING_START' is a new "
        "joiner who has not started yet. Joiner ids look like NJ1001, existing "
        "staff like EMP001."
    ),
    "entitlements": (
        "The entitlement catalogue. entitlement_id IS the entitlement name "
        "(e.g. 'SAP_FIN_DISPLAY'); external_id holds the source catalogue id "
        "(e.g. 'ENT001'). application is the system it belongs to "
        "(e.g. 'SAP ECC', 'PowerBI'). risk_score is 0-100."
    ),
    "employee_entitlements": (
        "Who currently holds what - the join table between employees and "
        "entitlements. A row here means the identity HAS that access today. "
        "New joiners have no rows here until access is provisioned."
    ),
    "policies": (
        "Governance policies. rule_definition is JSONB; for "
        "RISK_THRESHOLD_APPROVAL it holds min_risk_score and approval_tier."
    ),
    "sod_rules": (
        "Segregation-of-duties rules. Each row is a toxic pair: holding both "
        "entitlement_1 and entitlement_2 is a conflict."
    ),
    "joiner_analyses": (
        "One row per analysis run for a joiner. Holds the peer group "
        "(peer_ids, peer_count, matching_strategy) and overall status."
    ),
    "recommendations": (
        "The per-entitlement decision from an analysis. recommendation_status is "
        "one of AUTO_APPROVED, MANAGER_APPROVAL, HUMAN_REVIEW, BLOCKED, "
        "REJECTED, NOT_RECOMMENDED. decision_trace (JSONB) records which rule "
        "fired at each step. Join to joiner_analyses on analysis_id."
    ),
    "recommendation_evidence": "The peers whose access justified a recommendation.",
    "recommendation_explanations": "Generated narrative and structured explanation per recommendation.",
    "policy_results": "Which policies were evaluated for a recommendation and what they returned.",
    "sod_results": "Which SoD rules fired for a recommendation.",
    "sailpoint_requests": (
        "Generated access-request payloads. status is always SIMULATED - nothing "
        "has been provisioned to a real system."
    ),
}

# Facts a question-answerer needs and cannot infer from column names.
_SEMANTIC_NOTES = (
    "IMPORTANT SEMANTICS:",
    "- To answer whether someone CAN access an application today, look for rows in",
    "  employee_entitlements joined to entitlements on that application. A",
    "  recommendation is NOT access: an AUTO_APPROVED recommendation means a request",
    "  was raised, not that access exists.",
    "- risk bands: 0-30 LOW, 31-69 MEDIUM, 70-89 HIGH, 90-100 CRITICAL.",
    "- An entitlement with no risk score in the source data is stored as risk_score",
    "  100 with risk_category 'UNSCORED', so that it fails closed.",
    "- employees.manager_id is usually NULL because the source manager ids are not",
    "  themselves identities; manager_external_id holds the source value (e.g. MGR400).",
    "- Timestamps are timezone-aware; use created_at to find the most recent analysis.",
)


@lru_cache(maxsize=1)
def schema_description() -> str:
    """Render the readable schema as text for a prompt."""
    lines: list[str] = ["READABLE TABLES (PostgreSQL, schema 'public'):", ""]

    for table_name in sorted(READABLE_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        lines.append(f"{table_name}")
        note = _TABLE_NOTES.get(table_name)
        if note:
            lines.append(f"  -- {note}")
        for column in table.columns:
            flags = []
            if column.primary_key:
                flags.append("PK")
            if column.foreign_keys:
                target = next(iter(column.foreign_keys)).target_fullname
                flags.append(f"FK->{target}")
            if not column.nullable:
                flags.append("NOT NULL")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"    {column.name}: {column.type}{suffix}")
        lines.append("")

    lines.extend(_SEMANTIC_NOTES)
    return "\n".join(lines)
