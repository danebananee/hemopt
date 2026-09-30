"""Runs the loop test: sets thermostats, keeps the house safe, puts everything back.

The test is a sequence of phases. In each phase every thermostat is set
either far above the room temperature (its loop opens) or far below (it
closes), following the pattern from `design`. Readings are stored once a
minute and analysed as they come in. When the verdict is certain, or the
test has run its longest, every thermostat, the heat pump and hemopt are put
back exactly as they were.

Safety comes first:

* a room outside the comfort limits gets its loop opened or closed if the
  test already knows which loop that is, otherwise the whole test pauses and
  the thermostats go back to normal until the room has recovered;
* if the add-on stops, the original setpoints are written back before it
  exits, and the test continues on the next start only if it was short;
* a room far outside the limits aborts the test.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .analysis import Sample, analyse, inferred_valve
from .design import make_design
from .discovery import default_settings, discover
from .ha import HAError, number
from .settings import Settings, data_dir, load_settings, save_settings

_LOGGER = logging.getLogger(__name__)

ACTIVE = {"running", "paused"}
ANALYSE_EVERY_S = 15 * 60
WRITE_RETRY_S = 5 * 60
PAUSE_MAX_S = 2 * 3600
RESUME_MAX_GAP_S = 2 * 3600
BOOST_AFTER_S = 45 * 60
HARD_MARGIN = 3.0
MIN_DELIVERY = 4.0


class Runner:
    def __init__(self, ha: Any, directory: Path | None = None) -> None:
        self.ha = ha
        self.dir = directory or data_dir()
        self.lock = threading.RLock()
        self.settings: Settings = load_settings(self.dir / "settings.json")
        self.run: dict[str, Any] | None = self._load_json("run.json")
        self.samples: list[Sample] = self._load_samples()
        self.live: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self.tz = ZoneInfo("Europe/Stockholm")
        self.last_analysis = 0.0
        self.analyse_every_s = ANALYSE_EVERY_S
        self.found: dict[str, Any] | None = None
        self._cold_since: float | None = None

    # --- persistence ------------------------------------------------------
    def _load_json(self, name: str) -> Any:
        path = self.dir / name
        try:
            return json.loads(path.read_text()) if path.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_json(self, name: str, value: Any) -> None:
        path = self.dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(path)

    def _load_samples(self) -> list[Sample]:
        path = self.dir / "samples.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(Sample.from_dict(json.loads(line)))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
        return out

    def _append_sample(self, sample: Sample) -> None:
        self.samples.append(sample)
        with (self.dir / "samples.jsonl").open("a") as handle:
            handle.write(json.dumps(sample.as_dict()) + "\n")

    def _save_run(self) -> None:
        if self.run is not None:
            self._save_json("run.json", self.run)

    def _event(self, text: str) -> None:
        _LOGGER.info(text)
        if self.run is not None:
            self.run.setdefault("events", []).append({"t": self.ha.now(), "text": text})
            self.run["events"] = self.run["events"][-200:]

    # --- startup ----------------------------------------------------------
    def start(self) -> None:
        """Called once at boot: timezone, first-run settings, resume or restore.

        Never raises: a Home Assistant that is not reachable yet is shown in
        the panel and retried on every tick.
        """
        try:
            self._start()
        except Exception as exc:  # noqa: BLE001 - the panel must come up regardless
            _LOGGER.exception("start failed")
            self.error = f"Uppstarten misslyckades: {exc}"

    def _start(self) -> None:
        try:
            zone = (self.ha.config() or {}).get("time_zone")
            if zone:
                self.tz = ZoneInfo(zone)
        except (HAError, KeyError, ValueError) as exc:
            _LOGGER.warning("could not read Home Assistant's time zone: %s", exc)
        try:
            states = self.ha.states()
        except HAError as exc:
            self.error = str(exc)
            _LOGGER.warning("Home Assistant not reachable at start: %s", exc)
            states = None
        with self.lock:
            if states is not None:
                self.found = discover(states)
                if not self.settings.configured:
                    self.settings = default_settings(self.found, self.settings)
                    save_settings(self.settings, self.dir / "settings.json")
                _LOGGER.info(
                    "found %d thermostats, %d selected",
                    len(self.found["thermostats"]),
                    len(self.settings.thermostats),
                )
            run = self.run
            if run and run.get("status") in ACTIVE:
                gap = self.ha.now() - float(run.get("last_tick") or 0)
                if gap > RESUME_MAX_GAP_S:
                    self._event("Tillägget var avstängt för länge – testet avbröts.")
                    self._finish("stopped")
                else:
                    run["suspended"] = False
                    self._event("Testet fortsätter efter omstart.")
                    self._save_run()

    def shutdown(self) -> None:
        """The add-on is stopping: put the house back to normal right away."""
        with self.lock:
            if self.run and self.run.get("status") in ACTIVE:
                self._restore(keep_run=True)
                self.run["suspended"] = True
                self._event("Tillägget stängs – ursprungliga börvärden återställda.")
                self._save_run()

    # --- settings ---------------------------------------------------------
    def update_settings(self, raw: dict[str, Any]) -> Settings:
        with self.lock:
            if self.run and self.run.get("status") in ACTIVE:
                raise ValueError("Stoppa testet innan du ändrar inställningarna.")
            merged = {**self.settings.as_dict(), **raw, "configured": True}
            settings = Settings.from_dict(merged)
            save_settings(settings, self.dir / "settings.json")
            self.settings = settings
            return settings

    def rediscover(self) -> dict[str, Any]:
        self.found = discover(self.ha.states())
        return self.found

    # --- the test ---------------------------------------------------------
    def start_test(self) -> dict[str, Any]:
        with self.lock:
            if self.run and self.run.get("status") in ACTIVE:
                raise ValueError("Ett test pågår redan.")
            settings = self.settings
            if len(settings.thermostats) < 2:
                raise ValueError("Välj minst två termostater.")
            states = {s["entity_id"]: s for s in self.ha.states()}
            originals: dict[str, float] = {}
            for thermo in settings.thermostats:
                state = states.get(thermo["entity_id"])
                target = number((state or {}).get("attributes", {}).get("temperature"))
                if target is None:
                    raise ValueError(f"Kan inte läsa börvärdet för {thermo['name']}.")
                originals[thermo["entity_id"]] = target
            hp = None
            if settings.hp_setpoint_entity:
                value = _setpoint_of(states.get(settings.hp_setpoint_entity))
                if value is not None:
                    hp = {"entity": settings.hp_setpoint_entity, "original": value, "boost": 0.0}
            hemopt_was_on = None
            switch = settings.hemopt_switch_entity
            if switch and (states.get(switch) or {}).get("state") == "on":
                hemopt_was_on = True

            if (self.dir / "run.json").exists() and self.run:
                self._save_json("previous.json", self.run)
            (self.dir / "samples.jsonl").unlink(missing_ok=True)
            self.samples = []
            now = self.ha.now()
            self.run = {
                "id": uuid.uuid4().hex[:8],
                "status": "running",
                "started": now,
                "ended": None,
                "thermostats": [dict(t) for t in settings.thermostats],
                "extra": [dict(s) for s in settings.extra_sensors],
                "design": make_design(len(settings.thermostats), seed=1),
                "blocks": 1,
                "phase": 0,
                "phase_active_s": 0.0,
                "phase_s": settings.phase_hours * 3600,
                "originals": originals,
                "hp": hp,
                "hemopt_was_on": hemopt_was_on,
                "overrides": {},
                "pause": None,
                "tolerated": [],
                "written": {},
                "events": [],
                "result": None,
                "last_tick": now,
                "suspended": False,
            }
            if hemopt_was_on:
                self._call("switch", "turn_off", {"entity_id": switch})
                self._event("hemopt:s styrning är avstängd under testet.")
            self._event(
                f"Testet startade med {len(settings.thermostats)} termostater, "
                f"{len(self.run['design'])} faser à {settings.phase_hours:g} h."
            )
            self._save_run()
        self.tick()
        return self.status()

    def stop_test(self, *, analyse_now: bool = True) -> dict[str, Any]:
        with self.lock:
            if not self.run or self.run.get("status") not in ACTIVE:
                raise ValueError("Inget test pågår.")
            self._event("Testet avslutades i förtid." if analyse_now else "Testet avbröts.")
            self._finish("stopped" if analyse_now else "aborted")
        return self.status()

    def _finish(self, status: str) -> None:
        run = self.run
        assert run is not None
        self._restore(keep_run=False)
        if status != "aborted":
            run["result"] = self._analyse()
        run["status"] = status
        run["ended"] = self.ha.now()
        run["pause"] = None
        run["overrides"] = {}
        self._save_run()

    def _restore(self, *, keep_run: bool) -> None:
        run = self.run
        assert run is not None
        for entity, value in run["originals"].items():
            self._set_temperature(entity, value, force=True)
        hp = run.get("hp")
        if hp and hp.get("boost"):
            self._set_temperature(hp["entity"], hp["original"], force=True)
            if not keep_run:
                hp["boost"] = 0.0
        if run.get("hemopt_was_on") and not keep_run and self.settings.hemopt_switch_entity:
            self._call("switch", "turn_on", {"entity_id": self.settings.hemopt_switch_entity})
            self._event("hemopt:s styrning är påslagen igen.")
        now = self.ha.now()
        run["written"] = {
            entity: {"value": value, "t": now, "retries": 0} for entity, value in run["originals"].items()
        }

    # --- the heartbeat ----------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            try:
                states = {s["entity_id"]: s for s in self.ha.states()}
                self.error = None
            except HAError as exc:
                self.error = str(exc)
                return
            now = self.ha.now()
            if self.found is None:
                # Home Assistant was not reachable at boot: set up now instead.
                self.found = discover(list(states.values()))
                if not self.settings.configured:
                    self.settings = default_settings(self.found, self.settings)
                    save_settings(self.settings, self.dir / "settings.json")
            self._update_live(states)
            run = self.run
            if not run or run.get("status") not in ACTIVE:
                return
            if run.get("suspended"):
                run["suspended"] = False
            elapsed = max(0.0, min(600.0, now - float(run.get("last_tick") or now)))
            run["last_tick"] = now

            sample = self._sample(states, now)
            self._append_sample(sample)
            if run["status"] == "running":
                run["phase_active_s"] += elapsed
            self._guard(sample, now)
            if run["status"] in ACTIVE:
                self._heat_pump(sample, now)
            if run["status"] == "running" and run["phase_active_s"] >= run["phase_s"]:
                self._next_phase()
            if run["status"] in ACTIVE:
                self._apply(states, now)
                if now - self.last_analysis >= self.analyse_every_s:
                    run["result"] = self._analyse()
                    self.last_analysis = now
            self._save_run()

    def _update_live(self, states: dict[str, dict[str, Any]]) -> None:
        live = {}
        for thermo in self.settings.thermostats:
            attrs = (states.get(thermo["entity_id"]) or {}).get("attributes") or {}
            live[thermo["entity_id"]] = {
                "temp": number(attrs.get("current_temperature")),
                "target": number(attrs.get("temperature")),
            }
        for sensor in self.settings.extra_sensors:
            live[sensor["entity_id"]] = {"temp": number((states.get(sensor["entity_id"]) or {}).get("state"))}
        for key in ("supply_entity", "outdoor_entity"):
            entity = getattr(self.settings, key)
            if entity:
                live[key] = {"temp": number((states.get(entity) or {}).get("state"))}
        self.live = live

    def _sample(self, states: dict[str, dict[str, Any]], now: float) -> Sample:
        run = self.run
        assert run is not None
        temps: dict[str, float | None] = {}
        targets: dict[str, float | None] = {}
        cmd: dict[str, int | None] = {}
        for thermo in run["thermostats"]:
            entity = thermo["entity_id"]
            attrs = (states.get(entity) or {}).get("attributes") or {}
            temps[entity] = number(attrs.get("current_temperature"))
            targets[entity] = number(attrs.get("temperature"))
            cmd[entity] = self._command(entity) if run["status"] == "running" else None
        for sensor in run["extra"]:
            temps[sensor["entity_id"]] = number((states.get(sensor["entity_id"]) or {}).get("state"))
        supply = outdoor = None
        if self.settings.supply_entity:
            supply = number((states.get(self.settings.supply_entity) or {}).get("state"))
        if self.settings.outdoor_entity:
            outdoor = number((states.get(self.settings.outdoor_entity) or {}).get("state"))
        return Sample(t=now, temps=temps, targets=targets, cmd=cmd, supply=supply, outdoor=outdoor)

    def _command(self, entity: str) -> int:
        run = self.run
        assert run is not None
        if entity in run["overrides"]:
            return int(run["overrides"][entity])
        index = [t["entity_id"] for t in run["thermostats"]].index(entity)
        return int(run["design"][run["phase"]][index])

    def _next_phase(self) -> None:
        run = self.run
        assert run is not None
        run["phase"] += 1
        run["phase_active_s"] = 0.0
        if run["phase"] < len(run["design"]):
            return
        result = self._analyse()
        run["result"] = result
        if result.get("settled"):
            self._event("Svaret är säkert. Testet är klart.")
            self._finish("finished")
        elif run["blocks"] >= self.settings.max_blocks:
            self._event("Längsta testtiden är nådd. Resultatet visar hur säkert varje svar är.")
            self._finish("finished")
        else:
            run["blocks"] += 1
            extra = make_design(len(run["thermostats"]), seed=run["blocks"])
            run["design"] = run["design"] + extra
            hours = len(extra) * run["phase_s"] / 3600
            self._event(f"Alla svar är inte säkra än – testet förlängs med {hours:g} timmar.")

    # --- safety -----------------------------------------------------------
    def _guard(self, sample: Sample, now: float) -> None:
        run = self.run
        assert run is not None
        low, high = self.settings.min_temp, self.settings.max_temp
        names = {p["entity_id"]: p["name"] for p in run["thermostats"] + run["extra"]}
        result = run.get("result") or {}
        heats: dict[str, list[str]] = {}
        for verdict in result.get("thermostats", []):
            if verdict.get("confidence") == "säker" and verdict.get("heats"):
                heats.setdefault(verdict["heats"], []).append(verdict["key"])

        for key, temp in sample.temps.items():
            if temp is None:
                continue
            if temp < low - HARD_MARGIN or temp > high + HARD_MARGIN:
                self._event(
                    f"{names.get(key, key)} är {temp:.1f} °C, långt utanför gränserna. "
                    "Testet avbröts och allt är återställt."
                )
                self._finish("stopped")
                return

        # Loops the test already knows: steer them directly.
        for room, loops in heats.items():
            temp = sample.temps.get(room)
            if temp is None:
                continue
            for loop in loops:
                current = run["overrides"].get(loop)
                if temp < low and current != 1:
                    run["overrides"][loop] = 1
                    self._event(f"{names[room]} är kall ({temp:.1f} °C) – dess slinga hålls öppen.")
                elif temp > high and current != 0:
                    run["overrides"][loop] = 0
                    self._event(f"{names[room]} är varm ({temp:.1f} °C) – dess slinga hålls stängd.")
                elif current is not None and low + 0.5 <= temp <= high - 0.5:
                    del run["overrides"][loop]

        # Rooms whose loop is still unknown: pause the test instead.
        outside = [
            (key, temp)
            for key, temp in sample.temps.items()
            if temp is not None
            and key not in heats
            and key not in run["tolerated"]
            and (temp < low or temp > high)
        ]
        pause = run.get("pause")
        if run["status"] == "running" and outside:
            key, temp = outside[0]
            word = "kall" if temp < low else "varm"
            run["status"] = "paused"
            run["pause"] = {
                "since": now,
                "room": key,
                "reason": f"{names.get(key, key)} är för {word} ({temp:.1f} °C)",
            }
            self._event(f"Paus: {run['pause']['reason']}. Termostaterna går som vanligt tills vidare.")
            self._restore(keep_run=True)
        elif run["status"] == "paused" and pause:
            recovered = all(
                temp is None or key in run["tolerated"] or low + 0.3 <= temp <= high - 0.3
                for key, temp in sample.temps.items()
            )
            if recovered:
                run["status"] = "running"
                run["pause"] = None
                self._event("Temperaturen är tillbaka inom gränserna – testet fortsätter.")
            elif now - float(pause["since"]) >= PAUSE_MAX_S:
                room = pause.get("room")
                if room and room not in run["tolerated"]:
                    run["tolerated"].append(room)
                run["status"] = "running"
                run["pause"] = None
                self._event(
                    f"{names.get(room, room)} kommer inte tillbaka ens i vanlig drift. Testet "
                    "fortsätter; rummet bevakas bara mot stora avvikelser."
                )

    def _heat_pump(self, sample: Sample, now: float) -> None:
        """Make sure there is warm water in the loops, raising the setpoint if not."""
        run = self.run
        assert run is not None
        hp = run.get("hp")
        if sample.supply is None or run["status"] != "running":
            self._cold_since = None
            return
        temps = [t for t in sample.temps.values() if t is not None]
        if not temps:
            return
        delivery = sample.supply - sum(temps) / len(temps)
        if delivery >= MIN_DELIVERY:
            self._cold_since = None
            return
        if self._cold_since is None:
            self._cold_since = now
            return
        if now - self._cold_since < BOOST_AFTER_S:
            return
        self._cold_since = now
        if hp and hp["boost"] < self.settings.hp_boost_max:
            hp["boost"] = min(self.settings.hp_boost_max, hp["boost"] + 1.0)
            value = hp["original"] + hp["boost"]
            self._set_temperature(hp["entity"], value, force=True)
            self._event(
                f"Golvvärmevattnet är för svalt ({sample.supply:.0f} °C). Värmepumpens "
                f"börvärde höjs till {value:g} °C under testet."
            )
        elif not run.get("warned_no_heat"):
            run["warned_no_heat"] = True
            self._event(
                "Värmepumpen skickar inget varmt vatten till golvet. Kontrollera att värmen "
                "inte är avstängd (sommarläge). Testet fortsätter, men svaren blir osäkra."
            )

    # --- writing setpoints --------------------------------------------------
    def _apply(self, states: dict[str, dict[str, Any]], now: float) -> None:
        run = self.run
        assert run is not None
        for thermo in run["thermostats"]:
            entity = thermo["entity_id"]
            if run["status"] == "paused":
                desired = run["originals"][entity]
            else:
                on = self._command(entity)
                desired = self.settings.open_setpoint if on else self.settings.closed_setpoint
            current = number(((states.get(entity) or {}).get("attributes") or {}).get("temperature"))
            if current is not None and abs(current - desired) < 0.05:
                continue
            last = run["written"].get(entity)
            if last and last["value"] == desired and now - last["t"] < WRITE_RETRY_S:
                continue
            if last and last["value"] == desired:
                last["retries"] = last.get("retries", 0) + 1
                if last["retries"] == 3:
                    self._event(
                        f"{thermo['name']} ändras tillbaka av något annat (en automation, "
                        "appen eller hemopt?). Testet skriver om börvärdet."
                    )
            self._set_temperature(entity, desired)
            run["written"][entity] = {
                "value": desired,
                "t": now,
                "retries": (last or {}).get("retries", 0) if last and last["value"] == desired else 0,
            }

    def _set_temperature(self, entity: str, value: float, *, force: bool = False) -> None:
        domain = entity.split(".", 1)[0]
        if domain == "number" or domain == "input_number":
            self._call(domain, "set_value", {"entity_id": entity, "value": value})
        else:
            self._call("climate", "set_temperature", {"entity_id": entity, "temperature": value})

    def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            self.ha.call_service(domain, service, data)
        except (HAError, KeyError) as exc:
            self._event(f"Kunde inte styra {data.get('entity_id')}: {exc}")

    # --- analysis -----------------------------------------------------------
    def _analyse(self) -> dict[str, Any]:
        run = self.run
        assert run is not None
        return analyse(
            self.samples,
            [(t["entity_id"], t["name"]) for t in run["thermostats"]],
            [(s["entity_id"], s["name"]) for s in run["extra"]],
            tz=self.tz,
            source="test",
        )

    def quick_check(self, days: float = 7.0) -> dict[str, Any]:
        """Look at recent history without touching anything."""
        settings = self.settings
        thermos = settings.thermostats
        if not thermos:
            raise ValueError("Välj termostater först.")
        now = self.ha.now()
        start = now - days * 86400
        entities = [t["entity_id"] for t in thermos] + [s["entity_id"] for s in settings.extra_sensors]
        for extra in (settings.supply_entity, settings.outdoor_entity):
            if extra:
                entities.append(extra)
        history = self.ha.history(entities, start, now)
        samples = history_samples(history, settings, start, now)
        result = analyse(
            samples,
            [(t["entity_id"], t["name"]) for t in thermos],
            [(s["entity_id"], s["name"]) for s in settings.extra_sensors],
            tz=self.tz,
            source="history",
        )
        result["symptoms"] = symptoms(samples, thermos)
        result["days"] = days
        return result

    # --- for the panel ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            run = self.run
            payload: dict[str, Any] = {
                "settings": self.settings.as_dict(),
                "error": self.error,
                "live": self.live,
                "run": None,
                "now": self.ha.now(),
            }
            if run:
                phases = len(run["design"])
                done = min(phases, run["phase"]) + (
                    run["phase_active_s"] / run["phase_s"] if run["phase"] < phases else 0
                )
                remaining_s = max(0.0, (phases - done) * run["phase_s"])
                payload["run"] = {
                    key: run.get(key)
                    for key in (
                        "id",
                        "status",
                        "started",
                        "ended",
                        "blocks",
                        "phase",
                        "pause",
                        "overrides",
                        "result",
                        "thermostats",
                        "extra",
                        "hp",
                        "tolerated",
                    )
                }
                payload["run"].update(
                    {
                        "phases": phases,
                        "progress": done / phases if phases else 0,
                        "remaining_s": remaining_s,
                        "phase_hours": run["phase_s"] / 3600,
                        "events": list(reversed(run.get("events", [])))[:40],
                        "commands": {
                            t["entity_id"]: self._command(t["entity_id"]) for t in run["thermostats"]
                        }
                        if run["status"] == "running"
                        else {},
                    }
                )
            return payload

    def series(self, step_s: float = 600.0) -> dict[str, Any]:
        """Readings of the current test, thinned out for the charts."""
        with self.lock:
            points: list[dict[str, Any]] = []
            last = -1e18
            for sample in self.samples:
                if sample.t - last < step_s:
                    continue
                last = sample.t
                points.append(
                    {
                        "t": sample.t,
                        "temps": sample.temps,
                        "cmd": sample.cmd,
                        "supply": sample.supply,
                    }
                )
            return {"points": points}


def _setpoint_of(state: dict[str, Any] | None) -> float | None:
    if not state:
        return None
    if state["entity_id"].startswith("climate."):
        return number((state.get("attributes") or {}).get("temperature"))
    return number(state.get("state"))


def history_samples(history: dict[str, Any], settings: Settings, start: float, end: float) -> list[Sample]:
    """Five-minute samples from Home Assistant's history, holding the last value."""
    step = 300.0
    thermos = [t["entity_id"] for t in settings.thermostats]
    sensors = [s["entity_id"] for s in settings.extra_sensors]

    def values(entity: str, pick: str) -> list[tuple[float, float | None]]:
        rows = []
        for moment, state, attrs in history.get(entity, []):
            if pick == "state":
                rows.append((moment, number(state)))
            else:
                rows.append((moment, number(attrs.get(pick))))
        return sorted(rows)

    series = {}
    for entity in thermos:
        series[(entity, "temp")] = values(entity, "current_temperature")
        series[(entity, "target")] = values(entity, "temperature")
    for entity in sensors:
        series[(entity, "temp")] = values(entity, "state")
    if settings.supply_entity:
        series[("supply", "")] = values(settings.supply_entity, "state")
    if settings.outdoor_entity:
        series[("outdoor", "")] = values(settings.outdoor_entity, "state")

    cursors = {key: 0 for key in series}
    current: dict[tuple[str, str], float | None] = {key: None for key in series}
    samples = []
    moment = start
    while moment <= end:
        for key, rows in series.items():
            index = cursors[key]
            while index < len(rows) and rows[index][0] <= moment:
                current[key] = rows[index][1]
                index += 1
            cursors[key] = index
        temps = {e: current[(e, "temp")] for e in thermos + sensors}
        if any(v is not None for v in temps.values()):
            samples.append(
                Sample(
                    t=moment,
                    temps=temps,
                    targets={e: current[(e, "target")] for e in thermos},
                    cmd={e: None for e in thermos},
                    supply=current.get(("supply", "")),
                    outdoor=current.get(("outdoor", "")),
                )
            )
        moment += step
    return samples


def symptoms(samples: list[Sample], thermostats: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Signs of a mix-up that show up without any test.

    A thermostat whose loop is in another room asks for heat all the time
    while its own room stays cold, and the room that does get the heat runs
    warm with a thermostat that never asks for any.
    """
    out = []
    for thermo in thermostats:
        entity = thermo["entity_id"]
        valves, gaps = [], []
        for sample in samples:
            current, target = sample.temps.get(entity), sample.targets.get(entity)
            valve = inferred_valve(current, target)
            if valve is None or current is None or target is None:
                continue
            valves.append(valve)
            gaps.append(current - target)
        if len(valves) < 24:
            out.append(
                {"key": entity, "name": thermo["name"], "flag": "no_data", "text": "För lite historik."}
            )
            continue
        calling = sum(valves) / len(valves)
        gap = sum(gaps) / len(gaps)
        if calling > 0.7 and gap < -0.7:
            flag, text = (
                "cold",
                (
                    f"Begär värme {calling:.0%} av tiden men ligger i snitt {-gap:.1f} °C under "
                    "börvärdet. Typiskt när slingan den styr ligger i ett annat rum."
                ),
            )
        elif calling < 0.1 and gap > 0.8:
            flag, text = (
                "warm",
                (
                    f"Begär nästan aldrig värme men ligger i snitt {gap:.1f} °C över börvärdet. "
                    "Rummet värms troligen av en annan termostats slinga."
                ),
            )
        else:
            flag, text = (
                "normal",
                (f"Begär värme {calling:.0%} av tiden och håller börvärdet ({gap:+.1f} °C i snitt)."),
            )
        out.append(
            {
                "key": entity,
                "name": thermo["name"],
                "flag": flag,
                "calling": round(calling, 2),
                "gap": round(gap, 2),
                "text": text,
            }
        )
    return out


def run_forever(runner: Runner, interval: float = 60.0, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        started = time.monotonic()
        try:
            runner.tick()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            _LOGGER.exception("tick failed")
        stop.wait(max(1.0, interval - (time.monotonic() - started)))
