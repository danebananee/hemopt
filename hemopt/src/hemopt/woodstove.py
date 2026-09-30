"""Wood stove (braskamin) detection, learning and lighting advice.

The stove is not controlled by hemopt — the household lights it. What the
model can do is notice when it is burning, learn how hard it pushes nearby
rooms, and point at the expensive cold hours where a fire would displace the
most heat-pump work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
    # Heat pump electricity a fire in this stretch would save, in SEK. Only
    # known once the stove's effect has been learnt.
    saving_sek: float | None = None


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
                    "saving_sek": None
                    if window.saving_sek is None
                    else round(window.saving_sek, 1),
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
    stove_kw: float | None = None,
    displaceable_kw: list[float] | None = None,
    cop: list[float] | None = None,
    awake_hours: tuple[int, int] = (7, 23),
) -> list[WoodStoveWindow]:
    """Pick the plan stretches where a fire displaces the most expensive heat.

    Once the stove's effect is known (`stove_kw`, in heat pump kW it can
    replace) and the plan says how much heat the stove's rooms will get
    (`displaceable_kw`), each step is worth what the heat pump would have
    paid for the heat the fire replaces: min(stove, planned) / COP × price.
    Before that, a heuristic of price, cold and heat pump demand ranks them.
    Contiguous high-scoring steps are merged into windows; the best few are
    returned. Only `awake_hours` count: nobody lights a fire at three in the
    morning, however cold and expensive it is.
    """
    if not times or len(times) != len(price_sek):
        return []

    dt = step_minutes / 60.0
    learnt = (
        stove_kw is not None
        and stove_kw > 0
        and displaceable_kw is not None
        and cop is not None
        and len(displaceable_kw) == len(times)
        and len(cop) == len(times)
    )
    scores: list[float] = []
    savings: list[float] = []
    for index, (price, outdoor, pump) in enumerate(
        zip(price_sek, outdoor_c, heat_pump_kw, strict=True)
    ):
        if not awake_hours[0] <= times[index].hour < awake_hours[1]:
            scores.append(0.0)
            savings.append(0.0)
            continue
        if learnt:
            displaced = min(stove_kw, max(displaceable_kw[index], 0.0))  # type: ignore[index,type-var]
            saving = displaced / max(cop[index], 1e-6) * price * dt  # type: ignore[index]
            savings.append(saving)
            scores.append(saving / dt)
        else:
            cold = max(0.0, 5.0 - outdoor) / 10.0  # 0 at +5 °C, 1 at -5 °C
            demand = max(0.0, pump)
            scores.append(price * (0.4 + 0.6 * cold) * (0.3 + 0.7 * min(demand / 3.0, 1.0)))

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
        span = slice(start, end)
        count = end - start
        windows.append(
            WoodStoveWindow(
                start=times[start],
                end=times[end - 1] + timedelta(minutes=step_minutes),
                mean_price_sek=sum(price_sek[span]) / count,
                mean_outdoor_c=sum(outdoor_c[span]) / count,
                mean_heat_pump_kw=sum(heat_pump_kw[span]) / count,
                score=sum(scores[span]) / count,
                saving_sek=sum(savings[span]) if learnt else None,
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
        report.summary = "Braskaminen är avstängd i konfigurationen."
        return report

    if not report.configured:
        report.status = "need_sensor"
        report.summary = "Koppla en givare så att hemopt ser när brasan brinner."
        report.detail = (
            "Ange temperature_entity (en givare på eller nära kaminen) eller "
            "binary_entity under wood_stove i hemopt.yaml. Utan den går det inte "
            "att lära sig hur mycket brasan värmer."
        )
        return report

    learnt = [e for e in effects if e.k_stove_per_hour > 0.02]

    if reading.lit:
        report.status = "lit"
        report.summary = f"{config.name} brinner."
        parts = [f"{e.room_name} +{e.k_stove_per_hour:.2f} °C/h" for e in learnt]
        sensor = ""
        if reading.sensor_c is not None:
            sensor = f"Givaren visar {reading.sensor_c:.0f} °C. "
        learnt_text = "Uppmätt värme från brasan: " + ", ".join(parts) + "." if parts else ""
        report.detail = (sensor + learnt_text).strip()
        return report

    if hours_lit_observed < 4 or not learnt:
        report.status = "need_data"
        report.summary = "hemopt lär sig fortfarande hur mycket brasan värmer."
        report.detail = (
            f"{hours_lit_observed:.0f} timmar med brasa observerade, helst 4–8 timmar "
            "fördelat på några kvällar. Därefter får varje rum ett uppmätt bidrag och "
            "tipsen nedan en uppskattad besparing i kronor."
        )
        return report

    if windows:
        first = windows[0]
        report.status = "recommend"
        saving = (
            f", sparar omkring {first.saving_sek:.0f} kr värmepumpsel"
            if first.saving_sek is not None and first.saving_sek >= 1
            else ""
        )
        report.summary = (
            f"Tänd gärna {first.start.strftime('%H:%M')}–{first.end.strftime('%H:%M')}{saving}."
        )
        eq = sum(e.equivalent_kw or 0.0 for e in learnt)
        rooms = ", ".join(e.room_name for e in learnt)
        report.detail = (
            f"Då är elen dyr ({first.mean_price_sek * 100:.0f} öre/kWh) och det är "
            f"{first.mean_outdoor_c:.0f} °C ute. Brasan ersätter ungefär {eq:.1f} kW "
            f"golvvärme i {rooms}."
        )
        return report

    report.status = "quiet"
    report.summary = "Ingen kväll sticker ut just nu."
    report.detail = (
        "Elpriset är jämnt eller värmebehovet lågt de närmaste 36 timmarna, så en brasa "
        "sparar inte mycket. Tänd för mysets skull."
    )
    return report
