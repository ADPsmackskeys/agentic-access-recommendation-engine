"""The chat surface lets a model write SQL. This is what stops it mattering.

These are the highest-value tests in the suite: every case here is something a
model could plausibly emit, either by mistake or because a user asked it to.
None of them need a database or an LLM.
"""

from __future__ import annotations

import pytest

from app.services.sql_guard import READABLE_TABLES, UnsafeSqlError, validate


# --------------------------------------------------------------------------- #
# Queries that must run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM employees",
        "SELECT employee_id, name FROM employees WHERE employee_id = 'NJ1001'",
        """
        SELECT e.name, ent.entitlement_id, ent.risk_score
        FROM employees e
        JOIN employee_entitlements ee ON ee.employee_id = e.employee_id
        JOIN entitlements ent ON ent.entitlement_id = ee.entitlement_id
        WHERE ent.application ILIKE '%SAP%'
        """,
        "WITH finance AS (SELECT * FROM employees WHERE department = 'Finance') "
        "SELECT count(*) FROM finance",
        "SELECT r.entitlement_id, r.recommendation_status FROM recommendations r "
        "JOIN joiner_analyses a ON a.analysis_id = r.analysis_id",
        "SELECT count(*) AS n FROM entitlements WHERE risk_score >= 70",
        # A string literal that merely contains a keyword is not a statement.
        "SELECT * FROM entitlements WHERE entitlement_name = 'DROP TABLE employees'",
    ],
)
def test_legitimate_queries_are_allowed(sql: str) -> None:
    result = validate(sql)
    assert result.sql
    assert result.tables


def test_every_readable_table_is_actually_queryable() -> None:
    """The allow-list must not name a table that does not exist in the model."""
    from app.db.models import Base

    missing = sorted(READABLE_TABLES - set(Base.metadata.tables))
    assert not missing, f"allow-list names non-existent tables: {missing}"


# --------------------------------------------------------------------------- #
# Queries that must not run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("sql", "because"),
    [
        ("DROP TABLE employees", "DDL"),
        ("DELETE FROM employees", "DML"),
        ("UPDATE employees SET name = 'x'", "DML"),
        ("INSERT INTO employees (employee_id) VALUES ('x')", "DML"),
        ("TRUNCATE employees", "DDL"),
        ("ALTER TABLE employees ADD COLUMN x int", "DDL"),
        ("GRANT ALL ON employees TO PUBLIC", "privilege change"),
        ("SELECT * FROM employees; DROP TABLE employees", "statement stacking"),
        ("SELECT * INTO backup FROM employees", "SELECT INTO writes"),
        ("SELECT * FROM pg_catalog.pg_user", "system catalogue"),
        ("SELECT * FROM information_schema.tables", "system catalogue"),
        ("SELECT * FROM alembic_version", "not on the allow-list"),
        ("SELECT pg_read_file('/etc/passwd')", "filesystem read"),
        ("SELECT lo_import('/etc/passwd')", "filesystem read"),
        ("SELECT dblink('host=evil', 'SELECT 1')", "remote connection"),
        ("SELECT pg_sleep(60)", "denial of service"),
        ("SELECT * FROM employees UNION SELECT * FROM alembic_version", "hidden in a UNION"),
        ("", "empty"),
        ("this is not sql at all", "unparseable"),
    ],
)
def test_dangerous_queries_are_refused(sql: str, because: str) -> None:
    with pytest.raises(UnsafeSqlError):
        validate(sql)


def test_the_refusal_explains_itself() -> None:
    """A caller has to be able to tell what was wrong with their question."""
    with pytest.raises(UnsafeSqlError, match="alembic_version"):
        validate("SELECT * FROM alembic_version")

    with pytest.raises(UnsafeSqlError, match="one statement"):
        validate("SELECT 1 FROM employees; SELECT 2 FROM employees")


# --------------------------------------------------------------------------- #
# Row capping
# --------------------------------------------------------------------------- #
def test_a_row_cap_is_always_applied() -> None:
    result = validate("SELECT * FROM employees", max_rows=25)
    assert result.limit == 25
    assert "LIMIT 25" in result.sql.upper()


def test_a_generated_limit_cannot_exceed_the_cap() -> None:
    """A model asking for 10000 rows does not get 10000 rows."""
    result = validate("SELECT * FROM employees LIMIT 10000", max_rows=50)
    assert result.limit == 50
    assert "10000" not in result.sql


def test_a_smaller_generated_limit_is_respected() -> None:
    result = validate("SELECT * FROM employees LIMIT 5", max_rows=200)
    assert result.limit == 5


def test_cte_names_are_not_mistaken_for_tables() -> None:
    """A CTE named after nothing real must not trip the allow-list."""
    result = validate(
        "WITH scratch AS (SELECT * FROM employees) SELECT * FROM scratch"
    )
    assert result.tables == ("employees",)
