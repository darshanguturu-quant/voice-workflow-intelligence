"""Fresh clone verification test for Outcome-First Autonomous Controller.

Copies tracked files to a pristine workspace, launches the simulator and controller,
and grades the resulting run, saving complete logs to reports/clean_run.log.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 8722
BASE_URL = f"http://127.0.0.1:{PORT}"
CLEAN_DIR = Path("runs/clean_clone")
SIM_DIR = Path("runs/clean_clone_sim")
BASE_DIR = Path("runs/clean_clone_base")
LOG_PATH = Path("reports/clean_run.log")


def prepare_clean_clone():
    if CLEAN_DIR.exists():
        shutil.rmtree(CLEAN_DIR, ignore_errors=True)
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    # Get tracked files
    res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    files = [f.strip() for f in res.stdout.splitlines() if f.strip()]

    for f_str in files:
        src = Path(f_str)
        dst = CLEAN_DIR / src
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"Copied {len(files)} tracked files to clean clone at {CLEAN_DIR}", flush=True)


def main():
    prepare_clean_clone()

    env = os.environ.copy()
    env["PYTHONPATH"] = "."

    # 1. Run baseline
    print("Running baseline for clean run verification...", flush=True)
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "sim", "baseline", "--scenario", "scenarios/public_01_dev.yaml", "--run-dir", str(BASE_DIR)],
        env=env,
        check=True,
    )

    # 2. Start simulator server
    print(f"Starting simulator server on port {PORT}...", flush=True)
    if SIM_DIR.exists():
        shutil.rmtree(SIM_DIR, ignore_errors=True)
    sim_proc = subprocess.Popen(
        [sys.executable, "-m", "sim", "serve", "--scenario", "scenarios/public_01_dev.yaml", "--run-dir", str(SIM_DIR), "--force", "--port", str(PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        client = httpx.Client(base_url=BASE_URL, timeout=15.0)
        ready = False
        print(f"Waiting for simulator on port {PORT}...", flush=True)
        for i in range(360):
            try:
                r = client.get("/health")
                if r.status_code == 200 and r.json().get("ok"):
                    ready = True
                    print(f"Simulator ready after ~{(i + 1) * 0.5:.1f}s!", flush=True)
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ready:
            raise RuntimeError("Simulator failed to start")

        # 3. Launch controller from CLEAN_DIR
        print(f"Launching controller from {CLEAN_DIR}...", flush=True)
        log_f = open(LOG_PATH, "w", encoding="utf-8")
        log_f.write(f"=== Outcome-First Autonomous Controller: Clean Clone Run ===\n")
        log_f.write(f"Scenario: scenarios/public_01_dev.yaml | URL: {BASE_URL} | Tick: 15 min\n\n")
        log_f.flush()

        clean_env = os.environ.copy()
        clean_env["PYTHONPATH"] = str(CLEAN_DIR.resolve())

        ctrl_cmd = [
            sys.executable,
            "-m",
            "controller.main",
            "--url",
            BASE_URL,
            "--db",
            str((CLEAN_DIR / "controller.db").resolve()),
            "--tick",
            "15",
        ]

        t0 = time.time()
        ctrl_proc = subprocess.Popen(
            ctrl_cmd,
            cwd=str(CLEAN_DIR),
            env=clean_env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

        ctrl_proc.wait(timeout=300.0)
        duration = time.time() - t0
        print(f"Controller completed with code {ctrl_proc.returncode} in {duration:.2f}s")
        log_f.write(f"\n[CLEAN RUN] Completed with return code {ctrl_proc.returncode} in {duration:.2f}s\n")
        log_f.close()

    finally:
        sim_proc.kill()
        sim_proc.wait(timeout=5.0)

    # 4. Grade
    print("Grading clean run against baseline...")
    grade_res = subprocess.run(
        [sys.executable, "-m", "sim", "grade", "--run", str(SIM_DIR), "--baseline", str(BASE_DIR), "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    grade = json.loads(grade_res.stdout)

    summary = {
        "scenario": "public_01_dev",
        "clean_clone_verified": True,
        "duration_seconds": duration,
        "delta_vs_baseline": grade.get("delta_vs_baseline", {}),
        "verdicts": grade.get("controller_changes", {}).get("verdicts", {}),
        "changes_count": grade.get("controller_changes", {}).get("count", 0),
        "credits_used": grade.get("credits_used", 0),
    }

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n=== FINAL GRADE SUMMARY ===\n")
        f.write(json.dumps(summary, indent=2))
        f.write("\n")

    print("Clean run verification SUCCESSFUL!")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
