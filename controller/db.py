"""SQLite persistence layer with WAL mode for streaming ingestion and audit trail.

Ensures atomicity, idempotency, and crash recovery.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 10000;

CREATE TABLE IF NOT EXISTS events_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_chunk INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL,
    sim_minute INTEGER NOT NULL,
    sim_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incremental_aggregates (
    workflow_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    welcome_message TEXT NOT NULL,
    voice TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    engaged INTEGER DEFAULT 0,
    hallucinated INTEGER DEFAULT 0,
    duration_s INTEGER DEFAULT 0,
    turns INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, cohort_id, config_version, welcome_message, voice, caller_id)
);

CREATE TABLE IF NOT EXISTS caller_id_stats (
    workflow_id TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, caller_id)
);

CREATE TABLE IF NOT EXISTS caller_id_daily (
    workflow_id TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    dials INTEGER DEFAULT 0,
    connected INTEGER DEFAULT 0,
    PRIMARY KEY (workflow_id, caller_id, day)
);

CREATE TABLE IF NOT EXISTS proposal_attempts (
    attempt_key TEXT PRIMARY KEY,
    last_attempt INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    is_incident INTEGER DEFAULT 0,
    change_summary TEXT,
    diff_json TEXT,
    affected_leads INTEGER,
    dry_run_json TEXT,
    impact_score REAL,
    rationale TEXT,
    evidence_json TEXT,
    proposed_config_json TEXT NOT NULL,
    created_minute INTEGER,
    created_ts TEXT,
    expires_minute INTEGER,
    decided_minute INTEGER,
    apply_by_minute INTEGER,
    approval_id TEXT,
    reasons_json TEXT,
    applied_version INTEGER
);

CREATE TABLE IF NOT EXISTS decisions_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER NOT NULL,
    sim_ts TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    category TEXT NOT NULL,
    action_type TEXT NOT NULL,
    proposal_id TEXT,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_features (
    person_id TEXT PRIMARY KEY,
    lang TEXT,
    city_tier TEXT,
    age_band TEXT,
    value_band TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_wf_status ON proposals (workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_wf_ts ON decisions_audit_log (workflow_id, sim_ts);
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    # ---------------------------------------------------------------- Cursor ops
    def get_cursor(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM events_cursor WHERE id = 1").fetchone()
            if row:
                return dict(row)
            return None

    def update_cursor(self, last_chunk: int, last_event_id: int, sim_minute: int, sim_ts: str):
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO events_cursor (id, last_chunk, last_event_id, sim_minute, sim_ts)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_chunk=excluded.last_chunk,
                    last_event_id=excluded.last_event_id,
                    sim_minute=excluded.sim_minute,
                    sim_ts=excluded.sim_ts
                """,
                (last_chunk, last_event_id, sim_minute, sim_ts),
            )
            conn.commit()

    # --------------------------------------------------------- Incremental Ingest
    def commit_chunk(
        self,
        chunk_num: int,
        last_event_id: int,
        sim_minute: int,
        sim_ts: str,
        aggregates_deltas: dict[tuple[str, str, int, str, str, str], dict[str, int]],
        did_deltas: dict[tuple[str, str], dict[str, int]],
        new_leads: list[tuple[str, str, str, str, str]],
        did_daily_deltas: dict[tuple[str, str, int], dict[str, int]] | None = None,
    ):
        """Atomically applies a chunk's event deltas and updates the cursor in ONE transaction."""
        with self.connection() as conn:
            cursor = conn.cursor()

            # 1. Update aggregates
            for (wf, cohort, ver, w_msg, voice, did), d in aggregates_deltas.items():
                cursor.execute(
                    """
                    INSERT INTO incremental_aggregates (
                        workflow_id, cohort_id, config_version, welcome_message, voice, caller_id,
                        dials, connected, engaged, hallucinated, duration_s, turns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, cohort_id, config_version, welcome_message, voice, caller_id)
                    DO UPDATE SET
                        dials = dials + excluded.dials,
                        connected = connected + excluded.connected,
                        engaged = engaged + excluded.engaged,
                        hallucinated = hallucinated + excluded.hallucinated,
                        duration_s = duration_s + excluded.duration_s,
                        turns = turns + excluded.turns
                    """,
                    (
                        wf, cohort, ver, w_msg, voice, did,
                        d.get("dials", 0), d.get("connected", 0), d.get("engaged", 0),
                        d.get("hallucinated", 0), d.get("duration_s", 0), d.get("turns", 0),
                    ),
                )

            # 2. Update caller ID stats (cumulative and daily)
            for (wf, did), d in did_deltas.items():
                cursor.execute(
                    """
                    INSERT INTO caller_id_stats (workflow_id, caller_id, dials, connected)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(workflow_id, caller_id)
                    DO UPDATE SET
                        dials = dials + excluded.dials,
                        connected = connected + excluded.connected
                    """,
                    (wf, did, d.get("dials", 0), d.get("connected", 0)),
                )

            if did_daily_deltas:
                for (wf, did, c_day), d in did_daily_deltas.items():
                    cursor.execute(
                        """
                        INSERT INTO caller_id_daily (workflow_id, caller_id, day, dials, connected)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(workflow_id, caller_id, day)
                        DO UPDATE SET
                            dials = dials + excluded.dials,
                            connected = connected + excluded.connected
                        """,
                        (wf, did, c_day, d.get("dials", 0), d.get("connected", 0)),
                    )
            else:
                current_day = sim_minute // 1440
                for (wf, did), d in did_deltas.items():
                    cursor.execute(
                        """
                        INSERT INTO caller_id_daily (workflow_id, caller_id, day, dials, connected)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(workflow_id, caller_id, day)
                        DO UPDATE SET
                            dials = dials + excluded.dials,
                            connected = connected + excluded.connected
                        """,
                        (wf, did, current_day, d.get("dials", 0), d.get("connected", 0)),
                    )

            # 3. Store new lead features
            if new_leads:
                cursor.executemany(
                    """
                    INSERT OR IGNORE INTO lead_features (person_id, lang, city_tier, age_band, value_band)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    new_leads,
                )

            # 4. Update cursor
            cursor.execute(
                """
                INSERT INTO events_cursor (id, last_chunk, last_event_id, sim_minute, sim_ts)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_chunk=excluded.last_chunk,
                    last_event_id=excluded.last_event_id,
                    sim_minute=excluded.sim_minute,
                    sim_ts=excluded.sim_ts
                """,
                (chunk_num, last_event_id, sim_minute, sim_ts),
            )
            conn.commit()

    # ------------------------------------------------------------- Governance Ops
    def record_proposal_intent(
        self,
        workflow_id: str,
        base_version: int,
        proposed_config: dict[str, Any],
        change_summary: str,
        diff: dict[str, Any],
        affected_leads: int,
        dry_run: dict[str, Any],
        impact_score: float,
        rationale: str,
        evidence: dict[str, Any] | None,
        is_incident: bool = False,
    ) -> tuple[str, int]:
        """Calculates deterministic proposal_id with persisted attempt counter to resolve PRB-001.

        Returns:
            (proposal_id, attempt_number)
        """
        canonical_cfg = json.dumps(proposed_config, sort_keys=True)
        cfg_hash = hashlib.sha256(canonical_cfg.encode("utf-8")).hexdigest()[:10]
        attempt_key = f"{workflow_id}:v{base_version}:{cfg_hash}"

        with self.connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT last_attempt FROM proposal_attempts WHERE attempt_key = ?", (attempt_key,)
            ).fetchone()
            attempt = (row["last_attempt"] + 1) if row else 1

            cursor.execute(
                """
                INSERT INTO proposal_attempts (attempt_key, last_attempt)
                VALUES (?, ?)
                ON CONFLICT(attempt_key) DO UPDATE SET last_attempt = excluded.last_attempt
                """,
                (attempt_key, attempt),
            )

            raw_id = f"{workflow_id}:{base_version}:{attempt}:{cfg_hash}"
            proposal_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

            cursor.execute(
                """
                INSERT INTO proposals (
                    proposal_id, workflow_id, base_version, attempt, status, is_incident,
                    change_summary, diff_json, affected_leads, dry_run_json, impact_score,
                    rationale, evidence_json, proposed_config_json
                ) VALUES (?, ?, ?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id, workflow_id, base_version, attempt, int(is_incident),
                    change_summary, json.dumps(diff), affected_leads, json.dumps(dry_run),
                    impact_score, rationale, json.dumps(evidence) if evidence else None,
                    canonical_cfg,
                ),
            )
            conn.commit()
            return proposal_id, attempt

    def mark_proposal_submitted(
        self,
        proposal_id: str,
        approval_id: str | None,
        created_minute: int,
        created_ts: str,
        expires_minute: int,
        is_auto_applied: bool = False,
        applied_version: int | None = None,
    ):
        status = "applied" if is_auto_applied else "pending"
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE proposals SET
                    status = ?,
                    approval_id = ?,
                    created_minute = ?,
                    created_ts = ?,
                    expires_minute = ?,
                    applied_version = ?
                WHERE proposal_id = ?
                """,
                (status, approval_id, created_minute, created_ts, expires_minute, applied_version, proposal_id),
            )
            conn.commit()

    def mark_proposal_decided(
        self,
        proposal_id: str,
        status: str,
        decided_minute: int,
        reasons: list[str],
        apply_by_minute: int | None = None,
    ):
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE proposals SET
                    status = ?,
                    decided_minute = ?,
                    reasons_json = ?,
                    apply_by_minute = ?
                WHERE proposal_id = ?
                """,
                (status, decided_minute, json.dumps(reasons), apply_by_minute, proposal_id),
            )
            conn.commit()

    def mark_proposal_applied(self, proposal_id: str, applied_version: int):
        with self.connection() as conn:
            conn.execute(
                "UPDATE proposals SET status = 'applied', applied_version = ? WHERE proposal_id = ?",
                (applied_version, proposal_id),
            )
            conn.commit()

    def get_proposals_by_status(self, status: str, workflow_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if workflow_id:
                rows = conn.execute(
                    "SELECT * FROM proposals WHERE status = ? AND workflow_id = ?", (status, workflow_id)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM proposals WHERE status = ?", (status,)).fetchall()
            return [dict(r) for r in rows]

    def count_proposals_last_24h(self, workflow_id: str, current_minute: int) -> int:
        cutoff = current_minute - 1440
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM proposals
                WHERE workflow_id = ? AND created_minute >= ? AND status != 'intent'
                """,
                (workflow_id, cutoff),
            ).fetchone()
            return row["cnt"] if row else 0

    # ------------------------------------------------------------- Audit Log Ops
    def log_decision(
        self,
        sim_minute: int,
        sim_ts: str,
        workflow_id: str,
        category: str,
        action_type: str,
        proposal_id: str | None,
        details: dict[str, Any],
    ):
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO decisions_audit_log (
                    sim_minute, sim_ts, workflow_id, category, action_type, proposal_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sim_minute, sim_ts, workflow_id, category, action_type, proposal_id, json.dumps(details)),
            )
            conn.commit()

    def query_audit_log(
        self,
        workflow_id: str | None = None,
        sim_minute_from: int | None = None,
        sim_minute_to: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM decisions_audit_log WHERE 1=1"
        params: list[Any] = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if sim_minute_from is not None:
            query += " AND sim_minute >= ?"
            params.append(sim_minute_from)
        if sim_minute_to is not None:
            query += " AND sim_minute <= ?"
            params.append(sim_minute_to)
        query += " ORDER BY sim_minute ASC LIMIT ?"
        params.append(limit)

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------- Data Query Helpers
    def get_caller_id_stats(self, workflow_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM caller_id_stats WHERE workflow_id = ?", (workflow_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_caller_id_stats(self, workflow_id: str, current_day: int, days_back: int = 2) -> list[dict[str, Any]]:
        min_day = max(0, current_day - days_back)
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT workflow_id, caller_id, SUM(dials) as dials, SUM(connected) as connected
                FROM caller_id_daily
                WHERE workflow_id = ? AND day >= ?
                GROUP BY workflow_id, caller_id
                """,
                (workflow_id, min_day),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_variant_stats(
        self, workflow_id: str, cohort_id: str = "default", config_version: int | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT welcome_message, voice,
                   SUM(dials) as dials,
                   SUM(connected) as connected,
                   SUM(engaged) as engaged,
                   SUM(hallucinated) as hallucinated
            FROM incremental_aggregates
            WHERE workflow_id = ? AND cohort_id = ?
        """
        params: list[Any] = [workflow_id, cohort_id]
        if config_version is not None:
            query += " AND config_version = ?"
            params.append(config_version)
        query += " GROUP BY welcome_message, voice"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
