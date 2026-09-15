"""Wood stove (braskamin) detection, learning and lighting advice.

The stove is not controlled by hemopt — the household lights it. What the
model can do is notice when it is burning, learn how hard it pushes nearby
rooms, and point at the expensive cold hours where a fire would displace the
most heat-pump work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import WoodStoveConfig


@dataclass(slots=True)
class WoodStoveReading:
    """Live detection state."""

    lit: bool = False
    sensor_c: float | None = None
    source: str = "none"  # binary | temperature | none
    updated_at: datetime | None = None


@dataclass(slots=True)
class WoodStoveEffect:
    """Learned heating contribution in one room while the stove is lit."""

    room_key: str
    room_name: str
    k_stove_per_hour: float
    equivalent_kw: float | None
    tau_hours: float


@dataclass(slots=True)
class WoodStoveWindow:
    """A stretch of the plan where lighting the stove is especially useful."""

    start: datetime
    end: datetime
    mean_price_sek: float
    mean_outdoor_c: float
    mean_heat_pump_kw: float
    score: float


@dataclass(slots=True)
class WoodStoveReport:
    enabled: bool = False
    configured: bool = False
    name: str = "Braskamin"
    reading: WoodStoveReading = field(default_factory=WoodStoveReading)
    effects: list[WoodStoveEffect] = field(default_factory=list)
    windows: list[WoodStoveWindow] = field(default_factory=list)
    status: str = "disabled"  # disabled | need_sensor | need_data | lit | recommend | quiet
    summary: str = ""
    detail: str = ""
    sessions_observed: int = 0
    hours_lit_observed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "sessions_observed": self.sessions_observed,
            "hours_lit_observed": round(self.hours_lit_observed, 1),
            "reading": {
                "lit": self.reading.lit,
                "sensor_c": None
                if self.reading.sensor_c is None
                else round(self.reading.sensor_c, 1),
                "source": self.reading.source,
                "updated_at": self.reading.updated_at.isoformat()
                if self.reading.updated_at
                else None,
            },
            "effects": [
                {
                    "room_key": effect.room_key,
                    "room_name": effect.room_name,
                    "k_stove_per_hour": round(effect.k_stove_per_hour, 3),
                    "equivalent_kw": None
                    if effect.equivalent_kw is None
                    else round(effect.equivalent_kw, 2),
                    "tau_hours": round(effect.tau_hours, 1),
                }
                for effect in self.effects
            ],
            "windows": [
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "mean_price_sek": round(window.mean_price_sek, 3),
                    "mean_outdoor_c": round(window.mean_outdoor_c, 1),
                    "mean_heat_pump_kw": round(window.mean_heat_pump_kw, 2),
                    "score": round(window.score, 2),
                }
                for window in self.windows
            ],
        }


def detect_lit(
    config: WoodStoveConfig,
    *,
    binary_on: bool | None,
    temperature_c: float | None,
    previously_lit: bool,
) -> WoodStoveReading:
    """Hysteretic on/off from a binary sensor or a temperature probe."""
    if not config.enabled:
        return WoodStoveReading(lit=False, source="none")

    if binary_on is not None:
        return WoodStoveReading(lit=bool(binary_on), source="binary", sensor_c=temperature_c)

    if temperature_c is None:
        return WoodStoveReading(lit=previously_lit, source="none", sensor_c=None)

    if previously_lit:
        lit = temperature_c >= config.lit_below_c
    else:
        lit = temperature_c >= config.lit_above_c
    return WoodStoveReading(lit=lit, source="temperature", sensor_c=temperature_c)


def recommend_windows(
    *,
    times: list[datetime],
    price_sek: list[float],
    outdoor_c: list[float],
    heat_pump_kw: list[float],
    step_minutes: int,
    min_hours: float = 2.0,
    top_n: int = 3,
) -> list[WoodStoveWindow]:
    """Pick the plan stretches where a fire displaces the most expensive heat.

    Score per step: price × heat-pump demand × coldness factor. Contiguous
    high-scoring steps are merged into windows; the best few are returned.
    """
    if not times or len(times) != len(price_sek):
        return []

    scores: list[float] = []
    for price, outdoor, pump in zip(price_sek, outdoor_c, heat_pump_kw, strict=True):
        cold = max(0.0, 5.0 - outdoor) / 10.0  # 0 at +5 C, 1 at -5 C
        demand = max(0.0, pump)
        scores.append(price * (0.4 + 0.6 * cold) * (0.3 + 0.7 * min(demand / 3.0, 1.0)))

    if not scores:
        return []

    peak = max(scores)
    if peak <= 0:
        return []
    # Keep only the clearly better stretches — not everything above the median.
    threshold = max(peak * 0.55, sorted(scores)[int(0.85 * (len(scores) - 1))])
    min_steps = max(1, int(round(min_hours * 60 / max(step_minutes, 1))))

    windows: list[WoodStoveWindow] = []
    index = 0
    while index < len(scores):
        if scores[index] < threshold:
            index += 1
            continue
        start = index
        while index < len(scores) and scores[index] >= threshold * 0.85:
            index += 1
        end = index
        if end - start < min_steps:
            continue
        slice_price = price_sek[start:end]
        slice_out = outdoor_c[start:end]
        slice_pump = heat_pump_kw[start:end]
        slice_score = scores[start:end]
        end_time = times[end - 1]
        windows.append(
            WoodStoveWindow(
                start=times[start],
                end=end_time,
                mean_price_sek=sum(slice_price) / len(slice_price),
                mean_outdoor_c=sum(slice_out) / len(slice_out),
                mean_heat_pump_kw=sum(slice_pump) / len(slice_pump),
                score=sum(slice_score) / len(slice_score),
            )
        )

    windows.sort(key=lambda window: window.score, reverse=True)
    return windows[:top_n]


def build_report(
    config: WoodStoveConfig,
    reading: WoodStoveReading,
    effects: list[WoodStoveEffect],
    windows: list[WoodStoveWindow],
    *,
    sessions_observed: int = 0,
    hours_lit_observed: float = 0.0,
) -> WoodStoveReport:
    report = WoodStoveReport(
        enabled=config.enabled,
        configured=bool(config.binary_entity or config.temperature_entity),
        name=config.name,
        reading=reading,
        effects=effects,
        windows=windows,
        sessions_observed=sessions_observed,
        hours_lit_observed=hours_lit_observed,
    )

    if not config.enabled:
        report.status = "disabled"
        report.summary = "Braskaminen ar avstangd i konfigurationen."
        return report

    if not report.configured:
        report.status = "need_sensor"
        report.summary = "Koppla en givare for att detektera nar brasan ar tand."
        report.detail = (
            "Lagg temperature_entity (yta/nara brasans) eller binary_entity i "
            "wood_stove i hemopt.yaml. Utan detektion kan bidraget inte laras."
        )
        return report

    if reading.lit:
        effect_txt = ""
        if effects:
            parts = [
                f"{e.room_name} +{e.k_stove_per_hour:.2f} K/h"
                for e in effects
                if e.k_stove_per_hour > 0.02
            ]
            if parts:
                effect_txt = " Larnt bidrag: " + ", ".join(parts) + "."
        report.status = "lit"
        report.summary = f"{config.name} verkar vara tand just nu."
        report.detail = (
            f"Sensor {reading.sensor_c:.1f} C." if reading.sensor_c is not None else ""
        ) + effect_txt
        return report

    if hours_lit_observed < 4 or not effects:
        report.status = "need_data"
        report.summary = (
            f"Mer braseldning behovs for att lara inverkan "
            f"({hours_lit_observed:.0f} h observerat, helst minst 4–8 h)."
        )
        report.detail = (
            "Nar brasan varit tand i nagra sessioner far varje rum ett "
            "bidrag i K/h och en upppskattad VP-ekvivalent. Tradigheten "
            "(tau) rattas samtidigt sa att brasvarme inte blandas ihop med "
            "golvvarmen."
        )
        if windows:
            first = windows[0]
            report.detail += (
                f" Tills vidare: dyraste kallaste fonstret i planen ar "
                f"{first.start.strftime('%a %H:%M')}–{first.end.strftime('%H:%M')}."
            )
        return report

    if windows:
        first = windows[0]
        eq = sum(e.equivalent_kw or 0.0 for e in effects)
        report.status = "recommend"
        report.summary = (
            f"Bra lage att tanda kring {first.start.strftime('%H:%M')}–"
            f"{first.end.strftime('%H:%M')} "
            f"(pris {first.mean_price_sek:.2f} kr/kWh, ute {first.mean_outdoor_c:.0f} C)."
        )
        report.detail = (
            f"Larnt brasbidrag ca {eq:.1f} kW VP-ekvivalent i "
            + ", ".join(e.room_name for e in effects if (e.equivalent_kw or 0) > 0.05)
            + ". Tand da planen annars vill kora varmepumpen dyrt."
            if eq > 0.05
            else "Tand nar elpriset och VP-behovet toppar samtidigt."
        )
        return report

    report.status = "quiet"
    report.summary = "Ingen stark tandrekommendation i narmaste planfonster."
    report.detail = (
        "Priserna ar jamna eller VP-behovet lagt — brasan ger da mindre "
        "ekonomisk utdelning an en kall dyr kvall."
    )
    return report
