"""FastAPI dependencies.

Routes are declared with `def` (not `async def`) so FastAPI runs them in its
worker threadpool. That is what lets them use the synchronous SQLAlchemy
session directly without blocking the event loop, and keeps one session style
across REST, MCP tools and the workflow.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import session_scope
from app.services.analysis_service import AnalysisService


def get_db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_analysis_service(session: DbSession) -> AnalysisService:
    return AnalysisService(session)


AnalysisSvc = Annotated[AnalysisService, Depends(get_analysis_service)]
