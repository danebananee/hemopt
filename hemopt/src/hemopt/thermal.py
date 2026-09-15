"""Per-room thermal model identification.

Each room is treated as a first-order RC network:

    dT/dt = (T_out - T_in) / tau + k_heat * u + k_gain

`tau` is the thermal time constant the user asked about, the house's inertia
in hours. `k_heat` is how fast the room climbs with the loop fully open and
`k_gain` collects solar and internal gains. All three are identified from
recorded history by least squares, which stays linear because the discretised
form is linear in the parameters.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

_LOGGER = logging.getLogger(__name__)

# Physically plausible envelope for a heated Swedish house. Fits outside this
# range mean the data was too thin or too quiet, not that the house is exotic.
MIN_TAU_HOURS = 2.0
MAX_TAU_HOURS = 400.0
MIN_HEAT_RATE = 0.02
MAX_HEAT_RATE = 6.0


@dataclass(frozen=True, slots=True)
class ThermalModel:
    """Identified inertia and heating authority for one room."""

    tau_hours: float
    k_heat_per_hour: float
    k_gain_per_hour: float
    r_squared: float
    samples: int
    fitted: bool

    @classmethod
    def default(cls) -> ThermalModel:
        """Conservative prior for a concrete-slab underfloor-heated room."""
        return cls(
            tau_hours=60.0,
            k_heat_per_hour=0.35,
            k_gain_per_hour=0.0,
            r_squared=0.0,
            samples=0,
            fitted=False,
        )

    def step(self, indoor: float, outdoor: float, heat_fraction: float, dt_hours: float) -> float:
        """Advance the room one step. Mirrors the optimiser's constraint exactly."""
        drift = (outdoor - indoor) / self.tau_hours
        return indoor + dt_hours * (
            drift + self.k_heat_per_hour * heat_fraction + self.k_gain_per_hour
        )

    def coefficients(self, dt_hours: float) -> tuple[float, float, float]:
        """Discrete coefficients (a, b, c) for T+ = T + a(Tout - T) + b*u + c."""
        return (
            dt_hours / self.tau_hours,
            dt_hours * self.k_heat_per_hour,
            dt_hours * self.k_gain_per_hour,
        )

    def free_fall_hours(self, indoor: float, outdoor: float, floor: float) -> float:
        """Hours of coasting before the room falls to `floor` with no heat.

        This is what makes pre-heating safe to plan: it bounds how long a room
        can ride through an expensive block.
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


def _nnls_3(design: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Least squares with all coefficients constrained non-negative.

    With only three parameters the active set can be enumerated exhaustively,
    which is both exact and shorter than a general NNLS implementation.
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


def identify(
    samples: list[ThermalSample], step_minutes: int = 15, prior: ThermalModel | None = None
) -> ThermalModel:
    """Fit the RC model to recorded history.

    Falls back to `prior` when the data cannot support a fit, which is the
    normal state for the first days after installation.
    """
    fallback = prior or ThermalModel.default()
    segments = resample(samples, step_minutes)
    dt_hours = step_minutes / 60.0

    rows: list[list[float]] = []
    targets: list[float] = []
    for segment in segments:
        for current, following in zip(segment, segment[1:], strict=False):
            rows.append(
                [
                    current.outdoor - current.indoor,
                    current.heat_fraction,
                    1.0,
                ]
            )
            targets.append(following.indoor - current.indoor)

    if len(rows) < 96:
        _LOGGER.info("thermal fit skipped, only %d usable transitions", len(rows))
        return fallback

    design = np.asarray(rows, dtype=float)
    target = np.asarray(targets, dtype=float)

    # A room whose loop never modulated carries no information about k_heat.
    if float(np.ptp(design[:, 1])) < 0.05:
        _LOGGER.info("thermal fit skipped, heat input never varied")
        return fallback

    coefficients = _nnls_3(design, target)
    a, b, c = (float(v) for v in coefficients)

    if a <= 1e-9:
        _LOGGER.info("thermal fit rejected, no measurable heat loss")
        return fallback

    tau_hours = dt_hours / a
    k_heat = b / dt_hours
    k_gain = c / dt_hours

    if not MIN_TAU_HOURS <= tau_hours <= MAX_TAU_HOURS:
        _LOGGER.info("thermal fit rejected, tau %.1f h out of range", tau_hours)
        return fallback
    if not MIN_HEAT_RATE <= k_heat <= MAX_HEAT_RATE:
        _LOGGER.info("thermal fit rejected, heat rate %.3f K/h out of range", k_heat)
        return fallback

    residual = target - design @ coefficients
    variance = float(np.sum((target - target.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residual**2)) / variance if variance > 0 else 0.0

    return ThermalModel(
        tau_hours=tau_hours,
        k_heat_per_hour=k_heat,
        k_gain_per_hour=k_gain,
        r_squared=r_squared,
        samples=len(rows),
        fitted=True,
    )
