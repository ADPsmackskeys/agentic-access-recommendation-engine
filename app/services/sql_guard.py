"""Validation for model-generated SQL.

The chat endpoint lets an LLM compose a query over the governance schema. That
is a deliberate product decision, and this module is what makes it survivable:
nothing generated reaches the database until it has been parsed and proved to be
a single, read-only, allow-listed `SELECT`.

Three layers, because none of them is sufficient alone:

1. **This parser.** Rejects anything that is not one read-only statement over
   known tables. Parsing beats pattern-matching - `DROP` in a string literal is
   fine, a `DROP` statement is not, and only a parser can tell them apart.
2. **A read-only transaction** (`app.services.readonly_session`). Even a
   statement that slips past this cannot write, because PostgreSQL refuses.
3. **A statement timeout and a row cap.** A syntactically valid query can still
   be a cross join over every table.

Layer 2 is the real guarantee. This module exists to give a clear error instead
of a database one, and to keep obviously wrong queries off the wire.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from app.domain.exceptions import DomainError

# Tables the chat surface may read. Deliberately an allow-list: a new table is
# invisible to chat until someone decides it should be readable. `alembic_version`
# and anything added later are excluded by default.
READABLE_TABLES: frozenset[str] = frozenset(
    {
        "employees",
        "entitlements",
        "employee_entitlements",
        "policies",
        "sod_rules",
        "joiner_analyses",
        "recommendations",
        "recommendation_evidence",
        "recommendation_explanations",
        "policy_results",
        "sod_results",
        "sailpoint_requests",
    }
)

# Statement types that are unambiguously reads. Anything else - INSERT, UPDATE,
# DELETE, MERGE, CREATE, DROP, ALTER, TRUNCATE, GRANT, COPY, CALL - is refused
# by virtue of not appearing here.
_ALLOWED_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Expression types that execute or escape the query context regardless of the
# statement they are embedded in.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Into,          # SELECT ... INTO writes a new table
    exp.Command,       # anything sqlglot could not classify (SET, COPY, VACUUM)
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

# Functions that read or write outside the query. `pg_read_file` and friends
# are the obvious ones; `dblink` reaches another server entirely.
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
        "current_setting",
        "query_to_xml",
    }
)


class UnsafeSqlError(DomainError):
    """The generated SQL is not a safe, read-only query."""

    code = "unsafe_sql"


@dataclass(frozen=True)
class ValidatedQuery:
    """A query that passed every check, with a row cap applied."""

    sql: str
    tables: tuple[str, ...]
    limit: int


def validate(sql: str, *, max_rows: int = 200) -> ValidatedQuery:
    """Parse `sql` and return a safe, row-capped equivalent.

    Raises `UnsafeSqlError` with a specific reason. The reason is safe to show
    the caller: it describes their own query, not the database internals.
    """
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise UnsafeSqlError("No SQL was generated.")

    try:
        statements = sqlglot.parse(text, read="postgres")
    except Exception as exc:  # sqlglot raises several parse error types
        raise UnsafeSqlError(f"The generated SQL could not be parsed: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSqlError(
            f"Exactly one statement is allowed; the query contained {len(statements)}."
        )

    statement = statements[0]
    if not isinstance(statement, _ALLOWED_STATEMENTS):
        raise UnsafeSqlError(
            f"Only SELECT queries are permitted here (got {type(statement).__name__.upper()})."
        )

    for node_type in _FORBIDDEN_NODES:
        if tuple(statement.find_all(node_type)):
            raise UnsafeSqlError(
                f"The query uses a construct that is not permitted ({node_type.__name__})."
            )

    for func in statement.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name in _FORBIDDEN_FUNCTIONS:
            raise UnsafeSqlError(f"The function '{name}' is not permitted.")

    # Common table expressions are fine to read from, but their names must not
    # be mistaken for base tables when checking the allow-list.
    cte_names = {
        (cte.alias_or_name or "").lower()
        for cte in statement.find_all(exp.CTE)
    }

    referenced: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        schema = (table.db or "").lower()
        if schema and schema not in {"public", ""}:
            raise UnsafeSqlError(
                f"Only the 'public' schema may be queried (got '{schema}')."
            )
        referenced.add(name)

    if not referenced:
        raise UnsafeSqlError("The query does not read any known table.")

    unknown = sorted(referenced - READABLE_TABLES)
    if unknown:
        raise UnsafeSqlError(
            f"These tables are not readable from chat: {', '.join(unknown)}. "
            f"Readable tables are: {', '.join(sorted(READABLE_TABLES))}."
        )

    # Apply or tighten the row cap. A generated LIMIT larger than the cap is
    # replaced rather than trusted.
    existing = statement.args.get("limit")
    effective = max_rows
    if existing is not None:
        try:
            requested = int(existing.expression.name)
            effective = min(requested, max_rows)
        except (AttributeError, ValueError):
            effective = max_rows
    capped = statement.limit(effective)

    return ValidatedQuery(
        sql=capped.sql(dialect="postgres"),
        tables=tuple(sorted(referenced)),
        limit=effective,
    )
