"""Sun and wind: the weather the room models learn from.

The planner already knows the outdoor temperature. Two more things decide how
fast a room gains or loses heat, and both are in the weather forecast:

* **Sun through the windows.** A clear March noon heats a south-facing room
  for free. How much sun reaches the house is modelled from the sun's height
  above the horizon at the house's position, dimmed by the forecast cloud
  cover. Each room then learns its own gain per unit of sun, which captures
  window size and orientation without anyone measuring them.
* **Wind.** Wind drives cold air through every gap and strips heat from the
  walls, so a windy night costs more than a still one at the same
  temperature. Each room learns how much faster it loses heat per m/s.

Everything here is plain arithmetic so it can run for every planning step.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

# Typical fraction of clear-sky sun that gets through when cloud cover is not
# known: the Swedish long-term average is roughly two thirds cloud.
UNKNOWN_CLOUD_PCT = 65.0


def solar_elevation(moment: datetime, latitude: float, longitude: float) -> float:
    """The sun's height above the horizon in degrees (NOAA approximation).

    Accurate to a fraction of a degree, which is far better than the cloud
    forecast it is combined with.
    """
    utc = moment.astimezone(UTC)
    day_of_year = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12.0) / 24.0)

    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    solar_minutes = hour * 60.0 + equation_of_time + 4.0 * longitude
    hour_angle = math.radians(solar_minutes / 4.0 - 180.0)
    lat = math.radians(latitude)

    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def sun_factor(
    moment: datetime,
    latitude: float | None,
    longitude: float | None,
    cloud_pct: float | None = None,
) -> float:
    """Sun reaching the house, 0 (night or overcast) to 1 (clear, sun overhead).

    Clear-sky strength follows the sine of the sun's elevation; cloud cover
    dims it with the Kasten–Czeplak relation, which lets thin cloud through
    and cuts most of the sun only when the sky is nearly covered.
    """
    if latitude is None or longitude is None:
        return 0.0
    elevation = solar_elevation(moment, latitude, longitude)
    if elevation <= 0:
        return 0.0
    clear = math.sin(math.radians(elevation))
    cover = UNKNOWN_CLOUD_PCT if cloud_pct is None else max(0.0, min(100.0, cloud_pct))
    return clear * (1.0 - 0.75 * (cover / 100.0) ** 3.4)
