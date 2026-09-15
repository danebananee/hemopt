from __future__ import annotations

from hemopt.config import Config, RoomConfig
from hemopt.engine import Engine
from hemopt.storage import Store


def test_comfort_band_is_restored_from_store(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.set_setting("comfort_vardagsrum", {"min": 20.0, "max": 21.5})

    config = Config(
        rooms=[
            RoomConfig(
                key="vardagsrum",
                name="Vardagsrum",
                temperature_entity="sensor.t",
                comfort_min=21.0,
                comfort_max=22.5,
            )
        ],
        database_path=str(db),
    )

    engine = Engine(config, store=store)
    room = engine.config.room("vardagsrum")

    assert room.comfort_min == 20.0
    assert room.comfort_max == 21.5
