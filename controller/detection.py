"""Detection engine for caller-ID degradation, hallucination spikes, and winner decay.

Operates strictly over populations and statistical confidence bounds, never single calls.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from controller.db import Database
from controller.stats import two_proportion_z_test, wilson_score_interval


def detect_caller_id_degradation(
    db: Database,
    workflow_id: str,
    current_day: int | None = None,
    min_dials: int = 40,
    mde_pickup: float = 0.05,
) -> list[dict[str, Any]]:
    """Identifies caller IDs whose pickup rate has significantly degraded relative to peers.

    Compares concurrently against other caller IDs in the recent time window, with history as secondary (PRB-002).
    """
    stats = []
    if current_day is not None and current_day > 0:
        stats = db.get_recent_caller_id_stats(workflow_id, current_day, days_back=2)

    total_recent = sum(s["dials"] for s in stats)
    if total_recent < min_dials * 2:
        stats = db.get_caller_id_stats(workflow_id)

    if len(stats) < 2:
        return []

    total_dials = sum(s["dials"] for s in stats)
    if total_dials < min_dials * 2:
        return []

    degraded = []
    for s in stats:
        did = s["caller_id"]
        dials = s["dials"]
        conn = s["connected"]
        if dials < min_dials:
            continue

        p_did = conn / dials
        did_lo, did_hi = wilson_score_interval(conn, dials, confidence=0.95)

        # Peer average
        peer_dials = sum(p["dials"] for p in stats if p["caller_id"] != did)
        peer_conn = sum(p["connected"] for p in stats if p["caller_id"] != did)
        if peer_dials < min_dials:
            continue

        p_peer = peer_conn / peer_dials

        # If the upper bound of this DID is lower than the peer rate minus MDE
        # or two-proportion z-test shows statistically significant deficit
        z, p_val = two_proportion_z_test(conn, dials, peer_conn, peer_dials)

        if z < -2.0 and (p_peer - p_did) >= mde_pickup:
            degraded.append({
                "caller_id": did,
                "dials": dials,
                "connected": conn,
                "pickup_rate": round(p_did, 4),
                "peer_pickup_rate": round(p_peer, 4),
                "ci_95": (round(did_lo, 4), round(did_hi, 4)),
                "z_score": round(z, 2),
                "p_value": round(p_val, 5),
            })

    return degraded


def detect_hallucination_spike(
    db: Database,
    workflow_id: str,
    config_version: int | None = None,
    min_calls: int = 40,
    threshold: float = 0.03,
) -> dict[str, Any] | None:
    """Detects prompt hallucination spikes using Wilson confidence lower bound.

    Accounts for noisy automated QA detector false positives and slices by config_version.
    """
    variants = db.get_variant_stats(workflow_id, config_version=config_version)
    tot_connected = sum(v.get("connected", 0) for v in variants)
    tot_halluc = sum(v.get("hallucinated", 0) for v in variants)

    if tot_connected < min_calls:
        return None

    rate = tot_halluc / tot_connected
    lo, hi = wilson_score_interval(tot_halluc, tot_connected, confidence=0.95)

    # Trigger only if statistical lower bound exceeds safety threshold
    if lo > threshold:
        return {
            "workflow_id": workflow_id,
            "config_version": config_version,
            "connected_calls": tot_connected,
            "hallucinated_calls": tot_halluc,
            "observed_rate": round(rate, 4),
            "lower_bound_95": round(lo, 4),
            "threshold": threshold,
        }

    return None


def get_initial_incumbents(config: dict[str, Any] | None, cohort_id: str = "default") -> dict[str, str]:
    """Derives the incumbent variant for each dimension from the initial run configuration.

    The incumbent is the variant with the dominant (maximum) allocation in the configuration.
    """
    if not config:
        return {}
    from controller.cohorts import get_dimension_distribution
    incumbents = {}
    for dim in ("welcome_message", "voice"):
        alloc = get_dimension_distribution(config, cohort_id, dim)
        if alloc:
            best_var = max(alloc.items(), key=lambda kv: kv[1])[0]
            incumbents[dim] = best_var
    return incumbents


def detect_decaying_variants(
    db: Database,
    workflow_id: str,
    cohort_id: str = "default",
    config_version: int | list[int] | None = None,
    current_config: dict[str, Any] | None = None,
    initial_config: dict[str, Any] | None = None,
    min_connected: int = 150,
    decay_delta: float = 0.04,
) -> list[dict[str, Any]]:
    """Identifies promoted or dominant variants whose engagement rate has significantly decayed (C7, Final Pass).

    Combines two rigorous statistical tests:
    1. Longitudinal Decay: Compares a variant's recent performance against its historical baseline across time blocks.
       Requires >= 3 blocks of observations, degradation across >= 2 consecutive blocks, drop >= 0.04, and z < -2.33 (p < 0.01).
    2. Concurrent Degradation: Compares a dominant incumbent (share >= 0.50) against a well-sampled competing arm (n >= 250).
       Guards against false rollback: never flags a derived baseline incumbent during initial multi-arm exploration.
    """
    from controller.cohorts import get_dimension_distribution

    decaying = []
    seen_decaying: set[tuple[str, str]] = set()

    # 1. Longitudinal Decay via 12-hour block stats
    block_rows = db.get_variant_block_stats(workflow_id, cohort_id, config_version=config_version)
    blocks_by_dim_var: dict[tuple[str, str], dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"connected": 0, "engaged": 0})
    )
    for r in block_rows:
        tb = r["time_block"]
        w = r["welcome_message"]
        v = r["voice"]
        blocks_by_dim_var[("welcome_message", w)][tb]["connected"] += r["connected"]
        blocks_by_dim_var[("welcome_message", w)][tb]["engaged"] += r["engaged"]
        blocks_by_dim_var[("voice", v)][tb]["connected"] += r["connected"]
        blocks_by_dim_var[("voice", v)][tb]["engaged"] += r["engaged"]

    for (dim, var), tb_map in blocks_by_dim_var.items():
        cur_alloc = get_dimension_distribution(current_config, cohort_id, dim) if current_config else {}
        if current_config and cur_alloc.get(var, 0.0) <= 0.05:
            continue

        sorted_blocks = sorted(tb_map.keys())
        if len(sorted_blocks) >= 3:
            # Baseline: earlier blocks (e.g. initial 2 or all but last 2 blocks)
            base_blocks = sorted_blocks[:-2] if len(sorted_blocks) >= 4 else sorted_blocks[:1]
            recent_blocks = sorted_blocks[-2:] if len(sorted_blocks) >= 4 else sorted_blocks[1:]

            base_conn = sum(tb_map[b]["connected"] for b in base_blocks)
            base_eng = sum(tb_map[b]["engaged"] for b in base_blocks)

            recent_conn = sum(tb_map[b]["connected"] for b in recent_blocks)
            recent_eng = sum(tb_map[b]["engaged"] for b in recent_blocks)

            if base_conn >= 80 and recent_conn >= 100:
                p_base = base_eng / base_conn
                p_recent = recent_eng / recent_conn
                drop = p_base - p_recent

                # Consecutive-block check: Both recent blocks must individually show degradation below baseline
                b1, b2 = recent_blocks[-2], recent_blocks[-1]
                conn_b1, eng_b1 = tb_map[b1]["connected"], tb_map[b1]["engaged"]
                conn_b2, eng_b2 = tb_map[b2]["connected"], tb_map[b2]["engaged"]

                both_blocks_degraded = (
                    conn_b1 >= 30 and conn_b2 >= 30 and
                    (eng_b1 / conn_b1) < p_base and
                    (eng_b2 / conn_b2) < p_base
                )

                if drop >= decay_delta and both_blocks_degraded:
                    z, p_val = two_proportion_z_test(recent_eng, recent_conn, base_eng, base_conn)
                    if z < -2.33:
                        decaying.append({
                            "dim": dim,
                            "variant": var,
                            "rate": round(p_recent, 4),
                            "peer_rate": round(p_base, 4),
                            "baseline_rate": round(p_base, 4),
                            "z_score": round(z, 2),
                            "sample_size": recent_conn,
                            "detection_mode": "longitudinal_decay",
                        })
                        seen_decaying.add((dim, var))

    # 2. Concurrent Degradation for dominant variants
    stats = db.get_variant_stats(workflow_id, cohort_id, config_version=config_version)
    if len(stats) >= 2:
        initial_incumbents = get_initial_incumbents(initial_config or current_config, cohort_id)
        for dim in ("welcome_message", "voice"):
            cur_alloc = get_dimension_distribution(current_config, cohort_id, dim) if current_config else {}
            dominant_variants = [k for k, v in cur_alloc.items() if v >= 0.50 and (dim, k) not in seen_decaying]
            if not dominant_variants and current_config:
                continue

            dim_stats: dict[str, dict[str, int]] = {}
            for row in stats:
                val = row[dim]
                if val not in dim_stats:
                    dim_stats[val] = {"connected": 0, "engaged": 0}
                dim_stats[val]["connected"] += row["connected"]
                dim_stats[val]["engaged"] += row["engaged"]

            for var in (dominant_variants if current_config else dim_stats.keys()):
                if (dim, var) in seen_decaying or var not in dim_stats:
                    continue

                # Guardrail: Never flag the derived initial baseline incumbent during multi-arm exploration.
                # If peer is an exploratory arm, promote the peer via the experiment engine, don't rollback baseline!
                incumbent_var = initial_incumbents.get(dim)
                if var == incumbent_var and any(cur_alloc.get(other_v, 0.0) < 0.50 for other_v in cur_alloc if other_v != var):
                    continue

                d = dim_stats[var]
                conn = d["connected"]
                eng = d["engaged"]
                if conn < min_connected:
                    continue
                p_var = eng / conn

                peer_conn = sum(other["connected"] for other_v, other in dim_stats.items() if other_v != var)
                peer_eng = sum(other["engaged"] for other_v, other in dim_stats.items() if other_v != var)
                if peer_conn < min_connected:
                    continue
                p_peer = peer_eng / peer_conn

                z, p_val = two_proportion_z_test(eng, conn, peer_eng, peer_conn)
                if z < -2.57 and (p_peer - p_var) >= decay_delta:
                    decaying.append({
                        "dim": dim,
                        "variant": var,
                        "rate": round(p_var, 4),
                        "peer_rate": round(p_peer, 4),
                        "z_score": round(z, 2),
                        "sample_size": conn,
                        "detection_mode": "concurrent_degradation",
                    })
                    seen_decaying.add((dim, var))

    return decaying
