"""SQLAlchemy declarative base and shared column types.

PostgreSQL-specific types (UUID, JSONB) are used deliberately: this system
targets PostgreSQL only and there is no SQLite fallback.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import DateTime, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Predictable constraint names keep Alembic autogenerate diffs stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_uuid() -> str:
    return str(uuid.uuid4())


# UUID stored natively in PostgreSQL but surfaced to Python as `str`, which
# keeps every domain model and JSON payload free of UUID coercion noise.
StrUUID = UUID(as_uuid=False)

# --- Reusable annotated column types ---------------------------------------
BusinessKey = Annotated[str, mapped_column(String(64), primary_key=True)]
ShortStr = Annotated[str, mapped_column(String(128))]
LongStr = Annotated[str, mapped_column(String(512))]


def uuid_pk() -> Any:
    return mapped_column(StrUUID, primary_key=True, default=new_uuid)


def created_at_col() -> Any:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def updated_at_col() -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def jsonb_col(**kwargs: Any) -> Any:
    return mapped_column(JSONB, **kwargs)


__all__ = [
    "Base",
    "BusinessKey",
    "JSONB",
    "LongStr",
    "ShortStr",
    "StrUUID",
    "created_at_col",
    "datetime",
    "jsonb_col",
    "new_uuid",
    "updated_at_col",
    "uuid_pk",
]
