"""Real-time peak guard.

The planner works on a 15-minute grid and reruns every few minutes, which is
too slow to catch an oven and a dishwasher starting together. This guard runs
every minute against the live meter and answers one question: given what the
clock hour has already banked, may the heat pump keep running?

Its decisions are enforced through the heat pump's EXT input rather than
through setpoints, because a thermostat is free to ignore a setpoint and an
external input is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import ExtControlConfig
from .peaks import HourAccumulator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuardDecision:
    block: bool
    reason: str
    allowed_kw: float | None = None


@dataclass(slots=True)
class PeakGuard:
    """Hysteretic on/off decision for the EXT heating block."""

    config: ExtControlConfig
    blocked_since: datetime | None = None
    released_at: datetime | None = None
    last_reason: str = "inactive"

    def evaluate(
        self,
        now: datetime,
        accumulator: HourAccumulator,
        threshold_kw: float,
        in_peak_window: bool,
        heat_pump_kw: float,
        coldest_room_c: float | None,
    ) -> GuardDecision:
        """Decide whether the heat pump should be held off right now.

        Always evaluated, even when EXT actuation is off, so the decision can
        be watched in Home Assistant for a few days before the cable is
        allowed to act on it. `ExtControlConfig.enabled` gates the actuation,
        not the reasoning.
        """
        if not in_peak_window or threshold_kw <= 0:
            return self._release(now, "outside the billed window")

        # Comfort always outranks the tariff. One expensive hour costs a few
        # tens of kronor; a cold house costs a lot more than that.
        if coldest_room_c is not None and coldest_room_c < self.config.min_room_temperature:
            return self._release(now, f"room down to {coldest_room_c:.1f} °C")

        if self.blocked_since is not None:
            held = (now - self.blocked_since).total_seconds() / 60.0
            if held >= self.config.max_block_minutes:
                return self._release(now, f"block hit the {self.config.max_block_minutes} min cap")

        allowed = accumulator.allowed_kw(now, threshold_kw)

        if self.blocked_since is None:
            if self.released_at is not None:
                since_release = (now - self.released_at).total_seconds() / 60.0
                if since_release < self.config.min_release_minutes:
                    return GuardDecision(False, "waiting out the minimum release", allowed)
            # Blocking only helps if the pump is actually drawing something.
            if heat_pump_kw > 0.1 and allowed < heat_pump_kw:
                return self._block(now, f"only {allowed:.1f} kW left this hour", allowed)
            return GuardDecision(False, "within budget", allowed)

        # Already blocking: hold until there is real headroom, so the pump does
        # not chatter on and off around the threshold.
        if allowed > heat_pump_kw + 0.3:
            return self._release(now, "headroom recovered", allowed)
        return GuardDecision(True, self.last_reason, allowed)

    def _block(self, now: datetime, reason: str, allowed: float | None = None) -> GuardDecision:
        if self.blocked_since is None:
            self.blocked_since = now
            _LOGGER.info("peak guard engaged: %s", reason)
        self.last_reason = reason
        return GuardDecision(True, reason, allowed)

    def _release(self, now: datetime, reason: str, allowed: float | None = None) -> GuardDecision:
        if self.blocked_since is not None:
            self.released_at = now
            self.blocked_since = None
            _LOGGER.info("peak guard released: %s", reason)
        self.last_reason = reason
        return GuardDecision(False, reason, allowed)

    def blocked_for(self, now: datetime) -> timedelta:
        if self.blocked_since is None:
            return timedelta(0)
        return now - self.blocked_since
