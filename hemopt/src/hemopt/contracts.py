"""What each contract type would have cost for a measured load.

Swedish retail contracts differ in one respect that matters here: how finely
the spot price is resolved before it is multiplied by your consumption.

    quarterly  every 15 minutes gets its own price
    hourly     every clock hour gets its own price
    daily      one price per day, the day's mean spot
    monthly    one price per month, the month's mean spot
    fixed      one agreed price, spot ignored entirely

The consequence is easy to state and easy to miss: under a monthly or daily
contract, moving a kWh from an expensive hour to a cheap one changes nothing,
because both hours are billed at the same average. Any optimiser that shifts
load is worth zero on the energy part of such a contract. Quantifying that gap
is what this module is for.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .config import CONTRACT_NAMES, Contract, EnergyPriceConfig, PeakWindow
from .timeutil import is_high_load_energy


@dataclass(frozen=True, slots=True)
class LoadSample:
    """Consumption over one step of the measurement grid."""

    start: datetime
    kwh: float


@dataclass(frozen=True, slots=True)
class ContractCost:
    """Cost breakdown for one contract type, all figures including VAT."""

    contract: Contract
    name: str
    energy_sek: float
    adders_sek: float
    fixed_sek: float
    kwh: float

    @property
    def total_sek(self) -> float:
        return self.energy_sek + self.adders_sek + self.fixed_sek

    @property
    def mean_ore_per_kwh(self) -> float:
        return 100.0 * self.total_sek / self.kwh if self.kwh else 0.0


def _bucket(moment: datetime, contract: Contract) -> object:
    """The key whose members all settle at the same price."""
    if contract == "quarterly":
        return (moment.date(), moment.hour, moment.minute // 15)
    if contract == "hourly":
        return (moment.date(), moment.hour)
    if contract == "daily":
        return moment.date()
    return (moment.year, moment.month)


def settle(
    load: list[LoadSample],
    spot: list[float],
    contract: Contract,
    pricing: EnergyPriceConfig,
    peak_window: PeakWindow,
    *,
    days: float | None = None,
) -> ContractCost:
    """Cost of `load` under `contract`, given the matching `spot` series.

    `spot` is the raw price excluding VAT, aligned element-for-element with
    `load`. `days` scales the standing charges; it defaults to the span the
    samples actually cover.
    """
    if len(load) != len(spot):
        raise ValueError("load and spot must be the same length")

    total_kwh = sum(sample.kwh for sample in load)
    vat = 1.0 if pricing.prices_include_vat else 1.0 + pricing.vat_rate

    if contract == "fixed":
        energy = total_kwh * pricing.fixed_price_ore / 100.0 * vat
    else:
        # Average the price over each settlement bucket first, then bill the
        # bucket's whole consumption at it. For quarterly and hourly the bucket
        # holds a single price and this reduces to the obvious product.
        prices: dict[object, list[float]] = defaultdict(list)
        energy_kwh: dict[object, float] = defaultdict(float)
        for sample, price in zip(load, spot, strict=True):
            key = _bucket(sample.start, contract)
            prices[key].append(price)
            energy_kwh[key] += sample.kwh

        energy = 0.0
        for key, values in prices.items():
            mean_price = sum(values) / len(values)
            energy += energy_kwh[key] * mean_price
        energy *= vat

    adders = sum(
        sample.kwh * pricing.adder_sek_per_kwh(is_high_load_energy(sample.start, peak_window))
        for sample in load
    )

    if days is None:
        days = _span_days(load)
    fixed = pricing.fixed_sek_per_year() * days / 365.0

    return ContractCost(
        contract=contract,
        name=CONTRACT_NAMES.get(contract, contract),
        energy_sek=energy,
        adders_sek=adders,
        fixed_sek=fixed,
        kwh=total_kwh,
    )


def _span_days(load: list[LoadSample]) -> float:
    if len(load) < 2:
        return 0.0
    span = max(s.start for s in load) - min(s.start for s in load)
    # The last sample covers a step too, which for a month-long comparison is
    # the difference between 30 and 31 days of standing charge.
    step = span / (len(load) - 1)
    return (span + step).total_seconds() / 86400.0


def compare(
    load: list[LoadSample],
    spot: list[float],
    pricing: EnergyPriceConfig,
    peak_window: PeakWindow,
    contracts: tuple[Contract, ...] = ("monthly", "daily", "hourly", "quarterly"),
) -> list[ContractCost]:
    """Cost under each contract type, cheapest first."""
    results = [settle(load, spot, contract, pricing, peak_window) for contract in contracts]
    return sorted(results, key=lambda cost: cost.total_sek)


def shift_flexible_load(
    load: list[LoadSample],
    spot: list[float],
    *,
    flexible_share: float,
    max_step_kwh: float,
) -> list[LoadSample]:
    """Move the flexible part of each day's load into that day's cheapest steps.

    Comparing contracts on today's unchanged consumption answers the wrong
    question. Nobody switches to hourly pricing and then keeps behaving as if
    they were on a monthly average; they switch *in order to* shift. So the
    comparison worth making is between today's contract as lived and the
    alternative as it would be used.

    The redistribution is a greedy fill of the cheapest steps up to a per-step
    ceiling, which is the optimal answer to "minimise cost, same total energy,
    bounded power". It ignores comfort and thermal limits, so it is an upper
    bound on what shifting can achieve, not a plan.
    """
    if not 0.0 < flexible_share <= 1.0 or max_step_kwh <= 0:
        return list(load)

    by_day: dict[object, list[int]] = defaultdict(list)
    for index, sample in enumerate(load):
        by_day[sample.start.date()].append(index)

    result = [LoadSample(sample.start, sample.kwh * (1.0 - flexible_share)) for sample in load]

    for indices in by_day.values():
        movable = sum(load[i].kwh for i in indices) * flexible_share
        for index in sorted(indices, key=lambda i: spot[i]):
            if movable <= 0:
                break
            room = max_step_kwh - result[index].kwh
            if room <= 0:
                continue
            take = min(room, movable)
            result[index] = LoadSample(result[index].start, result[index].kwh + take)
            movable -= take

        # Anything that did not fit under the ceiling stays where it was.
        if movable > 0:
            spread = movable / len(indices)
            for index in indices:
                result[index] = LoadSample(result[index].start, result[index].kwh + spread)

    return result


def annualise(cost_sek: float, load: list[LoadSample]) -> float:
    """Scale a measured-period cost to a year.

    Crude on purpose: it assumes the measured period is representative. A month
    of January extrapolates to a far too expensive year, so callers should feed
    it a long span or treat the result as indicative.
    """
    days = _span_days(load)
    return cost_sek * 365.0 / days if days else 0.0
