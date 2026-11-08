"""Unit tests for controller/stats.py statistical functions."""
import math
import pytest

from controller.stats import (
    norm_cdf,
    norm_ppf,
    wilson_score_interval,
    two_proportion_z_test,
    holm_bonferroni,
    sample_size_mde,
    validation_sample_size_adaptive,
)


def test_norm_cdf_and_ppf():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-6
    assert abs(norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(norm_ppf(0.5) - 0.0) < 1e-6
    assert abs(norm_ppf(0.975) - 1.95996) < 1e-3


def test_wilson_score_interval():
    # n = 0 edge case
    lo, hi = wilson_score_interval(0, 0)
    assert lo == 0.0 and hi == 1.0

    # 50 successes out of 100
    lo, hi = wilson_score_interval(50, 100, 0.95)
    assert 0.40 < lo < 0.50
    assert 0.50 < hi < 0.60
    assert lo < 0.5 < hi

    # 0 out of 100
    lo, hi = wilson_score_interval(0, 100, 0.95)
    assert lo == 0.0
    assert hi > 0.0 and hi < 0.05


def test_two_proportion_z_test():
    # Equal proportions -> z near 0, p near 1
    z, p = two_proportion_z_test(50, 100, 50, 100)
    assert abs(z) < 1e-6
    assert abs(p - 1.0) < 1e-6

    # Large difference: 80/100 vs 20/100
    z, p = two_proportion_z_test(80, 100, 20, 100)
    assert z > 8.0
    assert p < 1e-6

    # Edge cases
    z, p = two_proportion_z_test(0, 0, 5, 10)
    assert z == 0.0 and p == 1.0


def test_holm_bonferroni():
    # Empty
    assert holm_bonferroni([]) == []

    # Two hypotheses: p = [0.01, 0.04], alpha = 0.05
    # Rank 1: p=0.01 <= 0.05/2 = 0.025 -> True
    # Rank 2: p=0.04 <= 0.05/1 = 0.05 -> True
    res = holm_bonferroni([0.01, 0.04], alpha=0.05)
    assert res == [True, True]

    # Two hypotheses: p = [0.03, 0.04]
    # Rank 1: p=0.03 > 0.05/2 = 0.025 -> False -> Step down stops! Both False
    res = holm_bonferroni([0.03, 0.04], alpha=0.05)
    assert res == [False, False]


def test_sample_size_mde():
    n = sample_size_mde(0.5, 0.05)
    assert 1000 < n < 2000

    n_small = sample_size_mde(0.5, 0.15)
    assert 100 < n_small < 500


def test_validation_sample_size_adaptive():
    # Close margin (e.g. 0.975 vs 0.97 threshold -> margin 0.005)
    n_close = validation_sample_size_adaptive(pass_threshold=0.97, expected_pass=0.975)
    assert n_close >= 500

    # Large margin (e.g. 0.995 vs 0.95 -> margin 0.045)
    n_far = validation_sample_size_adaptive(pass_threshold=0.95, expected_pass=0.995)
    assert n_far <= 300
