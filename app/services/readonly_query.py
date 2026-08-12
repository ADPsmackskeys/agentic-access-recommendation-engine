"""Execution of validated, model-generated SQL.

`sql_guard` decides whether a query is allowed to run. This module decides how,
and it assumes the guard has already failed at least once in its life: every
statement runs inside a transaction PostgreSQL itself has marked READ ONLY, so a
write that somehow passed validation is rejected by the server rather than
executed. The transaction is always rolled back.

A statement timeout bounds runaway queries - a valid `SELECT` can still be a
cross join over the whole schema - and results are truncated to the row cap the
guard applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.exceptions import DomainError
from app.logging import get_logger
from app.services.sql_guard import ValidatedQuery

logger = get_logger(__name__)


class QueryExecutionError(DomainError):
    """The validated query failed to execute."""

    code = "query_failed"


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def execute(
    session: Session, query: ValidatedQuery, *, timeout_seconds: int = 10
) -> QueryResult:
    """Run a validated query in a read-only transaction and roll back.

    The session is left usable: the surrounding transaction is rolled back
    whether the query succeeded or failed, so nothing this function does can
    persist or leave the session in a failed state.
    """
    try:
        # A nested transaction (SAVEPOINT) keeps this isolated from whatever the
        # caller is doing, and lets the rollback be unconditional.
        with session.begin_nested():
            session.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_seconds)}s'"))
            # The real guarantee. Anything that tries to write from here on is
            # refused by PostgreSQL, not by our parser.
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = session.execute(text(query.sql))
            columns = list(result.keys())
            rows = [dict(row) for row in result.mappings().all()]
            raise _Done(columns, rows)
    except _Done as done:
        logger.info(
            "chat.query_executed",
            tables=list(query.tables),
            row_count=len(done.rows),
        )
        return QueryResult(columns=done.columns, rows=done.rows)
    except SQLAlchemyError as exc:
        message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
        logger.warning("chat.query_failed", error=message, tables=list(query.tables))
        raise QueryExecutionError(f"The query could not be executed: {message}") from exc
    finally:
        session.rollback()


class _Done(Exception):
    """Carries the result out of the nested transaction so it always rolls back.

    Returning normally from inside `begin_nested()` would commit the savepoint.
    Nothing here should ever commit, so the success path leaves by exception too
    and the rollback in `finally` is unconditional.
    """

    def __init__(self, columns: list[str], rows: list[dict[str, Any]]) -> None:
        super().__init__("query complete")
        self.columns = columns
        self.rows = rows
