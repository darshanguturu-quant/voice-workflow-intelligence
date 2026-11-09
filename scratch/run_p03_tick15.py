"""Run public_03 load test with --tick 15.

Measures:
- ticks/day (96 ticks)
- live seconds/day (wall clock and controller time)
- events/second
- per-tick decision lag
- peak RSS (via psutil)
- Ingestion and SQLite commit counted as controller time
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx
import psutil

from controller.db import Database
from controller.ingest import IngestionEngine
from controller.governance import GovernanceClient
from controller.incidents import IncidentManager
from controller.detection import (
    detect_caller_id_degradation,
    detect_decaying_variants,
    detect_hallucination_spike,
)
from controller.experiments import (
    VariantCooldownManager,
    evaluate_and_propose_variants,
    plan_caller_id_rotation,
    plan_decay_rollback,
    plan_exploration_allocation,
)

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"
RUN_DIR = Path("runs/load_sim_tick15")
DB_PATH = Path("runs/load_ctrl_tick15.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_tick15")


def start_simulator():
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "sim",
        "serve",
        "--scenario",
        "scenarios/public_03_load.yaml",
        "--run-dir",
        str(RUN_DIR),
        "--force",
        "--port",
        str(PORT),
    ]
    logger.info("Starting simulator on port %d...", PORT)
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Wait for simulator readiness
    client = httpx.Client(base_url=BASE_URL, timeout=5.0)
    ready = False
    for i in range(800):
        try:
            r = client.get("/health")
            if r.status_code == 200 and r.json().get("ok"):
                ready = True
                logger.info("Simulator ready after ~%.1fs", (i + 1) * 0.5)
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        proc.kill()
        raise RuntimeError("Simulator failed to start within timeout.")
    return proc


def run_load_test():
    proc = start_simulator()
    try:
        db_file = DB_PATH
        for f in [db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")]:
            if f.exists():
                f.unlink()

        db = Database(db_file)
        client = httpx.Client(base_url=BASE_URL, timeout=60.0)
        ingestion = IngestionEngine(client, db)
        governance = GovernanceClient(client, db)
        incident_mgr = IncidentManager(client, db, governance)
        cooldown_mgr = VariantCooldownManager(default_cooldown_minutes=1440, retest_share=0.05)

        # Workflows
        wf_resp = client.get("/workflows")
        wf_data = wf_resp.json()
        wf_list = wf_data if isinstance(wf_data, list) else wf_data.get("workflows", [])
        workflows = {w.get("workflow_id", w.get("id")): w for w in wf_list}
        logger.info("Loaded %d workflows: %s", len(workflows), list(workflows.keys()))

        # Sync history
        logger.info("Syncing 7-day pre-D0 history chunks...")
        t_boot_0 = time.perf_counter()
        hist_events = ingestion.sync_history()
        t_boot_1 = time.perf_counter()
        boot_duration = t_boot_1 - t_boot_0
        logger.info("History sync complete: %d events in %.2fs", hist_events, boot_duration)

        # Cache initial configs & record cohort definition hashes
        initial_configs = {}
        cached_configs = {}
        cached_versions = {}
        for wid in workflows:
            r = client.get(f"/workflows/{wid}/config")
            if r.status_code == 200:
                d = r.json()
                initial_configs[wid] = d["config"]
                cached_configs[wid] = d["config"]
                cached_versions[wid] = d["version"]
                db.record_config_version(wid, d["version"], d["config"])

        ps_proc = psutil.Process()
        peak_rss = ps_proc.memory_info().rss

        timers = defaultdict(float)
        counts = defaultdict(int)
        per_tick_decision_lags: list[float] = []

        total_live_events = 0
        tick_count = 0
        target_minutes = 1440
        sim_minute = 0

        logger.info("Beginning live simulation with --tick 15 for 1 simulated day (96 ticks)...")
        t_live_start = time.perf_counter()

        while sim_minute < target_minutes:
            rss = ps_proc.memory_info().rss
            if rss > peak_rss:
                peak_rss = rss

            # 1. Sim advance (15 min)
            t0 = time.perf_counter()
            adv_resp = client.post("/sim/advance", json={"minutes": 15})
            t_adv = time.perf_counter() - t0
            timers["sim_advance"] += t_adv
            counts["sim_advance"] += 1

            if adv_resp.status_code == 409:
                logger.info("Simulation reached 409 finished.")
                break
            adv_data = adv_resp.json()
            sim_minute = adv_data["sim_minute"]
            sim_ts = adv_data["ts"]
            new_chunks = adv_data.get("new_chunks", [])

            # Decision timer for this tick (controller time: ingest + SQLite + logic)
            t_tick_decision_start = time.perf_counter()

            # 2. Ingest new chunk events (streaming NDJSON + SQLite commit)
            t0 = time.perf_counter()
            ev_count, lifecycle_events = ingestion.ingest_new_chunks(new_chunks)
            t_ingest = time.perf_counter() - t0
            timers["ingest_chunks"] += t_ingest
            counts["ingest_chunks"] += len(new_chunks)
            total_live_events += ev_count
            tick_count += 1

            # 3. Lifecycle events (supervisor edits)
            t0 = time.perf_counter()
            touched_wids: set[str] = set()
            for ev in lifecycle_events:
                if ev.get("type") == "config_changed":
                    wid = ev.get("workflow_id")
                    touched_wids.add(wid)
                    actor = ev.get("actor")
                    changed_dims_list = ev.get("changed_dims", [])
                    if actor == "supervisor":
                        r = client.get(f"/workflows/{wid}/config")
                        if r.status_code == 200:
                            d = r.json()
                            cached_configs[wid] = d["config"]
                            cached_versions[wid] = d["version"]
                            db.record_config_version(wid, d["version"], d["config"])
                            if "prompt_version" in changed_dims_list:
                                incident_mgr.proactively_validate_supervisor_prompt(
                                    workflow_id=wid,
                                    new_prompt_version=d["config"].get("prompt_version"),
                                    workflow_spec=workflows[wid],
                                    current_version=d["version"],
                                    current_config=d["config"],
                                    current_minute=sim_minute,
                                    current_ts=sim_ts,
                                )
            timers["lifecycle"] += time.perf_counter() - t0

            # 4. Governance reconciliation
            t0 = time.perf_counter()
            governance.reconcile_and_apply(sim_minute, sim_ts)
            timers["governance_reconcile"] += time.perf_counter() - t0

            # 5. Workflow evaluation
            # Skip evaluation for idle workflows without call activity or edits
            target_wids = ingestion.active_workflows.union(touched_wids) if ingestion.active_workflows else set(workflows.keys())
            ingestion.active_workflows.clear()

            for wid in target_wids:
                w_spec = workflows.get(wid)
                if not w_spec:
                    continue

                # Config fetch (cached)
                t0 = time.perf_counter()
                cur_config = cached_configs.get(wid)
                cur_version = cached_versions.get(wid, 1)
                if cur_config is None or wid in touched_wids:
                    r = client.get(f"/workflows/{wid}/config")
                    if r.status_code == 200:
                        d = r.json()
                        cur_config = d["config"]
                        cur_version = d["version"]
                        cached_configs[wid] = cur_config
                        cached_versions[wid] = cur_version
                        db.record_config_version(wid, cur_version, cur_config)
                timers["config_fetches"] += time.perf_counter() - t0
                counts["config_fetches"] += 1

                # Hallucination spike
                t0 = time.perf_counter()
                halluc_incident = detect_hallucination_spike(db, wid, config_version=cur_version)
                timers["detect_hallucination"] += time.perf_counter() - t0

                # Caller ID degradation
                t0 = time.perf_counter()
                current_day = sim_minute // 1440
                degraded_dids = detect_caller_id_degradation(db, wid, current_day=current_day)
                timers["detect_caller_id"] += time.perf_counter() - t0

                # Longitudinal decay
                t0 = time.perf_counter()
                decaying = detect_decaying_variants(
                    db, wid, cohort_id="default", current_config=cur_config, initial_config=initial_configs.get(wid), decay_delta=0.04
                )
                timers["detect_decay"] += time.perf_counter() - t0

                # Experimentation
                t0 = time.perf_counter()
                promo = evaluate_and_propose_variants(
                    db=db,
                    workflow_id=wid,
                    workflow_spec=w_spec,
                    current_version=cur_version,
                    current_config=cur_config,
                    cohort_id="default",
                )
                timers["experimentation"] += time.perf_counter() - t0

            # Record per-tick controller decision lag
            t_tick_decision = time.perf_counter() - t_tick_decision_start
            per_tick_decision_lags.append(t_tick_decision)

            if tick_count % 16 == 0:
                logger.info("Simulated minute %d/1440 (%d ticks) | Live events: %d | Decision lag: %.3fs",
                            sim_minute, tick_count, total_live_events, t_tick_decision)

        t_live_total = time.perf_counter() - t_live_start
        total_controller_time = (
            timers["ingest_chunks"] +
            timers["lifecycle"] +
            timers["governance_reconcile"] +
            timers["config_fetches"] +
            timers["detect_hallucination"] +
            timers["detect_caller_id"] +
            timers["detect_decay"] +
            timers["experimentation"]
        )

        avg_decision_lag = sum(per_tick_decision_lags) / len(per_tick_decision_lags) if per_tick_decision_lags else 0.0
        max_decision_lag = max(per_tick_decision_lags) if per_tick_decision_lags else 0.0
        p95_decision_lag = sorted(per_tick_decision_lags)[int(len(per_tick_decision_lags) * 0.95)] if per_tick_decision_lags else 0.0

        db_size_mb = db_file.stat().st_size / (1024 * 1024) if db_file.exists() else 0.0
        wal_file = Path(f"{db_file}-wal")
        wal_size_mb = wal_file.stat().st_size / (1024 * 1024) if wal_file.exists() else 0.0
        peak_rss_mb = peak_rss / (1024 * 1024)

        results = {
            "scenario": "public_03_load",
            "tick_step_minutes": 15,
            "ticks_per_day": tick_count,
            "sim_days_processed": 1.0,
            "total_live_events": total_live_events,
            "live_wall_clock_seconds": round(t_live_total, 2),
            "live_controller_seconds": round(total_controller_time, 2),
            "sim_advance_seconds": round(timers["sim_advance"], 2),
            "wall_throughput_events_per_sec": round(total_live_events / t_live_total, 1),
            "controller_throughput_events_per_sec": round(total_live_events / total_controller_time, 1),
            "sre_constraint_seconds_per_day": 600.0,
            "safety_margin_wall": round(600.0 / t_live_total, 2),
            "safety_margin_controller": round(600.0 / total_controller_time, 2),
            "peak_rss_mb": round(peak_rss_mb, 2),
            "database_size_mb": round(db_size_mb + wal_size_mb, 2),
            "per_tick_decision_lag": {
                "mean_seconds": round(avg_decision_lag, 4),
                "max_seconds": round(max_decision_lag, 4),
                "p95_seconds": round(p95_decision_lag, 4),
            },
            "timing_breakdown_seconds": {k: round(v, 2) for k, v in timers.items()},
        }

        logger.info("=" * 80)
        logger.info("LOAD TEST --tick 15 RESULTS:")
        logger.info("  Ticks/day: %d", tick_count)
        logger.info("  Total live events: %d", total_live_events)
        logger.info("  Live wall-clock: %.2fs (%.1f ev/s)", t_live_total, total_live_events / t_live_total)
        logger.info("  Live controller time (ingest + db + decisions): %.2fs (%.1f ev/s)", total_controller_time, total_live_events / total_controller_time)
        logger.info("  Per-tick decision lag: mean=%.3fs, p95=%.3fs, max=%.3fs", avg_decision_lag, p95_decision_lag, max_decision_lag)
        logger.info("  Peak RSS: %.2f MB", peak_rss_mb)
        logger.info("  Database size: %.2f MB", db_size_mb + wal_size_mb)
        logger.info("  SRE safety margin: %.2fx faster than 600s SLA", 600.0 / t_live_total)
        logger.info("=" * 80)

        Path("reports").mkdir(exist_ok=True)
        with open("reports/public_03_tick15.json", "w") as f:
            json.dump(results, f, indent=2)

        # Also update public_03_profile.json
        with open("reports/public_03_profile.json", "w") as f:
            json.dump(results, f, indent=2)

    finally:
        logger.info("Terminating simulator...")
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    run_load_test()
