"""Canonical configuration records; never mutate config entry contents in place."""

from typing import NotRequired, TypedDict
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_registry import EntityRegistry

DOMAIN = "uninus_water_control"
TITLE = "Uninus Water Control"


class Tank(TypedDict):
    id: str
    name: str
    level_sensor: str
    level_sensor_registry_id: NotRequired[str]
    refill_switch: str | None
    alarm_switch: str | None


class Zone(TypedDict):
    id: str
    name: str
    tanks: list[Tank]


def new_tank(registry: EntityRegistry, fields: dict, tank_id: str | None = None) -> Tank:
    """Build one tank record, preserving its identity on edit."""
    tank = Tank(
        id=tank_id or uuid4().hex,
        name=fields["tank_name"].strip(),
        level_sensor=fields["level_sensor"],
        refill_switch=fields.get("refill_switch"),
        alarm_switch=fields.get("alarm_switch"),
    )
    if source := registry.async_get(tank["level_sensor"]):
        tank["level_sensor_registry_id"] = source.id
    return tank


def new_zone(registry: EntityRegistry, fields: dict) -> Zone:
    """Build an initial zone and its mandatory first tank."""
    return Zone(id=uuid4().hex, name=fields["zone_name"].strip(), tanks=[new_tank(registry, fields)])


def level_source(registry: EntityRegistry, tank: Tank) -> str | None:
    """Resolve a binding without substituting a different registered source."""
    if "level_sensor_registry_id" in tank:
        source = registry.async_get(tank["level_sensor_registry_id"])
        return source.entity_id if source is not None else None
    # Unregistered (including legacy name-only) bindings cannot distinguish
    # unregistered replacements. Never infer ownership of a new registry row.
    entity_id = tank["level_sensor"]
    return None if registry.async_is_registered(entity_id) else entity_id


def inventory(entry: ConfigEntry) -> list[Zone]:
    """Read the effective inventory: native options replace the initial seed."""
    return entry.options["zones"] if "zones" in entry.options else entry.data["zones"]
