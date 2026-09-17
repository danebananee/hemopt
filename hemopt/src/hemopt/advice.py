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
SETTLEMENT_CONTRACTS: tuple[Contract, ...] = ("monthly", "daily", "hourly", "quarterly")


def _kr(value: float) -> str:
    """Kronor with a thin space between thousands, as Swedish invoices write it."""
    return f"{value:,.0f}".replace(",", "\u202f")


@dataclass(frozen=True, slots=True)
class SavingAction:
    """A concrete savings measure for the panel's «Besparingsåtgärder» block.

    `status` is one of:
      need_data  — not enough history (or missing peak / tariff table)
      change     — a concrete switch is worth making
      ok         — enough data, and the current choice is already right
    """

    key: str
    title: str
    status: str
    summary: str
    detail: str = ""
    annual_saving_sek: float = 0.0
    confidence: str = "medium"
    action: str = ""
    caveat: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "annual_saving_sek": round(self.annual_saving_sek, 0),
            "confidence": self.confidence,
            "action": self.action,
            "caveat": self.caveat,
            "meta": self.meta,
        }


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


@dataclass(frozen=True, slots=True)
class ContractScenario:
    """One settlement type priced both as lived and as the optimiser would use it."""

    contract: Contract
    name: str
    is_current: bool
    without_control: ContractCost
    with_control: ContractCost
    # Annualised difference vs the current contract *as lived* (positive = cheaper).
    vs_current_without_sek: float
    vs_current_with_sek: float


@dataclass(slots=True)
class AdviceReport:
    recommendations: list[Recommendation] = field(default_factory=list)
    actions: list[SavingAction] = field(default_factory=list)
    contract_costs: list[ContractCost] = field(default_factory=list)
    scenarios: list[ContractScenario] = field(default_factory=list)
    current_contract: Contract | None = None
    measured_days: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total_annual_saving_sek(self) -> float:
        return sum(r.annual_saving_sek for r in self.recommendations)

    def as_dict(self) -> dict:
        days = self.measured_days
        scale = 365.0 / days if days else 0.0
        scenarios = []
        for scenario in self.scenarios:
            scenarios.append(
                {
                    "contract": scenario.contract,
                    "name": scenario.name,
                    "is_current": scenario.is_current,
                    "without_control": {
                        "total_sek": round(scenario.without_control.total_sek, 2),
                        "annual_sek": round(scenario.without_control.total_sek * scale, 0),
                        "ore_per_kwh": round(scenario.without_control.mean_ore_per_kwh, 1),
                        "energy_sek": round(scenario.without_control.energy_sek, 2),
                        "kwh": round(scenario.without_control.kwh, 1),
                    },
                    "with_control": {
                        "total_sek": round(scenario.with_control.total_sek, 2),
                        "annual_sek": round(scenario.with_control.total_sek * scale, 0),
                        "ore_per_kwh": round(scenario.with_control.mean_ore_per_kwh, 1),
                        "energy_sek": round(scenario.with_control.energy_sek, 2),
                        "kwh": round(scenario.with_control.kwh, 1),
                    },
                    "vs_current_without_sek": round(scenario.vs_current_without_sek, 0),
                    "vs_current_with_sek": round(scenario.vs_current_with_sek, 0),
                }
            )
        return {
            "measured_days": round(self.measured_days, 1),
            "current_contract": self.current_contract,
            "current_contract_name": (
                CONTRACT_NAMES.get(self.current_contract, self.current_contract)
                if self.current_contract
                else None
            ),
            "total_annual_saving_sek": round(self.total_annual_saving_sek, 0),
            "recommendations": [r.as_dict() for r in self.recommendations],
            "actions": [a.as_dict() for a in self.actions],
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
            "scenarios": scenarios,
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
    report = AdviceReport(
        measured_days=_span_days(load),
        current_contract=config.energy_price.contract,
    )
    settings = config.advice

    if not settings.enabled:
        return report

    if len(load) < 2:
        report.notes.append("Ingen matdata an; radgivningen vantar pa historik.")
        report.actions = [
            _settlement_action_need_data(config, report.measured_days),
            _fuse_action_need_data(config, report.measured_days, peak_kw, reason="load"),
        ]
        return report

    report.contract_costs = compare(load, spot, config.energy_price, config.peak_tariff.window)
    report.scenarios = _contract_scenarios(config, load, spot)

    if config.energy_price.contract in {"monthly", "daily", "fixed"}:
        report.notes.append(
            "Med dygns- eller manadsmedel sparar lastflytt noll pa energidelen — "
            "byt till tim- eller kvartsavrakning for att fa ut varde av styrningen."
        )

    settlement_action = _settlement_action(config, report, load, spot)
    fuse_action = _fuse_action(config, report, peak_kw)
    report.actions = [settlement_action, fuse_action]

    candidates = [
        _recommendation_from_action(settlement_action),
        _advise_single_rate_grid(config, report, load, spot),
        _recommendation_from_action(fuse_action),
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


def _recommendation_from_action(action: SavingAction) -> Recommendation | None:
    if action.status != "change" or action.annual_saving_sek <= 0:
        return None
    return Recommendation(
        key=action.key,
        title=action.title,
        detail=action.detail or action.summary,
        annual_saving_sek=action.annual_saving_sek,
        confidence=action.confidence,
        action=action.action,
        caveat=action.caveat,
    )


def _contract_scenarios(
    config: Config, load: list[LoadSample], spot: list[float]
) -> list[ContractScenario]:
    """Cost of every settlement type, both as lived and with load shifted."""
    pricing = config.energy_price
    window = config.peak_tariff.window
    current = pricing.contract
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

    as_lived = {
        contract: settle(load, spot, contract, pricing, window) for contract in SETTLEMENT_CONTRACTS
    }
    if current not in as_lived:
        as_lived[current] = settle(load, spot, current, pricing, window)

    as_controlled = {
        contract: settle(shifted, spot, contract, pricing, window)
        for contract in SETTLEMENT_CONTRACTS
    }
    if current not in as_controlled:
        as_controlled[current] = settle(shifted, spot, current, pricing, window)

    baseline = as_lived[current]
    order = list(SETTLEMENT_CONTRACTS)
    if current not in order:
        order = [current, *order]

    scenarios: list[ContractScenario] = []
    for contract in order:
        without = as_lived[contract]
        with_ctrl = as_controlled[contract]
        scenarios.append(
            ContractScenario(
                contract=contract,
                name=CONTRACT_NAMES.get(contract, contract),
                is_current=contract == current,
                without_control=without,
                with_control=with_ctrl,
                vs_current_without_sek=annualise(baseline.total_sek - without.total_sek, load),
                vs_current_with_sek=annualise(baseline.total_sek - with_ctrl.total_sek, load),
            )
        )
    return scenarios


# --------------------------------------------------------------------- rules


def _confidence_for_span(days: float, settings: AdviceConfig) -> str:
    if days >= 300:
        return "high"
    if days >= settings.min_history_days:
        return "medium"
    return "low"


def _settlement_action_need_data(config: Config, measured_days: float) -> SavingAction:
    # Avtalsjämförelse behöver ungefär två dygn. Säkringsråd kräver längre
    # historik (advice.min_history_days) och hanteras separat.
    needed = 2
    return SavingAction(
        key="settlement",
        title="Elavtal (avräkning)",
        status="need_data",
        summary=(
            f"Mer mätdata behövs innan avtalet kan bedömas "
            f"({measured_days:.0f} av minst {needed:.0f} dygn med elmätare)."
        ),
        meta={
            "current_contract": config.energy_price.contract,
            "days_have": round(measured_days, 1),
            "days_needed": needed,
        },
    )


def _settlement_action(
    config: Config, report: AdviceReport, load: list[LoadSample], spot: list[float]
) -> SavingAction:
    """Switching how the spot price is settled — always returns a panel action."""
    pricing = config.energy_price
    current: Contract = pricing.contract
    window = config.peak_tariff.window
    current_name = CONTRACT_NAMES.get(current, current)

    if report.measured_days < 2:
        return _settlement_action_need_data(config, report.measured_days)

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
        for contract in SETTLEMENT_CONTRACTS
    }
    if current not in optimised:
        optimised[current] = settle(shifted, spot, current, pricing, window)

    best_contract = min(optimised, key=lambda key: optimised[key].total_sek)
    saving = annualise(optimised[current].total_sek - optimised[best_contract].total_sek, load)
    best_name = CONTRACT_NAMES[best_contract]
    confidence = _confidence_for_span(report.measured_days, config.advice)
    meta = {
        "current_contract": current,
        "best_contract": best_contract,
        "days_have": round(report.measured_days, 1),
        "days_needed": config.advice.min_history_days,
    }

    if best_contract == current or saving < config.advice.min_annual_saving_sek:
        return SavingAction(
            key="settlement",
            title="Elavtal (avrakning)",
            status="ok",
            summary=f"Du ligger ratt pa {current_name} — inget byte lönar sig just nu.",
            detail=(
                f"Med din uppmatta forbrukning (och rimlig lastflytt) ar "
                f"{current_name} redan det billigaste alternativet bland "
                f"manad/dygn/timme/kvart."
            ),
            confidence=confidence,
            meta=meta,
        )

    averaging = current in {"monthly", "daily", "fixed"}
    if averaging:
        detail = (
            f"Du har {current_name}, dar varje kWh debiteras till periodens "
            f"medelpris. Att flytta last till en billig timme sanker darfor "
            f"inte kostnaden med en krona. Med {best_name} och aktiv styrning "
            f"hade perioden kostat {_kr(optimised[best_contract].total_sek)} kr "
            f"i stallet for {_kr(as_used.total_sek)} kr."
        )
        caveat = (
            "Vinsten forutsatter att styrningen far flytta last. Utan den ar "
            "medelpris ofta billigare."
        )
    else:
        detail = (
            f"{best_name} ger finare upplosning an {current_name}, vilket ger "
            f"styrningen mer att arbeta med."
        )
        caveat = "Skillnaden mellan tim och kvart ar liten om lasten redan ar utjamnad."

    return SavingAction(
        key="settlement",
        title=f"Byt till {best_name}",
        status="change",
        summary=f"Byt avrakning till {best_name} — cirka {_kr(saving)} kr/ar.",
        detail=detail,
        annual_saving_sek=saving,
        confidence=confidence,
        action="Kontakta elhandlaren och begar byte av avrakningsform.",
        caveat=caveat,
        meta=meta,
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
        action=(
            "Elnatstariff byter du hos natbolaget (t.ex. Vattenfall Eldistribution): "
            "ring kundtjanst eller anvand deras webbformular och be om tariffen ovan. "
            "Sakringen sitter kvar — bara abonnemanget andras."
        ),
        caveat="Natbolag tar ibland ut en avgift for tariffbyte, och kan ha bindningstid.",
    )


def _fuse_action_need_data(
    config: Config,
    measured_days: float,
    peak_kw: float | None,
    *,
    reason: str,
) -> SavingAction:
    settings = config.advice
    current_amps = config.site.main_fuse_amps
    if reason == "tariffs":
        summary = (
            "Lagg in natbolagets sakringsabonnemang under advice.grid_tariffs "
            "i hemopt.yaml (16/20/25 A) sa sakringen kan bedomas."
        )
    elif reason == "peak":
        summary = (
            "Sakringsradet vantar pa en uppmatt effekttopp fran elmataren "
            f"({measured_days:.0f} dygn finns, men ingen topp an)."
        )
    else:
        summary = (
            f"Mer matdata behoves for sakringsrad "
            f"({measured_days:.0f} av minst {settings.min_history_days} dygn)."
        )
    return SavingAction(
        key="fuse",
        title="Huvudsakring",
        status="need_data",
        summary=summary,
        detail=(
            f"Du har {current_amps} A i dag. Nar det finns tillrackligt med "
            f"historik jamfors 16 A och 20 A mot hogsta uppmatta effekt "
            f"(plus {settings.fuse_margin_kw:.0f} kW marginal)."
        ),
        meta={
            "current_amps": current_amps,
            "peak_kw": None if peak_kw is None else round(peak_kw, 2),
            "margin_kw": settings.fuse_margin_kw,
            "days_have": round(measured_days, 1),
            "days_needed": settings.min_history_days,
            "options": [],
        },
    )


def _fuse_options(config: Config, peak_kw: float) -> list[dict]:
    """Evaluate each configured fuse size against the measured peak."""
    settings = config.advice
    voltage = config.site.voltage
    phases = config.site.phases
    current_amps = config.site.main_fuse_amps
    options = sorted(settings.grid_tariffs, key=lambda o: o.fuse_amps)
    rows = []
    for option in options:
        capacity = option.capacity_kw(voltage, phases)
        required = peak_kw + settings.fuse_margin_kw
        ok = capacity >= required
        rows.append(
            {
                "amps": option.fuse_amps,
                "name": option.name,
                "capacity_kw": round(capacity, 2),
                "required_kw": round(required, 2),
                "headroom_kw": round(capacity - peak_kw, 2),
                "ok": ok,
                "is_current": option.fuse_amps == current_amps,
                "subscription_sek_per_year": option.subscription_sek_per_year,
            }
        )
    return rows


def _fuse_action(config: Config, report: AdviceReport, peak_kw: float | None) -> SavingAction:
    """Whether a smaller main fuse is safe — always returns a panel action."""
    settings = config.advice
    current_amps = config.site.main_fuse_amps

    if not settings.grid_tariffs:
        return _fuse_action_need_data(config, report.measured_days, peak_kw, reason="tariffs")
    if peak_kw is None:
        return _fuse_action_need_data(config, report.measured_days, peak_kw, reason="peak")
    if report.measured_days < settings.min_history_days:
        action = _fuse_action_need_data(config, report.measured_days, peak_kw, reason="history")
        # Still show provisional option table so the household sees the direction.
        meta = dict(action.meta)
        meta["options"] = _fuse_options(config, peak_kw)
        meta["peak_kw"] = round(peak_kw, 2)
        return SavingAction(
            key=action.key,
            title=action.title,
            status=action.status,
            summary=action.summary,
            detail=action.detail,
            meta=meta,
        )

    options = _fuse_options(config, peak_kw)
    smaller_ok = [row for row in options if row["amps"] < current_amps and row["ok"]]
    confidence = "high" if report.measured_days >= 300 else "low"
    meta = {
        "current_amps": current_amps,
        "peak_kw": round(peak_kw, 2),
        "margin_kw": settings.fuse_margin_kw,
        "days_have": round(report.measured_days, 1),
        "days_needed": settings.min_history_days,
        "options": options,
    }

    if not smaller_ok:
        too_small = [row for row in options if row["amps"] < current_amps and not row["ok"]]
        parts = []
        for row in too_small:
            parts.append(
                f"{row['amps']} A klarar {row['capacity_kw']:.1f} kW men topp "
                f"{peak_kw:.1f} kW + {settings.fuse_margin_kw:.0f} kW marginal "
                f"kraver {row['required_kw']:.1f} kW"
            )
        detail = (
            f"Hogsta uppmatta effekt pa {report.measured_days:.0f} dygn ar "
            f"{peak_kw:.1f} kW. "
            + ("; ".join(parts) + ". " if parts else "")
            + f"Behall {current_amps} A."
        )
        return SavingAction(
            key="fuse",
            title="Huvudsakring",
            status="ok",
            summary=f"Du ligger ratt pa {current_amps} A — mindre sakring tar for snavt.",
            detail=detail,
            confidence=confidence,
            caveat=(
                "Radet blir sakrare nar historiken tacker en vinter. "
                "Elbilsladdning + varmepump samtidigt ar det som slar ut en mindre sakring."
            ),
            meta=meta,
        )

    # Cheapest safe smaller fuse (lowest subscription among those that fit).
    best = min(smaller_ok, key=lambda row: row["subscription_sek_per_year"])
    current = next((o for o in settings.grid_tariffs if o.fuse_amps == current_amps), None)
    current_fee = (
        current.subscription_sek_per_year
        if current
        else config.energy_price.grid_subscription_sek_per_year
    )
    vat = 1.0 if config.energy_price.prices_include_vat else 1.0 + config.energy_price.vat_rate
    saving = (current_fee - best["subscription_sek_per_year"]) * vat

    also = [row for row in smaller_ok if row["amps"] != best["amps"]]
    also_txt = ""
    if also:
        also_txt = " Aven " + " och ".join(f"{r['amps']} A" for r in also) + " klarar toppen."

    return SavingAction(
        key="fuse",
        title=f"Sank huvudsakringen till {best['amps']} A",
        status="change",
        summary=(
            f"Du klarar dig pa {best['amps']} A (nu {current_amps} A) — "
            f"cirka {_kr(saving)} kr/ar i abonnemang."
        ),
        detail=(
            f"Hogsta uppmatta effekt pa {report.measured_days:.0f} dygn ar "
            f"{peak_kw:.1f} kW. {best['amps']} A klarar {best['capacity_kw']:.1f} kW "
            f"({best['headroom_kw']:.1f} kW marginal).{also_txt}"
        ),
        annual_saving_sek=saving,
        confidence=confidence,
        action=(
            "Sa byter du sakring: 1) begar lagre sakringsabonnemang hos natbolaget, "
            "2) de skickar en bekraftelse och ny tariff, 3) en behorig elektriker byter "
            "sakringarna i elcentralen. Har du 25 A i dag ar nasta steg 20 A."
        ),
        caveat=(
            "Matdata maste tacka en vinter for att vara rattvisande. "
            "Elbilsladdning och laddbox som startar samtidigt som varmepumpen "
            "ar det som slar ut en mindre sakring."
        ),
        meta=meta,
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
