"""Peer-matching confidence scoring."""

from __future__ import annotations

import pytest

from app.domain.enums import MatchingStrategy
from app.services.peer_service import (
    STRATEGY_BASE_CONFIDENCE,
    STRATEGY_ORDER,
    compute_confidence,
)


def test_strategies_are_ordered_most_to_least_precise() -> None:
    assert STRATEGY_ORDER == (
        MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL,
        MatchingStrategy.JOB_ROLE_DEPARTMENT,
        MatchingStrategy.DEPARTMENT_JOB_LEVEL,
        MatchingStrategy.DEPARTMENT,
    )
    scores = [STRATEGY_BASE_CONFIDENCE[s] for s in STRATEGY_ORDER]
    assert scores == sorted(scores, reverse=True), "looser strategies must score lower"


def test_no_peers_means_no_confidence() -> None:
    assert compute_confidence(MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL, 0) == 0.0
    assert compute_confidence(MatchingStrategy.NONE, 5) == 0.0


def test_exact_match_at_saturation_scores_the_base_weight() -> None:
    assert compute_confidence(MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL, 8) == 0.95


def test_confidence_rises_with_group_size_then_saturates() -> None:
    strategy = MatchingStrategy.JOB_ROLE_DEPARTMENT_JOB_LEVEL
    values = [compute_confidence(strategy, n) for n in (1, 2, 4, 8)]
    assert values == sorted(values)
    assert compute_confidence(strategy, 20) == compute_confidence(strategy, 8)


@pytest.mark.parametrize("peer_count", [1, 3, 8, 50])
def test_a_looser_strategy_never_outscores_a_tighter_one(peer_count: int) -> None:
    scores = [compute_confidence(s, peer_count) for s in STRATEGY_ORDER]
    assert scores == sorted(scores, reverse=True)


def test_confidence_stays_within_bounds() -> None:
    for strategy in STRATEGY_ORDER:
        for peer_count in (0, 1, 5, 100):
            assert 0.0 <= compute_confidence(strategy, peer_count) <= 1.0
