"""Home Assistant REST client.

Only three capabilities are needed: read current states, backfill history for
model training, and call services to apply the plan.

Under the Supervisor the Core API is reached at ``http://supervisor/core``
with ``SUPERVISOR_TOKEN`` as a Bearer token. Paths always include the ``/api``
prefix (``/api/states``), and ``_url`` builds absolute URLs so a shared client
cannot drop the ``/core`` path segment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

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

    async def states(self) -> dict[str, str]:
        response = await self._http.get(self._url("/api/states"), headers=self._auth_headers())
        response.raise_for_status()
        return {row["entity_id"]: row["state"] for row in response.json()}

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
        """
        result: dict[str, list[StatePoint]] = {entity: [] for entity in entity_ids}
        batch_size = 5

        for index in range(0, len(entity_ids), batch_size):
            batch = entity_ids[index : index + batch_size]
            params = {
                "filter_entity_id": ",".join(batch),
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
                    value = parse_numeric(row.get("state"))
                    if value is None:
                        continue
                    stamp = row.get("last_changed") or row.get("last_updated")
                    if stamp is None:
                        continue
                    points.append(StatePoint(moment=datetime.fromisoformat(stamp), value=value))
                result[entity_id] = points

        return result

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

        points: list[ForecastPoint] = []
        for row in rows:
            temperature = row.get("temperature")
            stamp = row.get("datetime")
            if temperature is None or stamp is None:
                continue
            points.append(
                ForecastPoint(moment=datetime.fromisoformat(stamp), temperature=float(temperature))
            )
        points.sort(key=lambda point: point.moment)
        return points

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
    points: list[ForecastPoint], times: list[datetime], fallback: float
) -> list[float]:
    """Interpolate an hourly forecast onto the optimiser's step grid.

    Linear interpolation matters here: a step change every hour would make the
    planner see phantom load spikes on the hour boundary.
    """
    if not points:
        return [fallback] * len(times)

    ordered = sorted(points, key=lambda point: point.moment)
    result: list[float] = []

    for moment in times:
        if moment <= ordered[0].moment:
            result.append(ordered[0].temperature)
            continue
        if moment >= ordered[-1].moment:
            result.append(ordered[-1].temperature)
            continue

        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.moment <= moment <= later.moment:
                span = (later.moment - earlier.moment).total_seconds()
                if span <= 0:
                    result.append(earlier.temperature)
                else:
                    ratio = (moment - earlier.moment).total_seconds() / span
                    result.append(
                        earlier.temperature + ratio * (later.temperature - earlier.temperature)
                    )
                break
        else:
            result.append(fallback)

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
