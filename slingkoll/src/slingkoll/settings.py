"""What the test controls and watches, stored as JSON in the add-on's data folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FLOOR_PHASE_HOURS = {"slow": 3.0, "fast": 2.0}


def data_dir() -> Path:
    path = Path(os.environ.get("SLINGKOLL_DATA", "/data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(slots=True)
class Settings:
    thermostats: list[dict[str, str]] = field(default_factory=list)
    extra_sensors: list[dict[str, str]] = field(default_factory=list)
    hp_setpoint_entity: str | None = None
    supply_entity: str | None = None
    outdoor_entity: str | None = None
    hemopt_switch_entity: str | None = None
    # Comfort limits during the test.
    min_temp: float = 18.0
    max_temp: float = 25.0
    # slow: concrete slab somewhere in the house; fast: only timber floors.
    floor: str = "slow"
    open_setpoint: float = 28.0
    closed_setpoint: float = 10.0
    hp_boost_max: float = 3.0
    max_blocks: int = 2
    configured: bool = False

    @property
    def phase_hours(self) -> float:
        return FLOOR_PHASE_HOURS.get(self.floor, 3.0)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["phase_hours"] = self.phase_hours
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Settings:
        known = {name for name in cls.__dataclass_fields__}
        clean = {key: value for key, value in raw.items() if key in known}
        settings = cls(**clean)
        settings.validate()
        return settings

    def validate(self) -> None:
        for group in (self.thermostats, self.extra_sensors):
            for item in group:
                if not str(item.get("entity_id", "")).strip():
                    raise ValueError("En vald givare saknar entitet.")
                item["name"] = str(item.get("name") or item["entity_id"]).strip()
        ids = [item["entity_id"] for item in self.thermostats + self.extra_sensors]
        if len(ids) != len(set(ids)):
            raise ValueError("Samma entitet är vald två gånger.")
        for name in ("hp_setpoint_entity", "supply_entity", "outdoor_entity", "hemopt_switch_entity"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                setattr(self, name, None)
        self.min_temp = float(self.min_temp)
        self.max_temp = float(self.max_temp)
        if not 10 <= self.min_temp < self.max_temp <= 30:
            raise ValueError("Komfortgränserna måste ligga mellan 10 och 30 °C, lägst först.")
        if self.max_temp - self.min_temp < 3:
            raise ValueError("Lämna minst 3 °C mellan lägsta och högsta temperatur.")
        if self.floor not in FLOOR_PHASE_HOURS:
            self.floor = "slow"
        self.max_blocks = max(1, min(5, int(self.max_blocks)))


def load_settings(path: Path | None = None) -> Settings:
    path = path or data_dir() / "settings.json"
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(path.read_text()))
    except (ValueError, TypeError, json.JSONDecodeError):
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or data_dir() / "settings.json"
    data = asdict(settings)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)
