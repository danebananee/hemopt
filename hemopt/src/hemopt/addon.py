"""Thin add-on entry: bind the port before the optimiser is imported.

`hemopt.engine` pulls in numpy and HiGHS. On a Raspberry Pi that import can
take long enough for Home Assistant's ingress to report 502 / "not ready".
This module stays free of those imports so uvicorn can open :8099 first.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .panel_routes import VERSION, register_panel_routes

_LOGGER = logging.getLogger(__name__)


def create_app() -> FastAPI:
    holder: dict[str, Any] = {"engine": None, "error": None, "booting": True}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(_boot(holder))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            engine = holder.get("engine")
            if engine is not None:
                await engine.stop()

    app = FastAPI(
        title="hemopt",
        description="Cost optimisation for heating, hot water and peak power",
        version=VERSION,
        lifespan=lifespan,
    )
    register_panel_routes(app, lambda: holder["engine"], lambda: holder.get("error"))
    return app


async def _boot(holder: dict[str, Any]) -> None:
    try:
        # Heavy imports happen here — after uvicorn has already bound the port.
        from .api import _adopt_meter_if_missing, _bootstrap_then_run, _build_addon_engine

        engine = await asyncio.to_thread(_build_addon_engine)
        holder["engine"] = engine
        holder["booting"] = False
        await engine.start()
        await _adopt_meter_if_missing(engine)
        await _bootstrap_then_run(engine)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        holder["booting"] = False
        holder["error"] = str(exc)
        _LOGGER.exception("add-on failed to start")
