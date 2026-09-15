"""Temporary diagnostic: floor-loop ↔ thermostat cross-wiring.

Passive analysis of Home Assistant recorder history. Correlates each room's
heat call (setpoint − indoor) with lagged temperature rise in every room. If
thermostat A's call heats room B more than A, the loop is likely swapped.

This module is intentionally separate from the optimiser. Delete it and the
API/CLI hooks when the house wiring is verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np

Status = Literal["ok", "suspect_swap", "weak", "insufficient_data"]

# Underfloor loops respond slowly; search lags from 30 min to 6 h.
DEFAULT_STEP_MINUTES = 15
MIN_LAG_STEPS = 2
MAX_LAG_STEPS = 24
MIN_CALL_SPAN = 0.08
MIN_SAMPLES = 96
MIN_SCORE = 0.12
MIN_MARGIN = 0.06
TAU_HOURS_PRIOR = 60.0


@dataclass(slots=True)
class RoomSeries:
    """Sparse history for one configured room."""

    key: str
    name: str
    climate_entity: str | None
    indoor: dict[datetime, float]
    setpoint: dict[datetime, float]


@dataclass(slots=True)
class RoomVerdict:
    room_key: str
    room_name: str
    climate_entity: str | None
    best_response_key: str | None
    best_score: float
    own_score: float
    margin: float
    best_lag_hours: float | None
    status: Status
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "room_key": self.room_key,
            "room_name": self.room_name,
            "climate_entity": self.climate_entity,
            "best_response_key": self.best_response_key,
            "best_score": round(self.best_score, 4),
            "own_score": round(self.own_score, 4),
            "margin": round(self.margin, 4),
            "best_lag_hours": (
                round(self.best_lag_hours, 2) if self.best_lag_hours is not None else None
            ),
            "status": self.status,
            "message": self.message,
        }


@dataclass(slots=True)
class SuggestedSwap:
    room_a: str
    room_b: str
    confidence: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "room_a": self.room_a,
            "room_b": self.room_b,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass(slots=True)
class LoopMappingReport:
    """Result of one passive cross-coupling analysis."""

    temporary: bool = True
    analyzed_at: datetime | None = None
    history_days: int = 0
    step_minutes: int = DEFAULT_STEP_MINUTES
    samples: int = 0
    rooms: list[RoomVerdict] = field(default_factory=list)
    # actuator_key -> response_key -> best correlation
    matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    suggested_swaps: list[SuggestedSwap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "temporary": self.temporary,
            "analyzed_at": self.analyzed_at.isoformat() if self.analyzed_at else None,
            "history_days": self.history_days,
            "step_minutes": self.step_minutes,
            "samples": self.samples,
            "rooms": [room.as_dict() for room in self.rooms],
            "matrix": {
                actuator: {response: round(score, 4) for response, score in responses.items()}
                for actuator, responses in self.matrix.items()
            },
            "suggested_swaps": [swap.as_dict() for swap in self.suggested_swaps],
            "notes": self.notes,
        }


def _nearest(
    series: dict[datetime, float],
    ordered: list[datetime],
    moment: datetime,
    tolerance_s: float = 45 * 60,
) -> float | None:
    if not ordered:
        return None
    best = min(ordered, key=lambda stamp: abs((stamp - moment).total_seconds()))
    if abs((best - moment).total_seconds()) > tolerance_s:
        return None
    return series[best]


def _heat_call(setpoint: float | None, indoor: float) -> float:
    if setpoint is None:
        return 0.0
    return max(0.0, min(1.0, (setpoint - indoor) / 0.5))


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 8:
        return 0.0
    if float(np.ptp(x)) < 1e-9 or float(np.ptp(y)) < 1e-9:
        return 0.0
    x_c = x - x.mean()
    y_c = y - y.mean()
    denom = float(np.sqrt(np.sum(x_c * x_c) * np.sum(y_c * y_c)))
    if denom <= 0.0:
        return 0.0
    return float(np.sum(x_c * y_c) / denom)


def _resample_grid(
    rooms: list[RoomSeries],
    outdoor: dict[datetime, float],
    step_minutes: int,
) -> tuple[list[datetime], dict[str, np.ndarray], dict[str, np.ndarray], np.ndarray] | None:
    """Build aligned heat-call and indoor arrays on a uniform grid."""
    indoor_moments = [moment for room in rooms for moment in room.indoor]
    if not indoor_moments:
        return None

    start = min(indoor_moments)
    end = max(indoor_moments)
    step = timedelta(minutes=step_minutes)
    start = start.replace(second=0, microsecond=0)
    minute = (start.minute // step_minutes) * step_minutes
    start = start.replace(minute=minute)

    moments: list[datetime] = []
    cursor = start
    while cursor <= end:
        moments.append(cursor)
        cursor += step
    if len(moments) < MIN_SAMPLES + MAX_LAG_STEPS:
        return None

    outdoor_times = sorted(outdoor)
    heat: dict[str, list[float]] = {room.key: [] for room in rooms}
    indoor_vals: dict[str, list[float]] = {room.key: [] for room in rooms}
    outdoor_vals: list[float] = []
    keep_mask: list[bool] = []

    for moment in moments:
        row_ok = True
        outdoor_now = _nearest(outdoor, outdoor_times, moment)
        if outdoor_now is None:
            row_ok = False
            outdoor_now = 0.0

        room_indoor: dict[str, float] = {}
        room_call: dict[str, float] = {}
        for room in rooms:
            indoor_times = sorted(room.indoor)
            indoor = _nearest(room.indoor, indoor_times, moment)
            if indoor is None:
                row_ok = False
                indoor = 0.0
            setpoint_times = sorted(room.setpoint)
            setpoint = _nearest(room.setpoint, setpoint_times, moment)
            room_indoor[room.key] = indoor
            room_call[room.key] = _heat_call(setpoint, indoor)

        keep_mask.append(row_ok)
        outdoor_vals.append(outdoor_now)
        for room in rooms:
            indoor_vals[room.key].append(room_indoor[room.key])
            heat[room.key].append(room_call[room.key])

    valid_indices = [index for index, ok in enumerate(keep_mask) if ok]
    if len(valid_indices) < MIN_SAMPLES + MAX_LAG_STEPS:
        return None

    first, last = valid_indices[0], valid_indices[-1]
    moments = moments[first : last + 1]
    outdoor_arr = np.asarray(outdoor_vals[first : last + 1], dtype=float)
    heat_arr = {
        key: np.asarray(values[first : last + 1], dtype=float) for key, values in heat.items()
    }
    indoor_arr = {
        key: np.asarray(values[first : last + 1], dtype=float)
        for key, values in indoor_vals.items()
    }
    if len(moments) < MIN_SAMPLES + MAX_LAG_STEPS:
        return None
    return moments, heat_arr, indoor_arr, outdoor_arr


def _residual_rise(
    indoor: np.ndarray,
    outdoor: np.ndarray,
    lag_steps: int,
    dt_hours: float,
) -> np.ndarray:
    """Temperature rise over `lag_steps`, minus free-fall from outdoor drift."""
    lag_hours = lag_steps * dt_hours
    observed = indoor[lag_steps:] - indoor[:-lag_steps]
    mid_outdoor = 0.5 * (outdoor[lag_steps:] + outdoor[:-lag_steps])
    expected = (mid_outdoor - indoor[:-lag_steps]) * (lag_hours / TAU_HOURS_PRIOR)
    return observed - expected


def analyse_loop_mapping(
    rooms: list[RoomSeries],
    outdoor: dict[datetime, float],
    *,
    step_minutes: int = DEFAULT_STEP_MINUTES,
    history_days: int = 0,
    now: datetime | None = None,
) -> LoopMappingReport:
    """Cross-correlate thermostat heat calls with lagged room temperature rise."""
    report = LoopMappingReport(
        analyzed_at=now,
        history_days=history_days,
        step_minutes=step_minutes,
        notes=[
            "Tillfällig diagnostik — inte del av styrningen. Kan tas bort när "
            "slingorna är verifierade.",
        ],
    )
    usable = [room for room in rooms if room.indoor and room.climate_entity]
    if len(usable) < 2:
        report.notes.append("Behöver minst två rum med climate_entity och temperaturhistorik.")
        return report

    grid = _resample_grid(usable, outdoor, step_minutes)
    if grid is None:
        report.notes.append(
            "För lite sammanhängande historik. Samla data några dygn med "
            "varierande börvärden (styrning får gärna vara av)."
        )
        return report

    _moments, heat, indoor, outdoor_arr = grid
    report.samples = len(_moments)
    dt_hours = step_minutes / 60.0
    keys = [room.key for room in usable]
    names = {room.key: room.name for room in usable}
    climates = {room.key: room.climate_entity for room in usable}

    best: dict[str, dict[str, tuple[float, float]]] = {
        actuator: {response: (0.0, 0.0) for response in keys} for actuator in keys
    }

    for lag in range(MIN_LAG_STEPS, MAX_LAG_STEPS + 1):
        lag_hours = lag * dt_hours
        for response in keys:
            residual = _residual_rise(indoor[response], outdoor_arr, lag, dt_hours)
            for actuator in keys:
                call = heat[actuator][:-lag]
                if float(np.ptp(call)) < MIN_CALL_SPAN:
                    continue
                score = _pearson(call, residual)
                previous, _ = best[actuator][response]
                if score > previous:
                    best[actuator][response] = (score, lag_hours)

    report.matrix = {
        actuator: {response: score for response, (score, _) in responses.items()}
        for actuator, responses in best.items()
    }

    for actuator in keys:
        ranked = sorted(
            ((response, score, lag) for response, (score, lag) in best[actuator].items()),
            key=lambda item: item[1],
            reverse=True,
        )
        best_response, best_score, best_lag = ranked[0]
        own_score = best[actuator][actuator][0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best_score - (own_score if best_response != actuator else second)

        if float(np.ptp(heat[actuator])) < MIN_CALL_SPAN:
            verdict = RoomVerdict(
                room_key=actuator,
                room_name=names[actuator],
                climate_entity=climates[actuator],
                best_response_key=None,
                best_score=0.0,
                own_score=own_score,
                margin=0.0,
                best_lag_hours=None,
                status="insufficient_data",
                message=(
                    f"{names[actuator]}: börvärdet har knappt rört sig — "
                    "ingen excitation att korrelera mot."
                ),
            )
        elif best_score < MIN_SCORE:
            verdict = RoomVerdict(
                room_key=actuator,
                room_name=names[actuator],
                climate_entity=climates[actuator],
                best_response_key=best_response,
                best_score=best_score,
                own_score=own_score,
                margin=margin,
                best_lag_hours=best_lag,
                status="weak",
                message=(
                    f"{names[actuator]}: svag koppling till alla rum "
                    f"(bäst {names[best_response]} @ {best_score:.2f}). "
                    "Behöver mer variation eller kallare väder."
                ),
            )
        elif best_response == actuator and (best_score - second) >= MIN_MARGIN:
            verdict = RoomVerdict(
                room_key=actuator,
                room_name=names[actuator],
                climate_entity=climates[actuator],
                best_response_key=best_response,
                best_score=best_score,
                own_score=own_score,
                margin=best_score - second,
                best_lag_hours=best_lag,
                status="ok",
                message=(
                    f"{names[actuator]}: termostaten värmer eget rum "
                    f"(score {best_score:.2f}, lag {best_lag:.1f} h)."
                ),
            )
        elif best_response != actuator and (best_score - own_score) >= MIN_MARGIN:
            verdict = RoomVerdict(
                room_key=actuator,
                room_name=names[actuator],
                climate_entity=climates[actuator],
                best_response_key=best_response,
                best_score=best_score,
                own_score=own_score,
                margin=best_score - own_score,
                best_lag_hours=best_lag,
                status="suspect_swap",
                message=(
                    f"{names[actuator]}: anropet värmer {names[best_response]} "
                    f"({best_score:.2f}) mer än eget rum ({own_score:.2f}). "
                    "Misstänkt felkopplad slinga."
                ),
            )
        else:
            verdict = RoomVerdict(
                room_key=actuator,
                room_name=names[actuator],
                climate_entity=climates[actuator],
                best_response_key=best_response,
                best_score=best_score,
                own_score=own_score,
                margin=margin,
                best_lag_hours=best_lag,
                status="weak",
                message=(
                    f"{names[actuator]}: otydlig vinnare "
                    f"(bäst {names[best_response]} {best_score:.2f}, "
                    f"eget {own_score:.2f})."
                ),
            )
        report.rooms.append(verdict)

    report.suggested_swaps = _suggest_swaps(report.rooms, names)
    if report.suggested_swaps:
        report.notes.append(
            "Föreslagna byten byter bara climate_entity i hemopts profil — "
            "de rör inte den fysiska ventilen. Kontrollera i fördelarskåpet "
            "innan du litar på resultatet."
        )
    elif any(room.status == "ok" for room in report.rooms):
        report.notes.append("Inga tydliga korskopplingar hittades i den här perioden.")

    return report


def _suggest_swaps(verdicts: list[RoomVerdict], names: dict[str, str]) -> list[SuggestedSwap]:
    """Find reciprocal (or near-reciprocal) mis-pairings worth proposing."""
    by_key = {verdict.room_key: verdict for verdict in verdicts}
    suspects = [
        verdict
        for verdict in verdicts
        if verdict.status == "suspect_swap" and verdict.best_response_key
    ]
    swaps: list[SuggestedSwap] = []
    seen: set[frozenset[str]] = set()

    for verdict in suspects:
        partner_key = verdict.best_response_key
        if partner_key is None or partner_key not in by_key:
            continue
        pair = frozenset({verdict.room_key, partner_key})
        if pair in seen:
            continue
        partner = by_key[partner_key]
        if partner.best_response_key == verdict.room_key and partner.status in {
            "suspect_swap",
            "weak",
            "ok",
        }:
            confidence = min(1.0, (verdict.margin + max(partner.margin, 0.0)) / 0.4)
            swaps.append(
                SuggestedSwap(
                    room_a=verdict.room_key,
                    room_b=partner_key,
                    confidence=confidence,
                    reason=(
                        f"{names[verdict.room_key]} värmer {names[partner_key]} och "
                        f"tvärtom — byt climate_entity mellan rummen."
                    ),
                )
            )
            seen.add(pair)
            continue
        if verdict.margin >= MIN_MARGIN * 2 and verdict.best_score >= MIN_SCORE * 1.5:
            confidence = min(1.0, verdict.margin / 0.3)
            swaps.append(
                SuggestedSwap(
                    room_a=verdict.room_key,
                    room_b=partner_key,
                    confidence=confidence,
                    reason=(
                        f"{names[verdict.room_key]} värmer tydligt "
                        f"{names[partner_key]}; ensidigt tecken på felkoppling."
                    ),
                )
            )
            seen.add(pair)

    return swaps


def apply_climate_swaps(
    rooms: list[Any],
    swaps: list[SuggestedSwap],
    *,
    min_confidence: float = 0.35,
) -> list[dict[str, str]]:
    """Swap climate_entity between room configs for high-confidence pairs.

    `rooms` is the live Config.rooms list (mutated in place). Returns the
    applied swaps for logging / API response.
    """
    by_key = {room.key: room for room in rooms}
    applied: list[dict[str, str]] = []
    for swap in swaps:
        if swap.confidence < min_confidence:
            continue
        room_a = by_key.get(swap.room_a)
        room_b = by_key.get(swap.room_b)
        if room_a is None or room_b is None:
            continue
        climate_a = room_a.climate_entity
        climate_b = room_b.climate_entity
        if not climate_a or not climate_b or climate_a == climate_b:
            continue
        room_a.climate_entity = climate_b
        room_b.climate_entity = climate_a
        applied.append(
            {
                "room_a": swap.room_a,
                "room_b": swap.room_b,
                "climate_a_was": climate_a,
                "climate_b_was": climate_b,
            }
        )
    return applied


def format_report(report: LoopMappingReport) -> str:
    """Human-readable CLI summary."""
    lines = [
        "Loop-mapping (tillfällig diagnostik)",
        f"samples={report.samples}  history_days={report.history_days}  "
        f"step={report.step_minutes} min",
        "",
    ]
    for room in report.rooms:
        lines.append(f"[{room.status:16s}] {room.message}")
    if report.suggested_swaps:
        lines.append("")
        lines.append("Föreslagna byten av climate_entity:")
        for swap in report.suggested_swaps:
            lines.append(
                f"  {swap.room_a} <-> {swap.room_b}  "
                f"confidence={swap.confidence:.2f}  ({swap.reason})"
            )
    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"note  {note}")
    return "\n".join(lines)
