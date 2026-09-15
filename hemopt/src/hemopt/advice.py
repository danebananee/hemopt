"""Recommendations about the contracts themselves, not just how to run the house.

Optimising consumption is only half the bill. The other half is decided by
which rows you occupy in two price tables: the grid operator's tariff, and the
retailer's offer. Those are chosen once and then forgotten for years, which is
exactly why they are worth re-examining against a year of measured data.

Every recommendation here has to clear the same bar: it names a concrete
change, it quantifies the annual saving from your own measurements, and it says
what it is unsure about. A suggestion without a number attached is a nag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import CONTRACT_NAMES, AdviceConfig, Config, Contract, EnergyPriceConfig
from .contracts import (
    ContractCost,
    LoadSample,
    annualise,
    compare,
    settle,
    shift_flexible_load,
)

# Ranked so the panel can show the most trustworthy advice first.
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _kr(value: float) -> str:
    """Kronor with a thin space between thousands, as Swedish invoices write it."""
    return f"{value:,.0f}".replace(",", "\u202f")


@dataclass(frozen=True, slots=True)
class Recommendation:
    key: str
    title: str
    detail: str
    annual_saving_sek: float
    confidence: str = "medium"
    action: str = ""
    caveat: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "annual_saving_sek": round(self.annual_saving_sek, 0),
            "confidence": self.confidence,
            "action": self.action,
            "caveat": self.caveat,
        }


@dataclass(slots=True)
class AdviceReport:
    recommendations: list[Recommendation] = field(default_factory=list)
    contract_costs: list[ContractCost] = field(default_factory=list)
    measured_days: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total_annual_saving_sek(self) -> float:
        return sum(r.annual_saving_sek for r in self.recommendations)

    def as_dict(self) -> dict:
        return {
            "measured_days": round(self.measured_days, 1),
            "total_annual_saving_sek": round(self.total_annual_saving_sek, 0),
            "recommendations": [r.as_dict() for r in self.recommendations],
            "contract_costs": [
                {
                    "contract": cost.contract,
                    "name": cost.name,
                    "total_sek": round(cost.total_sek, 2),
                    "energy_sek": round(cost.energy_sek, 2),
                    "ore_per_kwh": round(cost.mean_ore_per_kwh, 1),
                    "kwh": round(cost.kwh, 1),
                }
                for cost in self.contract_costs
            ],
            "notes": self.notes,
        }


def _span_days(load: list[LoadSample]) -> float:
    if len(load) < 2:
        return 0.0
    span = max(s.start for s in load) - min(s.start for s in load)
    step = span / (len(load) - 1)
    return (span + step).total_seconds() / 86400.0


def build_advice(
    config: Config,
    load: list[LoadSample],
    spot: list[float],
    *,
    peak_kw: float | None = None,
) -> AdviceReport:
    """Everything worth changing about the contracts, given measured load.

    `load` and `spot` must be aligned and should cover as long a span as you
    have. `peak_kw` is the largest short-term draw observed, which is what a
    fuse actually has to survive; without it the fuse advice is withheld rather
    than guessed from hourly means.
    """
    report = AdviceReport(measured_days=_span_days(load))
    settings = config.advice

    if not settings.enabled:
        return report

    if len(load) < 2:
        report.notes.append("Ingen matdata an; radgivningen vantar pa historik.")
        return report

    report.contract_costs = compare(load, spot, config.energy_price, config.peak_tariff.window)

    candidates = [
        _advise_settlement(config, report, load, spot),
        _advise_single_rate_grid(config, report, load, spot),
        _advise_fuse(config, report, peak_kw),
        _advise_supplier(config, report, load, spot),
    ]

    report.recommendations = sorted(
        (
            rec
            for rec in candidates
            if rec is not None and rec.annual_saving_sek >= settings.min_annual_saving_sek
        ),
        key=lambda rec: (-rec.annual_saving_sek, CONFIDENCE_ORDER.get(rec.confidence, 9)),
    )
    return report


# --------------------------------------------------------------------- rules


def _confidence_for_span(days: float, settings: AdviceConfig) -> str:
    if days >= 300:
        return "high"
    if days >= settings.min_history_days:
        return "medium"
    return "low"


def _advise_settlement(
    config: Config, report: AdviceReport, load: list[LoadSample], spot: list[float]
) -> Recommendation | None:
    """Switching how the spot price is settled.

    Judged on how each contract would be *used*, not on today's consumption
    replayed unchanged. Nobody moves to hourly pricing and then keeps behaving
    as if they were on a monthly average, and comparing as-is would often
    recommend staying put: if your load already sits in expensive hours, an
    averaging contract is genuinely cheaper until you start shifting.
    """
    pricing = config.energy_price
    current: Contract = pricing.contract
    window = config.peak_tariff.window

    step_hours = _step_hours(load)
    ceiling = (
        config.site.main_fuse_amps * config.site.voltage * config.site.phases / 1000.0
    ) * step_hours

    shifted = shift_flexible_load(
        load,
        spot,
        flexible_share=config.advice.flexible_share,
        max_step_kwh=ceiling,
    )

    as_used = settle(load, spot, current, pricing, window)
    optimised = {
        contract: settle(shifted, spot, contract, pricing, window)
        for contract in ("monthly", "daily", "hourly", "quarterly")
    }
    if current not in optimised:
        optimised[current] = as_used

    best_contract = min(optimised, key=lambda key: optimised[key].total_sek)

    # Both sides are priced with the load shifted, so this is the value of the
    # contract change alone. Crediting it with the shifting as well would count
    # the optimiser's own saving twice, once here and once in the plan.
    saving = annualise(optimised[current].total_sek - optimised[best_contract].total_sek, load)
    if saving <= 0:
        return None

    best_name = CONTRACT_NAMES[best_contract]
    averaging = current in {"monthly", "daily", "fixed"}

    if averaging:
        detail = (
            f"Du har {CONTRACT_NAMES[current]}, dar varje kWh debiteras till "
            f"periodens medelpris. Att flytta last till en billig timme sanker "
            f"darfor inte kostnaden med en krona, och hela optimeringens vinst "
            f"pa energidelen ar oatkomlig sa lange avtalet ser ut sa. Med "
            f"{best_name} och aktiv styrning hade din uppmatta forbrukning "
            f"kostat {_kr(optimised[best_contract].total_sek)} kr i stallet for "
            f"{_kr(as_used.total_sek)} kr."
        )
        caveat = (
            "Vinsten forutsatter att styrningen far flytta last. Utan den ar "
            "manadsmedel ofta billigare, eftersom det jamnar ut dyra timmar at dig."
        )
    else:
        detail = (
            f"{best_name} ger finare upplosning an {CONTRACT_NAMES[current]}, "
            f"vilket ger styrningen mer att arbeta med."
        )
        caveat = "Skillnaden mellan tim och kvart ar liten om lasten redan ar utjamnad."

    return Recommendation(
        key="settlement",
        title=f"Byt till {best_name}",
        detail=detail,
        annual_saving_sek=saving,
        confidence=_confidence_for_span(report.measured_days, config.advice),
        action="Kontakta elhandlaren och begar byte av avrakningsform.",
        caveat=caveat,
    )


def _step_hours(load: list[LoadSample]) -> float:
    if len(load) < 2:
        return 1.0
    ordered = sorted(sample.start for sample in load)
    return (ordered[1] - ordered[0]).total_seconds() / 3600.0 or 1.0


def _advise_single_rate_grid(
    config: Config, report: AdviceReport, load: list[LoadSample], spot: list[float]
) -> Recommendation | None:
    """Swapping the grid tariff itself, fuse size held constant.

    Only tariffs at the current fuse size are considered here; dropping a fuse
    is a different decision with a different risk, handled separately.
    """
    pricing = config.energy_price
    options = [
        option
        for option in config.advice.grid_tariffs
        if option.fuse_amps == config.site.main_fuse_amps
    ]
    if not options:
        return None

    baseline = settle(load, spot, pricing.contract, pricing, config.peak_tariff.window)

    best_option, best_cost = None, baseline.total_sek
    for option in options:
        variant = _pricing_for(pricing, option)
        cost = settle(load, spot, pricing.contract, variant, config.peak_tariff.window)
        if cost.total_sek < best_cost:
            best_option, best_cost = option, cost.total_sek

    if best_option is None:
        return None

    saving = annualise(baseline.total_sek - best_cost, load)
    return Recommendation(
        key="grid_tariff",
        title=f"Byt elnatstariff till {best_option.name}",
        detail=(
            f"Med din uppmatta forbrukningsprofil ar {best_option.name} billigare "
            f"an den tariff du ligger pa, utan att du behover andra sakringen."
        ),
        annual_saving_sek=saving,
        confidence=_confidence_for_span(report.measured_days, config.advice),
        action="Elnatstariff byter du hos natbolaget, inte hos elhandlaren.",
        caveat="Natbolag tar ibland ut en avgift for tariffbyte, och kan ha bindningstid.",
    )


def _advise_fuse(
    config: Config, report: AdviceReport, peak_kw: float | None
) -> Recommendation | None:
    """Dropping to a smaller main fuse.

    Deliberately conservative. The subscription saving is real money every
    month, but a main fuse that blows in January is worse than the saving, so
    the advice is withheld unless there is both enough history and enough
    headroom.
    """
    settings = config.advice
    if peak_kw is None:
        return None
    if report.measured_days < settings.min_history_days:
        report.notes.append(
            f"Sakringsradet vantar pa {settings.min_history_days} dygns matdata "
            f"({report.measured_days:.0f} finns)."
        )
        return None

    current_amps = config.site.main_fuse_amps
    smaller = sorted(
        (
            option
            for option in settings.grid_tariffs
            if option.fuse_amps < current_amps
            and option.capacity_kw(config.site.voltage, config.site.phases)
            >= peak_kw + settings.fuse_margin_kw
        ),
        key=lambda option: option.subscription_sek_per_year,
    )
    if not smaller:
        return None

    option = smaller[0]
    current = next(
        (o for o in settings.grid_tariffs if o.fuse_amps == current_amps),
        None,
    )
    current_fee = (
        current.subscription_sek_per_year
        if current
        else config.energy_price.grid_subscription_sek_per_year
    )
    vat = 1.0 if config.energy_price.prices_include_vat else 1.0 + config.energy_price.vat_rate
    saving = (current_fee - option.subscription_sek_per_year) * vat
    capacity = option.capacity_kw(config.site.voltage, config.site.phases)

    return Recommendation(
        key="fuse",
        title=f"Sank huvudsakringen till {option.fuse_amps} A",
        detail=(
            f"Hogsta uppmatta effekt pa {report.measured_days:.0f} dygn ar "
            f"{peak_kw:.1f} kW. En {option.fuse_amps} A sakring klarar "
            f"{capacity:.1f} kW, alltsa {capacity - peak_kw:.1f} kW marginal. "
            f"Abonnemanget blir {_kr(saving)} kr billigare per ar."
        ),
        annual_saving_sek=saving,
        confidence="high" if report.measured_days >= 300 else "low",
        action="Sakringsbyte bestaller du hos natbolaget; en elektriker byter den.",
        caveat=(
            "Matdata maste tacka en vinter for att vara rattvisande. Elbilsladdning "
            "och laddbox som startar samtidigt som varmepumpen ar det som slar ut en "
            "mindre sakring."
        ),
    )


def _advise_supplier(
    config: Config, report: AdviceReport, load: list[LoadSample], spot: list[float]
) -> Recommendation | None:
    """Switching retailer, holding the settlement type constant."""
    offers = config.advice.supplier_offers
    if not offers:
        return None

    pricing = config.energy_price
    baseline = settle(load, spot, pricing.contract, pricing, config.peak_tariff.window)

    # Settlement is held at the current one on purpose. Changing it is its own
    # recommendation, and letting both rules move it would count the same
    # saving twice in the total.
    best_offer, best_cost = None, baseline.total_sek
    for offer in offers:
        variant = pricing.model_copy(
            update={
                "supplier_markup_ore": offer.markup_ore,
                "certificate_ore": offer.certificate_ore,
                "supplier_fee_sek_per_year": offer.yearly_fee_sek,
            }
        )
        cost = settle(load, spot, pricing.contract, variant, config.peak_tariff.window)
        if cost.total_sek < best_cost:
            best_offer, best_cost = offer, cost.total_sek

    if best_offer is None:
        return None

    saving = annualise(baseline.total_sek - best_cost, load)
    current_markup = pricing.supplier_markup_ore + pricing.certificate_ore
    offer_markup = best_offer.markup_ore + best_offer.certificate_ore

    return Recommendation(
        key="supplier",
        title=f"Byt elhandlare till {best_offer.name}",
        detail=(
            f"Du betalar {current_markup:.2f} ore/kWh i paslag plus "
            f"{pricing.supplier_fee_sek_per_year:.0f} kr/ar i arsavgift. "
            f"{best_offer.name} tar {offer_markup:.2f} ore/kWh och "
            f"{best_offer.yearly_fee_sek:.0f} kr/ar."
        ),
        annual_saving_sek=saving,
        confidence=_confidence_for_span(report.measured_days, config.advice),
        action="Byte av elhandlare ar kostnadsfritt och tar cirka en manad.",
        caveat="Kontrollera bindningstid och om paslaget ar en kampanjniva.",
    )


def _pricing_for(pricing: EnergyPriceConfig, option) -> EnergyPriceConfig:
    """A copy of the pricing with one grid tariff swapped in."""
    high = option.transfer_high_ore if option.transfer_high_ore is not None else option.transfer_ore
    normal = (
        option.transfer_normal_ore
        if option.transfer_normal_ore is not None
        else option.transfer_ore
    )
    return pricing.model_copy(
        update={
            "transfer_fee_high_ore": high,
            "transfer_fee_normal_ore": normal,
            "grid_subscription_sek_per_year": option.subscription_sek_per_year,
        }
    )


def samples_from_hourly(power_kw: dict[datetime, float]) -> list[LoadSample]:
    """Turn the stored hourly mean power into energy samples."""
    return [LoadSample(moment, kw) for moment, kw in sorted(power_kw.items())]
