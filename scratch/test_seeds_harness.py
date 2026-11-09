"""Test harness to evaluate controller robustness across multiple random seeds.

Evaluates:
- public_01 across seeds [101, 102, 103]
- public_02 across seeds [202, 203, 204]

Measures:
- Primary KPI lift vs baseline for each workflow
- Harmful / neutral / beneficial change counts
- Guardrail compliance (pickup rate delta >= -tolerance)
- Flagged DID containment
- Incident MTTR (for public_02)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 8733
BASE_URL = f"http://127.0.0.1:{PORT}"
RUN_DIR = Path("runs/seed_eval")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_harness")


def run_seed_evaluation(scenario_path: str, seed: int, scenario_name: str) -> dict:
    scenario_file = Path(scenario_path)
    content = scenario_file.read_text(encoding="utf-8")

    # Replace seed in yaml
    import re
    new_content = re.sub(r"^seed:\s*\d+", f"seed: {seed}", content, flags=re.MULTILINE)
    
    seed_dir = RUN_DIR / f"{scenario_name}_seed_{seed}"
    seed_dir.mkdir(parents=True, exist_ok=True)
    custom_yaml = seed_dir / f"scenario.yaml"
    custom_yaml.write_text(new_content, encoding="utf-8")

    base_dir = seed_dir / "baseline"
    sim_dir = seed_dir / "sim_run"
    sim_log = seed_dir / "sim.log"
    ctrl_db = seed_dir / "ctrl.db"
    ctrl_log = seed_dir / "ctrl.log"

    for f in [ctrl_db, Path(f"{ctrl_db}-wal"), Path(f"{ctrl_db}-shm")]:
        if f.exists():
            f.unlink()

    env = os.environ.copy()
    env["PYTHONPATH"] = "."

    # 1. Run baseline
    logger.info("[%s seed %d] Running baseline...", scenario_name, seed)
    if base_dir.exists():
        shutil.rmtree(base_dir, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "sim", "baseline", "--scenario", str(custom_yaml), "--run-dir", str(base_dir)],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 2. Start sim serve
    logger.info("[%s seed %d] Starting simulator server...", scenario_name, seed)
    if sim_dir.exists():
        shutil.rmtree(sim_dir, ignore_errors=True)
    sim_log_f = open(sim_log, "w", encoding="utf-8")
    sim_proc = subprocess.Popen(
        [sys.executable, "-m", "sim", "serve", "--scenario", str(custom_yaml), "--run-dir", str(sim_dir), "--force", "--port", str(PORT)],
        env=env,
        stdout=sim_log_f,
        stderr=subprocess.STDOUT,
    )

    try:
        client = httpx.Client(base_url=BASE_URL, timeout=15.0)
        ready = False
        for i in range(400):
            try:
                r = client.get("/health")
                if r.status_code == 200 and r.json().get("ok"):
                    ready = True
                    logger.info("[%s seed %d] Simulator ready after ~%.1fs", scenario_name, seed, (i + 1) * 0.5)
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            sim_proc.kill()
            raise RuntimeError("Simulator failed to start within 200s")

        # 3. Launch controller
        logger.info("[%s seed %d] Launching controller...", scenario_name, seed)
        ctrl_log_f = open(ctrl_log, "w", encoding="utf-8")
        ctrl_cmd = [
            sys.executable,
            "-m",
            "controller.main",
            "--url",
            BASE_URL,
            "--db",
            str(ctrl_db),
            "--tick",
            "15",
        ]
        ctrl_proc = subprocess.Popen(ctrl_cmd, env=env, stdout=ctrl_log_f, stderr=subprocess.STDOUT)

        # Wait until finished
        while True:
            if ctrl_proc.poll() is not None:
                logger.info("Controller exited with code %s", ctrl_proc.returncode)
                break
            try:
                st = client.get("/sim/state").json()
                if st.get("finished", False):
                    logger.info("[%s seed %d] Simulator finished at minute %s", scenario_name, seed, st.get("sim_minute"))
                    break
            except Exception:
                pass
            time.sleep(2.0)

        # Stop controller
        if ctrl_proc.poll() is None:
            ctrl_proc.terminate()
            try:
                ctrl_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                ctrl_proc.kill()
        ctrl_log_f.close()

    finally:
        sim_proc.kill()
        sim_proc.wait(timeout=5.0)
        sim_log_f.close()

    # 4. Grade run
    logger.info("[%s seed %d] Grading run against baseline...", scenario_name, seed)
    res = subprocess.run(
        [sys.executable, "-m", "sim", "grade", "--run", str(sim_dir), "--baseline", str(base_dir), "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    grade = json.loads(res.stdout)

    # Check rollbacks from database
    import sqlite3
    rollbacks = []
    if ctrl_db.exists():
        conn = sqlite3.connect(str(ctrl_db))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for row in cur.execute("SELECT * FROM decisions_audit_log WHERE category = 'decay_detected'"):
            rollbacks.append(dict(row))
        conn.close()

    summary = {
        "scenario": scenario_name,
        "seed": seed,
        "delta_vs_baseline": grade.get("delta_vs_baseline", {}),
        "kpis": grade.get("kpis", {}),
        "baseline_kpis": grade.get("baseline_kpis", {}),
        "controller_changes": grade.get("controller_changes", {}).get("verdicts", {}),
        "changes_count": grade.get("controller_changes", {}).get("count", 0),
        "credits_used": grade.get("credits_used", 0),
        "incidents": grade.get("incidents", []),
        "decaying_variant_share": grade.get("decaying_variant_share_after_onset", {}),
        "decay_rollbacks_count": len(rollbacks),
        "decay_rollbacks": rollbacks,
    }

    report_path = seed_dir / "grade_summary.json"
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    results = {}
    seeds_p01 = [101, 102, 103]
    seeds_p02 = [202, 203, 204]

    logger.info("Starting seed robustness evaluation across seeds...")
    
    for s in seeds_p01:
        res = run_seed_evaluation("scenarios/public_01_dev.yaml", s, "public_01")
        results[f"public_01_seed_{s}"] = res

    for s in seeds_p02:
        res = run_seed_evaluation("scenarios/public_02_incidents.yaml", s, "public_02")
        results[f"public_02_seed_{s}"] = res

    out = Path("reports/seed_robustness.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Seed robustness evaluation complete! Written to %s", out)


if __name__ == "__main__":
    main()
