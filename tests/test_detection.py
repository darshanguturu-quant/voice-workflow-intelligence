"""Unit tests for controller/detection.py."""
import tempfile
from pathlib import Path

from controller.db import Database
from controller.detection import detect_caller_id_degradation, detect_hallucination_spike


def test_detect_caller_id_degradation():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        # Ingest healthy DIDs: D1, D2, D3 (~30% pickup on 200 dials each)
        # Degraded DID: D4 (10% pickup on 200 dials)
        did_deltas = {
            ("nw_renewal", "D1"): {"dials": 200, "connected": 60},
            ("nw_renewal", "D2"): {"dials": 200, "connected": 62},
            ("nw_renewal", "D3"): {"dials": 200, "connected": 58},
            ("nw_renewal", "D4"): {"dials": 200, "connected": 20},
        }
        db.commit_chunk(0, 800, 60, "2027-01-04T01:00:00+05:30", {}, did_deltas, [])

        degraded = detect_caller_id_degradation(db, "nw_renewal", min_dials=50)
        assert len(degraded) == 1
        assert degraded[0]["caller_id"] == "D4"
        assert degraded[0]["pickup_rate"] == 0.10
        assert degraded[0]["peer_pickup_rate"] > 0.28
        assert degraded[0]["z_score"] < -2.33


def test_detect_caller_id_no_false_alarm_on_small_n():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        # Only 20 dials each - below min_dials
        did_deltas = {
            ("nw_renewal", "D1"): {"dials": 20, "connected": 6},
            ("nw_renewal", "D4"): {"dials": 20, "connected": 2},
        }
        db.commit_chunk(0, 40, 60, "2027-01-04T01:00:00+05:30", {}, did_deltas, [])

        degraded = detect_caller_id_degradation(db, "nw_renewal", min_dials=50)
        assert len(degraded) == 0  # Does not trigger on small populations!


def test_detect_hallucination_spike():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        # Baseline: 100 connected calls, 1 hallucination (1% rate)
        agg_deltas_normal = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {
                "dials": 200, "connected": 100, "engaged": 50, "hallucinated": 1
            }
        }
        db.commit_chunk(0, 200, 60, "2027-01-04T01:00:00+05:30", agg_deltas_normal, {}, [])

        # Should NOT trigger incident (normal noise)
        incident = detect_hallucination_spike(db, "nw_renewal", min_calls=50, threshold=0.03)
        assert incident is None

        # Incident: additional 100 connected calls with 15 hallucinations (15% rate)
        agg_deltas_spike = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {
                "dials": 200, "connected": 100, "engaged": 40, "hallucinated": 15
            }
        }
        db.commit_chunk(1, 400, 120, "2027-01-04T02:00:00+05:30", agg_deltas_spike, {}, [])

        incident = detect_hallucination_spike(db, "nw_renewal", min_calls=50, threshold=0.03)
        assert incident is not None
        assert incident["workflow_id"] == "nw_renewal"
        assert incident["observed_rate"] > 0.05
        assert incident["lower_bound_95"] > 0.03
