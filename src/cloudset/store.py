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
  status TEXT NOT NULL DEFAULT 'pending',
  confirmed_at TEXT,
  unsubscribed_at TEXT,
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
CREATE TABLE IF NOT EXISTS outcomes (
  subscription_id INTEGER NOT NULL,
  forecast_date TEXT NOT NULL,
  rating INTEGER NOT NULL,
  predicted_score REAL,
  comment TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(subscription_id, forecast_date),
  FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);
"""

STATUSES = {"pending", "active", "unsubscribed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
            if "notification_times" not in columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN notification_times TEXT NOT NULL DEFAULT '[\"morning\",\"final_90\"]'")
            if "custom_minutes" not in columns:
                conn.execute("ALTER TABLE subscriptions ADD COLUMN custom_minutes INTEGER")
            if "status" not in columns:
                # Subscriptions created before double opt-in existed are kept live.
                conn.execute("ALTER TABLE subscriptions ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
                conn.execute("ALTER TABLE subscriptions ADD COLUMN confirmed_at TEXT")
                conn.execute("ALTER TABLE subscriptions ADD COLUMN unsubscribed_at TEXT")
                conn.execute("UPDATE subscriptions SET status='active', confirmed_at=created_at WHERE active=1")
                conn.execute("UPDATE subscriptions SET status='unsubscribed' WHERE active=0")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- settings -----------------------------------------------------------

    def get_json(self, key: str, fallback):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return fallback if row is None else json.loads(row["value"])

    def set_json(self, key: str, value) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, separators=(",", ":")), _now()),
            )

    # --- subscriptions ------------------------------------------------------

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        record = dict(row)
        record["notification_times"] = json.loads(record["notification_times"])
        return record

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
        """Create or update a watch. Returns the subscription id.

        A brand-new watch starts ``pending`` until the confirmation link is
        opened. Re-submitting an existing watch keeps its confirmed status so
        people can adjust the threshold without re-confirming; an unsubscribed
        watch goes back to ``pending`` and must be confirmed again.
        """
        email = email.strip().lower()
        times = json.dumps(notification_times or ["morning", "final_90"], separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions(email, latitude, longitude, label, threshold, notification_times, custom_minutes, created_at, status) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT(email, latitude, longitude) DO UPDATE SET "
                "label=excluded.label, threshold=excluded.threshold, notification_times=excluded.notification_times, "
                "custom_minutes=excluded.custom_minutes, active=1, "
                "status=CASE WHEN subscriptions.status='active' THEN 'active' ELSE 'pending' END, "
                "unsubscribed_at=NULL",
                (email, latitude, longitude, label[:100], threshold, times, custom_minutes, _now()),
            )
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE email=? AND latitude=? AND longitude=?",
                (email, latitude, longitude),
            ).fetchone()
            return int(row["id"])

    def subscription(self, subscription_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
        return self._record(row)

    def subscriptions(self, status: str | None = "active") -> list[dict]:
        """Active (deliverable) watches by default; pass ``None`` for every row."""
        with self.connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM subscriptions WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        return [self._record(row) for row in rows]

    def subscriptions_for_email(self, email: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE email=? AND status != 'unsubscribed' ORDER BY created_at DESC",
                (email.strip().lower(),),
            ).fetchall()
        return [self._record(row) for row in rows]

    def count_for_email(self, email: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE email=? AND status != 'unsubscribed'",
                (email.strip().lower(),),
            ).fetchone()
        return int(row["n"])

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        with self.connect() as conn:
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM subscriptions GROUP BY status"):
                counts[row["status"]] = int(row["n"])
        return counts

    def confirm(self, subscription_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE subscriptions SET status='active', active=1, confirmed_at=COALESCE(confirmed_at, ?), unsubscribed_at=NULL "
                "WHERE id=? AND status != 'unsubscribed'",
                (_now(), subscription_id),
            )
            return cur.rowcount > 0

    def unsubscribe(self, subscription_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE subscriptions SET status='unsubscribed', active=0, unsubscribed_at=? WHERE id=? AND status != 'unsubscribed'",
                (_now(), subscription_id),
            )
            return cur.rowcount > 0

    def unsubscribe_email(self, email: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE subscriptions SET status='unsubscribed', active=0, unsubscribed_at=? WHERE email=? AND status != 'unsubscribed'",
                (_now(), email.strip().lower()),
            )
            return cur.rowcount

    def update_subscription(
        self,
        subscription_id: int,
        *,
        latitude: float,
        longitude: float,
        label: str,
        threshold: int,
        notification_times: list[str],
        custom_minutes: int | None,
    ) -> bool:
        times = json.dumps(notification_times, separators=(",", ":"))
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "UPDATE subscriptions SET latitude=?, longitude=?, label=?, threshold=?, notification_times=?, custom_minutes=? "
                    "WHERE id=? AND status != 'unsubscribed'",
                    (latitude, longitude, label[:100], threshold, times, custom_minutes, subscription_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("You already have a watch at exactly that location") from exc
            return cur.rowcount > 0

    def delete_subscription(self, subscription_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("DELETE FROM notification_events WHERE subscription_id=?", (subscription_id,))
            cur = conn.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
            return cur.rowcount > 0

    # --- runs ---------------------------------------------------------------

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

    # --- notifications ------------------------------------------------------

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
                (subscription_id, forecast_date, event_key, score, result, _now()),
            )

    def alerts_for_date(self, subscription_id: int, forecast_date: str) -> list[dict]:
        """Alerts already sent for one sunset, excluding downgrade notices and confirmations."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_events WHERE subscription_id=? AND forecast_date=? "
                "AND event_key NOT IN ('downgrade', 'confirmation') ORDER BY sent_at",
                (subscription_id, forecast_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_notifications(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT n.*, s.email, s.label FROM notification_events n "
                "LEFT JOIN subscriptions s ON s.id = n.subscription_id "
                "ORDER BY n.sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- outcomes -----------------------------------------------------------

    def record_outcome(self, subscription_id: int, forecast_date: str, rating: int, predicted_score: float | None, comment: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO outcomes(subscription_id, forecast_date, rating, predicted_score, comment, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(subscription_id, forecast_date) DO UPDATE SET rating=excluded.rating, comment=excluded.comment, created_at=excluded.created_at",
                (subscription_id, forecast_date, rating, predicted_score, comment[:500], _now()),
            )

    def outcomes(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT o.*, s.email, s.label, s.latitude, s.longitude FROM outcomes o "
                "LEFT JOIN subscriptions s ON s.id = o.subscription_id ORDER BY o.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def outcome_summary(self) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, AVG(rating) AS avg_rating, AVG(predicted_score) AS avg_score FROM outcomes").fetchone()
            by_rating = {int(r["rating"]): int(r["n"]) for r in conn.execute("SELECT rating, COUNT(*) AS n FROM outcomes GROUP BY rating")}
        return {"count": int(row["n"]), "average_rating": row["avg_rating"], "average_predicted_score": row["avg_score"], "by_rating": by_rating}

    def sends_since(self, since_iso: str) -> dict:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT result, COUNT(*) AS n FROM notification_events WHERE sent_at >= ? GROUP BY result", (since_iso,)
            ).fetchall()
        return {row["result"]: int(row["n"]) for row in rows}
