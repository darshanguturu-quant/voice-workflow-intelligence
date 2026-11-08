"""Ingest Throughput Benchmark on real public_03_load event stream (Phase 3).

Measures wall-clock time and throughput (events/sec) for 850,000 events (1 full simulated day)
against the Platform SRE constraint (< 10 wall-clock minutes / 600s).
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from controller.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")


def benchmark_load_ingest(
    chunks_dir: str = "runs/bench03/history",
    db_path: str = "runs/bench_ingest.db",
    target_events: int = 850_000,
):
    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink(missing_ok=True)
    db = Database(db_file)

    chunk_files = sorted(Path(chunks_dir).glob("*.jsonl"))
    if not chunk_files:
        raise FileNotFoundError(f"No jsonl chunks found in {chunks_dir}")

    logger.info("Found %d chunk files in %s", len(chunk_files), chunks_dir)
    logger.info("Benchmarking ingestion of %d events (1 simulated day of public_03_load)...", target_events)

    events_ingested = 0
    chunk_num = 0
    t_start = time.time()

    agg_deltas: dict[tuple[str, str, int, str, str, str], dict[str, int]] = defaultdict(
        lambda: {"dials": 0, "connected": 0, "engaged": 0, "hallucinated": 0, "duration_s": 0, "turns": 0}
    )
    did_deltas: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"dials": 0, "connected": 0})
    new_leads: list[tuple[str, str, str, str, str]] = []

    last_event_id = 0
    sim_ts = ""
    sim_minute = 0
    batch_size = 25_000

    for cfile in chunk_files:
        logger.info("Streaming chunk file %s...", cfile.name)
        with open(cfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                events_ingested += 1
                last_event_id = ev.get("event_id", last_event_id)
                sim_ts = ev.get("ts", sim_ts)

                ev_type = ev.get("type")
                if ev_type == "call":
                    wf = ev.get("workflow_id", "")
                    cohort = ev.get("cohort_id", "default")
                    ver = ev.get("config_version", 1)
                    var = ev.get("variant", {})
                    w_msg = var.get("welcome_message", "")
                    voice = var.get("voice", "")
                    did = var.get("caller_id", "")

                    conn = 1 if ev.get("connected") else 0
                    eng = 1 if ev.get("engaged") else 0
                    qa = ev.get("qa") or {}
                    halluc = 1 if qa.get("hallucination_flag") else 0
                    dur = int(ev.get("duration_s", 0))
                    turns = int(ev.get("turns", 0))

                    k = (wf, cohort, ver, w_msg, voice, did)
                    d = agg_deltas[k]
                    d["dials"] += 1
                    d["connected"] += conn
                    d["engaged"] += eng
                    d["hallucinated"] += halluc
                    d["duration_s"] += dur
                    d["turns"] += turns

                    dd = did_deltas[(wf, did)]
                    dd["dials"] += 1
                    dd["connected"] += conn

                elif ev_type == "lead":
                    pid = ev.get("person_id")
                    feats = ev.get("features", {})
                    if pid:
                        new_leads.append((
                            pid,
                            feats.get("lang", ""),
                            feats.get("city_tier", ""),
                            feats.get("age_band", ""),
                            feats.get("value_band", ""),
                        ))

                if events_ingested % batch_size == 0:
                    chunk_num += 1
                    db.commit_chunk(
                        chunk_num=chunk_num,
                        last_event_id=last_event_id,
                        sim_minute=sim_minute,
                        sim_ts=sim_ts,
                        aggregates_deltas=agg_deltas,
                        did_deltas=did_deltas,
                        new_leads=new_leads,
                    )
                    agg_deltas.clear()
                    did_deltas.clear()
                    new_leads.clear()

                    cur_elapsed = time.time() - t_start
                    logger.info(
                        "Ingested %d/%d events (%.1f%%) in %.2fs (%.1f ev/s)",
                        events_ingested, target_events, (events_ingested / target_events) * 100.0,
                        cur_elapsed, events_ingested / max(cur_elapsed, 0.001)
                    )

                if events_ingested >= target_events:
                    break

        if events_ingested >= target_events:
            break

    # Flush remaining
    if agg_deltas or did_deltas or new_leads:
        chunk_num += 1
        db.commit_chunk(
            chunk_num=chunk_num,
            last_event_id=last_event_id,
            sim_minute=sim_minute,
            sim_ts=sim_ts,
            aggregates_deltas=agg_deltas,
            did_deltas=did_deltas,
            new_leads=new_leads,
        )

    total_wall_clock = time.time() - t_start
    overall_throughput = events_ingested / max(total_wall_clock, 0.001)
    db_size_mb = Path(db_path).stat().st_size / (1024 * 1024)

    logger.info("==================================================================")
    logger.info("PUBLIC_03 LOAD BENCHMARK RESULTS (1 Simulated Day):")
    logger.info("Total Events Ingested: %d", events_ingested)
    logger.info("Total Wall-Clock Time: %.2f seconds (%.2f minutes)", total_wall_clock, total_wall_clock / 60.0)
    logger.info("Overall Throughput: %.1f events/second", overall_throughput)
    logger.info("Database Size: %.2f MB", db_size_mb)
    logger.info("SRE Constraint (< 600s / 10 minutes per simulated day): %s", "PASSED" if total_wall_clock < 600 else "FAILED")
    logger.info("Safety Margin vs 10-minute SLA: %.1fx faster", 600.0 / max(total_wall_clock, 0.001))
    logger.info("==================================================================")

    res_data = {
        "scenario": "public_03_load",
        "description": "1 simulated day benchmark (850,000 events)",
        "events_ingested": events_ingested,
        "wall_clock_seconds": round(total_wall_clock, 2),
        "wall_clock_minutes": round(total_wall_clock / 60.0, 2),
        "throughput_events_per_sec": round(overall_throughput, 1),
        "database_size_mb": round(db_size_mb, 2),
        "sre_limit_seconds": 600,
        "passed_sre_constraint": total_wall_clock < 600,
        "safety_margin_factor": round(600.0 / max(total_wall_clock, 0.001), 2),
    }

    out_file = Path("reports/public_03_load_benchmark.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(res_data, indent=2))
    return res_data


if __name__ == "__main__":
    benchmark_load_ingest()
