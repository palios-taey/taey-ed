"""Clopper-Pearson promotion math for cache promotion."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from spark_v2.learning.cache import (
    CLEANLINESS_REQUIRED,
    CROSS_UNIT_HOLDOUT_MIN,
    LOWER_CI_THRESHOLD,
    VERIFIED_COUNT_MIN,
    _determine_cache_class,
    store_cached_bt,
)
from spark_v2.learning.cleanliness import cleanliness_score, is_clean
from spark_v2.learning.outcome_log import get_outcomes_for, log_promotion_event
from spark_v2.learning.provenance import compute_provenance_hash

try:
    from scipy.stats import beta as scipy_beta
except Exception:
    scipy_beta = None

OUTCOME_HISTORY_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    def betacf(alpha: float, beta_value: float, value: float) -> float:
        max_iterations = 200
        epsilon = 3e-14
        qab = alpha + beta_value
        qap = alpha + 1.0
        qam = alpha - 1.0
        c = 1.0
        d = 1.0 - qab * value / qap
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        h = d
        for iteration in range(1, max_iterations + 1):
            m2 = 2 * iteration
            aa = iteration * (beta_value - iteration) * value / ((qam + m2) * (alpha + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30:
                d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            h *= d * c
            aa = -(alpha + iteration) * (qab + iteration) * value / ((alpha + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30:
                d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < epsilon:
                break
        return h

    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * betacf(a, b, x) / a
    return 1.0 - front * betacf(b, a, 1.0 - x) / b


def _inverse_regularized_incomplete_beta(probability: float, a: float, b: float) -> float:
    low = 0.0
    high = 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        value = _regularized_incomplete_beta(mid, a, b)
        if abs(value - probability) < 1e-12:
            return mid
        if value < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _beta_ppf(probability: float, a: float, b: float) -> float:
    if scipy_beta is not None:
        return float(scipy_beta.ppf(probability, a, b))
    return _inverse_regularized_incomplete_beta(probability, a, b)


def lower_95_ci_clopper_pearson(successes: int, total: int) -> float:
    if successes < 0 or total < successes:
        raise ValueError("invalid counts")
    if successes == 0:
        return 0.0
    if successes == total:
        return _beta_ppf(0.025, total, 1)
    return _beta_ppf(0.025, successes, total - successes + 1)


def _canonical_bt_from_clean_successes(clean_successes: list[dict]) -> dict:
    if not clean_successes:
        raise ValueError("clean_successes must not be empty")
    buckets: dict[str, dict[str, Any]] = {}
    for outcome in clean_successes:
        serialized = json.dumps(outcome.get("plan"), sort_keys=True, separators=(",", ":"))
        bucket = buckets.setdefault(serialized, {"count": 0, "latest_timestamp": "", "plan": outcome.get("plan")})
        bucket["count"] += 1
        timestamp = str(outcome.get("timestamp") or "")
        if timestamp >= bucket["latest_timestamp"]:
            bucket["latest_timestamp"] = timestamp
            bucket["plan"] = outcome.get("plan")
    winner = max(
        buckets.values(),
        key=lambda bucket: (bucket["count"], bucket["latest_timestamp"]),
    )
    return json.loads(json.dumps(winner["plan"]))


def attempt_promotion(platform: str, skeleton_hash: str, screen_type: str) -> dict | None:
    relevant = get_outcomes_for(skeleton_hash, platform, limit=OUTCOME_HISTORY_LIMIT)
    clean_successes = [outcome for outcome in relevant if is_clean(outcome)]
    verified_count = len(clean_successes)
    total_attempts = len(relevant)

    if total_attempts == 0 or verified_count < VERIFIED_COUNT_MIN:
        return None

    lower_bound = lower_95_ci_clopper_pearson(verified_count, total_attempts)
    if not (lower_bound > LOWER_CI_THRESHOLD):
        return None

    validated_courses = sorted(
        {
            str(outcome.get("course_id") or "")
            for outcome in clean_successes
            if str(outcome.get("course_id") or "")
        }
    )
    if len(validated_courses) < CROSS_UNIT_HOLDOUT_MIN:
        return None

    score = cleanliness_score(relevant)
    if score != CLEANLINESS_REQUIRED:
        return None

    consult_ids = [str(outcome.get("consultation_id") or "") for outcome in clean_successes if outcome.get("consultation_id")]
    if not consult_ids:
        return None

    entry = {
        "cache_class": _determine_cache_class(screen_type),
        "bt": _canonical_bt_from_clean_successes(clean_successes),
        "screen_type": screen_type,
        "screen_variant_hash": skeleton_hash,
        "verified_count": verified_count,
        "p_success_lower_95ci": lower_bound,
        "validated_courses": validated_courses,
        "cleanliness_score": score,
        "promoted_at": _now(),
        "last_validated_at": str(clean_successes[-1].get("timestamp") or _now()),
        "consecutive_failures_post_promotion": 0,
        "provenance_hash": compute_provenance_hash(consult_ids),
        "provenance_consults": consult_ids,
    }
    store_cached_bt(platform, skeleton_hash, entry)
    log_promotion_event(platform, skeleton_hash, entry)
    return entry
