"""Unit tests for controller/detection.py."""
import json
import tempfile
from pathlib import Path

import httpx

from controller.db import Database
from controller.detection import detect_caller_id_degradation, detect_hallucination_spike
from controller.governance import GovernanceClient
from controller.incidents import IncidentManager


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


def test_proactive_validation_safe_prompt():
    import json
    import httpx
    from controller.governance import GovernanceClient
    from controller.incidents import IncidentManager

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        def handler(request: httpx.Request):
            if request.url.path == "/validation/runs":
                return httpx.Response(200, json={"pass_rate": 0.99, "validation_run_id": "val_safe"})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
        gov = GovernanceClient(client, db)
        mgr = IncidentManager(client, db, gov)
        spec = {"governance": {"min_validation_pass_rate": 0.97, "approver_hours": [10, 19]}}
        res = mgr.proactively_validate_supervisor_prompt(
            "ks_cancel", "P3", spec, 2, {"prompt_version": "P3"}, 660, "2027-01-04T11:00:00+05:30"
        )
        assert res is None  # Safe prompt does not trigger incident response


def test_proactive_validation_unsafe_prompt():
    import json
    import httpx
    from controller.governance import GovernanceClient
    from controller.incidents import IncidentManager

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        def handler(request: httpx.Request):
            if request.url.path == "/validation/runs":
                body = json.loads(request.content)
                if body.get("prompt_version") == "P2":
                    return httpx.Response(200, json={"pass_rate": 0.70, "validation_run_id": "val_bad"})
                else:
                    return httpx.Response(
                        200, json={"pass_rate": 0.9933, "validation_run_id": "val_good", "prompt_version": "P5"}
                    )
            elif request.url.path == "/llm/suggest-fixes":
                return httpx.Response(200, json={"suggestions": [{"prompt_version": "P5"}]})
            elif request.url.path == "/approvals":
                return httpx.Response(200, json={"approval_id": "apr_test", "status": "pending"})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
        gov = GovernanceClient(client, db)
        mgr = IncidentManager(client, db, gov)
        spec = {
            "options": {"caller_id": ["D1"], "voice": ["V1"], "welcome_message": ["W1"]},
            "governance": {
                "min_validation_pass_rate": 0.97,
                "approver_hours": [10, 19],
                "proposal_ttl_hours": 24,
                "max_requests_per_24h": 4,
            },
        }
        res = mgr.proactively_validate_supervisor_prompt(
            "ks_cancel", "P2", spec, 2, {"prompt_version": "P2"}, 660, "2027-01-04T11:00:00+05:30"
        )
        assert res is not None
        assert res["status"] == "proposed"
        assert res["step"] == "approval_requested"


def test_borderline_validation_behavior():
    import json
    import httpx
    from controller.governance import GovernanceClient
    from controller.incidents import IncidentManager

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        # Scenario: candidate achieves 0.965 (strictly below the 0.970 policy threshold)
        def handler(request: httpx.Request):
            if request.url.path == "/validation/runs":
                return httpx.Response(200, json={"pass_rate": 0.965, "validation_run_id": "val_borderline", "prompt_version": "P5"})
            elif request.url.path == "/llm/suggest-fixes":
                return httpx.Response(200, json={"suggestions": [{"prompt_version": "P5"}]})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
        gov = GovernanceClient(client, db)
        mgr = IncidentManager(client, db, gov)
        spec = {
            "options": {"caller_id": ["D1"], "voice": ["V1"], "welcome_message": ["W1"]},
            "governance": {
                "min_validation_pass_rate": 0.97,
                "approver_hours": [10, 19],
                "proposal_ttl_hours": 24,
                "max_requests_per_24h": 4,
            },
        }
        res = mgr.handle_hallucination_incident(
            "ks_cancel", spec, 2, {"prompt_version": "P2"}, 660, "2027-01-04T11:00:00+05:30",
            {"workflow_id": "ks_cancel", "observed_rate": 0.08, "connected_calls": 100}
        )
        # Must be rejected because 0.965 < 0.970 threshold!
        assert res["status"] == "failed"
        assert res["step"] == "validation_failed"


def test_proactive_validation_borderline_escalation_to_safe():
    """Verify that a borderline initial pass rate (0.960) escalates to n=300, confirming safe if pooled >= threshold."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        calls = []

        def handler(request: httpx.Request):
            if request.url.path == "/validation/runs":
                body = json.loads(request.content)
                n = body.get("n", 100)
                calls.append(n)
                if n == 100:
                    # Initial borderline result: 0.960 (within 0.02 of 0.970)
                    return httpx.Response(200, json={"pass_rate": 0.960, "validation_run_id": "val_init"})
                elif n == 300:
                    # Escalated confirmation result: 0.985
                    # Pooled = (0.960 * 100 + 0.985 * 300) / 400 = 0.97875 >= 0.970
                    return httpx.Response(200, json={"pass_rate": 0.985, "validation_run_id": "val_conf"})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
        gov = GovernanceClient(client, db)
        mgr = IncidentManager(client, db, gov)
        spec = {
            "options": {"caller_id": ["D1"], "voice": ["V1"], "welcome_message": ["W1"]},
            "governance": {"min_validation_pass_rate": 0.97},
        }

        res = mgr.proactively_validate_supervisor_prompt(
            "ks_cancel", "P_borderline", spec, 2, {"prompt_version": "P_borderline"}, 660, "2027-01-04T11:00:00+05:30"
        )
        # Escalated from n=100 to n=300
        assert calls == [100, 300]
        # Confirmed safe without triggering a false alarm incident!
        assert res is None


def test_proactive_validation_borderline_escalation_to_unsafe():
    """Verify that a borderline initial pass rate (0.960) escalates to n=300, confirming unsafe if pooled < threshold."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        calls = []

        def handler(request: httpx.Request):
            if request.url.path == "/validation/runs":
                body = json.loads(request.content)
                n = body.get("n", 100)
                pv = body.get("prompt_version")
                calls.append(n)
                if pv == "P_borderline_bad":
                    if n == 100:
                        return httpx.Response(200, json={"pass_rate": 0.960, "validation_run_id": "val_init"})
                    elif n == 300:
                        return httpx.Response(200, json={"pass_rate": 0.955, "validation_run_id": "val_conf"})
                return httpx.Response(200, json={"pass_rate": 0.995, "validation_run_id": "val_fix", "prompt_version": pv or "P_fix"})
            elif request.url.path == "/llm/suggest-fixes":
                return httpx.Response(200, json={"suggestions": [{"prompt_version": "P_fix"}]})
            elif request.url.path == "/approvals":
                return httpx.Response(200, json={"approval_id": "apr_fix", "status": "pending"})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
        gov = GovernanceClient(client, db)
        mgr = IncidentManager(client, db, gov)
        spec = {
            "options": {"caller_id": ["D1"], "voice": ["V1"], "welcome_message": ["W1"]},
            "governance": {
                "min_validation_pass_rate": 0.97,
                "approver_hours": [10, 19],
                "proposal_ttl_hours": 24,
                "max_requests_per_24h": 4,
            },
        }

        res = mgr.proactively_validate_supervisor_prompt(
            "ks_cancel", "P_borderline_bad", spec, 2, {"prompt_version": "P_borderline_bad"}, 660, "2027-01-04T11:00:00+05:30"
        )
        assert 100 in calls and 300 in calls
        assert res is not None
        assert res["status"] == "proposed"


def test_detect_decaying_variants_no_false_rollback_on_baseline():
    """Verify that multi-arm exploration does NOT falsely flag the baseline incumbent as decayed (PRB-018)."""
    from controller.detection import detect_decaying_variants

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test_decay.db")
        wid = "nw_kyc"
        cur_cfg = {
            "allocations": {
                "default": {
                    "welcome_message": {"W1": 0.85, "W2": 0.15},
                    "voice": {"V1": 0.85, "V2": 0.15},
                }
            }
        }
        # W1 has 850 calls (500 engaged = 58.8%)
        # W2 has 150 calls (105 engaged = 70.0% - higher in small sample!)
        agg_explore = {
            (wid, "default", 2, "W1", "V1", "D1"): {"dials": 850, "connected": 850, "engaged": 500},
            (wid, "default", 2, "W2", "V2", "D1"): {"dials": 150, "connected": 150, "engaged": 105},
        }
        db.commit_chunk(1, 1000, 720, "2027-01-04T12:00:00+05:30", agg_explore, {}, [])

        decaying = detect_decaying_variants(db, wid, cohort_id="default", current_config=cur_cfg)
        # W1 must NOT be falsely rolled back!
        assert len(decaying) == 0


def test_detect_decaying_variants_catches_genuine_longitudinal_decay():
    """Verify that a promoted variant whose engagement rate declines over time is detected via longitudinal blocks."""
    from controller.detection import detect_decaying_variants

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test_longitudinal_decay.db")
        wid = "nw_renewal"
        cur_cfg = {
            "allocations": {
                "default": {
                    "welcome_message": {"W3": 1.0},
                    "voice": {"V1": 1.0},
                }
            }
        }
        # Ingest 4 consecutive 12-hour time blocks (Block 0..3)
        # Blocks 0 & 1: High baseline engagement (55% and 52.5%)
        # Blocks 2 & 3: Significant decay (40% and 35%)
        blocks_data = [
            (0, 60, "2027-01-04T01:00:00+05:30", 200, 110),
            (1, 750, "2027-01-04T12:30:00+05:30", 200, 105),
            (2, 1470, "2027-01-05T00:30:00+05:30", 200, 80),
            (3, 2190, "2027-01-05T12:30:00+05:30", 200, 70),
        ]
        for c_id, min_ts, iso_ts, conn, eng in blocks_data:
            agg = {(wid, "default", 3, "W3", "V1", "D1"): {"dials": conn, "connected": conn, "engaged": eng}}
            db.commit_chunk(c_id, (c_id + 1) * 200, min_ts, iso_ts, agg, {}, [])

        decaying = detect_decaying_variants(db, wid, cohort_id="default", current_config=cur_cfg)
        w3_decay = next((d for d in decaying if d["variant"] == "W3"), None)
        assert w3_decay is not None
        assert w3_decay["dim"] == "welcome_message"
        assert w3_decay["detection_mode"] == "longitudinal_decay"
        assert w3_decay["z_score"] < -2.33

