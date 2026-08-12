"""Chat: question -> SQL -> read-only execution -> answer.

Driven with a stub LLM that returns SQL chosen by the test, so the whole chain
runs against the real database with no network call and no nondeterminism. What
is asserted is the machinery around the model: that the query is capped and
validated, that execution cannot write, that a hostile query is refused, and
that a failure at any step still returns something the caller can act on.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.exceptions import LlmError
from app.main import create_app
from app.services.chat_service import ChatService, ChatUnavailableError
from app.services.llm_service import LLMService

pytestmark = pytest.mark.integration


class StubLLM(LLMService):
    """Returns queued responses in order; records what it was asked."""

    name = "stub"

    def __init__(self, *responses: str | Exception, model: str = "stub-model") -> None:
        self.responses = list(responses)
        self.model = model
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def generate_narrative(
        self, *, system_prompt: str, evidence: dict[str, Any], max_tokens: int | None = None
    ) -> str:
        self.prompts.append(system_prompt)
        if not self.responses:
            raise AssertionError("StubLLM was called more times than it had responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _service(session: Session, *responses: str | Exception) -> ChatService:
    return ChatService(session, llm=StubLLM(*responses))


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_question_is_answered_from_real_rows(db_session: Session) -> None:
    sql = (
        "SELECT e.name, ent.entitlement_id, ent.application "
        "FROM employees e "
        "JOIN employee_entitlements ee ON ee.employee_id = e.employee_id "
        "JOIN entitlements ent ON ent.entitlement_id = ee.entitlement_id "
        "WHERE e.employee_id = 'EMP001'"
    )
    service = _service(db_session, sql, "Ramesh holds three entitlements.")
    answer = service.ask("What does EMP001 have?")

    assert answer.row_count == 3, "EMP001 holds three entitlements in the client extract"
    assert {r["entitlement_id"] for r in answer.rows} == {
        "SAP_FIN_DISPLAY",
        "SAP_AP_INVOICE",
        "POWERBI_FINANCE",
    }
    assert answer.answer == "Ramesh holds three entitlements."
    assert answer.error is None
    assert answer.generator == "LLM"


def test_the_sql_is_always_returned_for_audit(db_session: Session) -> None:
    """An answer nobody can check is not usable in a governance context."""
    service = _service(db_session, "SELECT count(*) AS n FROM employees", "There are 20.")
    answer = service.ask("How many identities are there?")

    assert answer.sql and "employees" in answer.sql
    assert answer.tables == ["employees"]
    assert answer.columns == ["n"]
    assert answer.rows == [{"n": 20}]


def test_the_model_sees_the_schema_and_the_rows(db_session: Session) -> None:
    """Both calls must be grounded: schema for SQL, rows for prose."""
    stub = StubLLM("SELECT employee_id FROM employees LIMIT 1", "One identity.")
    ChatService(db_session, llm=stub).ask("Name one identity")

    assert "READABLE TABLES" in stub.prompts[0], "SQL prompt must carry the schema"
    assert "ONLY the query results" in stub.prompts[1], "prose prompt must be grounded"


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
def test_a_destructive_query_is_refused_and_never_executed(db_session: Session) -> None:
    before = db_session.execute(text("SELECT count(*) FROM employees")).scalar_one()

    service = _service(db_session, "DELETE FROM employees")
    answer = service.ask("Remove everyone")

    assert answer.error and "SELECT" in answer.error
    assert "rejected as unsafe" in answer.answer
    assert answer.row_count == 0
    after = db_session.execute(text("SELECT count(*) FROM employees")).scalar_one()
    assert after == before, "the table must be untouched"


def test_a_query_outside_the_allow_list_is_refused(db_session: Session) -> None:
    service = _service(db_session, "SELECT * FROM alembic_version")
    answer = service.ask("What migration is applied?")
    assert answer.error and "alembic_version" in answer.error


def test_the_row_cap_is_enforced_and_flagged(db_session: Session) -> None:
    service = _service(db_session, "SELECT * FROM employees", "Some identities.")
    answer = service.ask("List everyone", max_rows=3)

    assert answer.row_count == 3
    assert answer.truncated is True


def test_execution_cannot_write_even_if_validation_were_bypassed(
    db_session: Session,
) -> None:
    """The parser is the first layer, not the guarantee.

    This drives the executor directly with a write, which `validate` would have
    caught, to prove the READ ONLY transaction refuses it independently.
    """
    from app.services.readonly_query import QueryExecutionError, execute
    from app.services.sql_guard import ValidatedQuery

    smuggled = ValidatedQuery(
        sql="DELETE FROM employees WHERE employee_id = 'NJ1001'",
        tables=("employees",),
        limit=1,
    )
    with pytest.raises(QueryExecutionError, match="read-only"):
        execute(db_session, smuggled)

    still_there = db_session.execute(
        text("SELECT count(*) FROM employees WHERE employee_id = 'NJ1001'")
    ).scalar_one()
    assert still_there == 1


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #
def test_an_out_of_scope_question_is_declined_not_guessed(db_session: Session) -> None:
    service = _service(db_session, "CANNOT_ANSWER")
    answer = service.ask("What is the capital of France?")

    assert answer.generator == "REFUSED"
    assert answer.sql is None
    assert "cannot be answered" in answer.answer


def test_the_rows_survive_a_phrasing_failure(db_session: Session) -> None:
    """The data is the valuable part; losing the prose must not lose it."""
    service = _service(
        db_session,
        "SELECT count(*) AS n FROM employees",
        LlmError("model is down"),
    )
    answer = service.ask("How many identities?")

    assert answer.generator == "ROWS_ONLY"
    assert answer.rows == [{"n": 20}]
    assert answer.error and "model is down" in answer.error
    assert "1 row" in answer.answer


def test_a_broken_query_is_reported_not_swallowed(db_session: Session) -> None:
    service = _service(db_session, "SELECT no_such_column FROM employees")
    answer = service.ask("Something impossible")
    assert answer.error
    assert "could not be executed" in answer.answer


def test_chat_requires_an_llm(db_session: Session, monkeypatch) -> None:
    """The governance workflow runs without one; chat cannot."""
    from app.config import Settings

    settings = Settings(demo_mode=True, llm_provider="none")
    service = ChatService(db_session, settings=settings)
    with pytest.raises(ChatUnavailableError, match="requires a configured LLM"):
        service.ask("Can EMP001 access SAP ECC?")


def test_an_oversized_question_is_rejected(db_session: Session) -> None:
    service = _service(db_session)
    with pytest.raises(ChatUnavailableError, match="too long"):
        service.ask("x" * 1001)


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_endpoint_returns_503_without_an_llm(app_session_factory) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/chat", json={"question": "Who has AUDIT_TOOL?"})
    assert response.status_code == 503
    assert response.json()["error"] == "chat_unavailable"


def test_endpoint_validates_its_input(app_session_factory) -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/chat", json={}).status_code == 422
        assert client.post("/api/v1/chat", json={"question": ""}).status_code == 422
        assert (
            client.post(
                "/api/v1/chat", json={"question": "ok", "max_rows": 10_000}
            ).status_code
            == 422
        )


def test_endpoint_is_documented(app_session_factory) -> None:
    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json").json()
    assert "/api/v1/chat" in schema["paths"]
    assert "post" in schema["paths"]["/api/v1/chat"]
