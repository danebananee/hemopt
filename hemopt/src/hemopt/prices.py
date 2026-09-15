"""Nordpool day-ahead spot prices via the free elprisetjustnu.se feed.

The feed serves one static JSON file per day and price area, quoted in SEK/kWh
excluding VAT. It carries 24 hourly points before 1 October 2025 and 96
quarter-hourly points from then on, which the resampler handles transparently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from .config import EnergyPriceConfig, PeakWindow
from .timeutil import is_high_load_energy

_LOGGER = logging.getLogger(__name__)

API_TEMPLATE = (
    "https://www.elprisetjustnu.se/api/v1/prices/{year}/{month:02d}-{day:02d}_{area}.json"
)


@dataclass(frozen=True, slots=True)
class PricePoint:
    start: datetime
    end: datetime
    spot_sek_per_kwh: float


@dataclass(slots=True)
class PriceSeries:
    """Spot prices plus the retail adders, sampled on the optimiser grid."""

    times: list[datetime]
    spot: list[float]
    total: list[float]
    step_minutes: int
    forecast_from: datetime | None = None

    def __len__(self) -> int:
        return len(self.times)

    @property
    def is_partly_forecast(self) -> bool:
        return self.forecast_from is not None

    def slice_from(self, start: datetime, steps: int) -> PriceSeries:
        try:
            index = next(i for i, t in enumerate(self.times) if t >= start)
        except StopIteration as exc:
            raise ValueError("price series does not cover the requested start") from exc
        end = index + steps
        if end > len(self.times):
            raise ValueError(
                f"price series covers {len(self.times) - index} steps, {steps} requested"
            )
        return PriceSeries(
            times=self.times[index:end],
            spot=self.spot[index:end],
            total=self.total[index:end],
            step_minutes=self.step_minutes,
            forecast_from=self.forecast_from,
        )


class PriceClient:
    """Fetches and caches day-ahead prices."""

    def __init__(
        self,
        area: str,
        energy_price: EnergyPriceConfig,
        peak_window: PeakWindow,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._area = area
        self._energy_price = energy_price
        self._peak_window = peak_window
        self._client = client
        self._owns_client = client is None
        self._cache: dict[date, list[PricePoint]] = {}

    async def __aenter__(self) -> PriceClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_day(self, day: date) -> list[PricePoint] | None:
        """Return the published prices for `day`, or None if not yet available."""
        if day in self._cache:
            return self._cache[day]
        if self._client is None:
            raise RuntimeError("PriceClient must be used as an async context manager")

        url = API_TEMPLATE.format(year=day.year, month=day.month, day=day.day, area=self._area)
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            _LOGGER.warning("spot price request failed for %s: %s", day, exc)
            return None

        if response.status_code == 404:
            # Tomorrow's auction result is published around 13:00 local time.
            return None
        if response.status_code != 200:
            _LOGGER.warning("spot price feed returned %s for %s", response.status_code, day)
            return None

        points = [
            PricePoint(
                start=datetime.fromisoformat(row["time_start"]),
                end=datetime.fromisoformat(row["time_end"]),
                spot_sek_per_kwh=float(row["SEK_per_kWh"]),
            )
            for row in response.json()
        ]
        points.sort(key=lambda p: p.start)
        self._cache[day] = points
        return points

    async def series(self, start: datetime, steps: int, step_minutes: int) -> PriceSeries:
        """Build a price series covering `steps` steps from `start`.

        Any tail beyond the published auction results is filled by repeating
        the most recent full day, shifted by 24 hours. That keeps the optimiser
        running in the morning when tomorrow is still unknown, and the plan is
        recomputed as soon as the real prices land.
        """
        horizon_end = start + timedelta(minutes=step_minutes * steps)
        published: list[PricePoint] = []
        day = start.date()
        while day <= horizon_end.date():
            points = await self.fetch_day(day)
            if points is None:
                break
            published.extend(points)
            day += timedelta(days=1)

        if not published:
            raise RuntimeError("no spot prices available for the planning horizon")

        by_start = {point.start: point.spot_sek_per_kwh for point in published}
        last_published = max(by_start)
        forecast_from: datetime | None = None

        times: list[datetime] = []
        spot: list[float] = []
        for index in range(steps):
            moment = start + timedelta(minutes=step_minutes * index)
            value = self._lookup(by_start, published, moment)
            if value is None:
                shifted = moment - timedelta(days=1)
                value = self._lookup(by_start, published, shifted)
                if value is None:
                    value = spot[-1] if spot else 0.0
                if forecast_from is None:
                    forecast_from = moment
                    _LOGGER.info(
                        "spot prices published through %s, extrapolating from %s",
                        last_published.isoformat(),
                        moment.isoformat(),
                    )
            times.append(moment)
            spot.append(value)

        total = [
            value
            + self._energy_price.adder_sek_per_kwh(is_high_load_energy(moment, self._peak_window))
            for moment, value in zip(times, spot, strict=True)
        ]
        if not self._energy_price.prices_include_vat:
            vat = 1.0 + self._energy_price.vat_rate
            total = [
                spot_value * vat
                + self._energy_price.adder_sek_per_kwh(
                    is_high_load_energy(moment, self._peak_window)
                )
                for moment, spot_value in zip(times, spot, strict=True)
            ]

        return PriceSeries(
            times=times,
            spot=spot,
            total=total,
            step_minutes=step_minutes,
            forecast_from=forecast_from,
        )

    @staticmethod
    def _lookup(
        by_start: dict[datetime, float], points: list[PricePoint], moment: datetime
    ) -> float | None:
        """Find the published price covering `moment`.

        A direct hit is the common case; the scan handles an optimiser step
        that is finer than the published resolution, such as 15-minute steps
        against an hourly feed.
        """
        direct = by_start.get(moment)
        if direct is not None:
            return direct
        for point in points:
            if point.start <= moment < point.end:
                return point.spot_sek_per_kwh
        return None
