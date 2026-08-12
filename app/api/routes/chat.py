"""Natural-language question answering over the governance database.

A reporting surface, not a decision surface. It reads what the deterministic
engine already decided and what identities already hold; it never produces a new
access decision. See `app.services.chat_service` for why that boundary exists
and how it is kept.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import ChatSvc
from app.schemas.api import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="Ask a question about the governance data",
    description=(
        "Translates a question into a single read-only SELECT, runs it in a "
        "READ ONLY transaction against an allow-list of tables, and answers from "
        "the rows returned.\n\n"
        "The generated SQL and the rows behind the answer are always included so "
        "the answer can be checked.\n\n"
        "**This does not decide access.** It reports current holdings and the "
        "outcomes of past analyses. Access decisions are made deterministically "
        "by the recommendation engine, which no model participates in.\n\n"
        "Requires a configured LLM (`LLM_PROVIDER`, `LLM_API_KEY`, "
        "`DEMO_MODE=false`); returns 503 otherwise. Set `CHAT_LLM_MODEL` to run "
        "chat on a different model from the explanation layer."
    ),
    responses={
        503: {"description": "No LLM is configured, so questions cannot be interpreted."},
    },
)
def ask(payload: ChatRequest, service: ChatSvc) -> ChatResponse:
    answer = service.ask(payload.question, max_rows=payload.max_rows)
    return ChatResponse(
        question=answer.question,
        answer=answer.answer,
        sql=answer.sql,
        tables=answer.tables,
        columns=answer.columns,
        rows=answer.rows,
        row_count=answer.row_count,
        truncated=answer.truncated,
        generator=answer.generator,
        model=answer.model,
        error=answer.error,
    )
