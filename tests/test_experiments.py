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


def test_evaluate_and_propose_variants_insufficient_sample_size_deferred():
    """Verify that when traffic cannot reach the required sample size, promotion is deferred (returns None)."""
    with tempfile.TemporaryDirectory() as td:
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

        # Sub-case A: Total sample size < min_required_calls (e.g. only 20 connected calls)
        db_small = Database(Path(td) / "small.db")
        agg_small = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 100, "connected": 50, "engaged": 25},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 30, "connected": 20, "engaged": 18},
        }
        db_small.commit_chunk(0, 130, 60, "2027-01-04T01:00:00+05:30", agg_small, {}, [])

        res_small = evaluate_and_propose_variants(db_small, "nw_renewal", spec, current_version=1, current_config=cur_cfg, mde=0.05)
        # Even though V2 had 18/20 = 90% engagement vs V1's 50%, n=20 < min_required_calls (40), so promotion is deferred
        assert res_small is None

        # Sub-case B: Candidate has enough pooled calls (e.g. 80 connected), but only 1 block reached n >= 15
        db_one_block = Database(Path(td) / "one_block.db")
        # Block 0: 75 calls
        agg_b0 = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 150, "connected": 75, "engaged": 35},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 150, "connected": 75, "engaged": 60},
        }
        # Block 1: only 5 calls (insufficient to form an independent evaluation block)
        agg_b1 = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 10, "connected": 5, "engaged": 2},
            ("nw_renewal", "default", 1, "W1", "V2", "D1"): {"dials": 10, "connected": 5, "engaged": 4},
        }
        db_one_block.commit_chunk(0, 300, 60, "2027-01-04T01:00:00+05:30", agg_b0, {}, [])
        db_one_block.commit_chunk(1, 20, 750, "2027-01-04T12:30:00+05:30", agg_b1, {}, [])

        res_one_block = evaluate_and_propose_variants(db_one_block, "nw_renewal", spec, current_version=1, current_config=cur_cfg, mde=0.05)
        # Candidate has high pooled lift and passes Holm-Bonferroni, but len(evaluable_blocks) == 1 < 2.
        # Promotion must be deferred!
        assert res_one_block is None


def test_variant_cooldown_expiry_and_retesting():
    """Verify that a decaying variant enters cooldown, expires, and receives a low probe share upon re-testing."""
    from controller.experiments import VariantCooldownManager

    mgr = VariantCooldownManager(default_cooldown_minutes=1440, retest_share=0.05)
    wid = "nw_renewal"

    # Step 1: Record underperformance at min 600
    until = mgr.record_underperformance(wid, "voice", "V3", current_minute=600)
    assert until == 2040
    assert mgr.get_status(wid, "voice", "V3", current_minute=1000) == "cooldown"
    assert ("voice", "V3") in mgr.get_blocked_variants(wid, current_minute=1000)
    assert ("voice", "V3") not in mgr.get_retesting_variants(wid, current_minute=1000)

    # Step 2: Cooldown expires at min 2040
    assert mgr.get_status(wid, "voice", "V3", current_minute=2040) == "retesting"
    assert ("voice", "V3") not in mgr.get_blocked_variants(wid, current_minute=2040)
    retest_vars = mgr.get_retesting_variants(wid, current_minute=2040)
    assert retest_vars.get(("voice", "V3")) == 0.05

    # Step 3: Test exploration allocation with re-testing variant
    spec = {
        "options": {
            "voice": ["V1", "V2", "V3"],
            "welcome_message": ["W1"],
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
    # When V3 is retesting and V2 is a standard unexplored variant:
    explore_cfg = plan_exploration_allocation(
        workflow_spec=spec,
        current_config=cur_cfg,
        cohort_id="default",
        exploration_cap=0.15,
        blacklisted_variants=mgr.get_blocked_variants(wid, current_minute=2040),
        retesting_variants=mgr.get_retesting_variants(wid, current_minute=2040),
    )
    assert explore_cfg is not None
    v_alloc = explore_cfg["allocations"]["default"]["voice"]
    assert v_alloc["V3"] == 0.05  # Capped at low probe share
    assert v_alloc["V2"] == 0.10  # Standard unexplored gets remaining exploration budget (0.15 - 0.05)
    assert v_alloc["V1"] == 0.85  # Incumbent retains >= 85% traffic
    assert sum(v_alloc.values()) == 1.0


def test_variant_cooldown_continued_underperformance():
    """Verify that continued underperformance during re-testing applies exponential backoff."""
    from controller.experiments import VariantCooldownManager

    mgr = VariantCooldownManager(default_cooldown_minutes=1440, retest_share=0.05)
    wid = "ks_cancel"

    # Initial penalty at min 500 -> cooldown until 1940 (duration 1440)
    mgr.record_underperformance(wid, "welcome_message", "W2", current_minute=500)
    assert mgr.get_status(wid, "welcome_message", "W2", current_minute=1940) == "retesting"

    # Continued underperformance detected at min 2100 while re-testing
    until_2 = mgr.record_underperformance(wid, "welcome_message", "W2", current_minute=2100)
    # Exponential backoff: duration = 1440 * 2 = 2880 -> 2100 + 2880 = 4980
    assert until_2 == 4980
    assert mgr.get_status(wid, "welcome_message", "W2", current_minute=2200) == "cooldown"
    assert ("welcome_message", "W2") in mgr.get_blocked_variants(wid, current_minute=2200)

    # After prolonged cooldown at min 4980, it re-enters re-testing
    assert mgr.get_status(wid, "welcome_message", "W2", current_minute=4980) == "retesting"

