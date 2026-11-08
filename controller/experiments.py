"""Experimentation, variant allocation, and winner promotion engine.

Applies deterministic statistical hypothesis testing with Holm-Bonferroni correction and objective guardrail enforcement.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import logging
from typing import Any

from controller.cohorts import get_dimension_distribution
from controller.db import Database
from controller.stats import holm_bonferroni, sample_size_mde, two_proportion_z_test, wilson_score_interval

logger = logging.getLogger(__name__)


def plan_exploration_allocation(
    workflow_spec: dict[str, Any],
    current_config: dict[str, Any],
    cohort_id: str = "default",
    auto_apply_permitted: bool = False,
    max_shift_limit: float = 0.2,
    exploration_cap: float = 0.15,
    blacklisted_variants: set[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Proposes an exploration split across untried or under-explored options.

    Batches welcome_message and voice exploration to preserve approval slots.
    Enforces a strict exploration cap (10-15%) per cohort, keeping 85%+ on control/winner (Pass 4).
    Excludes variants that have been blacklisted due to confirmed degradation.
    """
    opts = workflow_spec.get("options", {})
    new_cfg = copy.deepcopy(current_config)
    changed = False
    blacklist = blacklisted_variants or set()

    # Determine effective non-incumbent exploration share
    effective_cap = min(max_shift_limit, exploration_cap) if auto_apply_permitted else exploration_cap

    # 1. Voice exploration
    voices = opts.get("voice", [])
    if len(voices) > 1:
        cur_v = get_dimension_distribution(current_config, cohort_id, "voice")
        # If currently 100% on one voice or only 1 voice has allocation
        active_voices = [v for v, w in cur_v.items() if w > 0.05]
        if len(active_voices) <= 1:
            incumbent = active_voices[0] if active_voices else voices[0]
            other_voices = [v for v in voices if v != incumbent and ("voice", v) not in blacklist]
            if other_voices:
                other_share = effective_cap
                per_other = other_share / len(other_voices)
                step_v = {v: per_other for v in other_voices}
                step_v[incumbent] = 1.0 - other_share

                new_cfg.setdefault("allocations", {}).setdefault(cohort_id, {})["voice"] = {
                    k: round(v, 4) for k, v in step_v.items()
                }
                changed = True

    # 2. Welcome message exploration
    welcomes = opts.get("welcome_message", [])
    if len(welcomes) > 1:
        cur_w = get_dimension_distribution(current_config, cohort_id, "welcome_message")
        active_w = [w for w, weight in cur_w.items() if weight > 0.05]
        if len(active_w) <= 1:
            incumbent_w = active_w[0] if active_w else welcomes[0]
            other_w = [w for w in welcomes if w != incumbent_w and ("welcome_message", w) not in blacklist]
            if other_w:
                other_share = effective_cap
                per_other = other_share / len(other_w)
                step_w = {w: per_other for w in other_w}
                step_w[incumbent_w] = 1.0 - other_share

                new_cfg.setdefault("allocations", {}).setdefault(cohort_id, {})["welcome_message"] = {
                    k: round(v, 4) for k, v in step_w.items()
                }
                changed = True

    return new_cfg if changed else None


def plan_decay_rollback(
    workflow_spec: dict[str, Any],
    current_config: dict[str, Any],
    decaying_variants: list[dict[str, Any]],
    cohort_id: str = "default",
) -> dict[str, Any] | None:
    """Removes decaying or degrading variants from the active allocation and shifts traffic to healthy peers (C7)."""
    new_cfg = copy.deepcopy(current_config)
    changed = False

    for item in decaying_variants:
        dim = item["dim"]
        bad_var = item["variant"]
        cur_alloc = get_dimension_distribution(current_config, cohort_id, dim)
        if cur_alloc.get(bad_var, 0.0) <= 0.0:
            continue

        # Redistribute bad_var share across remaining options
        healthy = {k: v for k, v in cur_alloc.items() if k != bad_var}
        if not healthy:
            continue

        tot = sum(healthy.values())
        new_alloc = {k: v / tot for k, v in healthy.items()}
        new_cfg.setdefault("allocations", {}).setdefault(cohort_id, {})[dim] = new_alloc
        changed = True

    return new_cfg if changed else None


def plan_caller_id_rotation(
    workflow_spec: dict[str, Any],
    current_config: dict[str, Any],
    degraded_dids: list[str],
    auto_apply_permitted: bool,
    max_shift_limit: float = 0.5,
) -> dict[str, Any] | None:
    """Plans a caller-ID allocation shift to remove or reduce degraded caller IDs.

    Uses auto-apply in steps <= max_shift where permitted, or full-shift approval otherwise (PRB-002).
    """
    opts = workflow_spec.get("options", {}).get("caller_id", [])
    healthy_dids = [d for d in opts if d not in degraded_dids]
    if not healthy_dids:
        return None

    new_cfg = copy.deepcopy(current_config)
    cur_alloc = get_dimension_distribution(current_config, "default", "caller_id")

    # Target: distribute evenly across healthy DIDs
    target_healthy_share = 1.0 / len(healthy_dids)
    target_alloc = {d: (target_healthy_share if d in healthy_dids else 0.0) for d in opts}

    if auto_apply_permitted:
        # Step towards target capped by max_shift
        step_alloc = {}
        for d in opts:
            cur_val = cur_alloc.get(d, 0.0)
            tgt_val = target_alloc.get(d, 0.0)
            diff = tgt_val - cur_val
            bounded_diff = max(-max_shift_limit, min(max_shift_limit, diff))
            step_alloc[d] = max(0.0, cur_val + bounded_diff)

        # Normalize cleanly
        tot = sum(step_alloc.values())
        if tot > 0:
            norm_alloc = {k: v / tot for k, v in step_alloc.items()}
        else:
            norm_alloc = target_alloc
    else:
        norm_alloc = target_alloc

    if norm_alloc == cur_alloc:
        return None

    new_cfg.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = norm_alloc
    return new_cfg


def evaluate_and_propose_variants(
    db: Database,
    workflow_id: str,
    workflow_spec: dict[str, Any],
    current_version: int,
    current_config: dict[str, Any],
    cohort_id: str = "default",
    mde: float = 0.03,
) -> dict[str, Any] | None:
    """Evaluates welcome_message and voice variants within a cohort against current incumbent.

    Uses two-proportion z-tests with Holm-Bonferroni correction and enforces objective guardrail tolerances.
    """
    stats = db.get_variant_stats(workflow_id, cohort_id, config_version=current_version)
    if not stats:
        return None

    opts = workflow_spec.get("options", {})
    voices = opts.get("voice", [])
    if len(voices) < 2:
        return None

    obj = workflow_spec.get("objective", {})
    primary_kpi = obj.get("primary", "engagement_rate")
    guardrail = obj.get("guardrail", {})
    pickup_drop_limit = guardrail.get("pickup_rate_max_drop", 0.02)
    engage_drop_limit = guardrail.get("engagement_rate_max_drop", 0.02)

    # Aggregate by voice across current cohort/version
    voice_stats: dict[str, dict[str, int]] = {}
    for row in stats:
        v = row["voice"]
        if v not in voice_stats:
            voice_stats[v] = {"dials": 0, "connected": 0, "engaged": 0}
        voice_stats[v]["dials"] += row["dials"]
        voice_stats[v]["connected"] += row["connected"]
        voice_stats[v]["engaged"] += row["engaged"]

    cur_alloc = get_dimension_distribution(current_config, cohort_id, "voice")
    # Identify currently exploited voice (highest weight)
    current_best_voice = max(cur_alloc.keys(), key=lambda k: cur_alloc.get(k, 0.0)) if cur_alloc else voices[0]
    base_stat = voice_stats.get(current_best_voice)
    if not base_stat or base_stat["connected"] < 50:
        return None

    base_p_eng = base_stat["engaged"] / max(base_stat["connected"], 1)
    base_p_pick = base_stat["connected"] / max(base_stat["dials"], 1)

    min_n = sample_size_mde(p_base=base_p_eng, mde=mde, alpha=0.05, power=0.80)
    min_required_calls = max(40, min(min_n, 120))

    candidate_voices = [
        v for v in voices
        if v != current_best_voice and voice_stats.get(v, {}).get("connected", 0) >= min_required_calls
    ]
    if not candidate_voices:
        return None

    # Test each candidate against base
    p_values = []
    candidates_data = []
    for cand in candidate_voices:
        c_stat = voice_stats[cand]
        cand_p_eng = c_stat["engaged"] / max(c_stat["connected"], 1)
        cand_p_pick = c_stat["connected"] / max(c_stat["dials"], 1)

        # Primary KPI check (lift)
        lift_eng = cand_p_eng - base_p_eng
        # Guardrail check
        if primary_kpi == "engagement_rate":
            drop_pick = base_p_pick - cand_p_pick
            if drop_pick > pickup_drop_limit:
                continue  # Guardrail violated!
            z, p_val = two_proportion_z_test(c_stat["engaged"], c_stat["connected"], base_stat["engaged"], base_stat["connected"])
        else:
            drop_eng = base_p_eng - cand_p_eng
            if drop_eng > engage_drop_limit:
                continue
            z, p_val = two_proportion_z_test(c_stat["connected"], c_stat["dials"], base_stat["connected"], base_stat["dials"])

        if z > 0:  # Candidate has positive lift
            p_values.append(p_val)
            candidates_data.append((cand, z, p_val, lift_eng, c_stat["connected"]))

    if not p_values:
        return None

    # Apply Holm-Bonferroni correction
    significant = holm_bonferroni(p_values, alpha=0.05)
    winning_candidates = [cand_info for cand_info, is_sig in zip(candidates_data, significant) if is_sig]
    if not winning_candidates:
        return None

    # Two-Block Promotion Rule (PRB-012):
    # A candidate cannot be promoted based on a transient single-window spike.
    # It must demonstrate:
    # 1. Consistent positive lift across at least 2 independent time blocks (e.g. 12h blocks).
    # 2. Guardrail compliance in every observed block.
    # 3. Overall pooled statistical significance (Holm-Bonferroni p < 0.05).
    block_rows = db.get_variant_block_stats(workflow_id, cohort_id, config_version=current_version)
    blocks_by_voice: dict[str, dict[int, dict[str, int]]] = defaultdict(dict)
    for r in block_rows:
        v = r["voice"]
        tb = r["time_block"]
        if tb not in blocks_by_voice[v]:
            blocks_by_voice[v][tb] = {"dials": 0, "connected": 0, "engaged": 0}
        blocks_by_voice[v][tb]["dials"] += r["dials"]
        blocks_by_voice[v][tb]["connected"] += r["connected"]
        blocks_by_voice[v][tb]["engaged"] += r["engaged"]

    base_blocks = blocks_by_voice.get(current_best_voice, {})

    consistent_winners = []
    for cand_info in winning_candidates:
        cand, z, p_val, lift_eng, sample_n = cand_info
        c_blocks = blocks_by_voice.get(cand, {})
        common_blocks = sorted(set(base_blocks.keys()) & set(c_blocks.keys()))

        evaluable_blocks = [
            b for b in common_blocks
            if c_blocks[b]["connected"] >= 15 and base_blocks[b]["connected"] >= 15
        ]

        if len(evaluable_blocks) < 2:
            logger.info("Candidate %s has fewer than 2 independent blocks with n>=15 (evaluable=%s). Deferring promotion.",
                        cand, evaluable_blocks)
            continue

        consistent = True
        for b in evaluable_blocks:
            c_eng_rate = c_blocks[b]["engaged"] / max(c_blocks[b]["connected"], 1)
            b_eng_rate = base_blocks[b]["engaged"] / max(base_blocks[b]["connected"], 1)
            b_lift = c_eng_rate - b_eng_rate
            if b_lift <= 0:
                logger.info("Candidate %s failed two-block consistency: non-positive lift (%.4f) in block %d", cand, b_lift, b)
                consistent = False
                break

            c_pick = c_blocks[b]["connected"] / max(c_blocks[b]["dials"], 1)
            b_pick = base_blocks[b]["connected"] / max(base_blocks[b]["dials"], 1)
            if primary_kpi == "engagement_rate" and (b_pick - c_pick) > pickup_drop_limit:
                consistent = False
                break
            elif primary_kpi != "engagement_rate" and (b_eng_rate - c_eng_rate) > engage_drop_limit:
                consistent = False
                break

        if consistent:
            consistent_winners.append(cand_info)

    if not consistent_winners:
        return None

    # Pick the winner with the highest z-score among consistent winners
    best_cand = max(consistent_winners, key=lambda x: x[1])
    winner_voice, z_stat, p_val, lift, sample_n = best_cand

    new_cfg = copy.deepcopy(current_config)
    # Promote winner: 85% to winner, 15% to incumbent/exploration
    new_cfg.setdefault("allocations", {}).setdefault(cohort_id, {})["voice"] = {
        winner_voice: 0.85,
        current_best_voice: 0.15,
    }

    return {
        "new_config": new_cfg,
        "winner_voice": winner_voice,
        "old_voice": current_best_voice,
        "z_stat": round(z_stat, 2),
        "p_val": round(p_val, 5),
        "lift": round(lift, 4),
        "sample_size": sample_n,
    }
