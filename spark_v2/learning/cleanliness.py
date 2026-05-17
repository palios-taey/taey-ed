"""Cleanliness gate for promotion candidacy."""

from __future__ import annotations


def is_clean(outcome: dict) -> bool:
    return (
        outcome.get("success") is True
        and outcome.get("tier", 0) == 0
        and not outcome.get("wrong_answer_retry", False)
        and outcome.get("worker_fallback", False) is False
        and outcome.get("step2_validated") is True
    )


def cleanliness_score(outcomes: list[dict]) -> float:
    total_successes = sum(1 for outcome in outcomes if outcome.get("success") is True)
    if total_successes == 0:
        return 0.0
    clean_successes = sum(
        1 for outcome in outcomes if outcome.get("success") is True and is_clean(outcome)
    )
    return clean_successes / total_successes
