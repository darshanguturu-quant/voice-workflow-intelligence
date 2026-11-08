import sqlite3
import json

conn = sqlite3.connect("runs/mine02_ctrl.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM decisions_audit_log WHERE workflow_id = 'ks_cancel' ORDER BY sim_minute ASC").fetchall()
print(f"Total audit entries for ks_cancel: {len(rows)}")
for r in rows:
    print(f"minute={r['sim_minute']} ts={r['sim_ts']} cat={r['category']} act={r['action_type']} pid={r['proposal_id']}")
    print(f"   details: {r['details_json']}")

print("\n--- Proposals for ks_cancel ---")
props = conn.execute("SELECT * FROM proposals WHERE workflow_id = 'ks_cancel' ORDER BY created_minute ASC").fetchall()
for p in props:
    print(f"pid={p['proposal_id']} base_v={p['base_version']} att={p['attempt']} st={p['status']} inc={p['is_incident']} created_m={p['created_minute']} decided_m={p['decided_minute']} app_v={p['applied_version']}")
    print(f"   diff={p['diff_json']}")
    print(f"   evidence={p['evidence_json']}")

conn.close()
