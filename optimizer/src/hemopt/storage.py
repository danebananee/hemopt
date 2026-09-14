"""SQLite persistence for samples, learned models and peak history.

The database is deliberately small and self-pruning so it can live on a
Raspberry Pi SD card for years without attention.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .hotwater import UsageProfile
from .thermal import ThermalModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    entity_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (entity_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS hourly_power (
    hour_start TEXT PRIMARY KEY,
    mean_kw    REAL NOT NULL,
    month      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS hourly_power_month ON hourly_power (month);

CREATE TABLE IF NOT EXISTS thermal_models (
    room_key   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hot_water_profile (
    weekday INTEGER NOT NULL,
    hour    INTEGER NOT NULL,
    kwh     REAL NOT NULL,
    PRIMARY KEY (weekday, hour)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peak_months (
    month      TEXT PRIMARY KEY,
    average_kw REAL NOT NULL,
    threshold_kw REAL NOT NULL,
    cost_sek   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    created_at TEXT PRIMARY KEY,
    payload    TEXT NOT NULL
);
"""


def _to_ts(moment: datetime) -> int:
    return int(moment.timestamp())


def _from_ts(value: int, tz: timezone | None = None) -> datetime:
    return datetime.fromtimestamp(value, tz=tz or UTC)


class Store:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                yield cursor
                self._connection.commit()
            finally:
                cursor.close()

    # --- samples --------------------------------------------------------
    def record_samples(self, rows: Iterable[tuple[str, datetime, float]]) -> int:
        payload = [(entity, _to_ts(moment), float(value)) for entity, moment, value in rows]
        if not payload:
            return 0
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO samples (entity_id, ts, value) VALUES (?, ?, ?)",
                payload,
            )
        return len(payload)

    def samples(
        self, entity_id: str, since: datetime, until: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        upper = _to_ts(until) if until else _to_ts(datetime.now(UTC)) + 86400
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT ts, value FROM samples WHERE entity_id = ? AND ts >= ? AND ts <= ?"
                " ORDER BY ts",
                (entity_id, _to_ts(since), upper),
            )
            return [(_from_ts(row["ts"]), row["value"]) for row in cursor.fetchall()]

    def latest_sample(self, entity_id: str) -> tuple[datetime, float] | None:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT ts, value FROM samples WHERE entity_id = ? ORDER BY ts DESC LIMIT 1",
                (entity_id,),
            )
            row = cursor.fetchone()
        return (_from_ts(row["ts"]), row["value"]) if row else None

    def prune_samples(self, older_than: datetime) -> int:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM samples WHERE ts < ?", (_to_ts(older_than),))
            return cursor.rowcount

    # --- hourly power ---------------------------------------------------
    def record_hourly_power(self, hour_start: datetime, mean_kw: float) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO hourly_power (hour_start, mean_kw, month) VALUES (?, ?, ?)",
                (hour_start.isoformat(), float(mean_kw), hour_start.strftime("%Y-%m")),
            )

    def hourly_power(self, month: str) -> dict[datetime, float]:
        with self._cursor() as cursor:
            cursor.execute("SELECT hour_start, mean_kw FROM hourly_power WHERE month = ?", (month,))
            return {
                datetime.fromisoformat(row["hour_start"]): row["mean_kw"]
                for row in cursor.fetchall()
            }

    def record_month_result(
        self, month: str, average_kw: float, threshold_kw: float, cost_sek: float
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO peak_months (month, average_kw, threshold_kw, cost_sek)"
                " VALUES (?, ?, ?, ?)",
                (month, float(average_kw), float(threshold_kw), float(cost_sek)),
            )

    def month_results(self, limit: int = 12) -> list[dict[str, float | str]]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT month, average_kw, threshold_kw, cost_sek FROM peak_months"
                " ORDER BY month DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def expected_peak_kw(self, month: str, fallback: float) -> float:
        """Best guess at where this month's Nth highest peak will land.

        The same month last year is the closest analogue because the tariff
        window is seasonal; otherwise the most recent measured month is used.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT threshold_kw FROM peak_months WHERE month LIKE ? ORDER BY month DESC"
                " LIMIT 1",
                (f"%-{month.split('-')[1]}",),
            )
            row = cursor.fetchone()
            if row:
                return float(row["threshold_kw"])
            cursor.execute("SELECT threshold_kw FROM peak_months ORDER BY month DESC LIMIT 1")
            row = cursor.fetchone()
        return float(row["threshold_kw"]) if row else fallback

    # --- learned models -------------------------------------------------
    def save_thermal_model(self, room_key: str, model: ThermalModel) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO thermal_models (room_key, payload, updated_at)"
                " VALUES (?, ?, ?)",
                (
                    room_key,
                    json.dumps(asdict(model)),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def thermal_model(self, room_key: str) -> ThermalModel | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM thermal_models WHERE room_key = ?", (room_key,))
            row = cursor.fetchone()
        return ThermalModel(**json.loads(row["payload"])) if row else None

    def save_hot_water_profile(self, profile: UsageProfile) -> None:
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO hot_water_profile (weekday, hour, kwh) VALUES (?, ?, ?)",
                [(key[0], key[1], value) for key, value in profile.grid.items()],
            )
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('dhw_observations', ?)",
                (str(profile.observations),),
            )

    def hot_water_profile(self) -> UsageProfile | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT weekday, hour, kwh FROM hot_water_profile")
            rows = cursor.fetchall()
            cursor.execute("SELECT value FROM settings WHERE key = 'dhw_observations'")
            meta = cursor.fetchone()
        if not rows:
            return None
        grid = {(row["weekday"], row["hour"]): row["kwh"] for row in rows}
        return UsageProfile(grid=grid, observations=int(meta["value"]) if meta else 0, fitted=True)

    # --- settings and plans ---------------------------------------------
    def set_setting(self, key: str, value: object) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )

    def setting(self, key: str, default: object = None) -> object:
        with self._cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
        return json.loads(row["value"]) if row else default

    def save_plan(self, created_at: datetime, payload: dict) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT OR REPLACE INTO plans (created_at, payload) VALUES (?, ?)",
                (created_at.isoformat(), json.dumps(payload)),
            )
            cursor.execute(
                "DELETE FROM plans WHERE created_at NOT IN"
                " (SELECT created_at FROM plans ORDER BY created_at DESC LIMIT 200)"
            )

    def latest_plan(self) -> dict | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT payload FROM plans ORDER BY created_at DESC LIMIT 1")
            row = cursor.fetchone()
        return json.loads(row["payload"]) if row else None

    def housekeeping(self, retain_days: int = 120) -> None:
        self.prune_samples(datetime.now(UTC) - timedelta(days=retain_days))
        with self._cursor() as cursor:
            cursor.execute("PRAGMA optimize")
