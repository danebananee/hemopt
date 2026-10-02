"""Which room does each thermostat actually heat?

Every room's temperature is modelled as

    dT/dt = Σ_j b_j · floor_j(t) + a · (T_out − T) + c + sun(time of day)

where floor_j is loop j's heat as the room feels it: the valve state times
how warm the water is, delayed by the floor. A concrete slab takes hours to
warm through, a timber floor on chipboard well under an hour, so the delay is
fitted per room from a handful of candidates. Each b_j says how many degrees
per hour loop j adds to this room with its valve fully open. The room where
b_j is largest is the room loop j lies in.

Measurements are averaged over 15-minute blocks, which removes most of the
0.1 °C rounding in the thermostats' readings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

import numpy as np

BLOCK_MINUTES = 15
LAG_CANDIDATES_H = (0.0, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0)

# A loop that adds less than this to a room, in °C per hour with its valve
# fully open, is not considered to heat it.
MIN_EFFECT = 0.04
SURE_Z = 3.0
LIKELY_Z = 1.5
MIN_VARIATION = 0.05


@dataclass(slots=True)
class Sample:
    """One reading of the house, normally once a minute."""

    t: float
    temps: dict[str, float | None]
    targets: dict[str, float | None] = field(default_factory=dict)
    # 1 = the test holds this thermostat's loop open, 0 = closed, None = the
    # thermostat runs normally and its valve state is inferred.
    cmd: dict[str, int | None] = field(default_factory=dict)
    supply: float | None = None
    outdoor: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 1),
            "temps": self.temps,
            "targets": self.targets,
            "cmd": self.cmd,
            "supply": self.supply,
            "outdoor": self.outdoor,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Sample:
        return cls(
            t=float(raw["t"]),
            temps=dict(raw.get("temps") or {}),
            targets=dict(raw.get("targets") or {}),
            cmd=dict(raw.get("cmd") or {}),
            supply=raw.get("supply"),
            outdoor=raw.get("outdoor"),
        )


@dataclass(frozen=True, slots=True)
class Place:
    key: str
    name: str
    thermostat: bool


def inferred_valve(current: float | None, target: float | None) -> float | None:
    """How open a thermostat's valve is when it regulates on its own.

    LK's controller opens the loop when the room is below the setpoint and
    closes it just above; a narrow ramp stands in for its hysteresis.
    """
    if current is None or target is None:
        return None
    return float(np.clip((target - current) / 0.2 + 0.5, 0.0, 1.0))


def _heat_scale(samples: list[Sample], places: list[Place]) -> tuple[list[float], str]:
    """How much heat the water carries, per sample, 1.0 being typical.

    A floor loop gives off heat in proportion to how much warmer the water is
    than the room. While the heat pump makes hot water, or stops heating in
    mild weather, an open valve gives nothing, and the regression must know.
    """
    raws: list[float | None] = []
    for sample in samples:
        if sample.supply is None:
            raws.append(None)
            continue
        temps = [sample.temps.get(p.key) for p in places]
        known = [value for value in temps if value is not None]
        room = sum(known) / len(known) if known else 21.0
        raws.append(max(0.0, sample.supply - room))
    measured = [raw for raw in raws if raw is not None]
    if not measured:
        return [1.0] * len(samples), "assumed"
    warm = [raw for raw in measured if raw > 3.0]
    if len(warm) < max(4, len(measured) // 10):
        return [1.0 if raw is None else 0.0 for raw in raws], "none"
    typical = float(np.median(warm))
    scale = [1.0 if raw is None else min(2.0, raw / typical) for raw in raws]
    return scale, "measured"


def _blocks(
    samples: list[Sample], thermostats: list[Place], places: list[Place], block_s: float
) -> dict[str, Any]:
    first = math.floor(samples[0].t / block_s) * block_s
    count = int((samples[-1].t - first) // block_s) + 1
    heat, heat_source = _heat_scale(samples, places)

    temp_sum = {p.key: np.zeros(count) for p in places}
    temp_n = {p.key: np.zeros(count) for p in places}
    in_sum = {p.key: np.zeros(count) for p in thermostats}
    in_n = {p.key: np.zeros(count) for p in thermostats}
    out_sum = np.zeros(count)
    out_n = np.zeros(count)

    for sample, scale in zip(samples, heat, strict=True):
        k = int((sample.t - first) // block_s)
        for place in places:
            value = sample.temps.get(place.key)
            if value is not None:
                temp_sum[place.key][k] += value
                temp_n[place.key][k] += 1
        for thermo in thermostats:
            command = sample.cmd.get(thermo.key)
            if command is not None:
                valve: float | None = float(command)
            else:
                valve = inferred_valve(sample.temps.get(thermo.key), sample.targets.get(thermo.key))
            if valve is not None:
                in_sum[thermo.key][k] += valve * scale
                in_n[thermo.key][k] += 1
        if sample.outdoor is not None:
            out_sum[k] += sample.outdoor
            out_n[k] += 1

    with np.errstate(invalid="ignore", divide="ignore"):
        temps = {key: np.where(temp_n[key] > 0, temp_sum[key] / temp_n[key], np.nan) for key in temp_sum}
        outdoor = np.where(out_n > 0, out_sum / out_n, np.nan)
        inputs = {}
        for key in in_sum:
            series = np.where(in_n[key] > 0, in_sum[key] / in_n[key], np.nan)
            inputs[key] = _fill(series, default=0.0)
    if np.isfinite(outdoor).any():
        outdoor = _fill(outdoor, default=float(np.nanmean(outdoor)))
    else:
        outdoor = None
    return {
        "first": first,
        "count": count,
        "temps": temps,
        "inputs": inputs,
        "outdoor": outdoor,
        "heat_source": heat_source,
    }


def _fill(series: np.ndarray, default: float) -> np.ndarray:
    """Carry the last known value forward (and the first one backward)."""
    out = series.copy()
    last = None
    for index, value in enumerate(out):
        if np.isfinite(value):
            last = value
        elif last is not None:
            out[index] = last
    finite = np.flatnonzero(np.isfinite(out))
    if finite.size == 0:
        return np.full_like(out, default)
    out[: finite[0]] = out[finite[0]]
    return out


def _floor_filter(values: np.ndarray, lag_h: float, dt_h: float) -> np.ndarray:
    """Average heat from the floor over each block, given the input series."""
    if lag_h <= 0:
        return values.copy()
    alpha = 1.0 - math.exp(-dt_h / lag_h)
    state = np.empty(values.size + 1)
    state[0] = values[0]
    for index, value in enumerate(values):
        state[index + 1] = state[index] + alpha * (value - state[index])
    return 0.5 * (state[:-1] + state[1:])


@dataclass(slots=True)
class RoomFit:
    key: str
    lag_h: float
    r2: float
    rows: int
    effects: dict[str, float]
    errors: dict[str, float]


def _fit_room(
    key: str,
    data: dict[str, Any],
    thermo_keys: list[str],
    dt_h: float,
    tz: tzinfo | None,
    with_daily: bool,
) -> RoomFit | None:
    temps = data["temps"][key]
    count = data["count"]
    y = (temps[1:] - temps[:-1]) / dt_h
    rows = np.isfinite(y) & np.isfinite(temps[:-1])

    extra_cols: list[np.ndarray] = [np.ones(count - 1)]
    if data["outdoor"] is not None:
        extra_cols.append(data["outdoor"][:-1] - np.nan_to_num(temps[:-1], nan=20.0))
    others = [data["temps"][other] for other in data["temps"] if other != key]
    if others:
        with np.errstate(invalid="ignore"):
            mean_others = np.nanmean(np.vstack(others), axis=0)
        extra_cols.append(np.nan_to_num(mean_others[:-1] - temps[:-1], nan=0.0))
    if with_daily:
        hours = np.array(
            [_local_hour(data["first"] + (index + 0.5) * dt_h * 3600, tz) for index in range(count - 1)]
        )
        extra_cols.append(np.sin(2 * math.pi * hours / 24))
        extra_cols.append(np.cos(2 * math.pi * hours / 24))

    params = len(thermo_keys) + len(extra_cols)
    n_rows = int(rows.sum())
    if n_rows < max(3 * params, 24):
        return None

    n_inputs = len(thermo_keys)
    target = y[rows]
    best: dict[str, Any] | None = None
    for lag in LAG_CANDIDATES_H:
        cols = [_floor_filter(data["inputs"][tk], lag, dt_h)[:-1] for tk in thermo_keys]
        design = np.column_stack(cols + extra_cols)[rows]
        coef, active = _nonnegative_fit(design, target, n_inputs)
        resid = target - design @ coef
        sse = float(resid @ resid)
        if best is None or sse < best["sse"]:
            best = {"sse": sse, "lag": lag, "coef": coef, "design": design, "resid": resid, "active": active}
    assert best is not None
    sse, lag, coef, design, resid = (best["sse"], best["lag"], best["coef"], best["design"], best["resid"])
    dof = max(1, n_rows - params)
    sigma2 = sse / dof
    cov = sigma2 * np.linalg.pinv(design.T @ design)
    # Residuals of a slow process are correlated from block to block; widen
    # the error bars accordingly instead of pretending every block is new.
    if resid.size > 3 and float(np.std(resid)) > 0:
        rho = float(np.corrcoef(resid[:-1], resid[1:])[0, 1])
        rho = 0.0 if not math.isfinite(rho) else max(0.0, min(0.9, rho))
    else:
        rho = 0.0
    inflate = math.sqrt((1 + rho) / (1 - rho))
    errors = np.sqrt(np.clip(np.diag(cov), 0, None)) * inflate
    sst = float(((target - target.mean()) ** 2).sum())
    r2 = 1 - sse / sst if sst > 0 else 0.0
    return RoomFit(
        key=key,
        lag_h=lag,
        r2=r2,
        rows=n_rows,
        effects={tk: float(coef[i]) for i, tk in enumerate(thermo_keys)},
        errors={tk: float(max(errors[i], 1e-6)) for i, tk in enumerate(thermo_keys)},
    )


def _nonnegative_fit(design: np.ndarray, target: np.ndarray, n_inputs: int) -> tuple[np.ndarray, list[bool]]:
    """Least squares where the loop effects cannot be negative.

    Opening a loop cannot cool a room. Without this constraint two loops that
    happened to be on together can trade a large positive effect for a large
    negative one and still fit the data. Inputs that want to go negative are
    fixed at zero one at a time, the most negative first.
    """
    active = [True] * design.shape[1]
    while True:
        cols = [i for i, keep in enumerate(active) if keep]
        sub, *_ = np.linalg.lstsq(design[:, cols], target, rcond=None)
        coef = np.zeros(design.shape[1])
        coef[cols] = sub
        negative = [i for i in range(n_inputs) if active[i] and coef[i] < 0]
        if not negative:
            return coef, active
        worst = min(negative, key=lambda i: coef[i])
        active[worst] = False


def _local_hour(epoch: float, tz: tzinfo | None) -> float:
    moment = datetime.fromtimestamp(epoch, tz)
    return moment.hour + moment.minute / 60


def analyse(
    samples: list[Sample],
    thermostats: list[tuple[str, str]],
    extra_rooms: list[tuple[str, str]] = (),
    *,
    tz: tzinfo | None = None,
    source: str = "test",
    block_minutes: int = BLOCK_MINUTES,
    floors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verdict per thermostat, and what to change, as a JSON-ready dict.

    `floors` maps thermostat and sensor keys to a floor. A loop is then only
    considered for rooms on its own floor: loops on separate manifolds cannot
    heat each other's rooms, and leaving them out removes most of what
    otherwise passes for heat from the wrong floor.
    """
    floors = {key: value for key, value in (floors or {}).items() if value}
    thermos = [Place(key, name, True) for key, name in thermostats]
    places = thermos + [Place(key, name, False) for key, name in extra_rooms]
    names = {p.key: p.name for p in places}
    result: dict[str, Any] = {
        "source": source,
        "hours": 0.0,
        "heat_supply": "assumed",
        "rooms": [],
        "matrix": {},
        "thermostats": [],
        "fixes": [],
        "notes": [],
        "settled": False,
    }
    samples = sorted((s for s in samples), key=lambda s: s.t)
    if len(samples) < 10 or not thermos:
        result["notes"].append("För lite mätdata än.")
        for thermo in thermos:
            result["thermostats"].append(_pending(thermo))
        return result

    block_s = block_minutes * 60.0
    dt_h = block_minutes / 60.0
    data = _blocks(samples, thermos, places, block_s)
    hours = (samples[-1].t - samples[0].t) / 3600
    result["hours"] = round(hours, 1)
    result["heat_supply"] = data["heat_source"]
    if data["heat_source"] == "none":
        result["notes"].append(
            "Värmepumpen verkar inte ha skickat ut något varmt vatten till golvet under "
            "perioden. Utan värme i slingorna går det inte att se vilket rum de värmer."
        )

    varying = [t.key for t in thermos if float(np.std(data["inputs"][t.key])) >= MIN_VARIATION]
    fits: dict[str, RoomFit] = {}
    if varying:
        for place in places:
            candidates = [
                tk
                for tk in varying
                if not floors.get(tk) or not floors.get(place.key) or floors[tk] == floors[place.key]
            ]
            if not candidates:
                continue
            fit = _fit_room(place.key, data, candidates, dt_h, tz, with_daily=hours >= 36)
            if fit is not None:
                fits[place.key] = fit
    for place in places:
        fit = fits.get(place.key)
        result["rooms"].append(
            {
                "key": place.key,
                "name": place.name,
                "thermostat": place.thermostat,
                "lag_hours": fit.lag_h if fit else None,
                "fit": round(fit.r2, 2) if fit else None,
            }
        )

    result["matrix"] = {
        tk: {
            key: {
                "effect": round(fit.effects[tk], 3),
                "z": round(fit.effects[tk] / fit.errors[tk], 1),
            }
            for key, fit in fits.items()
            if tk in fit.effects
        }
        for tk in varying
    }

    settled = bool(fits) and bool(varying)
    for thermo in thermos:
        if thermo.key not in varying:
            verdict = _pending(thermo)
            verdict["status"] = "no_variation"
            verdict["text"] = (
                "Termostatens ventil har nästan inte ändrat läge under perioden, så det "
                "går inte att se vad den styr."
            )
            settled = False
        elif not fits:
            verdict = _pending(thermo)
            settled = False
        else:
            verdict = _verdict(thermo, fits, names, capped=source != "test")
            if not verdict["settled"]:
                settled = False
        result["thermostats"].append(verdict)

    result["fixes"] = _fixes(result["thermostats"], thermos, names)
    heated = {v["heats"] for v in result["thermostats"] if v["status"] in {"ok", "wrong"}}
    for extra in result["thermostats"]:
        heated.update(extra.get("also_keys", []))
    cold = [t.name for t in thermos if t.key not in heated]
    if settled and cold:
        result["notes"].append(
            "Ingen termostat verkar värma: "
            + ", ".join(cold)
            + ". Slingan där styrs antingen av en termostat som inte är med i testet, "
            "eller så har rummet ingen egen slinga."
        )
    result["settled"] = settled and hours >= 6
    return result


def _pending(thermo: Place) -> dict[str, Any]:
    return {
        "key": thermo.key,
        "name": thermo.name,
        "status": "pending",
        "heats": None,
        "heats_name": None,
        "also": [],
        "also_keys": [],
        "confidence": None,
        "effect": None,
        "text": "Väntar på mer mätdata.",
        "settled": False,
    }


def _verdict(
    thermo: Place, fits: dict[str, RoomFit], names: dict[str, str], *, capped: bool
) -> dict[str, Any]:
    ranked = sorted(
        (
            (key, fit.effects[thermo.key], fit.errors[thermo.key])
            for key, fit in fits.items()
            if thermo.key in fit.effects
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    best_key, best, best_se = ranked[0]
    verdict = _pending(thermo)
    verdict["effect"] = round(best, 3)
    z_best = best / best_se

    if best < MIN_EFFECT or z_best < SURE_Z:
        verdict["status"] = "none"
        verdict["text"] = (
            "Inget av de mätta rummen blir varmare när den här termostaten öppnar sin "
            "slinga. Slingan ligger troligen i ett rum utan givare (t.ex. hall eller "
            "badrum), eller så är kanalen inte kopplad till något ställdon."
        )
        # Settled once the error bars rule out a real effect anywhere.
        verdict["settled"] = best + 2 * best_se < 2.5 * MIN_EFFECT
        return verdict

    if len(ranked) > 1:
        _, second, second_se = ranked[1]
        separation = (best - second) / math.hypot(best_se, second_se)
    else:
        separation = SURE_Z
    if separation >= SURE_Z and not capped:
        confidence = "säker"
    elif separation >= LIKELY_Z:
        confidence = "trolig"
    else:
        confidence = "osäker"

    also = [key for key, effect, se in ranked[1:] if effect >= 0.6 * best and effect / se >= 4.0]
    verdict.update(
        {
            "heats": best_key,
            "heats_name": names[best_key],
            "also": [names[key] for key in also],
            "also_keys": also,
            "confidence": confidence,
        }
    )
    extra = f" Den värmer också {', '.join(names[k] for k in also)}." if also else ""
    if confidence == "osäker":
        verdict["status"] = "unclear"
        runner_up = names[ranked[1][0]]
        verdict["text"] = f"Troligen {names[best_key]}, men {runner_up} går inte att utesluta än."
    elif best_key == thermo.key:
        verdict["status"] = "ok"
        verdict["text"] = f"Rätt kopplad: styr slingan i sitt eget rum.{extra}"
    else:
        verdict["status"] = "wrong"
        verdict["text"] = f"Styr slingan i {names[best_key]}, inte i {thermo.name}.{extra}"
    verdict["settled"] = confidence == "säker"
    return verdict


def _fixes(
    verdicts: list[dict[str, Any]], thermos: list[Place], names: dict[str, str]
) -> list[dict[str, Any]]:
    """Plain instructions: which channel belongs to which thermostat."""
    thermo_keys = {t.key for t in thermos}
    wrong = {
        v["key"]: v["heats"]
        for v in verdicts
        if v["status"] == "wrong" and v["confidence"] in {"säker", "trolig"}
    }
    fixes: list[dict[str, Any]] = []
    done: set[str] = set()
    for source, room in wrong.items():
        if source in done:
            continue
        if room in thermo_keys and wrong.get(room) == source:
            done.update({source, room})
            fixes.append(
                {
                    "kind": "swap",
                    "a": source,
                    "b": room,
                    "text": (
                        f"Byt kanal mellan termostaterna i {names[source]} och {names[room]}: "
                        f"deras slingor är förväxlade med varandra."
                    ),
                }
            )
            continue
        done.add(source)
        if room in thermo_keys:
            text = (
                f"Kanalen som termostaten i {names[source]} styr idag sitter på slingan i "
                f"{names[room]}. Koppla den kanalen till termostaten i {names[room]}."
            )
        else:
            text = (
                f"Kanalen som termostaten i {names[source]} styr idag sitter på slingan i "
                f"{names[room]}, som saknar egen termostat. Koppla den till den termostat "
                f"som ska reglera {names[room]}, eller låt den vara om det är avsiktligt."
            )
        fixes.append({"kind": "move", "a": source, "b": room, "text": text})
    return fixes
