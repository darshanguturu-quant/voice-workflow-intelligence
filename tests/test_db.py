"""Unit tests for controller/db.py."""
import tempfile
from pathlib import Path

from controller.db import Database


def test_db_init_and_cursor():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        assert db.get_cursor() is None

        db.update_cursor(last_chunk=5, last_event_id=1234, sim_minute=600, sim_ts="2027-01-04T10:00:00+05:30")
        cur = db.get_cursor()
        assert cur is not None
        assert cur["last_chunk"] == 5
        assert cur["last_event_id"] == 1234


def test_commit_chunk_atomic():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")

        agg_deltas = {
            ("nw_renewal", "default", 1, "W1", "V1", "D1"): {"dials": 10, "connected": 4, "engaged": 2},
        }
        did_deltas = {
            ("nw_renewal", "D1"): {"dials": 10, "connected": 4},
        }
        leads = [("p1", "hi", "1", "18-30", "low")]

        db.commit_chunk(0, 10, 60, "2027-01-04T01:00:00+05:30", agg_deltas, did_deltas, leads)

        cur = db.get_cursor()
        assert cur["last_chunk"] == 0
        assert cur["last_event_id"] == 10

        stats = db.get_caller_id_stats("nw_renewal")
        assert len(stats) == 1
        assert stats[0]["dials"] == 10
        assert stats[0]["connected"] == 4

        v_stats = db.get_variant_stats("nw_renewal")
        assert len(v_stats) == 1
        assert v_stats[0]["dials"] == 10
        assert v_stats[0]["connected"] == 4
        assert v_stats[0]["engaged"] == 2


def test_proposal_intent_attempt_counter_prb001():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        cfg = {"allocations": {"default": {"voice": {"V2": 1.0}}}}

        # First attempt
        pid1, attempt1 = db.record_proposal_intent(
            workflow_id="nw_renewal",
            base_version=1,
            proposed_config=cfg,
            change_summary="test 1",
            diff={"voice": "V2"},
            affected_leads=100,
            dry_run={"ok": True},
            impact_score=0.4,
            rationale="rationale 1",
            evidence=None,
        )
        assert attempt1 == 1

        # Second attempt for same config and base_version (e.g. after rejection/expiry)
        pid2, attempt2 = db.record_proposal_intent(
            workflow_id="nw_renewal",
            base_version=1,
            proposed_config=cfg,
            change_summary="test 2",
            diff={"voice": "V2"},
            affected_leads=100,
            dry_run={"ok": True},
            impact_score=0.4,
            rationale="rationale 2",
            evidence=None,
        )
        assert attempt2 == 2
        # Proposal IDs must be distinct to prevent 409 duplicate_proposal_id!
        assert pid1 != pid2


def test_audit_log_query():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        db.log_decision(
            sim_minute=60,
            sim_ts="2027-01-04T01:00:00+05:30",
            workflow_id="nw_renewal",
            category="did_rotation",
            action_type="auto_apply",
            proposal_id="p01",
            details={"reason": "degraded D4"},
        )
        entries = db.query_audit_log(workflow_id="nw_renewal")
        assert len(entries) == 1
        assert entries[0]["category"] == "did_rotation"
        assert entries[0]["proposal_id"] == "p01"
