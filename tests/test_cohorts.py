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


def test_cohort_redefinition_does_not_inherit_incompatible_stats():
    """Proves that redefined cohorts and prompt changes do not inherit incompatible historical statistics (PRB-017)."""
    import tempfile
    from pathlib import Path
    from controller.db import Database
    from controller.cohorts import compute_cohort_definition_hash

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test_cohort.db")
        wid = "nw_renewal"

        # Config v1: "tier1" defined strictly as city_tier == ["1"], prompt P1
        cfg_v1 = {
            "cohorts": [{"id": "tier1", "where": {"city_tier": ["1"]}}],
            "prompt_version": "P1",
            "allocations": {"tier1": {"voice": {"V1": 1.0}, "welcome_message": {"W1": 1.0}}},
        }
        db.record_config_version(wid, config_version=1, config=cfg_v1)
        # Commit v1 calls: 100 dials, 100 connected, 80 engaged
        agg_v1 = {
            (wid, "tier1", 1, "W1", "V1", "D1"): {"dials": 100, "connected": 100, "engaged": 80},
        }
        db.commit_chunk(1, 100, 60, "2027-01-04T01:00:00+05:30", agg_v1, {}, [])

        # Config v2: "tier1" REDEFINED to encompass city_tier == ["1", "2", "3"] (cohort definition changed!)
        cfg_v2 = {
            "cohorts": [{"id": "tier1", "where": {"city_tier": ["1", "2", "3"]}}],
            "prompt_version": "P1",
            "allocations": {"tier1": {"voice": {"V1": 1.0}, "welcome_message": {"W1": 1.0}}},
        }
        db.record_config_version(wid, config_version=2, config=cfg_v2)
        # Commit v2 calls: 20 dials, 20 connected, 10 engaged
        agg_v2 = {
            (wid, "tier1", 2, "W1", "V1", "D1"): {"dials": 20, "connected": 20, "engaged": 10},
        }
        db.commit_chunk(2, 120, 120, "2027-01-04T02:00:00+05:30", agg_v2, {}, [])

        # Verify cohort definition hashes are different
        hash_v1 = compute_cohort_definition_hash("tier1", cfg_v1["cohorts"])
        hash_v2 = compute_cohort_definition_hash("tier1", cfg_v2["cohorts"])
        assert hash_v1 != hash_v2

        # In v2, comparable versions must ONLY include [2], excluding incompatible v1!
        comp_v2 = db.get_comparable_config_versions(wid, "tier1", target_version=2)
        assert comp_v2 == [2]

        # Query stats using comparable versions
        stats_v2 = db.get_variant_stats(wid, "tier1", config_version=comp_v2)
        assert len(stats_v2) == 1
        assert stats_v2[0]["connected"] == 20  # Excludes v1's 100 calls!
        assert stats_v2[0]["engaged"] == 10

        # Config v3: Same cohort definition as v2, same prompt P1, but explores V2 (allocation shift)
        cfg_v3 = {
            "cohorts": [{"id": "tier1", "where": {"city_tier": ["1", "2", "3"]}}],
            "prompt_version": "P1",
            "allocations": {"tier1": {"voice": {"V1": 0.85, "V2": 0.15}, "welcome_message": {"W1": 1.0}}},
        }
        db.record_config_version(wid, config_version=3, config=cfg_v3)
        agg_v3 = {
            (wid, "tier1", 3, "W1", "V1", "D1"): {"dials": 30, "connected": 30, "engaged": 15},
            (wid, "tier1", 3, "W1", "V2", "D1"): {"dials": 10, "connected": 10, "engaged": 8},
        }
        db.commit_chunk(3, 160, 180, "2027-01-04T03:00:00+05:30", agg_v3, {}, [])

        # In v3, cohort definition matches v2, so comparable versions pools [2, 3]!
        comp_v3 = db.get_comparable_config_versions(wid, "tier1", target_version=3)
        assert comp_v3 == [2, 3]

        stats_v3 = db.get_variant_stats(wid, "tier1", config_version=comp_v3)
        v1_stat = next(s for s in stats_v3 if s["voice"] == "V1")
        assert v1_stat["connected"] == 50  # 20 from v2 + 30 from v3 (v1 still excluded!)
        assert v1_stat["engaged"] == 25

        # Config v4: Prompt version changes to P2 (prompt changed!)
        cfg_v4 = {
            "cohorts": [{"id": "tier1", "where": {"city_tier": ["1", "2", "3"]}}],
            "prompt_version": "P2",
            "allocations": cfg_v3["allocations"],
        }
        db.record_config_version(wid, config_version=4, config=cfg_v4)
        comp_v4 = db.get_comparable_config_versions(wid, "tier1", target_version=4)
        # Cannot pool with v2 or v3 because prompt version changed!
        assert comp_v4 == [4]

