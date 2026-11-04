"""SQLite persistence for request records and the small bits of state rules need."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    app TEXT,
    api TEXT,
    method TEXT,
    path TEXT,
    model TEXT,
    status INTEGER,
    latency_ms INTEGER,
    stream INTEGER,
    est_input_tokens INTEGER,
    context_window INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    finish_reason TEXT,
    error_code TEXT,
    health_score INTEGER,
    max_severity TEXT,
    findings_json TEXT,
    breakdown_json TEXT,
    judge_json TEXT,
    request_body TEXT,
    response_body TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_app ON requests(app);

CREATE TABLE IF NOT EXISTS prompt_versions (
    key TEXT NOT NULL,
    hash TEXT NOT NULL,
    first_seen REAL,
    last_seen REAL,
    count INTEGER DEFAULT 0,
    sample TEXT,
    PRIMARY KEY (key, hash)
);
CREATE TABLE IF NOT EXISTS prompt_last (
    key TEXT PRIMARY KEY,
    hash TEXT,
    ts REAL
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    instructions TEXT,
    hash TEXT,
    model TEXT,
    tools_count INTEGER,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS prefixes (
    key TEXT NOT NULL,
    hash TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    last_seen REAL,
    PRIMARY KEY (key, hash)
);
"""


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- requests
    def save_request(self, rec: dict[str, Any]) -> None:
        cols = ", ".join(rec)
        qs = ", ".join("?" for _ in rec)
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO requests ({cols}) VALUES ({qs})", list(rec.values()))
            self._conn.commit()

    def update_judge(self, req_id: str, judge: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE requests SET judge_json=? WHERE id=?", (json.dumps(judge), req_id))
            self._conn.commit()

    def list_requests(self, limit: int = 100, app: str | None = None, min_severity: str | None = None,
                      since: float | None = None) -> list[dict]:
        q = ("SELECT id, ts, app, api, method, path, model, status, latency_ms, stream, est_input_tokens, "
             "context_window, prompt_tokens, completion_tokens, cached_tokens, finish_reason, error_code, "
             "health_score, max_severity, findings_json, judge_json IS NOT NULL AS has_judge FROM requests WHERE 1=1")
        args: list[Any] = []
        if app:
            q += " AND app = ?"
            args.append(app)
        if since:
            q += " AND ts >= ?"
            args.append(since)
        if min_severity == "critical":
            q += " AND max_severity = 'critical'"
        elif min_severity == "warning":
            q += " AND max_severity IN ('warning','critical')"
        elif min_severity == "info":
            q += " AND max_severity IS NOT NULL"
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["findings"] = json.loads(d.pop("findings_json") or "[]")
            out.append(d)
        return out

    def get_request(self, req_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["findings"] = json.loads(d.pop("findings_json") or "[]")
        d["breakdown"] = json.loads(d.pop("breakdown_json") or "{}")
        d["judge"] = json.loads(d.pop("judge_json") or "null")
        return d

    def rows_since(self, since: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT app, api, model, status, latency_ms, est_input_tokens, prompt_tokens, completion_tokens, "
                "cached_tokens, health_score, max_severity, findings_json FROM requests WHERE ts >= ?", (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- prompt drift
    def record_prompt(self, key: str, h: str, sample: str) -> dict:
        """Record a system-prompt hash for `key`. Returns info about the previous state."""
        now = time.time()
        with self._lock:
            last = self._conn.execute("SELECT hash FROM prompt_last WHERE key=?", (key,)).fetchone()
            prev_hash = last["hash"] if last else None
            prev_sample = None
            if prev_hash and prev_hash != h:
                row = self._conn.execute("SELECT sample FROM prompt_versions WHERE key=? AND hash=?",
                                         (key, prev_hash)).fetchone()
                prev_sample = row["sample"] if row else None
            self._conn.execute(
                "INSERT INTO prompt_versions (key, hash, first_seen, last_seen, count, sample) VALUES (?,?,?,?,1,?) "
                "ON CONFLICT(key, hash) DO UPDATE SET last_seen=excluded.last_seen, count=count+1",
                (key, h, now, now, sample),
            )
            self._conn.execute("INSERT OR REPLACE INTO prompt_last (key, hash, ts) VALUES (?,?,?)", (key, h, now))
            versions = self._conn.execute(
                "SELECT COUNT(*) AS n, SUM(count) AS total FROM prompt_versions WHERE key=?", (key,)).fetchone()
            self._conn.commit()
        return {"prev_hash": prev_hash, "prev_sample": prev_sample,
                "distinct_versions": versions["n"], "total_requests": versions["total"]}

    # -------------------------------------------------------------- agents
    def get_agent(self, agent_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        return dict(r) if r else None

    def upsert_agent(self, agent_id: str, instructions: str | None, h: str | None, model: str | None,
                     tools_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents (agent_id, instructions, hash, model, tools_count, updated_at) "
                "VALUES (?,?,?,?,?,?)", (agent_id, instructions, h, model, tools_count, time.time()))
            self._conn.commit()

    # ------------------------------------------------------------ prefixes
    def record_prefix(self, key: str, h: str) -> int:
        """Returns how many times this prompt prefix was seen before (for cache analysis)."""
        with self._lock:
            row = self._conn.execute("SELECT count FROM prefixes WHERE key=? AND hash=?", (key, h)).fetchone()
            seen = row["count"] if row else 0
            self._conn.execute(
                "INSERT INTO prefixes (key, hash, count, last_seen) VALUES (?,?,1,?) "
                "ON CONFLICT(key, hash) DO UPDATE SET count=count+1, last_seen=excluded.last_seen",
                (key, h, time.time()))
            self._conn.commit()
        return seen
