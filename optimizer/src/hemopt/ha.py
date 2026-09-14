"""Home Assistant REST client.

Only three capabilities are needed: read current states, backfill history for
model training, and call services to apply the plan.
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


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(self, config: HomeAssistantConfig, client: httpx.AsyncClient | None = None):
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> HomeAssistantClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self._config.token}",
                    "Content-Type": "application/json",
                },
                verify=self._config.verify_ssl,
                timeout=httpx.Timeout(30.0, read=120.0),
            )
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

    async def ping(self) -> bool:
        try:
            response = await self._http.get("/api/")
        except httpx.HTTPError as exc:
            _LOGGER.warning("Home Assistant unreachable: %s", exc)
            return False
        return response.status_code == 200

    async def states(self) -> dict[str, str]:
        response = await self._http.get("/api/states")
        response.raise_for_status()
        return {row["entity_id"]: row["state"] for row in response.json()}

    async def state(self, entity_id: str) -> str | None:
        response = await self._http.get(f"/api/states/{entity_id}")
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
                    f"/api/history/period/{start.isoformat()}", params=params
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
        response = await self._http.post(f"/api/services/{domain}/{service}", json=data or {})
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"{domain}.{service} failed with {response.status_code}: {response.text[:200]}"
            )

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
