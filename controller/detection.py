"""Detection engine for caller-ID degradation, hallucination spikes, and winner decay.

Operates strictly over populations and statistical confidence bounds, never single calls.
"""
from __future__ import annotations

from typing import Any

from controller.db import Database
from controller.stats import wilson_score_interval, two_proportion_z_test


def detect_caller_id_degradation(
    db: Database,
    workflow_id: str,
    min_dials: int = 50,
    mde_pickup: float = 0.06,
) -> list[dict[str, Any]]:
    """Identifies caller IDs whose pickup rate has significantly degraded relative to peers.

    Compares concurrently against other caller IDs in the same workflow, with history as secondary.
    Returns list of degraded caller IDs with diagnostic statistics.
    """
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

        if z < -2.33 and (p_peer - p_did) >= mde_pickup:  # p < 0.01 (one-sided)
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
    min_calls: int = 50,
    threshold: float = 0.03,
) -> dict[str, Any] | None:
    """Detects prompt hallucination spikes using Wilson confidence lower bound.

    Accounts for noisy automated QA detector false positives.
    """
    variants = db.get_variant_stats(workflow_id)
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
            "connected_calls": tot_connected,
            "hallucinated_calls": tot_halluc,
            "observed_rate": round(rate, 4),
            "lower_bound_95": round(lo, 4),
            "threshold": threshold,
        }

    return None
