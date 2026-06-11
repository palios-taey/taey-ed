"""Self-learning substrate for spark_v2."""

from spark_v2.learning.cache import (
    CLEANLINESS_REQUIRED,
    CROSS_UNIT_HOLDOUT_MIN,
    INVALIDATION_AT,
    LOWER_CI_THRESHOLD,
    VERIFIED_COUNT_MIN,
)
from spark_v2.learning.cleanliness import cleanliness_score, is_clean
from spark_v2.learning.outcome_log import get_outcomes_for, get_platform_outcomes, log_outcome
from spark_v2.learning.promotion import attempt_promotion, lower_95_ci_clopper_pearson
from spark_v2.learning.provenance import compute_provenance_hash

__all__ = [
    "CLEANLINESS_REQUIRED",
    "CROSS_UNIT_HOLDOUT_MIN",
    "INVALIDATION_AT",
    "LOWER_CI_THRESHOLD",
    "VERIFIED_COUNT_MIN",
    "attempt_promotion",
    "cleanliness_score",
    "compute_provenance_hash",
    "get_outcomes_for",
    "get_platform_outcomes",
    "is_clean",
    "log_outcome",
    "lower_95_ci_clopper_pearson",
]
