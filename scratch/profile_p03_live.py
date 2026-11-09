"""Profile live public_03 loop using cProfile and per-phase timing.

Decomposes controller execution into:
- sim_advance: POST /sim/advance (simulator simulation execution)
- chunk_fetch_and_parse: GET /events/chunks/{id} and streaming line parsing
- db_commit: SQLite commit_chunk transaction
- config_fetches: GET /workflows/{wid}/config
- detection_checks: hallucination, caller ID, decay detectors
- experiment_checks: variant evaluation and exploration planning
- governance_reconciliation: GET/POST /approvals
"""
from __future__ import annotations

import cProfile
import copy
import io
import json
import logging
import os
import pstats
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
    evaluate_and_propose_variants,
    plan_caller_id_rotation,
    plan_decay_rollback,
    plan_exploration_allocation,
)

PORT = 8755
BASE_URL = f"http://127.0.0.1:{PORT}"
RUN_DIR = Path("runs/prof03_sim")
DB_PATH = Path("runs/prof03_ctrl.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("profiler")


def run_profiled_controller(target_sim_minute: int = 1440, skip_untouched: bool = False):
    db_file = DB_PATH
    for f in [db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")]:
        if f.exists():
            f.unlink()

    db = Database(db_file)
    client = httpx.Client(base_url=BASE_URL, timeout=60.0)
    ingestion = IngestionEngine(client, db)
    governance = GovernanceClient(client, db)
    incident_mgr = IncidentManager(client, db, governance)

    # Load workflows
    wf_resp = client.get("/workflows")
    wf_data = wf_resp.json()
    wf_list = wf_data if isinstance(wf_data, list) else wf_data.get("workflows", [])
    workflows = {w.get("workflow_id", w.get("id")): w for w in wf_list}

    # Bootstrap history chunks
    logger.info("Syncing history chunks...")
    t_boot_0 = time.perf_counter()
    hist_events = ingestion.sync_history()
    t_boot_1 = time.perf_counter()
    boot_duration = t_boot_1 - t_boot_0
    logger.info("History bootstrap completed (%d events) in %.2fs", hist_events, boot_duration)

    # Timing metrics
    timers = defaultdict(float)
    counts = defaultdict(int)

    blacklisted_variants: set[tuple[str, str, str]] = set()
    total_live_events = 0
    tick_count = 0
    t_live_start = time.perf_counter()

    # Track cached configs
    cached_configs: dict[str, dict] = {}
    cached_versions: dict[str, int] = {}
    for wid in workflows:
        r = client.get(f"/workflows/{wid}/config")
        if r.status_code == 200:
            d = r.json()
            cached_configs[wid] = d["config"]
            cached_versions[wid] = d["version"]

    ps_proc = psutil.Process()
    peak_rss = 0

    while True:
        rss = ps_proc.memory_info().rss
        if rss > peak_rss:
            peak_rss = rss

        # 1. Sim Advance
        t0 = time.perf_counter()
        adv_resp = client.post("/sim/advance", json={"minutes": 60})
        timers["sim_advance"] += time.perf_counter() - t0
        counts["sim_advance"] += 1

        if adv_resp.status_code == 409:
            break
        adv_data = adv_resp.json()
        sim_minute = adv_data["sim_minute"]
        sim_ts = adv_data["ts"]
        new_chunks = adv_data.get("new_chunks", [])

        # 2. Ingestion
        t0 = time.perf_counter()
        ev_count, lifecycle_events = ingestion.ingest_new_chunks(new_chunks)
        timers["ingest_chunks"] += time.perf_counter() - t0
        counts["ingest_chunks"] += len(new_chunks)
        total_live_events += ev_count
        tick_count += 1

        # 3. Lifecycle events
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

        # If skip_untouched is True, determine which workflows had new call activity
        if skip_untouched:
            # Check SQLite cursor or inspect active wids in this tick
            active_wids = db.get_active_workflow_ids_since(sim_minute - 60)
            target_wids = active_wids.union(touched_wids)
        else:
            target_wids = set(workflows.keys())

        # 5. Workflow checks
        for wid in target_wids:
            w_spec = workflows[wid]

            # Config fetch
            t0 = time.perf_counter()
            if skip_untouched and wid in cached_configs and wid not in touched_wids:
                cur_config = cached_configs[wid]
                cur_version = cached_versions[wid]
            else:
                cur_cfg_resp = client.get(f"/workflows/{wid}/config")
                if cur_cfg_resp.status_code != 200:
                    continue
                cur_cfg_data = cur_cfg_resp.json()
                cur_version = cur_cfg_data["version"]
                cur_config = cur_cfg_data["config"]
                cached_configs[wid] = cur_config
                cached_versions[wid] = cur_version
            timers["config_fetches"] += time.perf_counter() - t0
            counts["config_fetches"] += 1

            # Detect hallucination
            t0 = time.perf_counter()
            halluc_incident = detect_hallucination_spike(db, wid, config_version=cur_version)
            timers["detect_hallucination"] += time.perf_counter() - t0

            # Detect caller ID
            t0 = time.perf_counter()
            current_day = sim_minute // 1440
            degraded_dids = detect_caller_id_degradation(db, wid, current_day=current_day)
            timers["detect_caller_id"] += time.perf_counter() - t0
            if degraded_dids:
                deg_names = [d["caller_id"] for d in degraded_dids]
                cur_did_alloc = cur_config.get("allocations", {}).get("default", {}).get("caller_id", {})
                if any(cur_did_alloc.get(d, 0) > 0 for d in deg_names):
                    auto_allowed = "caller_id" in w_spec.get("governance", {}).get("auto_apply", {}).get("allowed_dims", [])
                    max_sh = w_spec.get("governance", {}).get("auto_apply", {}).get("max_shift", 0.5)
                    rot_cfg = plan_caller_id_rotation(w_spec, cur_config, deg_names, auto_allowed, max_sh)
                    if rot_cfg:
                        governance.execute_change(
                            workflow_id=wid,
                            workflow_spec=w_spec,
                            current_version=cur_version,
                            current_config=cur_config,
                            proposed_config=rot_cfg,
                            change_summary=f"Rotate traffic away from degraded caller IDs: {deg_names}",
                            rationale="Caller IDs showed pickup degradation.",
                            current_minute=sim_minute,
                            current_ts=sim_ts,
                            impact_score=0.8,
                            affected_leads=500,
                        )

            # Detect decay
            t0 = time.perf_counter()
            decaying = detect_decaying_variants(db, wid, config_version=None, current_config=cur_config, decay_delta=0.03)
            timers["detect_decay"] += time.perf_counter() - t0
            if decaying:
                for d in decaying:
                    blacklisted_variants.add((wid, d["dim"], d["variant"]))
                rollback_cfg = plan_decay_rollback(w_spec, cur_config, decaying)
                if rollback_cfg:
                    governance.execute_change(
                        workflow_id=wid,
                        workflow_spec=w_spec,
                        current_version=cur_version,
                        current_config=cur_config,
                        proposed_config=rollback_cfg,
                        change_summary=f"Roll back decaying variants: {[d['variant'] for d in decaying]}",
                        rationale="Significant engagement decay detected.",
                        current_minute=sim_minute,
                        current_ts=sim_ts,
                        impact_score=0.75,
                        affected_leads=1000,
                    )

            # Experimentation
            t0 = time.perf_counter()
            mod_hour = (sim_minute % 1440) / 60.0
            if 11.0 <= mod_hour <= 17.0:
                exp_res = evaluate_and_propose_variants(
                    db=db,
                    workflow_id=wid,
                    workflow_spec=w_spec,
                    current_version=cur_version,
                    current_config=cur_config,
                )
                if exp_res:
                    governance.execute_change(
                        workflow_id=wid,
                        workflow_spec=w_spec,
                        current_version=cur_version,
                        current_config=cur_config,
                        proposed_config=exp_res["new_config"],
                        change_summary=f"Promote winning voice {exp_res['winner_voice']}",
                        rationale=f"Statistically significant lift ({exp_res['lift']:+.3f}).",
                        current_minute=sim_minute,
                        current_ts=sim_ts,
                        impact_score=0.7,
                        affected_leads=1000,
                    )
                else:
                    auto_allowed = (
                        "voice" in w_spec.get("governance", {}).get("auto_apply", {}).get("allowed_dims", [])
                        and "welcome_message" in w_spec.get("governance", {}).get("auto_apply", {}).get("allowed_dims", [])
                    )
                    max_sh = w_spec.get("governance", {}).get("auto_apply", {}).get("max_shift", 0.2)
                    wf_blacklist = {(d, v) for w_id, d, v in blacklisted_variants if w_id == wid}
                    explore_cfg = plan_exploration_allocation(
                        workflow_spec=w_spec,
                        current_config=cur_config,
                        cohort_id="default",
                        auto_apply_permitted=auto_allowed,
                        max_shift_limit=max_sh,
                        exploration_cap=0.15,
                        blacklisted_variants=wf_blacklist,
                    )
                    if explore_cfg and explore_cfg != cur_config:
                        governance.execute_change(
                            workflow_id=wid,
                            workflow_spec=w_spec,
                            current_version=cur_version,
                            current_config=cur_config,
                            proposed_config=explore_cfg,
                            change_summary="Initiate balanced multi-arm exploration",
                            rationale="Allocate balanced traffic across untried options.",
                            current_minute=sim_minute,
                            current_ts=sim_ts,
                            impact_score=0.6,
                            affected_leads=500,
                        )
            timers["experimentation"] += time.perf_counter() - t0

        if sim_minute >= target_sim_minute:
            break

    t_live_end = time.perf_counter()
    live_duration = t_live_end - t_live_start
    live_eps = total_live_events / max(live_duration, 0.001)

    result = {
        "skip_untouched": skip_untouched,
        "target_sim_minute": target_sim_minute,
        "live_simulated_days": round(target_sim_minute / 1440.0, 2),
        "total_live_events": total_live_events,
        "live_duration_seconds": round(live_duration, 2),
        "live_seconds_per_simulated_day": round(live_duration / (target_sim_minute / 1440.0), 2),
        "live_throughput_events_per_sec": round(live_eps, 1),
        "peak_rss_mb": round(peak_rss / (1024 * 1024), 2),
        "tick_count": tick_count,
        "timing_breakdown_seconds": {k: round(v, 3) for k, v in timers.items()},
        "timing_percentages": {k: round(v / max(live_duration, 0.001) * 100, 1) for k, v in timers.items()},
    }
    return result


def main():
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR, ignore_errors=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = "."

    logger.info("Starting simulator on port %d...", PORT)
    sim_log = open("runs/prof03_sim.log", "w", encoding="utf-8")
    sim_proc = subprocess.Popen(
        [sys.executable, "-m", "sim", "serve", "--scenario", "scenarios/public_03_load.yaml", "--run-dir", str(RUN_DIR), "--port", str(PORT)],
        env=env,
        stdout=sim_log,
        stderr=subprocess.STDOUT,
    )

    try:
        c = httpx.Client(base_url=BASE_URL, timeout=15.0)
        ready = False
        for _ in range(800):
            if sim_proc.poll() is not None:
                raise RuntimeError(f"Simulator exited early with code {sim_proc.returncode}")
            try:
                r = c.get("/health")
                if r.status_code == 200 and r.json().get("ok"):
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            raise RuntimeError("Simulator failed to start within 400s")

        logger.info("Simulator ready. Profiling baseline controller loop (skip_untouched=False)...")
        pr = cProfile.Profile()
        pr.enable()
        res_baseline = run_profiled_controller(target_sim_minute=1440, skip_untouched=False)
        pr.disable()

        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
        ps.print_stats(30)
        cprofile_output = s.getvalue()

        output_path = Path("reports/public_03_profile.json")
        output_path.write_text(json.dumps(res_baseline, indent=2), encoding="utf-8")
        
        cprof_path = Path("reports/public_03_cprofile.txt")
        cprof_path.write_text(cprofile_output, encoding="utf-8")

        logger.info("Profile baseline results:\n%s", json.dumps(res_baseline, indent=2))

    finally:
        sim_proc.kill()
        sim_proc.wait(timeout=5.0)
        sim_log.close()


if __name__ == "__main__":
    main()
