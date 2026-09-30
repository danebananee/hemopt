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

from .fuse import PhaseHour
from .hotwater import UsageProfile
from .shadow import LedgerStep
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

-- Highest and average current per phase and hour, for the fuse check.
CREATE TABLE IF NOT EXISTS phase_hours (
    ts     INTEGER NOT NULL,
    phase  INTEGER NOT NULL,
    max_a  REAL NOT NULL,
    mean_a REAL NOT NULL,
    PRIMARY KEY (ts, phase)
) WITHOUT ROWID;

-- One row per planning step: the shadow ledger behind the savings estimate.
CREATE TABLE IF NOT EXISTS shadow_steps (
    ts                INTEGER PRIMARY KEY,
    spot              REAL,
    spot_hour         REAL,
    spot_day          REAL,
    adder             REAL,
    reference_kwh     REAL,
    optimised_kwh     REAL,
    house_kwh         REAL,
    reference_cold_dh REAL NOT NULL DEFAULT 0,
    optimised_cold_dh REAL NOT NULL DEFAULT 0,
    control_enabled   INTEGER NOT NULL DEFAULT 0
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

    def samples_bucketed(
        self, entity_id: str, since: datetime, bucket_seconds: int = 300
    ) -> list[tuple[datetime, float]]:
        """Samples averaged per time bucket, for training on long spans.

        hemopt logs every minute; weeks of that per entity is more than model
        fitting needs and more than a Raspberry Pi wants to hold in memory.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT (ts / ?) * ? AS bucket, AVG(value) AS value FROM samples"
                " WHERE entity_id = ? AND ts >= ? GROUP BY bucket ORDER BY bucket",
                (bucket_seconds, bucket_seconds, entity_id, _to_ts(since)),
            )
            return [(_from_ts(row["bucket"]), row["value"]) for row in cursor.fetchall()]

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

    def all_hourly_power(self, since: datetime | None = None) -> dict[datetime, float]:
        """Every recorded hourly mean, across months.

        The advice engine wants as long a span as exists rather than the
        current month, because a fuse or contract decision made on three weeks
        of autumn data is a decision made on the wrong season.
        """
        query = "SELECT hour_start, mean_kw FROM hourly_power"
        params: tuple = ()
        if since is not None:
            query += " WHERE hour_start >= ?"
            params = (since.isoformat(),)

        with self._cursor() as cursor:
            cursor.execute(query + " ORDER BY hour_start", params)
            return {
                datetime.fromisoformat(row["hour_start"]): row["mean_kw"]
                for row in cursor.fetchall()
            }

    def fill_hourly_power(self, rows: Iterable[tuple[datetime, float]]) -> int:
        """Store hourly means where nothing trustworthy is stored yet.

        Hours already holding a real measurement are kept. A stored value
        below 20 W counts as missing: no heated house draws that little, and
        versions before 0.2.0 wrote 0 kW for every finished hour.
        """
        written = 0
        with self._cursor() as cursor:
            for hour_start, mean_kw in rows:
                cursor.execute(
                    "SELECT mean_kw FROM hourly_power WHERE hour_start = ?",
                    (hour_start.isoformat(),),
                )
                existing = cursor.fetchone()
                if existing is not None and existing["mean_kw"] >= 0.02:
                    continue
                cursor.execute(
                    "INSERT OR REPLACE INTO hourly_power (hour_start, mean_kw, month)"
                    " VALUES (?, ?, ?)",
                    (hour_start.isoformat(), float(mean_kw), hour_start.strftime("%Y-%m")),
                )
                written += 1
        return written

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
        """Best guess at where `month`'s Nth highest peak will land.

        The same calendar month in an earlier year is the closest analogue
        because the tariff window is seasonal; otherwise the most recent
        completed month is used. The month being asked about is always
        excluded, since its own running threshold is exactly the number this
        estimate exists to replace.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT threshold_kw FROM peak_months WHERE month LIKE ? AND month <> ?"
                " ORDER BY month DESC LIMIT 1",
                (f"%-{month.split('-')[1]}", month),
            )
            row = cursor.fetchone()
            if row:
                return float(row["threshold_kw"])
            cursor.execute(
                "SELECT threshold_kw FROM peak_months WHERE month <> ? ORDER BY month DESC LIMIT 1",
                (month,),
            )
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
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload.setdefault("k_stove_per_hour", 0.0)
        return ThermalModel(**payload)

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

    # --- phase currents -------------------------------------------------
    def record_phase_hours(self, rows: Iterable[PhaseHour]) -> int:
        payload = [
            (_to_ts(row.hour_start), row.phase, float(row.max_a), float(row.mean_a)) for row in rows
        ]
        with self._cursor() as cursor:
            cursor.executemany(
                "INSERT OR REPLACE INTO phase_hours (ts, phase, max_a, mean_a) VALUES (?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    def phase_hours(self, since: datetime, tz: timezone | None = None) -> list[PhaseHour]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT ts, phase, max_a, mean_a FROM phase_hours WHERE ts >= ? ORDER BY ts",
                (_to_ts(since),),
            )
            rows = cursor.fetchall()
        return [
            PhaseHour(_from_ts(row["ts"], tz), row["phase"], row["max_a"], row["mean_a"])
            for row in rows
        ]

    # --- shadow ledger --------------------------------------------------
    def has_shadow_step(self, start: datetime) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM shadow_steps WHERE ts = ? AND reference_kwh IS NOT NULL",
                (_to_ts(start),),
            )
            return cursor.fetchone() is not None

    def record_shadow_step(self, step: LedgerStep) -> None:
        """Book a step's prices and twin energies; keeps any measured house energy."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shadow_steps (
                    ts, spot, spot_hour, spot_day, adder, reference_kwh, optimised_kwh,
                    reference_cold_dh, optimised_cold_dh, control_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    spot = excluded.spot,
                    spot_hour = excluded.spot_hour,
                    spot_day = excluded.spot_day,
                    adder = excluded.adder,
                    reference_kwh = excluded.reference_kwh,
                    optimised_kwh = excluded.optimised_kwh,
                    reference_cold_dh = excluded.reference_cold_dh,
                    optimised_cold_dh = excluded.optimised_cold_dh,
                    control_enabled = excluded.control_enabled
                """,
                (
                    _to_ts(step.start),
                    step.spot,
                    step.spot_hour,
                    step.spot_day,
                    step.adder,
                    step.reference_kwh,
                    step.optimised_kwh,
                    step.reference_cold_dh,
                    step.optimised_cold_dh,
                    int(step.control_enabled),
                ),
            )

    def record_house_energy(self, start: datetime, kwh: float) -> None:
        """Measured whole-house energy for one step, booked when the step ends."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shadow_steps (ts, house_kwh) VALUES (?, ?)
                ON CONFLICT(ts) DO UPDATE SET house_kwh = excluded.house_kwh
                """,
                (_to_ts(start), float(kwh)),
            )

    def shadow_steps(self, since: datetime, tz: timezone | None = None) -> list[LedgerStep]:
        """Fully priced ledger steps since `since`, oldest first."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM shadow_steps WHERE ts >= ? AND spot IS NOT NULL ORDER BY ts",
                (_to_ts(since),),
            )
            rows = cursor.fetchall()
        return [
            LedgerStep(
                start=_from_ts(row["ts"], tz),
                spot=row["spot"],
                spot_hour=row["spot_hour"],
                spot_day=row["spot_day"],
                adder=row["adder"],
                reference_kwh=row["reference_kwh"],
                optimised_kwh=row["optimised_kwh"],
                house_kwh=row["house_kwh"],
                reference_cold_dh=row["reference_cold_dh"],
                optimised_cold_dh=row["optimised_cold_dh"],
                control_enabled=bool(row["control_enabled"]),
            )
            for row in rows
        ]

    def housekeeping(self, retain_days: int = 120) -> None:
        self.prune_samples(datetime.now(UTC) - timedelta(days=retain_days))
        with self._cursor() as cursor:
            # The ledger is a few kB a day; a bit over a year covers a full
            # heating season plus the one before for comparison.
            cursor.execute(
                "DELETE FROM shadow_steps WHERE ts < ?",
                (_to_ts(datetime.now(UTC) - timedelta(days=400)),),
            )
        with self._cursor() as cursor:
            cursor.execute("PRAGMA optimize")
