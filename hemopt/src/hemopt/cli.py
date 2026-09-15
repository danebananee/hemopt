"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .config import Config
from .storage import Store

DEFAULT_PORT = 8723


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _build_engine(args: argparse.Namespace):
    from .engine import Engine

    if args.demo:
        from .demo import DemoEngine, demo_config

        config = demo_config(database_path=args.database or "hemopt-demo.db")
        return DemoEngine(config)

    config = Config.resolve(args.config)
    if args.database:
        config.database_path = args.database
    if not config.home_assistant.token and not (
        os.environ.get("HEMOPT_HA_URL") or os.environ.get("HEMOPT_DATA")
    ):
        raise SystemExit(
            "no Home Assistant credentials: pass --config, or run as the add-on "
            "so the Supervisor can provide them"
        )
    return Engine(config, store=Store(config.database_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hemopt",
        description="Cost optimisation for heating, hot water and peak power",
    )
    parser.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    parser.add_argument("-d", "--database", help="override the SQLite path")
    parser.add_argument("--demo", action="store_true", help="run against a simulated house")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the API and control panel")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    subparsers.add_parser("plan", help="compute one plan and print the summary")
    subparsers.add_parser("train", help="refit the models from recorder history")
    subparsers.add_parser("peaks", help="show this month's peak status")
    subparsers.add_parser("example-config", help="print a starter config.yaml")
    subparsers.add_parser("doctor", help="check every configured entity against Home Assistant")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "example-config":
        print(_EXAMPLE_CONFIG)
        return 0

    if args.command == "doctor":
        return _run_doctor(args)

    command = args.command or "serve"

    if command == "serve":
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", DEFAULT_PORT)
        log_level = "debug" if args.verbose else "info"

        if args.demo or args.config:
            # Local / demo runs already have everything imported; keep the
            # simple path so tests and `hemopt --demo serve` stay predictable.
            from .api import create_app

            uvicorn.run(
                create_app(_build_engine(args)),
                host=host,
                port=port,
                log_level=log_level,
            )
        else:
            # Add-on path: bind healthz before numpy/HiGHS finish loading.
            uvicorn.run(
                "hemopt.addon:create_app",
                factory=True,
                host=host,
                port=port,
                log_level=log_level,
            )
        return 0

    engine = _build_engine(args)
    return asyncio.run(_run_once(engine, command))


def _run_doctor(args: argparse.Namespace) -> int:
    from .doctor import format_report, run_doctor

    config = Config.resolve(args.config)
    if not config.home_assistant.token:
        print(
            "doctor needs Home Assistant credentials: pass -c config.yaml, or run "
            "it from inside the add-on",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(run_doctor(config))
    print(format_report(report))
    return 1 if report.failures else 0


async def _run_once(engine, command: str) -> int:
    await engine.start()
    try:
        if command == "train":
            await engine.train()
            for key, model in engine.models.items():
                print(
                    f"{key:14s} tau {model.tau_hours:6.1f} h  "
                    f"heat {model.k_heat_per_hour:5.2f} K/h  "
                    f"R2 {model.r_squared:4.2f}  "
                    f"{'fitted' if model.fitted else 'default'}"
                )
            print(f"hot water {engine.hot_water_profile.daily_total_kwh():.1f} kWh/day")
            return 0

        await engine.collect()
        plan = await engine.replan()
        if plan is None:
            print("planning failed:", *engine.status.errors[-3:], sep="\n  ", file=sys.stderr)
            return 1

        if command == "peaks":
            state = engine.peaks
            if state is None:
                print("no peak data yet")
                return 0
            print(f"threshold      {state.threshold_kw:6.2f} kW")
            print(f"month average  {state.average_kw:6.2f} kW")
            print(f"projected cost {state.projected_cost_sek:6.0f} SEK")
            for peak in state.counted:
                print(f"  {peak.day.date()}  {peak.kw:5.2f} kW at {peak.hour:02d}:00")
            return 0

        print(f"status          {plan.status} in {plan.solve_seconds:.2f} s")
        print(f"horizon         {len(plan.times) * plan.step_minutes / 60:.0f} h")
        print(f"energy cost     {plan.energy_cost_sek:7.2f} SEK")
        print(f"peak cost       {plan.peak_cost_sek:7.2f} SEK")
        baseline = plan.baseline_energy_cost_sek + plan.baseline_peak_cost_sek
        print(f"baseline        {baseline:7.2f} SEK")
        print(f"saving          {plan.savings_sek:7.2f} SEK")
        for note in plan.notes:
            print(f"note            {note}")
        return 0
    finally:
        await engine.stop()


_EXAMPLE_CONFIG = """\
site:
  price_area: SE3
  timezone: Europe/Stockholm
  main_fuse_amps: 20

home_assistant:
  base_url: http://homeassistant.local:8123
  token: PASTE_LONG_LIVED_TOKEN_HERE
  history_days: 21

mqtt:
  enabled: true
  host: homeassistant.local
  port: 1883
  username: hemopt
  password: CHANGE_ME

energy_price:
  supplier_markup_ore: 8.0
  energy_tax_ore: 43.9
  transfer_fee_high_ore: 31.12
  transfer_fee_normal_ore: 12.4

peak_tariff:
  enabled: true
  n_peaks: 5
  price_per_kw_sek: 67.5
  window:
    months: [1, 2, 3, 11, 12]
    hour_start: 7
    hour_end: 21
    weekdays_only: true

heat_pump:
  max_thermal_kw: 9.0
  max_electrical_kw: 3.2
  power_entity: sensor.h66_hppower_consumption
  outdoor_entity: sensor.h66_hpoutdoor

hot_water:
  enabled: true
  top_temperature_entity: sensor.h66_hpwarm_water_1_top
  tank_litres: 180
  min_temperature: 42
  target_temperature: 52

base_load:
  total_power_entity: sensor.p1_meter_active_power
  default_kw: 0.7

optimiser:
  horizon_hours: 36
  step_minutes: 15
  run_interval_minutes: 15
  apply_controls: false

rooms:
  - key: vardagsrum
    name: Vardagsrum
    floor: Övervåning
    priority: 5
    temperature_entity: sensor.fa_6a_ee_c8_9a_63_temperature
    comfort_min: 21.0
    comfort_max: 22.5
    heat_share: 2.2
"""


if __name__ == "__main__":
    raise SystemExit(main())
