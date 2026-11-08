"""Unit tests for controller/experiments.py."""
import tempfile
from pathlib import Path

from controller.db import Database
from controller.experiments import (
    evaluate_and_propose_variants,
    plan_caller_id_rotation,
    plan_exploration_allocation,
)


def test_plan_caller_id_rotation():
    spec = {
        "options": {"caller_id": ["D1", "D2", "D3", "D4"]},
        "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.5}},
    }
    cur_cfg = {
        "allocations": {
            "default": {
                "caller_id": {"D1": 0.5, "D4": 0.5},
            }
        }
    }

    # D4 degraded: rotate away
    rot_cfg = plan_caller_id_rotation(spec, cur_cfg, degraded_dids=["D4"], auto_apply_permitted=True, max_shift_limit=0.5)
    assert rot_cfg is not None
    new_alloc = rot_cfg["allocations"]["default"]["caller_id"]
    # D4 should be reduced by max_shift (0.5 - 0.5 = 0.0)
    assert new_alloc.get("D4", 0.0) == 0.0
    # Healthy DIDs (D1, D2, D3) get traffic
    assert new_alloc["D1"] > 0
    assert sum(new_alloc.values()) == 1.0


def test_plan_exploration_allocation():
    spec = {
        "options": {
            "voice": ["V1", "V2", "V3", "V4"],
            "welcome_message": ["W1", "W2"],
        }
    }
    cur_cfg = {
        "allocations": {
            "default": {
                "voice": {"V1": 1.0},
                "welcome_message": {"W1": 1.0},
            }
        }
    }

    explore_cfg = plan_exploration_allocation(spec, cur_cfg, cohort_id="default", auto_apply_permitted=False)
    assert explore_cfg is not None
    v_alloc = explore_cfg["allocations"]["default"]["voice"]
    w_alloc = explore_cfg["allocations"]["default"]["welcome_message"]

    # Other options should have non-zero allocation
    assert v_alloc["V2"] > 0
    assert v_alloc["V3"] > 0
    assert w_alloc["W2"] > 0
    assert sum(v_alloc.values()) == 1.0
    assert sum(w_alloc.values()) == 1.0


def test_evaluate_and_propose_variants_guardrail():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        spec = {
            "options": {"voice": ["V1", "V2"]},
            "objective": {
                "primary": "engagement_rate",
                "guardrail": {"pickup_rate_max_drop": 0.02},
            },
        }
        cur_cfg = {
            "allocations": {
                "default": {"voice": {"V1": 0.5, "V2": 0.5}},
            }
        }

        # Case 1: V2 has higher engagement (0.70 vs 0.50), but violates pickup drop limit:
        # V1 pickup = 100/200 = 0.50, V2 pickup = 80/200 = 0.40 (drop = 0.10 > 0.02)
        agg_violating = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 200, "connected": 100, "engaged": 50},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 200, "connected": 80, "engaged": 56},
        }
        db.commit_chunk(0, 400, 60, "2027-01-04T01:00:00+05:30", agg_violating, {}, [])

        res = evaluate_and_propose_variants(db, "nw_renewal", spec, current_version=1, current_config=cur_cfg)
        # Should be rejected because guardrail was violated!
        assert res is None

        # Case 2: V2 has higher engagement (0.80 vs 0.50) consistently across 2 independent time blocks
        db2 = Database(Path(td) / "test2.db")
        agg_block1 = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 150, "connected": 75, "engaged": 35},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 150, "connected": 75, "engaged": 60},
        }
        agg_block2 = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 150, "connected": 75, "engaged": 40},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 150, "connected": 75, "engaged": 60},
        }
        # Block 0 (minute 60) and Block 1 (minute 750)
        db2.commit_chunk(0, 300, 60, "2027-01-04T01:00:00+05:30", agg_block1, {}, [])
        db2.commit_chunk(1, 600, 750, "2027-01-04T12:30:00+05:30", agg_block2, {}, [])

        res_valid = evaluate_and_propose_variants(db2, "nw_renewal", spec, current_version=1, current_config=cur_cfg, mde=0.05)
        assert res_valid is not None
        assert res_valid["winner_voice"] == "V2"
        assert res_valid["lift"] > 0.20

        # Case 3: Two-Block Promotion Rule rejects decaying variants (e.g. W3 pattern)
        # Higher in block 0 (+0.30 lift), but decays in block 1 (-0.20 lift)
        db3 = Database(Path(td) / "test3.db")
        agg_spike = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 150, "connected": 75, "engaged": 35},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 150, "connected": 75, "engaged": 65},
        }
        agg_decay = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 150, "connected": 75, "engaged": 50},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 150, "connected": 75, "engaged": 30},
        }
        db3.commit_chunk(0, 300, 60, "2027-01-04T01:00:00+05:30", agg_spike, {}, [])
        db3.commit_chunk(1, 600, 750, "2027-01-04T12:30:00+05:30", agg_decay, {}, [])

        res_decay = evaluate_and_propose_variants(db3, "nw_renewal", spec, current_version=1, current_config=cur_cfg, mde=0.05)
        # Must be rejected because it failed positive lift consistency in Block 1!
        assert res_decay is None
