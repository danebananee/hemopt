"""Per-room thermal model identification.

Each room is a first-order RC network for the air, fed through a first-order
lag that stands in for the floor:

    slab:  ds/dt = (u - s) / tau_slab
    air:   dT/dt = (T_out - T_in) / tau + k_heat * s + k_stove * stove + k_gain

`u` is how open the room's loop is and `s` is the heat the floor actually
hands to the room. With `tau_slab = 0` the floor is instantaneous and the model
reduces to the classic single-node RC room.

The lag matters for underfloor heating. A concrete slab takes hours to charge
and keeps giving heat after the loop closes; chipboard under a timber floor
responds within the hour. A model without the lag pre-heats too late before an
expensive block and keeps heating too long into it, and it cannot see the slab
as the heat store it is.

`tau` is the room's inertia in hours, `k_heat` how fast it climbs with the
floor fully charged, `k_stove` the extra climb while a wood stove burns and
`k_gain` solar and internal gains. For every candidate floor lag the rest is
fitted by non-negative least squares on one-step differences; the lag itself is
chosen by how well the fitted model predicts four hours ahead, which is the
question the planner actually asks of it.
"""

from __future__ import annotations

import itertools
import logging
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

_LOGGER = logging.getLogger(__name__)

FloorType = Literal["concrete", "light"]

# Physically plausible envelope for a heated Swedish house. Fits outside this
# range mean the data was too thin or too quiet, not that the house is exotic.
MIN_TAU_HOURS = 2.0
MAX_TAU_HOURS = 400.0
MIN_HEAT_RATE = 0.02
MAX_HEAT_RATE = 6.0

# Floor lags tried during identification, in hours.
SLAB_CANDIDATES_HOURS = (0.0, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)

# How far ahead the lag is judged, and how often a validation window starts.
VALIDATION_HORIZON_HOURS = 4.0
VALIDATION_STRIDE_HOURS = 1.0

# Starting points before any history has been fitted. Concrete slabs are slow
# to charge and slow to give up their heat; chipboard and timber are not.
FLOOR_PRIORS: dict[str, dict[str, float]] = {
    "concrete": {"tau_hours": 60.0, "k_heat_per_hour": 0.35, "tau_slab_hours": 3.0},
    "light": {"tau_hours": 45.0, "k_heat_per_hour": 0.5, "tau_slab_hours": 0.75},
}


def slab_alpha(tau_slab_hours: float, dt_hours: float) -> float:
    """Fraction of the gap between loop and floor closed in one step (exact)."""
    if tau_slab_hours <= 0:
        return 1.0
    return 1.0 - math.exp(-dt_hours / tau_slab_hours)


@dataclass(frozen=True, slots=True)
class ThermalModel:
    """Identified inertia, floor lag and heating authority for one room."""

    tau_hours: float
    k_heat_per_hour: float
    k_gain_per_hour: float
    r_squared: float
    samples: int
    fitted: bool
    k_stove_per_hour: float = 0.0
    tau_slab_hours: float = 0.0
    # Root-mean-square error of a four-hour open-loop prediction, in kelvin.
    # This is the number that says whether the plan can be trusted.
    rmse_4h: float | None = None

    @classmethod
    def default(cls, floor_type: str | None = None) -> ThermalModel:
        """Conservative prior for the given floor construction."""
        prior = FLOOR_PRIORS.get(floor_type or "concrete", FLOOR_PRIORS["concrete"])
        return cls(
            tau_hours=prior["tau_hours"],
            k_heat_per_hour=prior["k_heat_per_hour"],
            k_gain_per_hour=0.0,
            r_squared=0.0,
            samples=0,
            fitted=False,
            k_stove_per_hour=0.0,
            tau_slab_hours=prior["tau_slab_hours"],
        )

    def slab_alpha(self, dt_hours: float) -> float:
        return slab_alpha(self.tau_slab_hours, dt_hours)

    def advance(
        self,
        indoor: float,
        slab: float,
        outdoor: float,
        heat_fraction: float,
        dt_hours: float,
        stove_on: float = 0.0,
    ) -> tuple[float, float]:
        """Advance air and floor one step. Mirrors the optimiser's constraints."""
        slab_next = slab + self.slab_alpha(dt_hours) * (heat_fraction - slab)
        drift = (outdoor - indoor) / self.tau_hours
        indoor_next = indoor + dt_hours * (
            drift
            + self.k_heat_per_hour * slab_next
            + self.k_stove_per_hour * stove_on
            + self.k_gain_per_hour
        )
        return indoor_next, slab_next

    def step(
        self,
        indoor: float,
        outdoor: float,
        heat_fraction: float,
        dt_hours: float,
        stove_on: float = 0.0,
    ) -> float:
        """Advance the air one step with the floor treated as instantaneous."""
        indoor_next, _ = self.advance(
            indoor, heat_fraction, outdoor, heat_fraction, dt_hours, stove_on
        )
        return indoor_next

    def coefficients(self, dt_hours: float) -> tuple[float, float, float]:
        """Discrete coefficients (a, b, c) for T+ = T + a(Tout - T) + b*s+ + c."""
        return (
            dt_hours / self.tau_hours,
            dt_hours * self.k_heat_per_hour,
            dt_hours * self.k_gain_per_hour,
        )

    def steady_heat_fraction(self, indoor: float, outdoor: float) -> float:
        """Floor output that holds the room where it is.

        The best estimate of the slab's state when nothing better is known:
        a room that is neither warming nor cooling is being fed exactly this.
        """
        if self.k_heat_per_hour <= 0:
            return 0.0
        needed = (indoor - outdoor) / self.tau_hours - self.k_gain_per_hour
        return max(0.0, min(1.0, needed / self.k_heat_per_hour))

    def stored_heat_hours(self) -> float:
        """Hours of full floor output held in a fully charged slab."""
        return max(self.tau_slab_hours, 0.0)

    def free_fall_hours(self, indoor: float, outdoor: float, floor: float) -> float:
        """Hours of coasting before the room falls to `floor` with no heat.

        This is what makes pre-heating safe to plan: it bounds how long a room
        can ride through an expensive block. The heat still in the slab is
        ignored, so the answer errs on the short side.
        """
        if indoor <= floor:
            return 0.0
        if outdoor >= floor:
            return math.inf
        ratio = (floor - outdoor) / (indoor - outdoor)
        if ratio <= 0:
            return math.inf
        return -self.tau_hours * math.log(ratio)


@dataclass(slots=True)
class ThermalSample:
    moment: datetime
    indoor: float
    outdoor: float
    heat_fraction: float
    stove_on: float = 0.0


def _nnls_n(design: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Least squares with all coefficients constrained non-negative.

    With only a handful of parameters the active set can be enumerated
    exhaustively, which is both exact and shorter than a general NNLS.
    """
    best_solution = np.zeros(design.shape[1])
    best_error = math.inf
    indices = range(design.shape[1])

    for size in range(design.shape[1], -1, -1):
        for active in itertools.combinations(indices, size):
            candidate = np.zeros(design.shape[1])
            if active:
                sub = design[:, list(active)]
                solution, *_ = np.linalg.lstsq(sub, target, rcond=None)
                if np.any(solution < -1e-12):
                    continue
                candidate[list(active)] = np.maximum(solution, 0.0)
            error = float(np.sum((design @ candidate - target) ** 2))
            if error < best_error - 1e-15:
                best_error = error
                best_solution = candidate

    return best_solution


# Backwards-compatible alias used by older tests/helpers.
_nnls_3 = _nnls_n


def resample(
    samples: list[ThermalSample], step_minutes: int, max_gap_minutes: int = 90
) -> list[list[ThermalSample]]:
    """Split history into uniformly spaced, gap-free segments.

    Identification differences consecutive samples, so a segment must not span
    a logging gap or the difference is meaningless.
    """
    if not samples:
        return []

    ordered = sorted(samples, key=lambda s: s.moment)
    step = timedelta(minutes=step_minutes)
    max_gap = timedelta(minutes=max_gap_minutes)

    segments: list[list[ThermalSample]] = []
    current: list[ThermalSample] = []
    cursor = ordered[0].moment
    index = 0
    latest: ThermalSample | None = None

    while cursor <= ordered[-1].moment:
        while index < len(ordered) and ordered[index].moment <= cursor:
            latest = ordered[index]
            index += 1

        if latest is not None and cursor - latest.moment <= max_gap:
            current.append(
                ThermalSample(
                    moment=cursor,
                    indoor=latest.indoor,
                    outdoor=latest.outdoor,
                    heat_fraction=latest.heat_fraction,
                    stove_on=latest.stove_on,
                )
            )
        else:
            if len(current) > 1:
                segments.append(current)
            current = []
        cursor += step

    if len(current) > 1:
        segments.append(current)
    return segments


@dataclass(slots=True)
class _Segment:
    indoor: np.ndarray
    outdoor: np.ndarray
    heat: np.ndarray
    stove: np.ndarray

    @classmethod
    def of(cls, samples: list[ThermalSample]) -> _Segment:
        return cls(
            indoor=np.array([s.indoor for s in samples], dtype=float),
            outdoor=np.array([s.outdoor for s in samples], dtype=float),
            heat=np.array([s.heat_fraction for s in samples], dtype=float),
            stove=np.array([s.stove_on for s in samples], dtype=float),
        )

    def slab(self, alpha: float) -> np.ndarray:
        """Floor state after each step's input, starting from steady input.

        Element i is the floor output during the transition i -> i+1.
        """
        state = float(self.heat[0])
        out = np.empty(len(self.heat), dtype=float)
        for index, value in enumerate(self.heat):
            state += alpha * (float(value) - state)
            out[index] = state
        return out


@dataclass(slots=True)
class _Fit:
    tau_slab_hours: float
    a: float
    b: float
    s: float
    c: float
    r_squared: float
    rmse: float
    rows: int


def _fit_for_lag(
    segments: list[_Segment],
    dt_hours: float,
    tau_slab_hours: float,
    use_stove: bool,
) -> _Fit | None:
    alpha = slab_alpha(tau_slab_hours, dt_hours)
    rows: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    slabs: list[np.ndarray] = []
    for segment in segments:
        slab = segment.slab(alpha)
        slabs.append(slab)
        # Skip the first steps of each segment while the assumed initial floor
        # state is still settling towards the real one.
        warmup = min(int(math.ceil(2.0 * tau_slab_hours / dt_hours)), len(segment.indoor) - 1)
        index = np.arange(warmup, len(segment.indoor) - 1)
        if index.size == 0:
            continue
        rows.append(
            np.column_stack(
                [
                    segment.outdoor[index] - segment.indoor[index],
                    slab[index],
                    segment.stove[index],
                    np.ones(index.size),
                ]
            )
        )
        targets.append(segment.indoor[index + 1] - segment.indoor[index])

    if not rows:
        return None
    design = np.vstack(rows)
    target = np.concatenate(targets)

    if use_stove:
        a, b, s, c = (float(v) for v in _nnls_n(design, target))
    else:
        # Drop the stove column so quiet data does not invent a fake k_stove.
        a, b, c = (float(v) for v in _nnls_n(design[:, [0, 1, 3]], target))
        s = 0.0

    if a <= 1e-9:
        return None

    coefficients = np.array([a, b, s, c])
    residual = target - design @ coefficients
    variance = float(np.sum((target - target.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residual**2)) / variance if variance > 0 else 0.0

    rmse = _multi_step_rmse(segments, slabs, dt_hours, a, b, s, c)
    return _Fit(
        tau_slab_hours=tau_slab_hours,
        a=a,
        b=b,
        s=s,
        c=c,
        r_squared=r_squared,
        rmse=rmse,
        rows=int(design.shape[0]),
    )


def _multi_step_rmse(
    segments: list[_Segment],
    slabs: list[np.ndarray],
    dt_hours: float,
    a: float,
    b: float,
    s: float,
    c: float,
) -> float:
    """Open-loop prediction error at the validation horizon.

    The room is started from a measured temperature and run forward on the
    recorded outdoor temperature and loop signal only, the way the planner
    uses it. The floor state comes from the recorded loop signal, which is
    known, so only the room itself is being predicted.
    """
    horizon = max(int(round(VALIDATION_HORIZON_HOURS / dt_hours)), 1)
    stride = max(int(round(VALIDATION_STRIDE_HOURS / dt_hours)), 1)
    errors: list[float] = []
    for segment, slab in zip(segments, slabs, strict=True):
        last_start = len(segment.indoor) - 1 - horizon
        for start in range(0, last_start + 1, stride):
            temperature = float(segment.indoor[start])
            for index in range(start, start + horizon):
                temperature += (
                    a * (float(segment.outdoor[index]) - temperature)
                    + b * float(slab[index])
                    + s * float(segment.stove[index])
                    + c
                )
            errors.append(temperature - float(segment.indoor[start + horizon]))
    if not errors:
        return math.inf
    return float(np.sqrt(np.mean(np.square(errors))))


def identify(
    samples: list[ThermalSample],
    step_minutes: int = 15,
    prior: ThermalModel | None = None,
    floor_type: str | None = None,
) -> ThermalModel:
    """Fit the room and floor model to recorded history.

    Falls back to `prior` when the data cannot support a fit, which is the
    normal state for the first days after installation.
    """
    fallback = prior or ThermalModel.default(floor_type)
    expected_lag = FLOOR_PRIORS.get(floor_type or "concrete", FLOOR_PRIORS["concrete"])[
        "tau_slab_hours"
    ]
    dt_hours = step_minutes / 60.0
    segments = [_Segment.of(segment) for segment in resample(samples, step_minutes)]
    transitions = sum(len(segment.indoor) - 1 for segment in segments)

    if transitions < 96:
        _LOGGER.info("thermal fit skipped, only %d usable transitions", transitions)
        return fallback

    heat = np.concatenate([segment.heat for segment in segments])
    # A room whose loop never modulated carries no information about k_heat.
    if float(np.ptp(heat)) < 0.05:
        _LOGGER.info("thermal fit skipped, heat input never varied")
        return fallback

    stove = np.concatenate([segment.stove for segment in segments])
    use_stove = float(np.ptp(stove)) >= 0.05

    best: _Fit | None = None
    best_score = math.inf
    for lag in SLAB_CANDIDATES_HOURS:
        fit = _fit_for_lag(segments, dt_hours, lag, use_stove)
        if fit is None or not math.isfinite(fit.rmse):
            continue
        tau_hours = dt_hours / fit.a
        k_heat = fit.b / dt_hours
        if not MIN_TAU_HOURS <= tau_hours <= MAX_TAU_HOURS:
            continue
        if not MIN_HEAT_RATE <= k_heat <= MAX_HEAT_RATE:
            continue
        # A slight pull towards what the floor construction suggests, so two
        # lags that predict equally well are settled by physics, not noise.
        pull = 1.0 + 0.02 * abs(math.log((lag + 0.25) / (expected_lag + 0.25)))
        score = fit.rmse * pull
        if score < best_score:
            best, best_score = fit, score

    if best is None:
        _LOGGER.info("thermal fit rejected, no floor lag gave a plausible room")
        return fallback

    k_stove = best.s / dt_hours
    if k_stove > MAX_HEAT_RATE:
        _LOGGER.info("thermal fit rejected, stove rate %.3f K/h out of range", k_stove)
        return fallback

    return ThermalModel(
        tau_hours=dt_hours / best.a,
        k_heat_per_hour=best.b / dt_hours,
        k_gain_per_hour=best.c / dt_hours,
        r_squared=best.r_squared,
        samples=best.rows,
        fitted=True,
        k_stove_per_hour=k_stove,
        tau_slab_hours=best.tau_slab_hours,
        rmse_4h=round(best.rmse, 3),
    )


def nearest_value(
    values: dict[datetime, float],
    ordered: list[datetime],
    moment: datetime,
    tolerance_s: float = 3600.0,
) -> float | None:
    """Value of the reading closest in time to `moment`, within a tolerance.

    `ordered` must be the sorted keys of `values`. Binary search keeps model
    training linear-logarithmic; a linear scan per sample made a three-week
    refit quadratic, which is minutes of CPU on a Raspberry Pi.
    """
    if not ordered:
        return None
    position = bisect_left(ordered, moment)
    candidates = []
    if position < len(ordered):
        candidates.append(ordered[position])
    if position > 0:
        candidates.append(ordered[position - 1])
    best = min(candidates, key=lambda t: abs((t - moment).total_seconds()))
    if abs((best - moment).total_seconds()) > tolerance_s:
        return None
    return values[best]


def previous_value(
    values: dict[datetime, float],
    ordered: list[datetime],
    moment: datetime,
    max_age_s: float = 24 * 3600.0,
) -> float | None:
    """Last reading at or before `moment` — the right lookup for a setpoint.

    A setpoint holds until it is changed, so the value in force is the most
    recent one, not whichever change happens to be closest in time.
    """
    if not ordered:
        return None
    position = bisect_left(ordered, moment)
    if position < len(ordered) and ordered[position] == moment:
        return values[moment]
    if position == 0:
        return None
    best = ordered[position - 1]
    if (moment - best).total_seconds() > max_age_s:
        return None
    return values[best]
