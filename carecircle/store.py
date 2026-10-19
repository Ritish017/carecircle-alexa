"""SQLite persistence for events, medication log, reminders, notifications and consent audit."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT, room TEXT, indoor INTEGER,
  event_type TEXT, sub_type TEXT, start_ms INTEGER, end_ms INTEGER, source TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS med_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, medication TEXT, dose_date TEXT, dose_time TEXT,
  taken_at TEXT, logged_by TEXT, note TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, device TEXT, text TEXT, status TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, recipient TEXT, channel TEXT, text TEXT,
  status TEXT, detail TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS consent_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, action TEXT, summary TEXT, digest TEXT,
  outcome TEXT, method TEXT, choices TEXT, note TEXT, reason TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, ts TEXT, kind TEXT, severity TEXT, text TEXT, status TEXT);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # generic helpers ------------------------------------------------------
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def reset(self) -> None:
        with self._lock:
            for t in ("events", "med_log", "reminders", "notifications", "consent_audit", "alerts"):
                self._db.execute(f"DELETE FROM {t}")
            self._db.commit()

    # events ---------------------------------------------------------------
    def upsert_event(self, e: dict[str, Any]) -> bool:
        """Insert an event; returns False if the id was already stored (idempotent ingest)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e["id"], e["device_id"], e["device_name"], e["room"], int(e["indoor"]), e["event_type"],
             e.get("sub_type"), e["start_ms"], e.get("end_ms"), e["source"], json.dumps(e.get("raw", {}))),
        )
        return cur.rowcount == 1

    def events_between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM events WHERE start_ms>=? AND start_ms<=? ORDER BY start_ms", (start_ms, end_ms))

    # medication -------------------------------------------------------------
    def log_dose(self, medication: str, dose_date: str, dose_time: str, taken_at: str, logged_by: str,
                 note: str | None = None, source: str = "user") -> int:
        cur = self._exec(
            "INSERT INTO med_log (medication,dose_date,dose_time,taken_at,logged_by,note,source) VALUES (?,?,?,?,?,?,?)",
            (medication, dose_date, dose_time, taken_at, logged_by, note, source))
        return int(cur.lastrowid)

    def doses_on(self, dose_date: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM med_log WHERE dose_date=? ORDER BY id", (dose_date,))

    # outbound -------------------------------------------------------------
    def add_reminder(self, ts: str, device: str, text: str, status: str, digest: str) -> int:
        return int(self._exec("INSERT INTO reminders (ts,device,text,status,digest) VALUES (?,?,?,?,?)",
                              (ts, device, text, status, digest)).lastrowid)

    def reminders(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM reminders ORDER BY id")

    def add_notification(self, ts: str, recipient: str, channel: str, text: str, status: str, detail: str,
                         digest: str) -> int:
        return int(self._exec(
            "INSERT INTO notifications (ts,recipient,channel,text,status,detail,digest) VALUES (?,?,?,?,?,?,?)",
            (ts, recipient, channel, text, status, detail, digest)).lastrowid)

    def notifications(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notifications ORDER BY id")

    # consent audit (sink for mcp-consent-gate) -------------------------------
    def record(self, entry: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO consent_audit (ts,action,summary,digest,outcome,method,choices,note,reason,details) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry["ts"], entry["action"], entry["summary"], entry["digest"], entry["outcome"], entry["method"],
             json.dumps(entry.get("choices", {})), entry.get("note"), entry.get("reason", ""),
             json.dumps(entry.get("details", {}), default=str)))

    def consent_log(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM consent_audit ORDER BY id")
        for r in rows:
            r["choices"] = json.loads(r["choices"] or "{}")
            r["details"] = json.loads(r["details"] or "{}")
        return rows

    # alerts -----------------------------------------------------------------
    def raise_alert(self, alert_id: str, ts: str, kind: str, severity: str, text: str) -> bool:
        return self._exec("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?)",
                          (alert_id, ts, kind, severity, text, "open")).rowcount == 1

    def close_alert(self, alert_id: str) -> None:
        self._exec("UPDATE alerts SET status='resolved' WHERE id=?", (alert_id,))

    def alerts(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM alerts WHERE status=? ORDER BY ts", (status,))
        return self._all("SELECT * FROM alerts ORDER BY ts")
