"""Human-readable explanations of what the optimiser is doing right now.

The solver produces numbers. Households need sentences: «laddar varmvatten
inför dyr period», «förvärmer vardagsrum», «håller tillbaka för effekttak».
"""

from __future__ import annotations

from dataclasses import dataclass

from .optimizer import Plan
from .woodstove import WoodStoveReport


@dataclass(frozen=True, slots=True)
class ModelAction:
    key: str
    title: str
    detail: str
    kind: str = "info"  # info | active | warn | tip
    when: str = "now"  # now | soon

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "kind": self.kind,
            "when": self.when,
        }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _upcoming_price_rise(plan: Plan, index: int, hours: float = 6.0) -> tuple[bool, float, float]:
    """Whether price rises meaningfully after `index` within `hours`."""
    steps = max(1, int(round(hours * 60 / max(plan.step_minutes, 1))))
    end = min(len(plan.price_sek_per_kwh), index + steps)
    if end <= index + 1:
        return False, plan.price_sek_per_kwh[index], plan.price_sek_per_kwh[index]
    now_price = plan.price_sek_per_kwh[index]
    later = plan.price_sek_per_kwh[index + 1 : end]
    peak_later = max(later)
    return peak_later >= now_price * 1.15 + 0.05, now_price, peak_later


def _upcoming_price_drop(
    plan: Plan, index: int, hours: float = 2.0
) -> tuple[bool, float, float, int]:
    """Whether a clearly cheaper slot arrives soon (e.g. within ~30–120 min).

    Returns (dropping, now_price, cheaper_price, minutes_until).
    """
    steps = max(1, int(round(hours * 60 / max(plan.step_minutes, 1))))
    end = min(len(plan.price_sek_per_kwh), index + steps + 1)
    if end <= index + 1:
        now_price = plan.price_sek_per_kwh[index]
        return False, now_price, now_price, 0
    now_price = plan.price_sek_per_kwh[index]
    best_i = index + 1
    best = plan.price_sek_per_kwh[best_i]
    for i in range(index + 1, end):
        if plan.price_sek_per_kwh[i] < best:
            best = plan.price_sek_per_kwh[i]
            best_i = i
    # Meaningful drop: at least 15% and 20 öre cheaper.
    dropping = best <= now_price * 0.85 - 0.05 or best <= now_price - 0.20
    minutes = max(plan.step_minutes, (best_i - index) * plan.step_minutes)
    return dropping, now_price, best, minutes


def explain_plan(
    plan: Plan | None,
    index: int,
    *,
    control_enabled: bool = False,
    guard_blocking: bool = False,
    guard_reason: str = "",
    wood_stove: WoodStoveReport | None = None,
) -> list[ModelAction]:
    """Build the narrative for the current step (and a couple of tips)."""
    actions: list[ModelAction] = []
    if plan is None or not plan.times:
        actions.append(
            ModelAction(
                key="no_plan",
                title="Ingen plan än",
                detail="Väntar på första beräkningen. Spotpris och rumsgivare måste finnas.",
                kind="warn",
            )
        )
        return actions

    index = max(0, min(index, len(plan.times) - 1))
    price = plan.price_sek_per_kwh[index]
    pump = plan.heat_pump_kw[index]
    rising, now_p, later_p = _upcoming_price_rise(plan, index)
    dropping, drop_now, drop_later, drop_minutes = _upcoming_price_drop(plan, index)

    if not control_enabled:
        actions.append(
            ModelAction(
                key="observe",
                title="Planerar utan att styra",
                detail=(
                    "Styr värmen är av — du ser planen men hemopt skriver inga "
                    "temperaturer till huset. Slå på «Styr värmen» när du litar på kurvan."
                ),
                kind="info",
            )
        )

    if guard_blocking:
        actions.append(
            ModelAction(
                key="peak_guard",
                title="Håller nere effekten just nu",
                detail=guard_reason
                or (
                    "Uttaget närmar sig månadens debiterbara toppar — "
                    "värmepumpen hålls tillbaka så tröskeln inte höjs."
                ),
                kind="warn",
            )
        )

    dhw = plan.hot_water
    charging = dhw is not None and dhw.charge_fraction[index] > 0.05
    if charging:
        if dropping and not rising:
            actions.append(
                ModelAction(
                    key="dhw_charge_wait",
                    title="Varmvatten — bättre att vänta",
                    detail=(
                        f"Nu {drop_now:.2f} kr/kWh, men om cirka {drop_minutes} min "
                        f"sjunker priset mot {drop_later:.2f} kr/kWh. "
                        "Fyll tanken hellre då om du kan."
                    ),
                    kind="warn",
                )
            )
        elif rising:
            actions.append(
                ModelAction(
                    key="dhw_precharge",
                    title="Laddar varmvatten inför dyrare el",
                    detail=(
                        f"Tanken fylls nu ({now_p:.2f} kr/kWh) innan priset stiger "
                        f"mot cirka {later_p:.2f} kr/kWh."
                    ),
                    kind="active",
                )
            )
        else:
            actions.append(
                ModelAction(
                    key="dhw_charge",
                    title="Laddar varmvatten",
                    detail=(
                        f"Elpriset är {price:.2f} kr/kWh — ett bra tillfälle att fylla tanken."
                    ),
                    kind="active",
                )
            )
    elif dhw is not None and dropping:
        actions.append(
            ModelAction(
                key="dhw_wait_cheap",
                title="Väntar på billigare el till varmvattnet",
                detail=(
                    f"Om cirka {drop_minutes} min är priset omkring {drop_later:.2f} kr/kWh "
                    f"(nu {drop_now:.2f}). Då är det läge att fylla tanken."
                ),
                kind="tip",
                when="soon",
            )
        )
    elif dhw is not None and rising:
        steps = max(1, int(round(4 * 60 / max(plan.step_minutes, 1))))
        end = min(len(dhw.charge_fraction), index + steps)
        if any(f > 0.05 for f in dhw.charge_fraction[index:end]):
            actions.append(
                ModelAction(
                    key="dhw_soon",
                    title="Varmvatten laddas innan priset toppar",
                    detail=(
                        f"Inom några timmar fylls tanken innan elen går upp mot "
                        f"{later_p:.2f} kr/kWh."
                    ),
                    kind="tip",
                    when="soon",
                )
            )

    preheating = []
    setback = []
    holding = []
    for room in plan.rooms:
        sp = room.setpoint[index]
        mid = (room.comfort_min + room.comfort_max) / 2.0
        if sp >= room.comfort_max - 0.05 and room.heat_fraction[index] > 0.15:
            preheating.append(room.name)
        elif sp <= room.comfort_min + 0.15 and room.priority >= 2:
            setback.append(room.name)
        elif abs(sp - mid) < 0.4 and room.priority == 1:
            holding.append(room.name)

    if preheating:
        actions.append(
            ModelAction(
                key="preheat",
                title="Förvärmer rum",
                detail=(
                    "Sparar värme i "
                    + ", ".join(preheating[:4])
                    + (" …" if len(preheating) > 4 else "")
                    + " innan dyrare timmar."
                ),
                kind="active",
            )
        )
    if setback:
        actions.append(
            ModelAction(
                key="setback",
                title="Sänker värmen i rum som får svaja",
                detail=(
                    ", ".join(setback[:4])
                    + (" …" if len(setback) > 4 else "")
                    + " får lite svalare temp så dyr el undviks. "
                    "Rum med hög prioritet hålls varmare."
                ),
                kind="active",
            )
        )
    if holding and not preheating:
        actions.append(
            ModelAction(
                key="hold",
                title="Håller temperaturen i viktiga rum",
                detail=", ".join(holding[:4]) + " hålls inom komfortbandet.",
                kind="info",
            )
        )

    if pump < 0.15 and plan.outdoor_c[index] < 5 and not guard_blocking:
        actions.append(
            ModelAction(
                key="coast",
                title="Pausar värmepumpen — kör på lagrad värme",
                detail=(
                    f"Ute {plan.outdoor_c[index]:.0f} °C men värmepumpen nära noll. "
                    "Huset rullar på tröghet och eventuell brasvärme."
                ),
                kind="active",
            )
        )
    elif pump > 2.0:
        actions.append(
            ModelAction(
                key="heat_hard",
                title=f"Värmepumpen går hårt ({pump:.1f} kW)",
                detail=f"Elpris just nu {price:.2f} kr/kWh.",
                kind="info",
            )
        )

    if wood_stove is not None and wood_stove.enabled:
        if wood_stove.reading.lit:
            actions.append(
                ModelAction(
                    key="stove_lit",
                    title=f"{wood_stove.name} är tänd",
                    detail=wood_stove.summary or "Brasbidraget minskar behovet av värmepump.",
                    kind="active",
                )
            )
        elif wood_stove.status == "recommend" and wood_stove.windows:
            window = wood_stove.windows[0]
            actions.append(
                ModelAction(
                    key="stove_tip",
                    title="Bra läge att tända brasan",
                    detail=(
                        f"Föreslaget fönster {window.start.strftime('%H:%M')}–"
                        f"{window.end.strftime('%H:%M')} "
                        f"({window.mean_price_sek:.2f} kr/kWh, ute {window.mean_outdoor_c:.0f} °C)."
                    ),
                    kind="tip",
                    when="soon",
                )
            )

    if not actions:
        actions.append(
            ModelAction(
                key="steady",
                title="Håller en lugn kurva",
                detail=f"Pris {price:.2f} kr/kWh, planerad värmepump {pump:.1f} kW.",
                kind="info",
            )
        )

    return actions


def headline(actions: list[ModelAction]) -> str:
    """Single-line summary for MQTT / glance cards."""
    if not actions:
        return "Ingen plan"
    preferred = next((a for a in actions if a.kind in {"active", "warn"}), actions[0])
    return preferred.title


def explain_upcoming(plan: Plan, index: int, *, limit: int = 4) -> list[ModelAction]:
    """Short look-ahead bullets for the next few hours."""
    if plan is None or not plan.times:
        return []
    steps = max(1, int(round(3 * 60 / max(plan.step_minutes, 1))))
    end = min(len(plan.times), index + steps * 3)
    items: list[ModelAction] = []

    if plan.hot_water is not None:
        for i in range(index + 1, end):
            if plan.hot_water.charge_fraction[i] > 0.15:
                if i == 0 or plan.hot_water.charge_fraction[i - 1] <= 0.15:
                    t = plan.times[i]
                    items.append(
                        ModelAction(
                            key=f"soon_dhw_{i}",
                            title="Varmvattenladdning",
                            detail=f"Planerad runt {t.strftime('%H:%M')}.",
                            kind="tip",
                            when="soon",
                        )
                    )
                    break

    for i in range(index + 1, end):
        if (
            plan.heat_pump_kw[i] > 2.0
            and plan.price_sek_per_kwh[i] > _mean(plan.price_sek_per_kwh) * 1.2
        ):
            t = plan.times[i]
            items.append(
                ModelAction(
                    key=f"soon_expensive_heat_{i}",
                    title="Dyr uppvärmning i sikte",
                    detail=(
                        f"Runt {t.strftime('%H:%M')}: {plan.price_sek_per_kwh[i]:.2f} kr/kWh "
                        f"och {plan.heat_pump_kw[i]:.1f} kW — förvärmning eller brasa lönar sig."
                    ),
                    kind="tip",
                    when="soon",
                )
            )
            break

    return items[:limit]
