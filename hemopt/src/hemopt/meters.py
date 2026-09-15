"""Find a whole-house power meter among Home Assistant entities.

Entity ids vary with how the device was named, so we match on shape rather
than a fixed id. A HomeWizard P1 typically exposes `sensor.p1_meter_active_power`
for the sum of all phases; the per-phase sensors are deliberately skipped
because the peak tariff is billed on the whole house.
"""

from __future__ import annotations

from .ha import parse_numeric

# Substrings that mark a total (all-phases) active-power sensor.
_TOTAL_MARKERS = ("active_power", "power_w", "total_power", "hushall", "house_power")

# Substrings that mark a single phase — never the whole-house reading.
_PHASE_MARKERS = (
    "_l1",
    "_l2",
    "_l3",
    "_p1",
    "_p2",
    "_p3",
    "phase_1",
    "phase_2",
    "phase_3",
    "phase1",
    "phase2",
    "phase3",
    "fas_1",
    "fas_2",
    "fas_3",
)


def _is_phase_sensor(entity_id: str) -> bool:
    name = entity_id.split(".", 1)[-1].lower()
    return any(marker in name for marker in _PHASE_MARKERS)


def _looks_like_total_power(entity_id: str) -> bool:
    if not entity_id.startswith("sensor."):
        return False
    if _is_phase_sensor(entity_id):
        return False
    name = entity_id.split(".", 1)[-1].lower()
    if name.endswith("_power") or name.endswith("_power_w"):
        return True
    return any(marker in name for marker in _TOTAL_MARKERS)


def rank_power_meter(entity_id: str) -> tuple[int, str]:
    """Lower is better. HomeWizard-style totals beat vague `*_power` names."""
    name = entity_id.split(".", 1)[-1].lower()
    if "active_power" in name and not _is_phase_sensor(entity_id):
        return (0, entity_id)
    if "p1" in name or "homewizard" in name or "hwe" in name:
        return (1, entity_id)
    return (2, entity_id)


def suggest_total_power_entities(states: dict[str, str]) -> list[dict[str, str | float | None]]:
    """Numeric whole-house power sensors, best match first."""
    found: list[dict[str, str | float | None]] = []
    for entity_id, state in states.items():
        if not _looks_like_total_power(entity_id):
            continue
        value = parse_numeric(state)
        if value is None:
            continue
        found.append({"entity_id": entity_id, "value": value, "state": state})

    found.sort(key=lambda row: rank_power_meter(str(row["entity_id"])))
    return found


def pick_default_total_power(states: dict[str, str]) -> str | None:
    """Adopt a meter automatically when the choice is unambiguous."""
    candidates = suggest_total_power_entities(states)
    if not candidates:
        return None

    best_rank = rank_power_meter(str(candidates[0]["entity_id"]))[0]
    top = [row for row in candidates if rank_power_meter(str(row["entity_id"]))[0] == best_rank]
    if len(top) == 1:
        return str(top[0]["entity_id"])
    return None
