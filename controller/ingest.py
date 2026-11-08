"""High-throughput streaming NDJSON ingestion pipeline.

Batches and accumulates chunk events in memory, committing to SQLite atomically in a single transaction.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any, Callable

import httpx

from controller.db import Database

_START_BASE = dt.datetime(2027, 1, 4, 0, 0)


def parse_iso_ts_to_minute(ts_str: str) -> int:
    """Parses simulator ISO timestamp to integer simulated minutes from D0 00:00."""
    if not ts_str:
        return 0
    clean = ts_str.split("+")[0]
    try:
        t = dt.datetime.fromisoformat(clean)
        diff = t - _START_BASE
        return int(diff.total_seconds() // 60)
    except Exception:
        return 0


class IngestionEngine:
    def __init__(self, client: httpx.Client, db: Database):
        self.client = client
        self.db = db

    def sync_history(self, on_chunk: Callable[[int, int], None] | None = None) -> int:
        """Fetches and ingests all pre-D0 history chunks if not already ingested.

        Returns total history events processed.
        """
        cursor = self.db.get_cursor()
        last_chunk = cursor["last_chunk"] if cursor else -1

        resp = self.client.get("/events/chunks", params={"kind": "history", "after": last_chunk})
        if resp.status_code != 200:
            return 0

        chunks = resp.json()
        total_events = 0
        for ch in chunks:
            chunk_num = ch["chunk"]
            events_count = self.ingest_chunk(chunk_num)
            total_events += events_count
            if on_chunk:
                on_chunk(chunk_num, events_count)
        return total_events

    def ingest_new_chunks(self, new_chunk_metas: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        """Ingests a list of new chunk metas returned by /sim/advance.

        Returns (total_events_ingested, list_of_lifecycle_events) where lifecycle events
        are config_changed or approval_decided.
        """
        cursor = self.db.get_cursor()
        last_chunk = cursor["last_chunk"] if cursor else -1

        total_events = 0
        lifecycle_events = []
        for ch in new_chunk_metas:
            chunk_num = ch["chunk"]
            if chunk_num <= last_chunk:
                continue
            ev_count, l_events = self._process_chunk(chunk_num)
            total_events += ev_count
            lifecycle_events.extend(l_events)
            last_chunk = chunk_num

        return total_events, lifecycle_events

    def ingest_chunk(self, chunk_num: int) -> int:
        ev_count, _ = self._process_chunk(chunk_num)
        return ev_count

    def _process_chunk(self, chunk_num: int) -> tuple[int, list[dict[str, Any]]]:
        resp = self.client.get(f"/events/chunks/{chunk_num}")
        if resp.status_code != 200:
            return 0, []

        agg_deltas: dict[tuple[str, str, int, str, str, str], dict[str, int]] = defaultdict(
            lambda: {"dials": 0, "connected": 0, "engaged": 0, "hallucinated": 0, "duration_s": 0, "turns": 0}
        )
        did_deltas: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"dials": 0, "connected": 0}
        )
        did_daily_deltas: dict[tuple[str, str, int], dict[str, int]] = defaultdict(
            lambda: {"dials": 0, "connected": 0}
        )
        var_block_deltas: dict[tuple[str, str, int, int, str, str], dict[str, int]] = defaultdict(
            lambda: {"dials": 0, "connected": 0, "engaged": 0}
        )
        new_leads: list[tuple[str, str, str, str, str]] = []
        lifecycle_events: list[dict[str, Any]] = []

        last_event_id = 0
        sim_minute = 0
        sim_ts = ""
        event_count = 0

        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            event_count += 1
            last_event_id = ev.get("event_id", last_event_id)
            ev_ts = ev.get("ts", "")
            if ev_ts:
                sim_ts = ev_ts
                sim_minute = parse_iso_ts_to_minute(ev_ts)

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

                key = (wf, cohort, ver, w_msg, voice, did)
                d = agg_deltas[key]
                d["dials"] += 1
                d["connected"] += conn
                d["engaged"] += eng
                d["hallucinated"] += halluc
                d["duration_s"] += dur
                d["turns"] += turns

                did_key = (wf, did)
                dd = did_deltas[did_key]
                dd["dials"] += 1
                dd["connected"] += conn

                call_day = parse_iso_ts_to_minute(ev_ts) // 1440
                daily_key = (wf, did, call_day)
                ddd = did_daily_deltas[daily_key]
                ddd["dials"] += 1
                ddd["connected"] += conn

                call_block = parse_iso_ts_to_minute(ev_ts) // 720
                block_key = (wf, cohort, ver, call_block, w_msg, voice)
                vbd = var_block_deltas[block_key]
                vbd["dials"] += 1
                vbd["connected"] += conn
                vbd["engaged"] += eng

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

            elif ev_type in ("config_changed", "approval_decided"):
                lifecycle_events.append(ev)

        # Commit chunk atomically to SQLite
        self.db.commit_chunk(
            chunk_num=chunk_num,
            last_event_id=last_event_id,
            sim_minute=sim_minute,
            sim_ts=sim_ts,
            aggregates_deltas=agg_deltas,
            did_deltas=did_deltas,
            new_leads=new_leads,
            did_daily_deltas=did_daily_deltas,
            var_block_deltas=var_block_deltas,
        )

        return event_count, lifecycle_events
