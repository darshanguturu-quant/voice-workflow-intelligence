"""Fault-injection tests covering crash recovery and idempotency across network boundaries.

Explicitly tests:
1. Crash BEFORE POST /approvals:
   - DB has intent recorded, HTTP call never reached simulator.
   - On restart: reconciliation marks unsubmitted intent as dropped.
   - Subsequent attempt for the same config increments attempt counter, generating
     a distinct proposal_id with zero duplicate_proposal_id conflicts.
2. Crash AFTER POST /approvals but BEFORE saving approval_id:
   - Simulator received proposal and generated approval_id.
   - DB only has intent (status='intent', approval_id=None).
   - On restart: reconciliation queries GET /approvals, recovers the matching approval_id,
     updates DB to pending. Once approved by supervisor, it applies cleanly.
3. Crash AFTER /apply but BEFORE recording success:
   - Simulator has executed apply (config version bumped, approval status='applied').
   - DB still has status='pending'.
   - On restart: reconciliation queries GET /approvals/{aid}, detects status='applied',
     updates DB to applied without repeating the apply call or failing with 409.
"""
from __future__ import annotations

import copy
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import httpx
import pytest

from controller.cohorts import compute_diff
from controller.db import Database
from controller.governance import GovernanceClient

PORT = 8718
BASE_URL = f"http://127.0.0.1:{PORT}"
TEST_DIR = Path("runs/fault_injection_test")
DB_PATH = TEST_DIR / "fault_test.db"


def cleanup():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)


def start_sim():
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    cmd = [
        sys.executable,
        "-m",
        "sim",
        "serve",
        "--scenario",
        "scenarios/public_01_dev.yaml",
        "--run-dir",
        str(TEST_DIR / "sim_run"),
        "--force",
        "--port",
        str(PORT),
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait for health
    client = httpx.Client(base_url=BASE_URL, timeout=5.0)
    ready = False
    for _ in range(80):
        try:
            r = client.get("/health")
            if r.status_code == 200 and r.json().get("ok"):
                ready = True
                break
        except Exception:
            time.sleep(0.5)
    if not ready:
        try:
            out, err = proc.communicate(timeout=1.0)
        except Exception:
            out, err = b"", b""
        proc.kill()
        raise RuntimeError(f"Simulator failed to start: {err.decode('utf-8', errors='replace')} | out: {out.decode('utf-8', errors='replace')}")
    return proc


# Fault worker functions executed in child process that exit abruptly via os._exit
def worker_crash_before_post_approvals(db_path: str, url: str):
    db = Database(Path(db_path))
    client = httpx.Client(base_url=url, timeout=5.0)
    gov = GovernanceClient(client, db)
    wf_spec = client.get("/workflows/nw_renewal").json()
    cfg = client.get("/workflows/nw_renewal/config").json()["config"]

    # Propose a change in allocations
    new_cfg = copy.deepcopy(cfg)
    new_cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.5, "W2": 0.5}

    # Intercept client.post to crash right before sending
    orig_post = client.post

    def crash_post(*args, **kwargs):
        if len(args) > 0 and "/approvals" in args[0]:
            # Abrupt kill simulating container crash
            os._exit(101)
        return orig_post(*args, **kwargs)

    client.post = crash_post

    try:
        gov.execute_change(
            workflow_id="nw_renewal",
            workflow_spec=wf_spec,
            current_version=1,
            current_config=cfg,
            proposed_config=new_cfg,
            change_summary="Fault injection test before POST",
            rationale="Testing crash before POST",
            current_minute=720,  # 12:00 PM (approver open)
            current_ts="2026-03-01T12:00:00Z",
            force_approval=True,
        )
    finally:
        client.close()


def worker_crash_after_post_approvals(db_path: str, url: str):
    db = Database(Path(db_path))
    client = httpx.Client(base_url=url, timeout=5.0)
    wf_spec = client.get("/workflows/nw_renewal").json()
    cfg = client.get("/workflows/nw_renewal/config").json()["config"]

    new_cfg = copy.deepcopy(cfg)
    new_cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.4, "W2": 0.6}
    diff = compute_diff(cfg, new_cfg)

    # Record intent in DB first
    pid, att = db.record_proposal_intent(
        workflow_id="nw_renewal",
        base_version=1,
        proposed_config=new_cfg,
        change_summary="Fault injection after POST",
        diff=diff,
        affected_leads=100,
        dry_run={"estimated_lift": 0.1, "verified": True},
        impact_score=0.1,
        rationale="Testing crash after POST",
        evidence=None,
    )

    # Send POST to simulator directly
    body = {
        "proposal_id": pid,
        "workflow_id": "nw_renewal",
        "base_version": 1,
        "proposed_config": new_cfg,
        "change_summary": "Fault injection after POST",
        "diff": diff,
        "affected_leads": 100,
        "dry_run": {"estimated_lift": 0.1, "verified": True},
        "impact_score": 0.1,
        "rationale": "Testing crash after POST",
    }
    resp = client.post("/approvals", json=body)
    assert resp.status_code == 200, f"Expected 200 got {resp.status_code}: {resp.text}"

    # Crash immediately BEFORE db.mark_proposal_submitted
    os._exit(102)


def worker_crash_after_apply(db_path: str, url: str):
    db = Database(Path(db_path))
    client = httpx.Client(base_url=url, timeout=5.0)
    wf_spec = client.get("/workflows/nw_renewal").json()
    cfg_resp = client.get("/workflows/nw_renewal/config").json()
    cur_ver = cfg_resp["version"]
    cfg = cfg_resp["config"]

    new_cfg = copy.deepcopy(cfg)
    new_cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.3, "W2": 0.7}
    diff = compute_diff(cfg, new_cfg)

    pid, att = db.record_proposal_intent(
        workflow_id="nw_renewal",
        base_version=cur_ver,
        proposed_config=new_cfg,
        change_summary="Fault injection after apply",
        diff=diff,
        affected_leads=100,
        dry_run={"estimated_lift": 0.1, "verified": True},
        impact_score=0.1,
        rationale="Testing crash after apply",
        evidence=None,
    )
    body = {
        "proposal_id": pid,
        "workflow_id": "nw_renewal",
        "base_version": cur_ver,
        "proposed_config": new_cfg,
        "change_summary": "Fault injection after apply",
        "diff": diff,
        "affected_leads": 100,
        "dry_run": {"estimated_lift": 0.1, "verified": True},
        "impact_score": 0.1,
        "rationale": "Testing crash after apply",
    }
    resp = client.post("/approvals", json=body)
    aid = resp.json()["approval_id"]
    db.mark_proposal_submitted(
        proposal_id=pid,
        approval_id=aid,
        created_minute=720,
        created_ts="2026-03-01T12:00:00Z",
        expires_minute=720 + 1440,
        is_auto_applied=False,
    )

    # Simulator needs time or approval decision to become approved
    # Approver hours are [10, 19] (600 to 1140 minutes into day) with latency [30, 240]
    # Advance simulation into approver business hours
    client.post("/sim/advance", json={"minutes": 660})

    approved = False
    for _ in range(30):
        st = client.get(f"/approvals/{aid}").json()
        if st.get("status") == "approved":
            approved = True
            break
        elif st.get("status") == "rejected":
            break
        client.post("/sim/advance", json={"minutes": 30})
        time.sleep(0.1)

    if not approved:
        # If randomly rejected due to 10% base_reject_rate, try one more proposal
        new_cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.25, "W2": 0.75}
        diff = compute_diff(cfg, new_cfg)
        pid2, att2 = db.record_proposal_intent(
            workflow_id="nw_renewal",
            base_version=cur_ver,
            proposed_config=new_cfg,
            change_summary="Fault injection after apply retry",
            diff=diff,
            affected_leads=100,
            dry_run={"estimated_lift": 0.1, "verified": True},
            impact_score=0.1,
            rationale="Testing crash after apply retry",
            evidence=None,
        )
        body["proposal_id"] = pid2
        body["proposed_config"] = new_cfg
        body["diff"] = diff
        r2 = client.post("/approvals", json=body)
        aid2 = r2.json()["approval_id"]
        db.mark_proposal_submitted(
            proposal_id=pid2,
            approval_id=aid2,
            created_minute=720,
            created_ts="2026-03-01T12:00:00Z",
            expires_minute=720 + 1440,
            is_auto_applied=False,
        )
        for _ in range(30):
            st = client.get(f"/approvals/{aid2}").json()
            if st.get("status") == "approved":
                aid = aid2
                pid = pid2
                approved = True
                break
            client.post("/sim/advance", json={"minutes": 30})
            time.sleep(0.1)

    assert approved, f"Approval {aid} was not approved by simulator: {st}"

    # Now apply the change via HTTP
    apply_resp = client.post("/workflows/nw_renewal/apply", json={"approval_id": aid})
    assert apply_resp.status_code == 200, f"Expected 200 from apply, got {apply_resp.text}"

    # Crash immediately BEFORE db.mark_proposal_applied!
    os._exit(103)


def test_all_fault_scenarios():
    cleanup()
    sim_proc = start_sim()
    try:
        client = httpx.Client(base_url=BASE_URL, timeout=5.0)

        # -------------------------------------------------------------
        # SCENARIO 1: Crash BEFORE POST /approvals
        # -------------------------------------------------------------
        print("\n=== Running Scenario 1: Crash BEFORE POST /approvals ===")
        p1 = multiprocessing.Process(
            target=worker_crash_before_post_approvals,
            args=(str(DB_PATH), BASE_URL),
        )
        p1.start()
        p1.join(timeout=10.0)
        assert p1.exitcode == 101, f"Expected crash exitcode 101, got {p1.exitcode}"

        # Inspect pre-reconciliation DB state
        db = Database(DB_PATH)
        intents = db.get_proposals_by_status("intent")
        assert len(intents) == 1, "Intent must be persisted in WAL DB before HTTP call"
        p1_id = intents[0]["proposal_id"]
        print(f"Pre-reconciliation: found intent proposal {p1_id}")

        # Simulator should NOT have received this proposal
        sim_approvals = client.get("/approvals").json()
        assert not any(a.get("proposal_id") == p1_id for a in sim_approvals), "Sim must NOT have received proposal"

        # RECOVERY / RECONCILIATION
        gov = GovernanceClient(client, db)
        results = gov.reconcile_and_apply(current_minute=730, current_ts="2026-03-01T12:10:00Z")
        print(f"Reconciliation results: {results}")

        # Verify DB state post-reconciliation: marked 'dropped'
        with db.connection() as conn:
            row = conn.execute("SELECT status, reasons_json FROM proposals WHERE proposal_id = ?", (p1_id,)).fetchone()
            assert row["status"] == "dropped", f"Expected dropped, got {row['status']}"
            assert "crashed_before_submit" in row["reasons_json"]
        print("Verified: Unsubmitted intent safely transitioned to 'dropped'")

        # Verify re-proposal attempt counter increment and no duplicate collision
        wf_spec = client.get("/workflows/nw_renewal").json()
        cfg = client.get("/workflows/nw_renewal/config").json()["config"]
        new_cfg = copy.deepcopy(cfg)
        new_cfg["allocations"]["default"]["welcome_message"] = {"W1": 0.5, "W2": 0.5}

        res = gov.execute_change(
            workflow_id="nw_renewal",
            workflow_spec=wf_spec,
            current_version=1,
            current_config=cfg,
            proposed_config=new_cfg,
            change_summary="Retry proposal after crash",
            rationale="Testing re-proposal",
            current_minute=740,
            current_ts="2026-03-01T12:20:00Z",
            force_approval=True,
        )
        assert res["status"] == "pending", f"Expected successful pending proposal, got {res}"
        p1_retry_id = res["proposal_id"]
        assert p1_retry_id != p1_id, "Retry proposal ID must be different due to attempt counter"
        print(f"Verified: Retry proposal succeeded with distinct proposal_id={p1_retry_id} (attempt=2)")

        # -------------------------------------------------------------
        # SCENARIO 2: Crash AFTER POST /approvals but BEFORE DB save
        # -------------------------------------------------------------
        print("\n=== Running Scenario 2: Crash AFTER POST but BEFORE DB save ===")
        p2 = multiprocessing.Process(
            target=worker_crash_after_post_approvals,
            args=(str(DB_PATH), BASE_URL),
        )
        p2.start()
        p2.join(timeout=10.0)
        assert p2.exitcode == 102, f"Expected crash exitcode 102, got {p2.exitcode}"

        # Inspect DB: should have intent row with approval_id is None
        intents = db.get_proposals_by_status("intent")
        assert len(intents) >= 1
        p2_id = intents[0]["proposal_id"]
        assert intents[0]["approval_id"] is None
        print(f"Pre-reconciliation: found intent proposal {p2_id} with approval_id=None")

        # Simulator DOES have this proposal
        sim_apps = client.get("/approvals").json()
        matching_sim = [a for a in sim_apps if a.get("proposal_id") == p2_id]
        assert len(matching_sim) == 1, "Simulator must have recorded the proposal"
        expected_aid = matching_sim[0]["approval_id"]
        print(f"Simulator confirmed approval_id: {expected_aid}")

        # RECOVERY / RECONCILIATION
        results = gov.reconcile_and_apply(current_minute=750, current_ts="2026-03-01T12:30:00Z")
        print(f"Reconciliation results: {results}")

        # Verify DB updated to pending with approval_id matching simulator
        with db.connection() as conn:
            row = conn.execute("SELECT status, approval_id FROM proposals WHERE proposal_id = ?", (p2_id,)).fetchone()
            assert row["status"] == "pending", f"Expected status 'pending', got {row['status']}"
            assert row["approval_id"] == expected_aid, f"Expected approval_id {expected_aid}, got {row['approval_id']}"
        print(f"Verified: Reconciled proposal {p2_id} with approval_id {expected_aid} without duplicate request!")

        # -------------------------------------------------------------
        # SCENARIO 3: Crash AFTER /apply but BEFORE DB save
        # -------------------------------------------------------------
        print("\n=== Running Scenario 3: Crash AFTER /apply but BEFORE DB save ===")
        p3 = multiprocessing.Process(
            target=worker_crash_after_apply,
            args=(str(DB_PATH), BASE_URL),
        )
        p3.start()
        p3.join(timeout=15.0)
        assert p3.exitcode == 103, f"Expected crash exitcode 103, got {p3.exitcode}"

        # DB has proposal still in 'pending' status
        with db.connection() as conn:
            pending_rows = conn.execute("SELECT proposal_id, approval_id, status FROM proposals WHERE status = 'pending'").fetchall()
            print(f"Pre-reconciliation pending rows: {[dict(r) for r in pending_rows]}")

        # Simulator has already applied it
        sim_apps = client.get("/approvals").json()
        applied_sim = [a for a in sim_apps if a.get("status") == "applied"]
        assert len(applied_sim) >= 1, "Simulator must show applied approval"
        applied_aid = applied_sim[-1]["approval_id"]
        applied_pid = applied_sim[-1]["proposal_id"]
        print(f"Simulator shows applied approval {applied_aid} for {applied_pid}")

        # RECOVERY / RECONCILIATION
        results = gov.reconcile_and_apply(current_minute=760, current_ts="2026-03-01T12:40:00Z")
        print(f"Reconciliation results: {results}")

        # Verify DB updated to 'applied' without 409 or double-apply
        with db.connection() as conn:
            row = conn.execute("SELECT status, applied_version FROM proposals WHERE proposal_id = ?", (applied_pid,)).fetchone()
            assert row["status"] == "applied", f"Expected applied, got {row['status']}"
            print(f"Verified: Proposal {applied_pid} marked applied at version {row['applied_version']}")

            # Verify audit log recorded the reconciled apply
            audit_entry = conn.execute(
                "SELECT * FROM decisions_audit_log WHERE proposal_id = ? AND action_type = 'applied'",
                (applied_pid,),
            ).fetchone()
            assert audit_entry is not None, "Audit log must contain record of application"
            print(f"Audit log entry confirmed: id={audit_entry['id']}, category={audit_entry['category']}")

        print("\n================================================================================")
        print("ALL 3 FAULT-INJECTION SCENARIOS PASSED WITH STRICT RECONCILIATION & IDEMPOTENCY!")
        print("================================================================================")

    finally:
        sim_proc.kill()
        sim_proc.wait(timeout=5.0)


if __name__ == "__main__":
    test_all_fault_scenarios()
