"""Segregation of Duties (Phase 9).

Checks the access the identity would end up holding - the requested set *plus*
what they already hold - against every enabled toxic-combination rule. Checking
only the requested set would miss the most common real-world conflict, where a
single new entitlement collides with access granted months earlier.

Any conflict is terminal for both sides of the pair: the entitlements are
blocked and routed to human review. Severity informs the reviewer; it never
downgrades the outcome.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.repositories.catalog_repo import SodRuleRepository
from app.db.repositories.employee_repo import EmployeeRepository
from app.domain.enums import Severity, SodStatus, max_severity
from app.domain.models import SodConflict, SodValidationResult
from app.logging import get_logger

logger = get_logger(__name__)


class SodService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.rules = SodRuleRepository(session)
        self.employees = EmployeeRepository(session)

    def check(
        self,
        employee_id: str,
        entitlement_ids: list[str],
        *,
        existing_entitlement_ids: list[str] | None = None,
    ) -> SodValidationResult:
        requested = list(dict.fromkeys(entitlement_ids))
        if existing_entitlement_ids is None:
            existing_entitlement_ids = self.employees.get_entitlement_ids(employee_id)
        existing = set(existing_entitlement_ids)
        effective = set(requested) | existing

        enabled_rules = self.rules.list_enabled()

        conflicts: list[SodConflict] = []
        for rule in enabled_rules:
            first, second = rule.entitlement_1, rule.entitlement_2
            if first not in effective or second not in effective:
                continue
            # At least one side must be newly requested; a pre-existing
            # conflict is a remediation matter, not a joiner decision.
            if first not in requested and second not in requested:
                continue

            severity = _coerce_severity(rule.severity)
            touches_existing = (first in existing) or (second in existing)
            conflicts.append(
                SodConflict(
                    sod_id=rule.sod_id,
                    name=rule.name,
                    entitlement_1=first,
                    entitlement_2=second,
                    severity=severity,
                    reason=(
                        rule.description
                        or f"{first} and {second} must not be held by the same identity."
                    ),
                    conflicts_with_existing_access=touches_existing,
                )
            )

        status = SodStatus.CONFLICT if conflicts else SodStatus.PASS
        severity = max_severity([c.severity.value for c in conflicts]) if conflicts else None

        logger.info(
            "sod.evaluated",
            employee_id=employee_id,
            entitlements=len(requested),
            rules_evaluated=len(enabled_rules),
            conflicts=len(conflicts),
            status=status.value,
            severity=severity.value if severity else None,
        )
        return SodValidationResult(
            employee_id=employee_id,
            status=status,
            severity=severity,
            conflicts=conflicts,
            evaluated_entitlement_ids=requested,
            evaluated_rule_ids=[r.sod_id for r in enabled_rules],
        )


def _coerce_severity(value: str) -> Severity:
    try:
        return Severity(value.upper())
    except ValueError:
        logger.warning("sod.unknown_severity", value=value)
        return Severity.HIGH
