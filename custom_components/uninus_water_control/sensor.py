"""Read-only tank level sensors."""

from math import isfinite
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfLength
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import DistanceConverter

from .inventory import Tank, Zone, inventory, level_source

LEVEL_UNITS = (UnitOfLength.METERS, UnitOfLength.CENTIMETERS, UnitOfLength.MILLIMETERS)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Expose exactly one level sensor for each tank."""
    async_add_entities(
        TankLevel(zone, tank, zone_index, tank_index)
        for zone_index, zone in enumerate(inventory(entry))
        for tank_index, tank in enumerate(zone["tanks"])
    )


class TankLevel(SensorEntity):
    """Publish a source level in metres, or unavailable if it cannot be read."""

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_suggested_unit_of_measurement = UnitOfLength.METERS
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "tank_level"
    _attr_native_value = None
    _attr_available = False

    def __init__(self, zone: Zone, tank: Tank, zone_index: int, tank_index: int) -> None:
        """Bind one stable tank identity to its configured source."""
        self._attr_unique_id = tank["id"]
        self._attr_translation_placeholders = {"zone": zone["name"], "tank": tank["name"]}
        self._attr_extra_state_attributes = {
            "uninus_water_dashboard": {
                "version": 1,
                "zone": {"id": zone["id"], "name": zone["name"], "index": zone_index},
                "tank": {"id": tank["id"], "name": tank["name"], "index": tank_index},
            }
        }
        self._tank = tank
        self._source: str | None = None
        self._unsub_source: CALLBACK_TYPE | None = None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        """Keep layout metadata discoverable when Core omits unavailable state extras."""
        return (super().capability_attributes or {}) | self._attr_extra_state_attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe and read the initial state without an intervening yield."""
        self.async_on_remove(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed)
        )
        self.async_on_remove(self._async_clear_source)
        self._async_refresh_source(level_source(er.async_get(self.hass), self._tank))

    @callback
    def _async_clear_source(self) -> None:
        if self._unsub_source is not None:
            self._unsub_source()
            self._unsub_source = None
        self._source = None

    @callback
    def _async_refresh_source(self, source: str | None) -> None:
        if source != self._source:
            self._async_clear_source()
            self._source = source
            if source is not None:
                self._unsub_source = async_track_state_change_event(
                    self.hass, [source], self._async_source_changed
                )
        self._read_source(self.hass.states.get(source) if source is not None else None)

    @callback
    def _async_registry_changed(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        source = level_source(er.async_get(self.hass), self._tank)
        if source != self._source:
            self._async_refresh_source(source)
            self.async_write_ha_state()

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        # Resolve again: a previously queued state event may refer to the old
        # entity ID after a registry move or removal has already happened.
        self._async_refresh_source(level_source(er.async_get(self.hass), self._tank))
        self.async_write_ha_state()

    def _read_source(self, state: State | None) -> None:
        self._attr_native_value = _level_metres(state)
        self._attr_available = self._attr_native_value is not None


def _level_metres(state: State | None) -> float | None:
    if state is None or (unit := state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)) not in LEVEL_UNITS:
        return None
    try:
        value = float(state.state)
    except (ValueError, OverflowError):
        return None
    if not isfinite(value):
        return None
    metres = DistanceConverter.convert(value, unit, UnitOfLength.METERS)
    return metres if isfinite(metres) else None
