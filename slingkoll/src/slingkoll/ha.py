"""Minimal Home Assistant REST client (standard library only)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

History = dict[str, list[tuple[float, str, dict[str, Any]]]]


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> HAClient:
        supervisor = os.environ.get("SUPERVISOR_TOKEN", "") or _container_env("SUPERVISOR_TOKEN")
        override = os.environ.get("HA_TOKEN", "")
        if supervisor and not override:
            return cls("http://supervisor/core/api", supervisor)
        base = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
        if not base.endswith("/api"):
            base += "/api"
        return cls(base, override or supervisor)

    def now(self) -> float:
        return time.time()

    def _request(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise HAError(f"Home Assistant svarade {exc.code} på {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HAError(f"Når inte Home Assistant ({exc})") from exc
        return json.loads(raw) if raw else None

    def config(self) -> dict[str, Any]:
        return self._request("GET", "/config") or {}

    def states(self) -> list[dict[str, Any]]:
        return self._request("GET", "/states") or []

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self._request("POST", f"/services/{domain}/{service}", data, timeout=60)

    def history(self, entity_ids: list[str], start: float, end: float | None = None) -> History:
        begin = datetime.fromtimestamp(start, UTC).isoformat()
        query = {
            "filter_entity_id": ",".join(entity_ids),
            "significant_changes_only": "0",
        }
        if end is not None:
            query["end_time"] = datetime.fromtimestamp(end, UTC).isoformat()
        path = f"/history/period/{urllib.parse.quote(begin)}?{urllib.parse.urlencode(query)}"
        raw = self._request("GET", path, timeout=120) or []
        out: History = {entity: [] for entity in entity_ids}
        for series in raw:
            entity = None
            for row in series:
                entity = row.get("entity_id") or entity
                if entity is None:
                    continue
                stamp = row.get("last_updated") or row.get("last_changed")
                try:
                    moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                out.setdefault(entity, []).append(
                    (moment, str(row.get("state")), row.get("attributes") or {})
                )
        return out


def _container_env(name: str) -> str:
    """A variable the Supervisor set, read from s6-overlay's copy of the environment.

    The base image's init system keeps the container environment in a
    directory instead of passing it on to the add-on's start script.
    """
    for folder in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return ""


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) < 1e6 else None
