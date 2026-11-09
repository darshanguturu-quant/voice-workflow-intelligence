import json
import sqlite3
from pathlib import Path

runs_dir = Path("runs/seed_eval")

seeds = [
    ("public_01", 101),
    ("public_01", 102),
    ("public_01", 103),
    ("public_02", 202),
    ("public_02", 203),
    ("public_02", 204),
]

for sc, s in seeds:
    folder = runs_dir / f"{sc}_seed_{s}"
    grade_file = folder / "grade_summary.json"
    sim_dir = folder / "sim_run"
    base_dir = folder / "baseline"
    db_file = folder / "ctrl.db"

    print("=" * 80)
    print(f"SCENARIO: {sc} | SEED: {s}")
    print("=" * 80)

    # Run sim grade with json to get full controller_changes.detail
    import subprocess, sys, os
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    res = subprocess.run(
        [sys.executable, "-m", "sim", "grade", "--run", str(sim_dir), "--baseline", str(base_dir), "--json"],
        capture_output=True, text=True
    )
    grade = json.loads(res.stdout) if res.returncode == 0 else {}
    changes_detail = grade.get("controller_changes", {}).get("detail", [])
    print(f"Total Applied Changes: {len(changes_detail)}")
    verdicts = grade.get("controller_changes", {}).get("verdicts", {})
    print(f"Verdicts: {verdicts}")

    # Query SQLite proposals and decisions_audit_log
    conn = sqlite3.connect(str(db_file)) if db_file.exists() else None
    
    for i, ch in enumerate(changes_detail, 1):
        wid = ch.get("workflow_id")
        ver = ch.get("version")
        minute = ch.get("minute")
        verdict = ch.get("verdict")
        dims = ch.get("dims")
        eng_delta = ch.get("engage_delta")
        did_delta = ch.get("did_health_delta")
        actor = ch.get("actor")

        # Get rationale from proposals or decisions_audit_log
        rationale = "N/A"
        summary = "N/A"
        if conn:
            row = conn.execute(
                "SELECT rationale, change_summary FROM proposals WHERE workflow_id=? AND applied_version=?",
                (wid, ver)
            ).fetchone()
            if row:
                rationale, summary = row[0], row[1]
            else:
                row_log = conn.execute(
                    "SELECT details_json FROM decisions_audit_log WHERE workflow_id=? AND sim_minute <= ? ORDER BY sim_minute DESC LIMIT 1",
                    (wid, minute)
                ).fetchone()
                if row_log:
                    try:
                        d_log = json.loads(row_log[0])
                        rationale = d_log.get("rationale", str(d_log))
                    except Exception:
                        pass
                
        print(f"  [{i}] Min {minute:>5} | WF: {wid:<12} | v{ver} | Actor: {actor:<18} | Dims: {dims} | Verdict: {verdict.upper():<10}")
        print(f"      engage_delta: {eng_delta:+.4f} | did_health_delta: {did_delta:+.4f}")
        print(f"      Summary: {summary}")
        print(f"      Rationale: {rationale}")
        print()

    if conn:
        conn.close()
