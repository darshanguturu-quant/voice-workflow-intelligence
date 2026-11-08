"""Unit tests for controller/cohorts.py."""
from controller.cohorts import (
    match_cohort,
    get_dimension_distribution,
    max_shift,
    changed_dims,
    compute_diff,
)


def test_match_cohort():
    cohorts = [
        {"id": "senior_ta", "where": {"age_band": ["60+"], "lang": ["ta"]}},
        {"id": "young", "where": {"age_band": ["18-30"]}},
    ]

    # Matches first cohort
    assert match_cohort({"age_band": "60+", "lang": "ta", "city_tier": "1"}, cohorts) == "senior_ta"

    # Matches second cohort
    assert match_cohort({"age_band": "18-30", "lang": "hi"}, cohorts) == "young"

    # Matches default
    assert match_cohort({"age_band": "31-45", "lang": "en"}, cohorts) == "default"


def test_max_shift_and_changed_dims():
    old_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 1.0},
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {},
    }

    new_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 0.8, "V2": 0.2},
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {},
    }

    assert changed_dims(old_cfg, new_cfg) == ["voice"]
    # Total variation distance: 0.5 * (|1.0 - 0.8| + |0.0 - 0.2|) = 0.5 * 0.4 = 0.2
    shift = max_shift(old_cfg, new_cfg)
    assert abs(shift - 0.2) < 1e-6

    diff = compute_diff(old_cfg, new_cfg)
    assert "allocations.voice" in diff
