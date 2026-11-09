import json
import sqlite3
from pathlib import Path

seeds = [202, 203, 204]
for s in seeds:
    db_path = Path(f"runs/seed_eval/public_02_seed_{s}/ctrl.db")
    if not db_path.exists():
        print(f"Seed {s}: DB not found at {db_path}")
        continue
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print("=" * 80)
    print(f"SEED {s} MTTR AUDIT BREAKDOWN")
    print("=" * 80)

    # 1. Incident Injection: D3 11:30 = 3*1440 + 11*60 + 30 = 4320 + 690 = 5010
    inj_minute = 5010

    # 2. Query decisions_audit_log for supervisor edit, proactive validation, proposal request, approval, apply
    rows = conn.execute("""
        SELECT sim_minute, sim_ts, workflow_id, category, action_type, proposal_id, details_json
        FROM decisions_audit_log
        WHERE workflow_id = 'ks_cancel' AND sim_minute >= 4900 AND sim_minute <= 6000
        ORDER BY sim_minute ASC, id ASC
    """).fetchall()

    for r in rows:
        print(f"Min {r['sim_minute']:>5} | {r['sim_ts']} | Cat: {r['category']:<20} | Act: {r['action_type']:<15} | Prop: {r['proposal_id']}")
        try:
            d = json.loads(r['details_json'])
            # Print relevant fields
            keys = ["version", "approval_id", "status", "rationale", "changed_dims", "pass_rate", "rate"]
            filtered = {k: d[k] for k in keys if k in d}
            if filtered:
                print(f"      Details: {filtered}")
        except Exception:
            pass

    # Query proposals table for proposal details
    prop_rows = conn.execute("""
        SELECT proposal_id, status, is_incident, created_minute, decided_minute, apply_by_minute, applied_version, rationale
        FROM proposals
        WHERE workflow_id = 'ks_cancel' AND is_incident = 1
    """).fetchall()

    for p in prop_rows:
        print("-" * 60)
        print(f"PROPOSAL: {p['proposal_id']} | Status: {p['status']} | Created: Min {p['created_minute']} | Decided: Min {p['decided_minute']} | Applied Ver: {p['applied_version']}")
        print(f"Rationale: {p['rationale']}")
    conn.close()
