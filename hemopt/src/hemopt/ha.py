"""Home Assistant REST client.

Only three capabilities are needed: read current states, backfill history for
model training, and call services to apply the plan.

Under the Supervisor the Core API is reached at ``http://supervisor/core``
with ``SUPERVISOR_TOKEN`` as a Bearer token. Paths always include the ``/api``
prefix (``/api/states``), and ``_url`` builds absolute URLs so a shared client
cannot drop the ``/core`` path segment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import HomeAssistantConfig

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE = {"unknown", "unavailable", "none", "", None}
TRUTHY = {"on", "true", "heat", "heating", "active", "1"}


@dataclass(frozen=True, slots=True)
class StatePoint:
    moment: datetime
    value: float


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    moment: datetime
    temperature: float
    cloud_coverage: float | None = None
    wind_ms: float | None = None


def parse_numeric(state: str | None) -> float | None:
    """Coerce a Home Assistant state string to a number.

    Binary states map to 1.0 and 0.0 so a compressor sensor and a temperature
    sensor can go through the same pipeline.
    """
    if state is None:
        return None
    text = state.strip().lower()
    if text in UNAVAILABLE:
        return None
    if text in TRUTHY:
        return 1.0
    if text in {"off", "false", "idle", "inactive", "0"}:
        return 0.0
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def wind_to_ms(value: float, unit: object) -> float:
    """Wind speed in m/s from whatever unit the weather integration reports."""
    text = str(unit or "").strip().lower()
    if text in {"km/h", "kmh", "kph"}:
        return value / 3.6
    if text in {"mph"}:
        return value * 0.44704
    if text in {"kn", "kt", "knots"}:
        return value * 0.514444
    return value


def parse_statistics(result: dict, statistic_id: str) -> list[tuple[datetime, float]]:
    """(hour start, mean kW) rows from a statistics_during_period result.

    Newer Home Assistant versions send `start` as milliseconds since the
    epoch, older ones as an ISO string; both are accepted.
    """
    rows: list[tuple[datetime, float]] = []
    for row in result.get(statistic_id, []) or []:
        mean = row.get("mean")
        start = row.get("start")
        if mean is None or start is None:
            continue
        if isinstance(start, (int, float)):
            moment = datetime.fromtimestamp(start / 1000.0, tz=UTC)
        else:
            moment = datetime.fromisoformat(str(start))
        rows.append((moment, float(mean)))
    return rows


def is_climate(entity_id: str | None) -> bool:
    return bool(entity_id) and entity_id.startswith("climate.")


def climate_setpoints(rows: list[dict]) -> dict[str, float]:
    """Target temperature of every climate entity in a /api/states dump."""
    result: dict[str, float] = {}
    for row in rows:
        entity_id = row.get("entity_id", "")
        if not is_climate(entity_id):
            continue
        raw = (row.get("attributes") or {}).get("temperature")
        value = parse_numeric(None if raw is None else str(raw))
        if value is not None:
            result[entity_id] = value
    return result


def normalize_ha_base_url(url: str) -> str:
    """Strip a trailing /api so paths can always start with /api/…."""
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith("/api"):
        return trimmed[: -len("/api")] or trimmed
    return trimmed


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(self, config: HomeAssistantConfig, client: httpx.AsyncClient | None = None):
        self._config = config
        self._base = normalize_ha_base_url(config.base_url)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> HomeAssistantClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers=self._auth_headers(),
                verify=self._config.verify_ssl,
                timeout=httpx.Timeout(30.0, read=120.0),
                follow_redirects=True,
            )
            self._owns_client = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HomeAssistantClient must be used as an async context manager")
        return self._client

    def _url(self, path: str) -> str:
        """Absolute URL for an API path.

        Always absolute: a leading ``/api/…`` relative path would replace the
        ``/core`` prefix on ``http://supervisor/core``, and a shared client with
        an empty ``base_url`` (``httpx.URL('')`` is truthy) would address the
        wrong host. Absolute URLs keep both cases correct.
        """
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self._base}{path}"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.token}"}

    def _json_headers(self) -> dict[str, str]:
        return {**self._auth_headers(), "Content-Type": "application/json"}

    async def diagnose(self) -> dict[str, object]:
        """Probe the API and return a structured result for logs and the panel."""
        result: dict[str, object] = {
            "base_url": self._base,
            "token_present": bool(self._config.token),
            "token_length": len(self._config.token or ""),
            "ok": False,
            "status_code": None,
            "error": None,
            "message": None,
        }
        try:
            response = await self._http.get(self._url("/api/config"), headers=self._auth_headers())
        except httpx.HTTPError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        result["status_code"] = response.status_code
        if response.status_code != 200:
            result["error"] = response.text[:300]
            return result

        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        result["ok"] = True
        result["message"] = body.get("location_name") or body.get("version") or "ok"
        # The home's position is what the sun model needs; it is already here.
        for key in ("latitude", "longitude"):
            if isinstance(body.get(key), (int, float)):
                result[key] = float(body[key])
        return result

    async def ping(self) -> bool:
        diagnosis = await self.diagnose()
        if diagnosis["ok"]:
            return True
        _LOGGER.warning(
            "Home Assistant unreachable at %s (token_len=%s): %s %s",
            diagnosis["base_url"],
            diagnosis["token_length"],
            diagnosis.get("status_code") or "",
            diagnosis.get("error") or "",
        )
        return False

    async def states_full(self) -> list[dict]:
        """Every entity's state object, attributes included."""
        response = await self._http.get(self._url("/api/states"), headers=self._auth_headers())
        response.raise_for_status()
        return list(response.json())

    async def states(self) -> dict[str, str]:
        return {row["entity_id"]: row["state"] for row in await self.states_full()}

    async def state(self, entity_id: str) -> str | None:
        response = await self._http.get(
            self._url(f"/api/states/{entity_id}"), headers=self._auth_headers()
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("state")

    async def numeric_states(self, entity_ids: list[str]) -> dict[str, float]:
        """Current numeric value for each entity, skipping unavailable ones."""
        states = await self.states()
        result: dict[str, float] = {}
        for entity_id in entity_ids:
            value = parse_numeric(states.get(entity_id))
            if value is not None:
                result[entity_id] = value
        return result

    async def history(
        self, entity_ids: list[str], start: datetime, end: datetime | None = None
    ) -> dict[str, list[StatePoint]]:
        """Fetch recorder history for model training.

        Entities are requested in small batches because Home Assistant builds
        the whole response in memory and a three-week window across a dozen
        sensors will otherwise time out on a Raspberry Pi.

        A thermostat's state is its mode ("heat"), which says nothing. What the
        models need is the setpoint, which lives in the `temperature`
        attribute and changes without the state changing. Climate entities are
        therefore fetched with attributes and reported as their setpoint.
        """
        result: dict[str, list[StatePoint]] = {entity: [] for entity in entity_ids}
        plain = [entity for entity in entity_ids if not is_climate(entity)]
        climate = [entity for entity in entity_ids if is_climate(entity)]

        await self._history_batches(plain, start, end, result, attribute=None, batch_size=5)
        # Full state objects are much larger, so fewer per request.
        await self._history_batches(
            climate, start, end, result, attribute="temperature", batch_size=2
        )
        return result

    async def _history_batches(
        self,
        entity_ids: list[str],
        start: datetime,
        end: datetime | None,
        result: dict[str, list[StatePoint]],
        *,
        attribute: str | None,
        batch_size: int,
    ) -> None:
        for index in range(0, len(entity_ids), batch_size):
            batch = entity_ids[index : index + batch_size]
            params = {"filter_entity_id": ",".join(batch)}
            if attribute is None:
                params |= {
                    "minimal_response": "",
                    "no_attributes": "",
                    "significant_changes_only": "",
                }
            if end is not None:
                params["end_time"] = end.isoformat()

            try:
                response = await self._http.get(
                    self._url(f"/api/history/period/{start.isoformat()}"),
                    params=params,
                    headers=self._auth_headers(),
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                _LOGGER.warning("history request failed for %s: %s", batch, exc)
                continue

            for series in response.json():
                if not series:
                    continue
                entity_id = series[0].get("entity_id")
                if entity_id is None:
                    continue
                points: list[StatePoint] = []
                for row in series:
                    if attribute is None:
                        value = parse_numeric(row.get("state"))
                        stamp = row.get("last_changed") or row.get("last_updated")
                    else:
                        raw = (row.get("attributes") or {}).get(attribute)
                        value = parse_numeric(None if raw is None else str(raw))
                        # An attribute change moves last_updated, not last_changed.
                        stamp = row.get("last_updated") or row.get("last_changed")
                    if value is None or stamp is None:
                        continue
                    points.append(StatePoint(moment=datetime.fromisoformat(stamp), value=value))
                result[entity_id] = points

    async def history_attributes(
        self,
        entity_id: str,
        attributes: list[str],
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[StatePoint]]:
        """Recorder history of several numeric attributes of one entity.

        Used for a weather entity, whose cloud cover and wind speed only exist
        as attributes. Each attribute becomes its own series.
        """
        params = {"filter_entity_id": entity_id}
        if end is not None:
            params["end_time"] = end.isoformat()
        result: dict[str, list[StatePoint]] = {name: [] for name in attributes}
        try:
            response = await self._http.get(
                self._url(f"/api/history/period/{start.isoformat()}"),
                params=params,
                headers=self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            _LOGGER.warning("attribute history failed for %s: %s", entity_id, exc)
            return result
        for series in response.json():
            for row in series or []:
                stamp = row.get("last_updated") or row.get("last_changed")
                attrs = row.get("attributes") or {}
                if stamp is None:
                    continue
                moment = datetime.fromisoformat(stamp)
                for name in attributes:
                    raw = attrs.get(name)
                    value = parse_numeric(None if raw is None else str(raw))
                    if value is None:
                        continue
                    if name == "wind_speed":
                        value = wind_to_ms(value, attrs.get("wind_speed_unit"))
                    result[name].append(StatePoint(moment=moment, value=value))
        return result

    def websocket_url(self) -> str:
        """Where the websocket API lives for the configured base URL."""
        base = self._base
        if base.startswith("https://"):
            scheme, rest = "wss://", base[len("https://") :]
        elif base.startswith("http://"):
            scheme, rest = "ws://", base[len("http://") :]
        else:
            scheme, rest = "ws://", base
        # Under the Supervisor the Core websocket is proxied at /core/websocket.
        path = "/websocket" if rest.rstrip("/").endswith("/core") else "/api/websocket"
        return f"{scheme}{rest.rstrip('/')}{path}"

    async def hourly_statistics(
        self, statistic_id: str, start: datetime, end: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        """Hourly mean from Home Assistant's long-term statistics, in kW.

        Home Assistant keeps raw history for about ten days but hourly
        statistics for as long as the database lives, and only offers them
        over the websocket API. Returns an empty list when the websocket
        cannot be reached or the sensor has no statistics.
        """
        try:
            import websockets
        except ImportError:  # pragma: no cover - shipped with uvicorn[standard]
            _LOGGER.warning("websockets is not installed; long-term statistics unavailable")
            return []

        request = {
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start.isoformat(),
            "statistic_ids": [statistic_id],
            "period": "hour",
            "types": ["mean"],
            "units": {"power": "kW"},
        }
        if end is not None:
            request["end_time"] = end.isoformat()
        try:
            async with websockets.connect(
                self.websocket_url(), max_size=64 * 1024 * 1024, open_timeout=20
            ) as socket:
                await socket.recv()  # auth_required
                await socket.send(json.dumps({"type": "auth", "access_token": self._config.token}))
                reply = json.loads(await socket.recv())
                if reply.get("type") != "auth_ok":
                    _LOGGER.warning("websocket authentication refused: %s", reply.get("message"))
                    return []
                await socket.send(json.dumps(request))
                while True:
                    message = json.loads(await asyncio.wait_for(socket.recv(), timeout=120))
                    if message.get("id") == 1:
                        break
        except Exception as exc:  # noqa: BLE001 - statistics are a nice-to-have
            _LOGGER.warning("long-term statistics unavailable: %s", exc)
            return []
        if not message.get("success"):
            _LOGGER.warning("statistics request failed: %s", message.get("error"))
            return []
        return parse_statistics(message.get("result") or {}, statistic_id)

    async def call_service(self, domain: str, service: str, data: dict | None = None) -> None:
        response = await self._http.post(
            self._url(f"/api/services/{domain}/{service}"),
            json=data or {},
            headers=self._json_headers(),
        )
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"{domain}.{service} failed with {response.status_code}: {response.text[:200]}"
            )

    async def ensure_lk_arc_climate(self) -> dict[str, object]:
        """Start the LK Arc Climate config flow if climate.*_thermostat is missing.

        Copying custom_components/lk_arc_climate is not enough — without a
        config entry the dashboard shows Entity not found and writes never
        reach the LK cloud (no «LK Arc Climate:» lines in Logs).
        """
        result: dict[str, object] = {
            "ok": False,
            "action": None,
            "detail": None,
            "sample_climate": None,
        }
        try:
            states = await self.states()
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"states failed: {exc}"
            return result

        climates = sorted(
            entity_id
            for entity_id in states
            if entity_id.startswith("climate.") and entity_id.endswith("_thermostat")
        )
        # Prefer MAC-slug thermostats (exclude short H66 names).
        lk_climates = [entity_id for entity_id in climates if entity_id.count("_") >= 5]
        if lk_climates:
            result["ok"] = True
            result["action"] = "already_present"
            result["sample_climate"] = lk_climates[0]
            result["detail"] = f"{len(lk_climates)} climate.*_thermostat"
            return result

        # Tell the user in the HA UI — Golvvärme "Entity not found" is the symptom.
        try:
            await self.call_service(
                "persistent_notification",
                "create",
                {
                    "notification_id": "hemopt_lk_arc_climate",
                    "title": "Golvvärme: termostater saknas",
                    "message": (
                        "climate.*_thermostat finns inte (Entity not found på Golvvärme). "
                        "hemopt installerar LK Arc Climate — **Restart Home Assistant** "
                        "om du inte redan gjort det. Sensorerna fungerar; bara "
                        "termostaterna saknas."
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("persistent_notification for LK Arc failed", exc_info=True)

        try:
            start = await self._http.post(
                self._url("/api/config/config_entries/flow"),
                json={"handler": "lk_arc_climate", "show_advanced_options": False},
                headers=self._json_headers(),
            )
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"flow start failed: {exc}"
            return result

        body: dict = {}
        try:
            body = start.json() if start.content else {}
        except ValueError:
            body = {}

        if start.status_code >= 400:
            text = (start.text or "")[:300]
            result["detail"] = f"flow HTTP {start.status_code}: {text}"
            if "invalid_handler" in text or start.status_code == 400:
                result["action"] = "component_not_loaded"
                result["detail"] = (
                    "lk_arc_climate syns inte i HA ännu — Restart Home Assistant "
                    "efter att hemopt installerat custom_components/lk_arc_climate"
                )
            return result

        flow_type = body.get("type")
        flow_id = body.get("flow_id")
        if flow_type == "create_entry":
            result["ok"] = True
            result["action"] = "create_entry"
            result["detail"] = str(body.get("title") or "LK Arc Climate")
            _LOGGER.warning(
                "LK Arc Climate: config entry skapad via HA API — "
                "climate.*_thermostat ska synas om en stund"
            )
            return result

        if flow_type == "abort":
            reason = body.get("reason")
            result["action"] = f"abort:{reason}"
            result["ok"] = reason in {"already_configured"}
            result["detail"] = reason
            if reason == "missing_lksystems":
                result["detail"] = "LK Systems saknas — installera angoyd/ha-lksystems först"
            return result

        if flow_type == "form" and flow_id:
            try:
                confirm = await self._http.post(
                    self._url(f"/api/config/config_entries/flow/{flow_id}"),
                    json={},
                    headers=self._json_headers(),
                )
                confirm_body = confirm.json() if confirm.content else {}
            except Exception as exc:  # noqa: BLE001
                result["detail"] = f"flow confirm failed: {exc}"
                return result
            result["action"] = confirm_body.get("type") or "confirm"
            result["ok"] = confirm_body.get("type") == "create_entry"
            result["detail"] = str(
                confirm_body.get("title") or confirm_body.get("reason") or confirm_body
            )[:200]
            if result["ok"]:
                _LOGGER.warning("LK Arc Climate: config entry skapad (form confirm)")
            return result

        result["action"] = flow_type or f"http_{start.status_code}"
        result["detail"] = str(body)[:300]
        return result

    async def weather_forecast(self, entity_id: str) -> list[ForecastPoint]:
        """Hourly outdoor temperature forecast from a weather entity.

        Uses the `weather.get_forecasts` service response rather than the
        long-deprecated forecast attribute, and returns an empty list when the
        integration cannot supply an hourly forecast so the caller can fall
        back to holding the current reading.
        """
        try:
            response = await self._http.post(
                self._url("/api/services/weather/get_forecasts"),
                params={"return_response": "true"},
                json={"entity_id": entity_id, "type": "hourly"},
                headers=self._json_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            _LOGGER.warning("weather forecast unavailable from %s: %s", entity_id, exc)
            return []

        body = response.json()
        payload = body.get("service_response", body)
        rows = (payload.get(entity_id) or {}).get("forecast", [])

        # The forecast rows carry wind in the entity's own unit, which only
        # the entity's attributes name. Asked for only when there is wind.
        unit = None
        if any(row.get("wind_speed") is not None for row in rows):
            unit = ((await self._weather_attributes(entity_id)) or {}).get("wind_speed_unit")
        points: list[ForecastPoint] = []
        for row in rows:
            temperature = row.get("temperature")
            stamp = row.get("datetime")
            if temperature is None or stamp is None:
                continue
            cloud = parse_numeric(_text(row.get("cloud_coverage")))
            wind = parse_numeric(_text(row.get("wind_speed")))
            points.append(
                ForecastPoint(
                    moment=datetime.fromisoformat(stamp),
                    temperature=float(temperature),
                    cloud_coverage=cloud,
                    wind_ms=None if wind is None else wind_to_ms(wind, unit),
                )
            )
        points.sort(key=lambda point: point.moment)
        return points

    async def _weather_attributes(self, entity_id: str) -> dict | None:
        try:
            response = await self._http.get(
                self._url(f"/api/states/{entity_id}"), headers=self._auth_headers()
            )
            if response.status_code != 200:
                return None
            return response.json().get("attributes") or {}
        except (httpx.HTTPError, ValueError):
            return None

    async def set_climate_temperature(self, entity_id: str, temperature: float) -> None:
        await self.call_service(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": round(temperature, 1)},
        )

    async def set_number(self, entity_id: str, value: float) -> None:
        await self.call_service(
            "number", "set_value", {"entity_id": entity_id, "value": round(value, 1)}
        )

    async def set_temperature_entity(self, entity_id: str, temperature: float) -> None:
        """Write a setpoint through climate.* or number.* (H66 uses both)."""
        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            await self.set_climate_temperature(entity_id, temperature)
        elif domain == "number":
            await self.set_number(entity_id, temperature)
        else:
            raise HomeAssistantError(f"cannot set temperature through {entity_id}")

    async def set_ext_port(self, entity_id: str, active: bool) -> None:
        """Drive a Husdata EXT control port.

        The gateway exposes the ports as climate entities whose target
        temperature carries the register value, 1 for an asserted signal and 0
        for released. Plain switches are supported too, for gateways or
        templates that present them that way.
        """
        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            await self.set_climate_temperature(entity_id, 1.0 if active else 0.0)
        elif domain == "number":
            await self.set_number(entity_id, 1.0 if active else 0.0)
        elif domain in {"switch", "input_boolean"}:
            await self.call_service(
                domain, "turn_on" if active else "turn_off", {"entity_id": entity_id}
            )
        else:
            raise HomeAssistantError(f"cannot drive EXT port through {entity_id}")


def resample_forecast(
    points: list[ForecastPoint],
    times: list[datetime],
    fallback: float,
    field: str = "temperature",
) -> list[float]:
    """Interpolate an hourly forecast onto the optimiser's step grid.

    Linear interpolation matters here: a step change every hour would make the
    planner see phantom load spikes on the hour boundary. `field` picks the
    quantity; points where it is missing are skipped.
    """
    ordered = sorted(
        (point.moment, float(getattr(point, field)))
        for point in points
        if getattr(point, field) is not None
    )
    if not ordered:
        return [fallback] * len(times)

    result: list[float] = []
    for moment in times:
        if moment <= ordered[0][0]:
            result.append(ordered[0][1])
            continue
        if moment >= ordered[-1][0]:
            result.append(ordered[-1][1])
            continue
        position = bisect_right([m for m, _ in ordered], moment)
        (t0, v0), (t1, v1) = ordered[position - 1], ordered[position]
        span = (t1 - t0).total_seconds()
        ratio = (moment - t0).total_seconds() / span if span > 0 else 0.0
        result.append(v0 + ratio * (v1 - v0))
    return result


def resample_history(
    points: list[StatePoint], start: datetime, step_minutes: int, steps: int
) -> list[float | None]:
    """Sample a step-and-hold history series onto a uniform grid."""
    grid: list[float | None] = []
    ordered = sorted(points, key=lambda p: p.moment)
    index = 0
    latest: float | None = None

    for position in range(steps):
        moment = start + timedelta(minutes=step_minutes * position)
        while index < len(ordered) and ordered[index].moment <= moment:
            latest = ordered[index].value
            index += 1
        grid.append(latest)

    return grid
