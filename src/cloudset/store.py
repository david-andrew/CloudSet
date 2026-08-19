from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  threshold INTEGER NOT NULL DEFAULT 70,
  notification_times TEXT NOT NULL DEFAULT '["morning","final_90"]',
  custom_minutes INTEGER,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(email, latitude, longitude)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  cells INTEGER NOT NULL DEFAULT 0,
  duration_ms REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notifications (
  subscription_id INTEGER NOT NULL,
  forecast_date TEXT NOT NULL,
  score REAL NOT NULL,
  result TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY(subscription_id, forecast_date),
  FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);
CREATE TABLE IF NOT EXISTS notification_events (
  subscription_id INTEGER NOT NULL,
  forecast_date TEXT NOT NULL,
  event_key TEXT NOT NULL,
  score REAL NOT NULL,
  result TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY(subscription_id, forecast_date, event_key),
  FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
            if "notification_times" not in columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN notification_times TEXT NOT NULL DEFAULT '[\"morning\",\"final_90\"]'")
            if "custom_minutes" not in columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN custom_minutes INTEGER")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_json(self, key: str, fallback):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return fallback if row is None else json.loads(row["value"])

    def set_json(self, key: str, value) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, separators=(",", ":")), now),
            )

    def subscribe(
        self,
        email: str,
        latitude: float,
        longitude: float,
        label: str,
        threshold: int,
        notification_times: list[str] | None = None,
        custom_minutes: int | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        times = json.dumps(notification_times or ["morning", "final_90"], separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions(email, latitude, longitude, label, threshold, notification_times, custom_minutes, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email, latitude, longitude) DO UPDATE SET "
                "label=excluded.label, threshold=excluded.threshold, notification_times=excluded.notification_times, "
                "custom_minutes=excluded.custom_minutes, active=1",
                (email.lower(), latitude, longitude, label[:100], threshold, times, custom_minutes, now),
            )
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE email=? AND latitude=? AND longitude=?",
                (email.lower(), latitude, longitude),
            ).fetchone()
            return int(row["id"])

    def subscriptions(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM subscriptions WHERE active=1 ORDER BY created_at DESC").fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["notification_times"] = json.loads(record["notification_times"])
        return records

    def add_run(self, started_at: str, finished_at: str, mode: str, cells: int, duration_ms: float, message: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at,finished_at,status,mode,cells,duration_ms,message) VALUES(?,?,?,?,?,?,?)",
                (started_at, finished_at, "complete", mode, cells, duration_ms, message),
            )
            return int(cur.lastrowid)

    def last_run(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def notification_exists(self, subscription_id: int, forecast_date: str, event_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_events WHERE subscription_id=? AND forecast_date=? AND event_key=?",
                (subscription_id, forecast_date, event_key),
            ).fetchone()
        return row is not None

    def record_notification(self, subscription_id: int, forecast_date: str, event_key: str, score: float, result: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO notification_events VALUES(?,?,?,?,?,?)",
                (subscription_id, forecast_date, event_key, score, result, datetime.now(timezone.utc).isoformat()),
            )
