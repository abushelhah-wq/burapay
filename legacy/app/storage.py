"""
Result storage — SQLite, one row per attempt plus one row per HTTP call.

Kept deliberately small: this records what happened so the results page can
aggregate it, and nothing else. No card data is ever written — adapters only pass
response excerpts to CallRecord, never request bodies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence

from app.timing import CallRecord, summarize

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    gateway       TEXT NOT NULL,
    flow          TEXT NOT NULL,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL,
    reference     TEXT NOT NULL,
    status        TEXT NOT NULL,
    detail        TEXT,
    gateway_ref   TEXT,
    call_count    INTEGER NOT NULL DEFAULT 0,
    total_ms      REAL NOT NULL DEFAULT 0,
    location      TEXT,
    context       TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id    TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    leg           TEXT NOT NULL,
    step          INTEGER NOT NULL,
    name          TEXT NOT NULL,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    timed         INTEGER NOT NULL,
    elapsed_ms    REAL NOT NULL,
    status_code   INTEGER,
    ok            INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    snippet       TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_attempt ON calls(attempt_id);
CREATE INDEX IF NOT EXISTS idx_attempts_gw_flow ON attempts(gateway, flow);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writing ---------------------------------------------------------- #

    def start_attempt(self, *, gateway: str, flow: str, amount: float, currency: str,
                      reference: str, location: Optional[str] = None,
                      context: Optional[Dict[str, Any]] = None) -> str:
        attempt_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO attempts (id, created_at, gateway, flow, amount, currency,"
                " reference, status, location, context) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, datetime.now(timezone.utc).isoformat(), gateway, flow,
                 amount, currency, reference, "pending", location,
                 json.dumps(context or {})))
        return attempt_id

    def record_calls(self, attempt_id: str, leg: str,
                     calls: Sequence[CallRecord]) -> None:
        if not calls:
            return
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO calls (attempt_id, leg, step, name, method, url, timed,"
                " elapsed_ms, status_code, ok, error, snippet)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(attempt_id, leg, c.step, c.name, c.method, c.url, int(c.timed),
                  c.elapsed_ms, c.status_code, int(c.ok), c.error, c.snippet)
                 for c in calls])
            # Recompute totals from the calls table so the summary always matches the
            # detail, including across the two legs of a hosted checkout.
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(elapsed_ms), 0) AS total"
                " FROM calls WHERE attempt_id = ? AND timed = 1", (attempt_id,)).fetchone()
            conn.execute("UPDATE attempts SET call_count = ?, total_ms = ? WHERE id = ?",
                         (row["n"], round(row["total"], 3), attempt_id))

    def finish_attempt(self, attempt_id: str, *, status: str, detail: str = "",
                       gateway_ref: Optional[str] = None,
                       context: Optional[Dict[str, Any]] = None) -> None:
        with self.connect() as conn:
            if context is None:
                conn.execute("UPDATE attempts SET status=?, detail=?, gateway_ref=?"
                             " WHERE id=?", (status, detail[:2000], gateway_ref, attempt_id))
            else:
                conn.execute("UPDATE attempts SET status=?, detail=?, gateway_ref=?,"
                             " context=? WHERE id=?",
                             (status, detail[:2000], gateway_ref,
                              json.dumps(context), attempt_id))

    def update_context(self, attempt_id: str, context: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET context=? WHERE id=?",
                         (json.dumps(context), attempt_id))

    # -- reading ---------------------------------------------------------- #

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                return None
            attempt = dict(row)
            attempt["context"] = json.loads(attempt.get("context") or "{}")
            attempt["calls"] = [dict(r) for r in conn.execute(
                "SELECT * FROM calls WHERE attempt_id=? ORDER BY id", (attempt_id,))]
            return attempt

    def recent_attempts(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM attempts ORDER BY created_at DESC LIMIT ?", (limit,))]

    def summary(self) -> List[Dict[str, Any]]:
        """Per gateway+flow aggregate over successful attempts only.

        Failed attempts are excluded from latency: a run that died on call 2 of 4 has a
        total that is not comparable with one that completed, and averaging them
        together would quietly flatter whichever gateway fails fastest.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT gateway, flow, id, call_count, total_ms FROM attempts"
                " WHERE status = 'succeeded' ORDER BY gateway, flow").fetchall()

        grouped: Dict[tuple, List[float]] = {}
        counts: Dict[tuple, List[int]] = {}
        for row in rows:
            key = (row["gateway"], row["flow"])
            grouped.setdefault(key, []).append(row["total_ms"])
            counts.setdefault(key, []).append(row["call_count"])

        out = []
        for (gateway, flow), values in sorted(grouped.items()):
            call_counts = sorted(set(counts[(gateway, flow)]))
            entry = {"gateway": gateway, "flow": flow,
                     "call_count": call_counts[0] if len(call_counts) == 1 else None,
                     "call_count_range": call_counts}
            entry.update(summarize(values))
            out.append(entry)
        return out

    def per_call_summary(self, gateway: str, flow: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT c.step, c.name, c.method, c.elapsed_ms FROM calls c"
                " JOIN attempts a ON a.id = c.attempt_id"
                " WHERE a.gateway=? AND a.flow=? AND a.status='succeeded' AND c.timed=1"
                " ORDER BY c.step", (gateway, flow)).fetchall()
        grouped: Dict[tuple, List[float]] = {}
        for row in rows:
            grouped.setdefault((row["step"], row["name"], row["method"]), []).append(
                row["elapsed_ms"])
        out = []
        for (step, name, method), values in sorted(grouped.items()):
            entry = {"step": step, "name": name, "method": method}
            entry.update(summarize(values))
            out.append(entry)
        return out
