"""MQTT discovery must use default_entity_id (HA 2026.4 drops object_id)."""

from __future__ import annotations

import json

from hemopt.config import Config, MqttConfig, RoomConfig
from hemopt.mqtt_bridge import MqttBridge


class _FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.messages.append((topic, payload))


def test_discovery_uses_default_entity_id_not_object_id():
    bridge = MqttBridge(
        Config(
            mqtt=MqttConfig(enabled=True, host="mqtt", node_id="hemopt"),
            rooms=[
                RoomConfig(
                    key="tvattstuga",
                    name="Tvattstuga",
                    temperature_entity="sensor.t",
                )
            ],
        )
    )
    fake = _FakeClient()
    bridge._client = fake  # noqa: SLF001
    bridge._connected = True
    bridge.publish_discovery()

    assert fake.messages
    for topic, raw in fake.messages:
        payload = json.loads(raw)
        assert "object_id" not in payload, topic
        assert "default_entity_id" in payload, topic
        assert payload["default_entity_id"].startswith(
            ("sensor.", "binary_sensor.", "switch.", "number.")
        )
        assert payload["unique_id"].startswith("hemopt_")

    setpoint = next(
        json.loads(raw)
        for topic, raw in fake.messages
        if topic.endswith("/setpoint_tvattstuga/config")
    )
    assert setpoint["name"].startswith("Plan (ej styrt)")
    assert setpoint["entity_category"] == "diagnostic"
    assert setpoint["default_entity_id"] == "sensor.hemopt_setpoint_tvattstuga"
