"""Natural-language question answering over the governance database.

Three steps, each isolated so a failure in one is reportable rather than silent:

    question -> SQL (LLM) -> validated + executed read-only -> answer (LLM)

The generated SQL is returned to the caller on every response, including
failures. That is the whole audit story for this surface: an answer nobody can
check is not usable in a governance context, and the query is the only thing
that explains where a number came from.

**This is a reporting surface, not a decision surface.** It reads what the
deterministic engine already decided and what identities already hold. It never
decides whether access should be granted - that remains
`app.services.decision_service`, which no model touches. A caller asking "can X
access Y?" gets a statement of current fact plus, where relevant, what a past
analysis concluded; it does not get a new access decision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.domain.exceptions import DomainError, LlmError
from app.logging import get_logger
from app.services.llm_service import LLMService, build_llm_service
from app.services.readonly_query import QueryExecutionError, execute
from app.services.schema_catalog import schema_description
from app.services.sql_guard import UnsafeSqlError, validate

logger = get_logger(__name__)

MAX_QUESTION_CHARS = 1000

# Output allowance for SQL generation, separate from the prose default.
SQL_TOKEN_BUDGET = 4096


class ChatUnavailableError(DomainError):
    """Chat requires a configured LLM and none is available."""

    code = "chat_unavailable"


_SQL_SYSTEM_PROMPT = """\
You translate questions about an identity-governance database into a single \
PostgreSQL SELECT query.

Rules, all mandatory:
- Return ONE SELECT statement. Never INSERT, UPDATE, DELETE, DDL or multiple
  statements.
- Only the tables listed below. Never system catalogues.
- Prefer explicit JOINs and return the columns a human needs to read the answer,
  including names, not just ids.
- Use ILIKE for free-text matching on names and applications, since the user's
  phrasing will not match stored values exactly.
- If the question cannot be answered from these tables, return exactly:
  CANNOT_ANSWER

Respond with the SQL and nothing else: no prose, no explanation, no markdown
fences.

{schema}
"""

_ANSWER_SYSTEM_PROMPT = """\
You answer questions about identity governance using ONLY the query results you \
are given.

Rules, all mandatory:
- Use only the rows provided. Never infer, estimate or supplement from your own
  knowledge.
- If the result set is empty, say plainly that there is no matching record. Do
  not speculate about why.
- Be specific: quote the entitlement names, risk scores and statuses from the
  rows.
- Never state or imply that access should or should not be granted. You report
  what the records say; the governance engine makes decisions.
- Two or three sentences unless the data genuinely needs more.
"""


@dataclass
class ChatAnswer:
    """Everything the caller needs to trust or dispute the answer."""

    question: str
    answer: str
    sql: str | None = None
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    generator: str = "LLM"
    model: str | None = None
    error: str | None = None


def chat_settings(settings: Settings) -> Settings:
    """Settings with `CHAT_LLM_MODEL` applied, if one is configured.

    Returns the original object when no override is set, so the common case
    shares the already-built settings rather than copying them.
    """
    override = (settings.chat_llm_model or "").strip()
    if not override or override == settings.llm_model:
        return settings
    return settings.model_copy(update={"llm_model": override})


def _strip_sql_fence(raw: str) -> str:
    """Remove markdown fencing a model adds despite being told not to."""
    text = (raw or "").strip()
    fence = re.match(r"^```(?:sql)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    return text


class ChatService:
    """Question in, grounded answer out."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        llm: LLMService | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.llm = llm or build_llm_service(chat_settings(self.settings))

    # ------------------------------------------------------------------ #
    def ask(self, question: str, *, max_rows: int = 200) -> ChatAnswer:
        question = (question or "").strip()
        if not question:
            raise ChatUnavailableError("A question is required.")
        if len(question) > MAX_QUESTION_CHARS:
            raise ChatUnavailableError(
                f"Question is too long ({len(question)} characters; "
                f"limit {MAX_QUESTION_CHARS})."
            )
        if not self.llm.available:
            raise ChatUnavailableError(
                "Chat requires a configured LLM. Set LLM_PROVIDER and LLM_API_KEY, "
                "and DEMO_MODE=false. The governance workflow itself does not need one."
            )

        answer = ChatAnswer(question=question, answer="", model=self.llm.model)

        # 1 - question to SQL
        try:
            raw_sql = self._generate_sql(question)
        except LlmError as exc:
            answer.error = str(exc)
            answer.answer = "The question could not be translated into a query."
            logger.warning("chat.sql_generation_failed", error=str(exc))
            return answer

        if raw_sql.strip().upper().startswith("CANNOT_ANSWER"):
            answer.answer = (
                "That cannot be answered from the governance database. It holds "
                "identities, entitlements, current access, policies, SoD rules and "
                "past analyses."
            )
            answer.generator = "REFUSED"
            return answer

        # 2 - validate and execute
        try:
            validated = validate(raw_sql, max_rows=max_rows)
        except UnsafeSqlError as exc:
            answer.sql = raw_sql
            answer.error = str(exc)
            answer.answer = "The generated query was rejected as unsafe and was not run."
            logger.warning("chat.sql_rejected", reason=str(exc))
            return answer

        answer.sql = validated.sql
        answer.tables = list(validated.tables)

        try:
            result = execute(self.session, validated)
        except QueryExecutionError as exc:
            answer.error = str(exc)
            answer.answer = "The query was valid but could not be executed."
            return answer

        answer.columns = result.columns
        answer.rows = result.rows
        answer.row_count = result.row_count
        answer.truncated = result.row_count >= validated.limit

        # 3 - rows to prose
        try:
            answer.answer = self._phrase_answer(question, answer)
        except LlmError as exc:
            # The data is the valuable part and it is already in hand; losing the
            # prose should not lose the result.
            answer.error = str(exc)
            answer.generator = "ROWS_ONLY"
            answer.answer = _describe_rows(answer)
            logger.warning("chat.phrasing_failed", error=str(exc))

        return answer

    # ------------------------------------------------------------------ #
    def _generate_sql(self, question: str) -> str:
        prompt = _SQL_SYSTEM_PROMPT.format(schema=schema_description())
        raw = self.llm.generate_narrative(
            system_prompt=prompt,
            evidence={"question": question},
            # Reasoning models spend this budget on internal thinking before
            # emitting anything, and a SELECT truncated mid-clause is worse than
            # a slow one: it fails validation and the user sees nothing useful.
            max_tokens=max(self.settings.llm_max_tokens, SQL_TOKEN_BUDGET),
        )
        return _strip_sql_fence(raw)

    def _phrase_answer(self, question: str, answer: ChatAnswer) -> str:
        # Cap what the model sees: a 200-row result does not need to be read in
        # full to be summarised, and the rows are returned to the caller anyway.
        preview = answer.rows[:50]
        return self.llm.generate_narrative(
            system_prompt=_ANSWER_SYSTEM_PROMPT,
            evidence={
                "question": question,
                "sql": answer.sql,
                "row_count": answer.row_count,
                "rows_shown": len(preview),
                "rows": json.loads(json.dumps(preview, default=str)),
            },
        )


def _describe_rows(answer: ChatAnswer) -> str:
    """Fallback prose when the phrasing call fails but the data arrived."""
    if answer.row_count == 0:
        return "The query returned no matching records."
    return (
        f"The query returned {answer.row_count} row(s) with columns "
        f"{', '.join(answer.columns)}. The rows are included in this response."
    )
