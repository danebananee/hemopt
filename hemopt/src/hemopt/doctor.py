"""Configuration check against a live Home Assistant.

Half of getting this running is knowing whether the entity ids in the config
actually exist, and entity ids generated from device names are hard to guess
from the outside. This walks the config, reports what is missing, and for
anything missing offers the closest ids Home Assistant does have.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

import httpx

from .config import Config
from .ha import HomeAssistantClient, parse_numeric

OK = "ok"
WARN = "varning"
FAIL = "fel"


@dataclass(slots=True)
class Finding:
    level: str
    label: str
    detail: str
    suggestions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return sum(1 for finding in self.findings if finding.level == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for finding in self.findings if finding.level == WARN)

    def add(self, level: str, label: str, detail: str, suggestions: list[str] | None = None):
        self.findings.append(Finding(level, label, detail, suggestions or []))


def _suggest(entity_id: str, known: list[str], limit: int = 3) -> list[str]:
    """Closest existing entity ids, matched on domain first then on name.

    A missing `climate.abc` is almost always a naming mismatch rather than a
    missing device, so candidates from the same domain are worth far more than
    a lexically closer id in some other domain.
    """
    domain = entity_id.split(".", 1)[0]
    same_domain = [candidate for candidate in known if candidate.startswith(f"{domain}.")]
    matches = difflib.get_close_matches(entity_id, same_domain, n=limit, cutoff=0.5)
    if matches:
        return matches

    # Fall back to the object id, which catches a device that exists but was
    # registered under a different domain than the config assumes.
    object_id = entity_id.split(".", 1)[-1]
    scored = [
        candidate
        for candidate in same_domain
        if object_id[:12] and object_id[:12] in candidate.split(".", 1)[-1]
    ]
    return scored[:limit]


async def run_doctor(config: Config) -> Report:
    report = Report()

    try:
        async with HomeAssistantClient(config.home_assistant) as ha:
            states = await ha.states()
    except (httpx.HTTPError, OSError) as exc:
        report.add(
            FAIL, "Home Assistant", f"gick inte att na {config.home_assistant.base_url}: {exc}"
        )
        return report

    report.add(OK, "Home Assistant", f"ansluten, {len(states)} entiteter")
    known = sorted(states)

    def check(entity_id: str | None, label: str, *, numeric: bool, required: bool) -> None:
        if not entity_id:
            level = FAIL if required else WARN
            report.add(level, label, "inte konfigurerad")
            return

        if entity_id not in states:
            report.add(
                FAIL if required else WARN,
                label,
                f"{entity_id} finns inte i Home Assistant",
                _suggest(entity_id, known),
            )
            return

        value = states[entity_id]
        if value in {"unavailable", "unknown"}:
            report.add(WARN, label, f"{entity_id} ar {value}")
        elif numeric and parse_numeric(value) is None:
            report.add(WARN, label, f"{entity_id} ar inte numerisk: {value!r}")
        else:
            report.add(OK, label, f"{entity_id} = {value}")

    check(config.site.weather_entity, "Vaderprognos", numeric=False, required=False)
    check(config.heat_pump.outdoor_entity, "Utetemperatur", numeric=True, required=True)
    check(config.heat_pump.power_entity, "Varmepumpens effekt", numeric=True, required=False)
    check(
        config.base_load.total_power_entity,
        "Husets totala effekt",
        numeric=True,
        required=False,
    )

    if config.hot_water.enabled:
        check(
            config.hot_water.top_temperature_entity,
            "Varmvatten, topp",
            numeric=True,
            required=True,
        )

    if config.ext_control.enabled:
        check(
            config.ext_control.block_heating_entity,
            "EXT, blockera varme",
            numeric=False,
            required=True,
        )
        check(
            config.ext_control.block_hot_water_entity,
            "EXT, blockera varmvatten",
            numeric=False,
            required=False,
        )

    if config.wood_stove.enabled:
        check(
            config.wood_stove.temperature_entity,
            "Braskamin, temperatur",
            numeric=True,
            required=not bool(config.wood_stove.binary_entity),
        )
        check(
            config.wood_stove.binary_entity,
            "Braskamin, binary",
            numeric=False,
            required=not bool(config.wood_stove.temperature_entity),
        )
        if not config.wood_stove.room_keys:
            report.add(
                WARN,
                "Braskamin",
                "enabled utan room_keys — ange vilka rum som kanner brasans varme",
            )

    if not config.rooms:
        report.add(FAIL, "Rum", "inga rum konfigurerade")

    rooms_with_climate = 0
    for room in config.rooms:
        check(room.temperature_entity, f"{room.name}, temperatur", numeric=True, required=True)
        if room.climate_entity:
            rooms_with_climate += 1
            if room.climate_entity not in states:
                report.add(
                    FAIL,
                    f"{room.name}, termostat",
                    f"{room.climate_entity} saknas (Entity not found på Golvvärme). "
                    "Update/Restart hemopt, sedan Restart Home Assistant — "
                    "LK Arc Climate skapas automatiskt.",
                    _suggest(room.climate_entity, known),
                )
            else:
                check(room.climate_entity, f"{room.name}, termostat", numeric=False, required=True)

    if config.heat_pump.room_setpoint_entity:
        check(
            config.heat_pump.room_setpoint_entity,
            "Husets rumsborvarde",
            numeric=False,
            required=False,
        )
        if rooms_with_climate == 0:
            report.add(
                OK,
                "Rumstyrning",
                "ingen climate per rum — hemopt skriver husborvarde via "
                f"{config.heat_pump.room_setpoint_entity}",
            )
    elif rooms_with_climate == 0 and config.rooms:
        report.add(
            WARN,
            "Rumstyrning",
            "inga climate per rum och ingen heat_pump.room_setpoint_entity — "
            "planen laser sensorer men skriver inget inomhusborvarde. "
            "Med LK Arc: sat room_setpoint_entity till climate.h66_hproom_temp_setpoint "
            "(kraver ofta ROOM_CTRL=1 pa H66)",
        )

    if config.peak_tariff.enabled and not config.base_load.total_power_entity:
        from .meters import suggest_total_power_entities

        suggestions = [row["entity_id"] for row in suggest_total_power_entities(states)]
        report.add(
            WARN,
            "Effektavgift",
            "paslagen utan matare pa hela huset, sa toppvakten ser bara varmepumpen",
            suggestions[:5],
        )
    elif not config.base_load.total_power_entity:
        from .meters import suggest_total_power_entities

        suggestions = [row["entity_id"] for row in suggest_total_power_entities(states)]
        if suggestions:
            report.add(
                WARN,
                "Husets totala effekt",
                "inte konfigurerad, men det finns kandidater",
                suggestions[:5],
            )

    return report


def format_report(report: Report) -> str:
    symbols = {OK: "  ok  ", WARN: " varn ", FAIL: " FEL  "}
    lines = []
    for finding in report.findings:
        lines.append(f"[{symbols[finding.level]}] {finding.label}: {finding.detail}")
        for suggestion in finding.suggestions:
            lines.append(f"              menade du {suggestion} ?")

    lines.append("")
    if report.failures:
        lines.append(f"{report.failures} fel och {report.warnings} varningar. Ratta felen forst.")
    elif report.warnings:
        lines.append(f"Inga fel, {report.warnings} varningar. Systemet kan koras.")
    else:
        lines.append("Allt ser bra ut.")
    return "\n".join(lines)
