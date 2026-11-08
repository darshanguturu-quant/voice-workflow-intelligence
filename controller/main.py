"""Autonomous Controller Main Loop.

Drives the simulator strictly over HTTP: detects, experiments, proposes, applies, learns, and explains.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from controller.db import Database
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
from controller.governance import GovernanceClient
from controller.incidents import IncidentManager
from controller.ingest import IngestionEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("controller")


def run_controller(sim_url: str, db_path: str = "controller.db", tick_step: int = 60):
    client = httpx.Client(base_url=sim_url, timeout=120.0)
    db = Database(db_path)
    ingestion = IngestionEngine(client, db)
    governance = GovernanceClient(client, db)
    incident_mgr = IncidentManager(client, db, governance)

    logger.info("Connecting to simulator at %s...", sim_url)
    health_resp = client.get("/health")
    if health_resp.status_code != 200:
        logger.error("Simulator health check failed!")
        sys.exit(1)

    state = client.get("/sim/state").json()
    logger.info("Simulator state: scenario=%s, minute=%d, finished=%s", state["scenario"], state["sim_minute"], state["finished"])

    # Load workflows
    wfs_data = client.get("/workflows").json()
    workflows = {w["workflow_id"]: w for w in wfs_data}
    logger.info("Loaded %d workflows: %s", len(workflows), list(workflows.keys()))

    # Sync pre-D0 history if starting fresh
    cur = db.get_cursor()
    if cur is None:
        logger.info("Bootstrapping pre-D0 history chunks...")
        t0 = time.time()
        hist_events = ingestion.sync_history()
        logger.info("Ingested %d pre-D0 history events in %.2fs", hist_events, time.time() - t0)

    # Reconcile on startup (crash recovery)
    reconciled = governance.reconcile_and_apply(state["sim_minute"], state["ts"])
    if reconciled:
        logger.info("Startup reconciliation applied %d pending proposals", len(reconciled))

    total_events_processed = 0
    t_start = time.time()
    blacklisted_variants: set[tuple[str, str, str]] = set()

    # Main Autonomous Loop
    while True:
        # Advance simulation
        adv_resp = client.post("/sim/advance", json={"minutes": tick_step})
        if adv_resp.status_code != 200:
            if adv_resp.status_code == 409 and "finished" in adv_resp.text:
                logger.info("Simulation reached completion.")
                break
            logger.error("Advance failed HTTP %s: %s", adv_resp.status_code, adv_resp.text)
            break

        adv_data = adv_resp.json()
        sim_minute = adv_data["sim_minute"]
        sim_ts = adv_data["ts"]
        new_chunks = adv_data.get("new_chunks", [])

        # 1. Ingest new chunk events
        ev_count, lifecycle_events = ingestion.ingest_new_chunks(new_chunks)
        total_events_processed += ev_count

        # 2. Process lifecycle events (e.g. supervisor edits)
        for ev in lifecycle_events:
            if ev.get("type") == "config_changed":
                wid = ev.get("workflow_id")
                actor = ev.get("actor")
                changed_dims_list = ev.get("changed_dims", [])
                if actor == "supervisor":
                    logger.info("[%s] Supervisor edited workflow %s (changed_dims=%s)! Refreshing cached config.",
                                sim_ts, wid, changed_dims_list)
                    db.log_decision(
                        sim_minute=sim_minute,
                        sim_ts=sim_ts,
                        workflow_id=wid,
                        category="supervisor_edit_detected",
                        action_type="sync",
                        proposal_id=None,
                        details={"version": ev.get("version"), "changed_dims": changed_dims_list},
                    )
                    # Proactive validation on supervisor prompt changes (Pass 2 requirement)
                    if "prompt_version" in changed_dims_list and wid in workflows:
                        new_cfg_resp = client.get(f"/workflows/{wid}/config")
                        if new_cfg_resp.status_code == 200:
                            new_cfg_data = new_cfg_resp.json()
                            new_cfg = new_cfg_data["config"]
                            new_pv = new_cfg.get("prompt_version")
                            logger.info("[%s] Supervisor updated prompt_version to %s. Triggering proactive validation.",
                                        sim_ts, new_pv)
                            incident_mgr.proactively_validate_supervisor_prompt(
                                workflow_id=wid,
                                new_prompt_version=new_pv,
                                workflow_spec=workflows[wid],
                                current_version=new_cfg_data["version"],
                                current_config=new_cfg,
                                current_minute=sim_minute,
                                current_ts=sim_ts,
                            )

        # 3. Reconcile pending approvals
        governance.reconcile_and_apply(sim_minute, sim_ts)

        # 4. Evaluation and proactive optimization
        for wid, w_spec in workflows.items():
            cur_cfg_resp = client.get(f"/workflows/{wid}/config")
            if cur_cfg_resp.status_code != 200:
                continue
            cur_cfg_data = cur_cfg_resp.json()
            cur_version = cur_cfg_data["version"]
            cur_config = cur_cfg_data["config"]

            # 4a. Detection: Hallucination spike (sliced by current config version)
            halluc_incident = detect_hallucination_spike(db, wid, config_version=cur_version)
            if halluc_incident:
                logger.warning("[%s] Hallucination incident on %s: %s", sim_ts, wid, halluc_incident)
                incident_mgr.handle_hallucination_incident(
                    workflow_id=wid,
                    workflow_spec=w_spec,
                    current_version=cur_version,
                    current_config=cur_config,
                    current_minute=sim_minute,
                    current_ts=sim_ts,
                    incident_info=halluc_incident,
                )
                continue  # Priority to safety incidents

            # 4b. Detection: Caller ID degradation (PRB-002: sliding recent window)
            current_day = sim_minute // 1440
            degraded_dids = detect_caller_id_degradation(db, wid, current_day=current_day)
            if degraded_dids:
                deg_names = [d["caller_id"] for d in degraded_dids]
                cur_did_alloc = cur_config.get("allocations", {}).get("default", {}).get("caller_id", {})
                if any(cur_did_alloc.get(d, 0) > 0 for d in deg_names):
                    auto_allowed = "caller_id" in w_spec.get("governance", {}).get("auto_apply", {}).get("allowed_dims", [])
                    max_sh = w_spec.get("governance", {}).get("auto_apply", {}).get("max_shift", 0.5)
                    rot_cfg = plan_caller_id_rotation(
                        workflow_spec=w_spec,
                        current_config=cur_config,
                        degraded_dids=deg_names,
                        auto_apply_permitted=auto_allowed,
                        max_shift_limit=max_sh,
                    )
                    if rot_cfg:
                        deg_summary = ", ".join(
                            f"{d['caller_id']} (pickup={d['pickup_rate']:.3f} vs peers={d['peer_pickup_rate']:.3f}, z={d['z_score']})"
                            for d in degraded_dids
                        )
                        logger.info("[%s] Rotating degraded caller IDs %s for %s", sim_ts, deg_names, wid)
                        governance.execute_change(
                            workflow_id=wid,
                            workflow_spec=w_spec,
                            current_version=cur_version,
                            current_config=cur_config,
                            proposed_config=rot_cfg,
                            change_summary=f"Rotate traffic away from degraded caller IDs: {deg_names}",
                            rationale=f"Caller IDs showed statistically significant pickup degradation: {deg_summary}. Rotating traffic to healthy DIDs.",
                            current_minute=sim_minute,
                            current_ts=sim_ts,
                            impact_score=0.8,
                            affected_leads=500,
                        )

            # 4c. Detection: Decaying variants rollback (C7, Pass 4)
            decaying = detect_decaying_variants(db, wid, config_version=None, current_config=cur_config, decay_delta=0.03)
            if decaying:
                for d in decaying:
                    blacklisted_variants.add((wid, d["dim"], d["variant"]))
                rollback_cfg = plan_decay_rollback(w_spec, cur_config, decaying)
                if rollback_cfg:
                    decay_summary = ", ".join(
                        f"{d['variant']} (eng={d['rate']:.3f} vs peers={d['peer_rate']:.3f}, z={d['z_score']})"
                        for d in decaying
                    )
                    logger.info("[%s] Rolling back decaying variants %s on %s", sim_ts, decaying, wid)
                    governance.execute_change(
                        workflow_id=wid,
                        workflow_spec=w_spec,
                        current_version=cur_version,
                        current_config=cur_config,
                        proposed_config=rollback_cfg,
                        change_summary=f"Roll back decaying variants: {[d['variant'] for d in decaying]}",
                        rationale=f"Significant engagement decay detected: {decay_summary}. Phasing allocation down to 0.0% and redistributing to healthy variants.",
                        current_minute=sim_minute,
                        current_ts=sim_ts,
                        impact_score=0.75,
                        affected_leads=1000,
                    )

            # 4c. Experimentation: Welcome message and Voice optimization
            # Run during day hours (between 11:00 and 17:00 IST)
            mod_hour = (sim_minute % 1440) / 60.0
            if 11.0 <= mod_hour <= 17.0:
                # 1. Try evaluating existing exploration data for a statistically significant winner
                exp_res = evaluate_and_propose_variants(
                    db=db,
                    workflow_id=wid,
                    workflow_spec=w_spec,
                    current_version=cur_version,
                    current_config=cur_config,
                )
                if exp_res:
                    winner_voice = exp_res["winner_voice"]
                    logger.info("[%s] Promoting statistically superior voice %s for %s (lift=%+.3f, z=%.2f)",
                                sim_ts, winner_voice, wid, exp_res["lift"], exp_res["z_stat"])
                    governance.execute_change(
                        workflow_id=wid,
                        workflow_spec=w_spec,
                        current_version=cur_version,
                        current_config=cur_config,
                        proposed_config=exp_res["new_config"],
                        change_summary=f"Promote winning voice {winner_voice} for cohort default",
                        rationale=(
                            f"Statistically significant lift ({exp_res['lift']:+.3f}) detected with "
                            f"z={exp_res['z_stat']}, p={exp_res['p_val']:.4f} across n={exp_res['sample_size']} calls. "
                            f"Objective guardrail verified."
                        ),
                        current_minute=sim_minute,
                        current_ts=sim_ts,
                        impact_score=0.7,
                        affected_leads=1000,
                    )
                else:
                    # 2. If not enough data, initiate multi-arm exploration if currently un-explored
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
                        logger.info("[%s] Initiating multi-arm variant exploration for %s", sim_ts, wid)
                        governance.execute_change(
                            workflow_id=wid,
                            workflow_spec=w_spec,
                            current_version=cur_version,
                            current_config=cur_config,
                            proposed_config=explore_cfg,
                            change_summary="Initiate balanced multi-arm exploration across voices and welcome messages",
                            rationale="Allocate balanced traffic across untried voice and welcome message options sized by MDE power requirements (alpha=0.05, power=0.80).",
                            current_minute=sim_minute,
                            current_ts=sim_ts,
                            impact_score=0.4,
                            affected_leads=1000,
                        )

        if adv_data.get("finished"):
            logger.info("Scenario reached completion flag.")
            break

    elapsed = time.time() - t_start
    final_st = client.get("/sim/state").json()
    logger.info("Simulation completed! Total events=%d, Wall-clock time=%.2fs, Throughput=%.1f events/s, Credits remaining=%.1f",
                total_events_processed, elapsed, total_events_processed / max(elapsed, 0.001), final_st.get("credits_remaining", 0.0))


def main():
    parser = argparse.ArgumentParser(description="Outcome-First Autonomous Controller")
    parser.add_argument("--url", default="http://127.0.0.1:8700", help="Simulator base URL")
    parser.add_argument("--db", default="controller.db", help="Path to SQLite database")
    parser.add_argument("--tick", type=int, default=60, help="Advance minutes per tick (multiple of 15)")
    args = parser.parse_args()

    run_controller(sim_url=args.url, db_path=args.db, tick_step=args.tick)


if __name__ == "__main__":
    main()
