"""MQTT bridge exposing the optimiser to Home Assistant.

Everything the planner knows becomes a discovered entity, so the existing
dashboards can chart the plan next to the heat pump's own registers. Room
priorities and the master switch are writable, which means the household can
retune the optimiser from the Home Assistant app without touching YAML.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .config import Config
from .optimizer import Plan
from .peaks import PeakState

_LOGGER = logging.getLogger(__name__)

DEVICE = {
    "identifiers": ["hemopt"],
    "name": "Kostnadsoptimering",
    "manufacturer": "hemopt",
    "model": "Heating and hot water scheduler",
}


class MqttBridge:
    def __init__(self, config: Config, on_command: Callable[[str, str], None] | None = None):
        self._config = config
        self._mqtt = config.mqtt
        self._on_command = on_command
        self._client: mqtt.Client | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _topic(self, suffix: str) -> str:
        return f"{self._mqtt.node_id}/{suffix}"

    def connect(self) -> None:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self._mqtt.node_id}-bridge"
        )
        if self._mqtt.username:
            client.username_pw_set(self._mqtt.username, self._mqtt.password)
        client.will_set(self._topic("status"), "offline", retain=True)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message

        client.connect_async(self._mqtt.host, self._mqtt.port, keepalive=60)
        client.loop_start()
        self._client = client

    def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.publish(self._topic("status"), "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        self._connected = False

    def _handle_connect(self, client: mqtt.Client, _userdata, _flags, reason_code, _props=None):
        if reason_code != 0:
            _LOGGER.error("MQTT connection refused: %s", reason_code)
            return
        self._connected = True
        client.publish(self._topic("status"), "online", retain=True)
        client.subscribe(self._topic("cmd/#"))
        self.publish_discovery()
        _LOGGER.info("MQTT connected to %s:%s", self._mqtt.host, self._mqtt.port)

    def _handle_disconnect(self, _client, _userdata, _flags, reason_code, _props=None):
        self._connected = False
        _LOGGER.warning("MQTT disconnected: %s", reason_code)

    def _handle_message(self, _client, _userdata, message: mqtt.MQTTMessage) -> None:
        if self._on_command is None:
            return
        key = message.topic.split("cmd/", 1)[-1]
        try:
            self._on_command(key, message.payload.decode())
        except Exception:  # noqa: BLE001 - a bad command must not kill the loop
            _LOGGER.exception("MQTT command %s failed", key)

    # --- discovery ------------------------------------------------------
    def _publish_config(self, component: str, object_id: str, payload: dict[str, Any]) -> None:
        if self._client is None:
            return
        payload = {
            "device": DEVICE,
            "availability_topic": self._topic("status"),
            "unique_id": f"hemopt_{object_id}",
            "object_id": f"hemopt_{object_id}",
            **payload,
        }
        topic = f"{self._mqtt.discovery_prefix}/{component}/{self._mqtt.node_id}/{object_id}/config"
        self._client.publish(topic, json.dumps(payload), retain=True)

    def publish_discovery(self) -> None:
        state_topic = self._topic("state")

        sensors: list[tuple[str, str, dict[str, Any]]] = [
            ("spot_price", "Spotpris nu", {"unit_of_measurement": "SEK/kWh", "icon": "mdi:cash"}),
            (
                "total_price",
                "Totalpris nu",
                {"unit_of_measurement": "SEK/kWh", "icon": "mdi:cash-multiple"},
            ),
            (
                "planned_power",
                "Planerad effekt",
                {
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "state_class": "measurement",
                },
            ),
            (
                "peak_threshold",
                "Effekttak",
                {
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "icon": "mdi:transmission-tower",
                },
            ),
            (
                "peak_average",
                "Manadens effektmedel",
                {
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "icon": "mdi:chart-bell-curve",
                },
            ),
            (
                "peak_cost",
                "Prognos effektavgift",
                {"unit_of_measurement": "SEK", "device_class": "monetary", "icon": "mdi:cash-lock"},
            ),
            (
                "hour_headroom",
                "Effektutrymme denna timme",
                {"unit_of_measurement": "kW", "device_class": "power", "icon": "mdi:speedometer"},
            ),
            (
                "planned_saving",
                "Besparing planperiod",
                {
                    "unit_of_measurement": "SEK",
                    "device_class": "monetary",
                    "icon": "mdi:piggy-bank",
                },
            ),
            (
                "cheapest_hour",
                "Billigaste timmen",
                {"icon": "mdi:clock-check"},
            ),
            (
                "most_expensive_hour",
                "Dyraste timmen",
                {"icon": "mdi:clock-alert"},
            ),
            ("plan_status", "Planstatus", {"icon": "mdi:calendar-check"}),
            ("guard_reason", "Effektvaktens skal", {"icon": "mdi:shield-search"}),
            (
                "advice_saving",
                "Mojlig besparing avtal",
                {
                    "unit_of_measurement": "SEK",
                    "device_class": "monetary",
                    "icon": "mdi:file-document-edit",
                },
            ),
            ("advice_top", "Basta avtalsradet", {"icon": "mdi:lightbulb-on"}),
        ]

        for object_id, name, extra in sensors:
            self._publish_config(
                "sensor",
                object_id,
                {
                    "name": name,
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.{object_id} }}}}",
                    **extra,
                },
            )

        binary_sensors = [
            ("heating_blocked", "Uppvarmning pausad", "mdi:pause-octagon"),
            ("preheating", "Forvarmer", "mdi:fire-alert"),
            ("peak_guard", "Effektvakt aktiv", "mdi:shield-flash"),
            ("in_peak_window", "Inom effektfonster", "mdi:calendar-clock"),
        ]
        for object_id, name, icon in binary_sensors:
            self._publish_config(
                "binary_sensor",
                object_id,
                {
                    "name": name,
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.{object_id} }}}}",
                    "payload_on": "true",
                    "payload_off": "false",
                    "icon": icon,
                },
            )

        self._publish_config(
            "switch",
            "control_enabled",
            {
                "name": "Styr varmepumpen",
                "state_topic": state_topic,
                "value_template": "{{ value_json.control_enabled }}",
                "command_topic": self._topic("cmd/control_enabled"),
                "payload_on": "true",
                "payload_off": "false",
                "state_on": "true",
                "state_off": "false",
                "icon": "mdi:robot",
            },
        )

        for room in self._config.rooms:
            self._publish_config(
                "number",
                f"priority_{room.key}",
                {
                    "name": f"Prioritet {room.name}",
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.priority_{room.key} }}}}",
                    "command_topic": self._topic(f"cmd/priority_{room.key}"),
                    "min": 1,
                    "max": 5,
                    "step": 1,
                    "mode": "slider",
                    "icon": "mdi:priority-high",
                },
            )
            self._publish_config(
                "sensor",
                f"setpoint_{room.key}",
                {
                    "name": f"Planerat borvarde {room.name}",
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.setpoint_{room.key} }}}}",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                },
            )
            self._publish_config(
                "sensor",
                f"inertia_{room.key}",
                {
                    "name": f"Troghet {room.name}",
                    "state_topic": state_topic,
                    "value_template": f"{{{{ value_json.inertia_{room.key} }}}}",
                    "unit_of_measurement": "h",
                    "icon": "mdi:timer-sand",
                    "entity_category": "diagnostic",
                },
            )

    # --- state ----------------------------------------------------------
    def publish_state(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            return
        self._client.publish(self._topic("state"), json.dumps(payload), retain=True)

    def publish_plan(self, plan: Plan) -> None:
        """Publish the full schedule for charting in the dashboard."""
        if self._client is None:
            return
        payload = {
            "start": plan.start.isoformat(),
            "step_minutes": plan.step_minutes,
            "times": [t.isoformat() for t in plan.times],
            "price": plan.price_sek_per_kwh,
            "heat_pump_kw": plan.heat_pump_kw,
            "total_kw": plan.total_power_kw,
            "rooms": {
                room.key: {"setpoint": room.setpoint, "temperature": room.temperature}
                for room in plan.rooms
            },
            "hot_water": (
                {
                    "temperature": plan.hot_water.temperature,
                    "charge": plan.hot_water.charge_fraction,
                }
                if plan.hot_water
                else None
            ),
        }
        self._client.publish(self._topic("plan"), json.dumps(payload), retain=True)

    def publish_peak_state(self, state: PeakState) -> None:
        if self._client is None:
            return
        payload = {
            "threshold_kw": round(state.threshold_kw, 3),
            "average_kw": round(state.average_kw, 3),
            "projected_cost_sek": round(state.projected_cost_sek, 2),
            "counted": [
                {"day": peak.day.date().isoformat(), "kw": round(peak.kw, 3), "hour": peak.hour}
                for peak in state.counted
            ],
        }
        self._client.publish(self._topic("peaks"), json.dumps(payload), retain=True)
