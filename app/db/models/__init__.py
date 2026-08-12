"""SQLAlchemy ORM models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test harness rely on.
"""

from app.db.base import Base
from app.db.models.analysis import (
    JoinerAnalysis,
    PolicyResult,
    Recommendation,
    RecommendationEvidence,
    RecommendationExplanationRow,
    SailPointRequest,
    SodResult,
)
from app.db.models.governance import Policy, SodRule
from app.db.models.identity import Employee, EmployeeEntitlement, Entitlement

__all__ = [
    "Base",
    "Employee",
    "EmployeeEntitlement",
    "Entitlement",
    "JoinerAnalysis",
    "Policy",
    "PolicyResult",
    "Recommendation",
    "RecommendationEvidence",
    "RecommendationExplanationRow",
    "SailPointRequest",
    "SodResult",
    "SodRule",
]
