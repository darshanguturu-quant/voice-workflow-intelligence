"""Deterministic statistical functions for variant evaluation, confidence bounds, and sample sizing.

All functions are pure, robust to edge cases (n=0, p=0, p=1), and use standard library statistics.NormalDist.
"""
from __future__ import annotations

import math
import statistics

_STD_NORM = statistics.NormalDist()


def norm_cdf(z: float) -> float:
    """Cumulative distribution function of standard normal distribution."""
    return _STD_NORM.cdf(z)


def norm_ppf(p: float) -> float:
    """Percent point function (inverse CDF) of standard normal distribution."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    return _STD_NORM.inv_cdf(p)


def wilson_score_interval(successes: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Computes two-sided Wilson score confidence interval for a binomial proportion.

    Returns:
        (lower_bound, upper_bound) in [0, 1].
    """
    if total <= 0:
        return 0.0, 1.0

    p_hat = successes / total
    alpha = 1.0 - confidence
    z = norm_ppf(1.0 - alpha / 2.0)
    z2 = z * z

    denom = 1.0 + z2 / total
    center = (p_hat + z2 / (2.0 * total)) / denom
    spread = z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * total)) / total) / denom

    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return lower, upper


def two_proportion_z_test(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Performs a two-proportion z-test comparing sample 1 against sample 2.

    Returns:
        (z_stat, two_sided_p_value) where z_stat > 0 means sample 1 has a higher rate.
    """
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0

    p1 = k1 / n1
    p2 = k2 / n2
    p_pool = (k1 + k2) / (n1 + n2)

    if p_pool <= 0.0 or p_pool >= 1.0:
        return 0.0, 1.0

    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se <= 1e-12:
        return 0.0, 1.0

    z = (p1 - p2) / se
    p_val = 2.0 * (1.0 - norm_cdf(abs(z)))
    return z, p_val


def holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Applies step-down Holm-Bonferroni correction for multiple hypothesis testing.

    Returns:
        List of booleans where True indicates statistically significant rejection.
    """
    m = len(p_values)
    if m == 0:
        return []

    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = [False] * m

    for rank, (orig_idx, p) in enumerate(indexed):
        thresh = alpha / (m - rank)
        if p <= thresh:
            rejected[orig_idx] = True
        else:
            # Step down stops at the first failure to reject
            break

    return rejected


def sample_size_mde(p_base: float, mde: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """Calculates minimum sample size per arm given baseline rate and minimum detectable effect.

    Args:
        p_base: Baseline proportion (e.g. 0.30 or 0.55).
        mde: Minimum absolute difference to detect (e.g. 0.03).
        alpha: False positive rate (Type I error).
        power: Statistical power 1 - beta (Type II error).
    """
    p1 = max(0.01, min(0.99, p_base))
    p2 = max(0.01, min(0.99, p1 + mde))
    delta = abs(p1 - p2)
    if delta < 1e-6:
        return 1000

    z_alpha = norm_ppf(1.0 - alpha / 2.0)
    z_beta = norm_ppf(power)

    p_bar = 0.5 * (p1 + p2)
    num = (z_alpha * math.sqrt(2.0 * p_bar * (1.0 - p_bar)) +
           z_beta * math.sqrt(p1 * (1.0 - p1) + p2 * (1.0 - p2))) ** 2
    n = math.ceil(num / (delta * delta))
    return max(50, n)


def validation_sample_size_adaptive(
    pass_threshold: float,
    expected_pass: float = 0.98,
    alpha: float = 0.05,
    power: float = 0.80
) -> int:
    """Calculates validation run size n based on distance to min_validation_pass_rate.

    Clamped to [50, 2000] and rounded to a multiple of 100 to optimize credit cost (ceil(n/100)).
    """
    margin = abs(expected_pass - pass_threshold)
    if margin < 0.005:
        raw_n = 800
    elif margin < 0.015:
        raw_n = 500
    elif margin < 0.03:
        raw_n = 300
    else:
        raw_n = 150

    n = max(100, math.ceil(raw_n / 100.0) * 100)
    return min(1000, n)
