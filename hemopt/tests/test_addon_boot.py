"""The thin add-on entry must answer before the optimiser is imported."""

from __future__ import annotations

import sys

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.mark.asyncio
async def test_thin_entry_answers_healthz_before_engine_import(monkeypatch):
    # Ensure a cold import: drop optimiser modules if a previous test loaded them.
    for name in list(sys.modules):
        if name.startswith("hemopt.engine") or name.startswith("hemopt.optimizer"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    import hemopt.addon as addon

    boot_started = False

    async def slow_boot(holder):
        nonlocal boot_started
        boot_started = True
        holder["booting"] = True
        # Do not load the engine — we only care that healthz already works.
        return

    monkeypatch.setattr(addon, "_boot", slow_boot)

    app = addon.create_app()
    async with LifespanManager(app):
        assert boot_started
        assert "hemopt.engine" not in sys.modules
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            health = await http.get("/healthz")
            assert health.status_code == 200
            assert health.json()["ready"] is False

            status = await http.get("/api/status")
            assert status.status_code == 200
            assert status.json()["booting"] is True

            page = await http.get("/")
            assert page.status_code == 200
            assert "Elmätare" in page.text
