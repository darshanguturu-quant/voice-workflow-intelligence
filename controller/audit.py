"""Queryable decision and audit store interface.

Answers: 'Why did workflow X change at time T, who approved it, and what happened next?'
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from controller.db import Database


def inspect_decision(db: Database, workflow_id: str, at_ts: str | None = None) -> list[dict[str, Any]]:
    """Retrieves full decision and proposal context for a workflow around a given timestamp."""
    entries = db.query_audit_log(workflow_id=workflow_id, limit=100)
    if not at_ts:
        return entries

    # Filter close to timestamp
    return [e for e in entries if at_ts in e["sim_ts"]]


def format_audit_report(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No audit records found matching criteria."

    lines = ["# Outcome-First Decision Audit Log", ""]
    for e in entries:
        lines.append(f"### [{e['sim_ts']}] Workflow: `{e['workflow_id']}` | Action: `{e['action_type']}`")
        lines.append(f"- **Category**: {e['category']}")
        lines.append(f"- **Proposal ID**: {e.get('proposal_id') or 'N/A'}")
        details = json.loads(e["details_json"])
        lines.append(f"- **Details**: ```json\n{json.dumps(details, indent=2)}\n```")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Query Controller Decision Audit Store")
    parser.add_argument("--db", default="controller.db", help="Path to SQLite database")
    parser.add_argument("--workflow", help="Workflow ID to query")
    parser.add_argument("--at", help="Timestamp or date substring (e.g. 2027-01-05)")
    args = parser.parse_args()

    db = Database(args.db)
    records = inspect_decision(db, args.workflow, args.at)
    print(format_audit_report(records))


if __name__ == "__main__":
    main()
