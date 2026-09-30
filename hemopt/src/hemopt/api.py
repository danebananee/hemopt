"""FastAPI application: JSON API plus the built-in control panel."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Config
from .engine import Engine
from .ha import HomeAssistantClient
from .meters import pick_default_total_power, suggest_total_power_entities
from .panel_routes import VERSION, register_panel_routes
from .storage import Store

_LOGGER = logging.getLogger(__name__)


def create_addon_app() -> FastAPI:
    """Thin entry used by the add-on — see ``hemopt.addon``."""
    from .addon import create_app as create_thin_app

    return create_thin_app()


def create_app(engine: Engine, run_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.start()
        tasks: list[asyncio.Task] = []
        if run_loops:
            # Sampling history and solving the first plan takes minutes on a
            # Raspberry Pi. Awaiting it here would hold the port closed for
            # that long, and Home Assistant's ingress reports an add-on that
            # refuses connections as "not ready". So bind first, plan after.
            tasks.append(asyncio.create_task(_bootstrap_then_run(engine)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.stop()

    app = FastAPI(
        title="hemopt",
        description="Cost optimisation for heating, hot water and peak power",
        version=VERSION,
        lifespan=lifespan,
    )
    register_panel_routes(app, lambda: engine)
    return app


def _build_addon_engine() -> Engine:
    """Construct the engine off the event loop so imports can finish slowly."""
    config = Config.resolve(None)
    if not config.home_assistant.token:
        # Still start: the panel and healthz must answer so ingress stops
        # showing 502. Status will report Home Assistant as offline.
        _LOGGER.error("SUPERVISOR_TOKEN saknas; panelen startar men Home Assistant ar offline")
    return Engine(config, store=Store(config.database_path))


async def _adopt_meter_if_missing(engine: Engine) -> None:
    if engine.config.base_load.total_power_entity:
        return
    try:
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
            if not await ha.ping():
                return
            states = await ha.states()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("could not scan for electricity meters")
        return

    chosen = pick_default_total_power(states)
    if chosen is None:
        candidates = suggest_total_power_entities(states)
        if candidates:
            _LOGGER.info(
                "found %d power meter candidates; pick one in the panel",
                len(candidates),
            )
        return

    engine.config.base_load.total_power_entity = chosen
    engine.config.save_profile()
    _LOGGER.info("adopted electricity meter %s", chosen)


async def _bootstrap(engine: Engine) -> None:
    """Get a plan on screen before the periodic loops take over."""
    try:
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
            if await ha.ping():
                lk = await ha.ensure_lk_arc_climate()
                _LOGGER.warning(
                    "LK Arc Climate check: ok=%s action=%s detail=%s sample=%s",
                    lk.get("ok"),
                    lk.get("action"),
                    lk.get("detail"),
                    lk.get("sample_climate"),
                )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("LK Arc Climate ensure failed")
    try:
        await engine.collect()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial sampling failed")
    try:
        await engine.replan()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial planning failed")
    try:
        await engine.backfill_power_history()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("consumption backfill failed")
    try:
        await engine.refresh_advice()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial advice failed")


async def _bootstrap_then_run(engine: Engine) -> None:
    await _bootstrap(engine)
    await engine.run_forever()
