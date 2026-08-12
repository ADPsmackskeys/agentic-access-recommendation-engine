"""Risk banding and the approval tier each band demands."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.domain.enums import ApprovalTier, RiskLevel
from app.services.risk_service import RISK_LEVEL_TO_TIER, band_bounds, classify_risk


@pytest.fixture
def default_settings() -> Settings:
    return Settings(
        postgres_password="",
        risk_low_max=30,
        risk_medium_max=69,
        risk_high_max=89,
    )


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, RiskLevel.LOW),
        (15, RiskLevel.LOW),
        (30, RiskLevel.LOW),
        (31, RiskLevel.MEDIUM),
        (62, RiskLevel.MEDIUM),
        (69, RiskLevel.MEDIUM),
        (70, RiskLevel.HIGH),
        (88, RiskLevel.HIGH),
        (89, RiskLevel.HIGH),
        (90, RiskLevel.CRITICAL),
        (96, RiskLevel.CRITICAL),
        (100, RiskLevel.CRITICAL),
    ],
)
def test_risk_bands_including_boundaries(
    score: int, expected: RiskLevel, default_settings: Settings
) -> None:
    assert classify_risk(score, default_settings) is expected


@pytest.mark.parametrize(
    ("level", "tier"),
    [
        (RiskLevel.LOW, ApprovalTier.AUTO),
        (RiskLevel.MEDIUM, ApprovalTier.AUTO),
        (RiskLevel.HIGH, ApprovalTier.MANAGER),
        (RiskLevel.CRITICAL, ApprovalTier.HUMAN_REVIEW),
    ],
)
def test_band_to_approval_tier(level: RiskLevel, tier: ApprovalTier) -> None:
    assert RISK_LEVEL_TO_TIER[level] is tier


def test_bands_are_configurable(default_settings: Settings) -> None:
    """Thresholds come from configuration, not from constants in the code."""
    strict = Settings(postgres_password="", risk_low_max=10, risk_medium_max=40,
                      risk_high_max=60)
    assert classify_risk(25, default_settings) is RiskLevel.LOW
    assert classify_risk(25, strict) is RiskLevel.MEDIUM
    assert classify_risk(65, strict) is RiskLevel.CRITICAL


def test_band_bounds_are_reported_for_the_audit_trail(default_settings: Settings) -> None:
    assert band_bounds(RiskLevel.LOW, default_settings) == "0-30"
    assert band_bounds(RiskLevel.MEDIUM, default_settings) == "31-69"
    assert band_bounds(RiskLevel.HIGH, default_settings) == "70-89"
    assert band_bounds(RiskLevel.CRITICAL, default_settings) == "90-100"


def test_misordered_band_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="risk_low_max < risk_medium_max"):
        Settings(postgres_password="", risk_low_max=80, risk_medium_max=40, risk_high_max=90)
