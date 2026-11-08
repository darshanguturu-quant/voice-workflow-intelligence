"""Ingest and Decision Throughput Benchmark on public_03_load (Phase 3 requirement).

Validates Kabir Menon's (Platform SRE) requirement:
'Ingest and decide on one simulated day of public_03_load in under 10 wall-clock minutes'.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import httpx

from controller.db import Database
from controller.ingest import IngestionEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")


def run_benchmark(sim_url: str, db_path: str = "runs/bench_p03.db", max_sim_minutes: int = 1440):
    client = httpx.Client(base_url=sim_url, timeout=300.0)
    db = Database(db_path)
    ingestion = IngestionEngine(client, db)

    logger.info("Connecting to simulator at %s...", sim_url)
    health = client.get("/health").json()
    st = client.get("/sim/state").json()
    logger.info("Starting benchmark on scenario %s, sim_minute=%d", st["scenario"], st["sim_minute"])

    start_sim_min = st["sim_minute"]
    target_sim_min = start_sim_min + max_sim_minutes

    total_events = 0
    t0 = time.time()
    tick_step = 60

    while True:
        tick_t0 = time.time()
        adv = client.post("/sim/advance", json={"minutes": tick_step}).json()
        sim_min = adv["sim_minute"]
        chunks = adv.get("new_chunks", [])

        # Ingestion
        ingest_t0 = time.time()
        ev_count, _ = ingestion.ingest_new_chunks(chunks)
        ingest_time = time.time() - ingest_t0

        total_events += ev_count
        tick_elapsed = time.time() - tick_t0

        logger.info(
            "Sim minute: %d/%d | Chunk events: %d (in %.2fs, %.1f ev/s) | Tick total: %.2fs",
            sim_min, target_sim_min, ev_count, ingest_time, ev_count / max(ingest_time, 0.001), tick_elapsed
        )

        if sim_min >= target_sim_min or adv.get("finished"):
            break

    total_wall_clock = time.time() - t0
    overall_throughput = total_events / max(total_wall_clock, 0.001)

    logger.info("==================================================================")
    logger.info("BENCHMARK COMPLETED FOR 1 SIMULATED DAY (%d minutes):", max_sim_minutes)
    logger.info("Total Events Ingested: %d", total_events)
    logger.info("Total Wall-Clock Time: %.2f seconds (%.2f minutes)", total_wall_clock, total_wall_clock / 60.0)
    logger.info("Overall Throughput: %.1f events/second", overall_throughput)
    logger.info("SRE Constraint (< 10 minutes per simulated day): %s", "PASSED" if total_wall_clock < 600 else "FAILED")
    logger.info("==================================================================")

    # Save benchmark results
    out_file = Path("reports/public_03_throughput_day1.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps({
        "scenario": st["scenario"],
        "sim_minutes_evaluated": max_sim_minutes,
        "total_events": total_events,
        "wall_clock_seconds": round(total_wall_clock, 2),
        "wall_clock_minutes": round(total_wall_clock / 60.0, 2),
        "throughput_events_per_sec": round(overall_throughput, 1),
        "sre_limit_seconds": 600,
        "passed_sre_constraint": total_wall_clock < 600,
    }, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8704")
    parser.add_argument("--db", default="runs/bench_p03.db")
    parser.add_argument("--minutes", type=int, default=1440)
    args = parser.parse_args()
    run_benchmark(args.url, args.db, args.minutes)


if __name__ == "__main__":
    main()
