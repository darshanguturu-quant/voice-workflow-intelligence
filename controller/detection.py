"""Detection engine for caller-ID degradation, hallucination spikes, and winner decay.

Operates strictly over populations and statistical confidence bounds, never single calls.
"""
from __future__ import annotations

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


def detect_decaying_variants(
    db: Database,
    workflow_id: str,
    cohort_id: str = "default",
    config_version: int | None = None,
    current_config: dict[str, Any] | None = None,
    min_connected: int = 40,
    decay_delta: float = 0.05,
) -> list[dict[str, Any]]:
    """Identifies promoted variants whose engagement rate has significantly decayed (C7).

    Focuses on promoted/majority variants (share >= 0.50) to distinguish decay from active multi-arm exploration.
    """
    stats = db.get_variant_stats(workflow_id, cohort_id, config_version=config_version)
    if len(stats) < 2:
        return []

    from controller.cohorts import get_dimension_distribution

    decaying = []
    for dim in ("welcome_message", "voice"):
        # Check current allocation
        cur_alloc = get_dimension_distribution(current_config, cohort_id, dim) if current_config else {}
        # Only evaluate variants that hold a dominant share (>= 50%), representing a promoted winner
        promoted_variants = [k for k, v in cur_alloc.items() if v >= 0.50]
        if not promoted_variants and current_config:
            continue

        dim_stats: dict[str, dict[str, int]] = {}
        for row in stats:
            val = row[dim]
            if val not in dim_stats:
                dim_stats[val] = {"connected": 0, "engaged": 0}
            dim_stats[val]["connected"] += row["connected"]
            dim_stats[val]["engaged"] += row["engaged"]

        for var in (promoted_variants if current_config else dim_stats.keys()):
            if var not in dim_stats:
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
            if z < -2.0 and (p_peer - p_var) >= decay_delta:
                decaying.append({
                    "dim": dim,
                    "variant": var,
                    "rate": round(p_var, 4),
                    "peer_rate": round(p_peer, 4),
                    "z_score": round(z, 2),
                    "sample_size": conn,
                })

    return decaying
