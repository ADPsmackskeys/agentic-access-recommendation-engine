"""Affinity calculation."""

from __future__ import annotations

import pytest

from app.services.affinity_service import calculate_affinity_score


@pytest.mark.parametrize(
    ("holders", "total", "expected"),
    [
        (8, 8, 100.0),
        (7, 8, 87.5),
        (6, 8, 75.0),
        (2, 8, 25.0),
        (5, 6, 83.33),
        (4, 6, 66.67),
        (1, 3, 33.33),
        (0, 8, 0.0),
    ],
)
def test_affinity_score(holders: int, total: int, expected: float) -> None:
    assert calculate_affinity_score(holders, total) == expected


def test_affinity_with_no_peers_is_zero_not_an_error() -> None:
    """A division by zero here would crash an analysis mid-flight."""
    assert calculate_affinity_score(0, 0) == 0.0
    assert calculate_affinity_score(3, 0) == 0.0


def test_threshold_comparison_is_inclusive() -> None:
    """A score exactly on the threshold is recommended, not withheld."""
    threshold = 75.0
    assert calculate_affinity_score(6, 8) >= threshold
