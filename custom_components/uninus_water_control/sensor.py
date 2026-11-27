"""Read-only tank level sensors."""

from asyncio import Handle
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange, ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, EVENT_STATE_CHANGED, UnitOfLength
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import DistanceConverter

from .inventory import DISPLAY_SCALES, Tank, Zone, inventory, level_source

LEVEL_UNITS = (UnitOfLength.METERS, UnitOfLength.CENTIMETERS, UnitOfLength.MILLIMETERS)
_METRE_SHIFTS = {
    unit: Decimal(str(DistanceConverter.convert(1, unit, UnitOfLength.METERS))).adjusted() for unit in LEVEL_UNITS
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Expose level and any configured read-only previews."""
    async_add_entities(
        TankLevel(entry, zone, tank, zone_index, tank_index)
        for zone_index, zone in enumerate(inventory(entry))
        for tank_index, tank in enumerate(zone["tanks"])
    )
    async_add_entities(ManualStatus(role) for role in entry.runtime_data.roles.values())
    async_add_entities(
        RefillPreview(entry, zone, tank)
        for zone in inventory(entry)
        for tank in zone["tanks"]
        if "refill_lower_m" in tank.get("thresholds", {})
    )


class ManualStatus(SensorEntity):
    """A command and HA feedback snapshot, never a claim of physical flow."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, role):
        self._role = role
        self._attr_unique_id = f"{role.owner.tank_id}_{role.name}_status"
        self._attr_translation_key = f"{role.name}_status"
        self._attr_translation_placeholders = role.owner.labels

    async def async_added_to_hass(self):
        self.async_on_remove(self._role.subscribe(self.async_write_ha_state))

    @property
    def native_value(self):
        return self._role.snapshot()["phase"]

    @property
    def extra_state_attributes(self):
        return {"uninus_water_manual": self._role.identity("status") | self._role.snapshot()}


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

    def __init__(self, entry: ConfigEntry, zone: Zone, tank: Tank, zone_index: int, tank_index: int) -> None:
        """Bind one stable tank identity to its configured source."""
        self._entry = entry
        self._attr_unique_id = tank["id"]
        self._attr_translation_placeholders = {"zone": zone["name"], "tank": tank["name"]}
        self._attr_extra_state_attributes = {
            "uninus_water_dashboard": {
                "version": 1,
                "zone": {"id": zone["id"], "name": zone["name"], "index": zone_index},
                "tank": {"id": tank["id"], "name": tank["name"], "index": tank_index},
            }
        }
        display = {"version": 1}
        thresholds = tank.get("thresholds", {})
        for name, first, second in (
            ("refill", "refill_lower_m", "refill_upper_m"),
            ("alarm", "alarm_lower_m", "alarm_delay_seconds"),
        ):
            if first in thresholds and second in thresholds:
                display[name] = (thresholds[first], thresholds[second])
        if len(display) > 1:
            self._attr_extra_state_attributes["uninus_water_display"] = display
        self._refresh_display_scale()
        self._tank = tank
        self._source: str | None = None
        self._unsub_source: CALLBACK_TYPE | None = None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        """Keep layout metadata discoverable when Core omits unavailable state extras."""
        return (super().capability_attributes or {}) | self._attr_extra_state_attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe and read the initial state without an intervening yield."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._async_entry_changed))
        self.async_on_remove(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed))
        self.async_on_remove(self._async_clear_source)
        self._async_refresh_source(level_source(er.async_get(self.hass), self._tank))

    def _refresh_display_scale(self) -> bool:
        """Change only the display maximum, without observing the source again."""
        key = "uninus_water_display"
        old = self._attr_extra_state_attributes.get(key, {"version": 1})
        value = self._entry.data.get(DISPLAY_SCALES, {}).get(self._attr_unique_id)
        if value == old.get("scale_max_m"):
            return False
        display = dict(old)
        if value is None:
            display.pop("scale_max_m", None)
        else:
            display["scale_max_m"] = value
        if len(display) > 1:
            self._attr_extra_state_attributes[key] = display
        else:
            self._attr_extra_state_attributes.pop(key, None)
        return True

    @callback
    def _async_entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if (change is ConfigEntryChange.UPDATED and entry is self._entry
                and entry.state is ConfigEntryState.LOADED and self._refresh_display_scale()):
            self.async_write_ha_state()

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
                self._unsub_source = async_track_state_change_event(self.hass, [source], self._async_source_changed)
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


def _threshold_level_metres(state: State | None) -> Decimal | None:
    """Reuse the level admission rules and scale the actual decimal sample exactly."""
    if state is None or _level_metres(state) is None:
        return None
    shift = _METRE_SHIFTS[state.attributes[ATTR_UNIT_OF_MEASUREMENT]]
    try:
        sign, digits, exponent = Decimal(state.state).as_tuple()
        # Metric conversions are powers of ten. Shifting the exponent avoids
        # both binary float artifacts and rounding an above-threshold sample.
        return Decimal((sign, digits, exponent + shift))
    except InvalidOperation:
        return None


class ThresholdEntity(Entity):
    """Observe the configured level source; never restore a previous projection."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_available = False

    def __init__(self, entry: ConfigEntry, zone: Zone, tank: Tank, kind: str) -> None:
        self._entry = entry
        self._tank = tank
        self._source: str | None = None
        self._attached = False
        self._pending_publish: Handle | None = None
        self._attr_unique_id = f"{tank['id']}_{kind}"
        self._attr_translation_key = kind
        self._attr_translation_placeholders = {"zone": zone["name"], "tank": tank["name"]}
        self._metadata = {"version": 1, "zone_id": zone["id"], "tank_id": tank["id"], "kind": kind}

    @property
    def capability_attributes(self) -> dict[str, Any]:
        return (super().capability_attributes or {}) | {"uninus_water_thresholds": self._metadata}

    @property
    def _live(self) -> bool:
        return self._attached and self._entry.state in (ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.LOADED)

    def _resolve_source(self) -> str | None:
        registry = er.async_get(self.hass)
        source = level_source(registry, self._tank)
        if source is not None and (row := registry.async_get(source)) is not None and row.disabled_by:
            return None
        return source

    async def async_added_to_hass(self) -> None:
        self._attached = True
        self.async_on_remove(self._async_detach)
        self.async_on_remove(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed))
        # Filter the native bus delivery synchronously. The keyed helper defers
        # its callbacks and may deliver old events to a just-added subscriber.
        self.async_on_remove(self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._async_state_changed, event_filter=self._matches_source,
        ))
        self._async_rebind(self._resolve_source())

    @callback
    def _matches_source(self, data: EventStateChangedData) -> bool:
        return self._source == data["entity_id"]

    @callback
    def _async_detach(self) -> None:
        self._attached = False
        if self._pending_publish is not None:
            self._pending_publish.cancel()
            self._pending_publish = None
        self._async_rebind(None)

    @callback
    def _async_rebind(self, source: str | None) -> None:
        self._observe(None)
        self._source = source
        if source is not None:
            self._observe(_threshold_level_metres(self.hass.states.get(source)))

    @callback
    def _async_registry_changed(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if self._live and (source := self._resolve_source()) != self._source:
            self._async_rebind(source)
            self._async_queue_publish()

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        if not self._live:
            return
        if (source := self._resolve_source()) != self._source:
            self._async_rebind(source)
        else:
            captured = event.data["new_state"]
            self._observe(_threshold_level_metres(captured))
            # An earlier bus listener can update this source inline, delivering
            # the nested frame first. Honor the outer frame's interruption, but
            # settle to the current State before publishing or yielding.
            current = self.hass.states.get(event.data["entity_id"])
            if current is not captured:
                self._observe(_threshold_level_metres(current))
        self._async_queue_publish()

    @callback
    def _async_queue_publish(self) -> None:
        # Consume each source event now, but publish after the bus finishes
        # dispatching it to all observers. Downstream reactions can update the
        # source synchronously and must not overtake the original frame.
        if self._pending_publish is None:
            self._pending_publish = self.hass.loop.call_soon(self._async_publish)

    @callback
    def _async_publish(self) -> None:
        self._pending_publish = None
        if self._live:
            self.async_write_ha_state()

    def _observe(self, level: Decimal | None) -> None:
        raise NotImplementedError


class RefillPreview(ThresholdEntity, SensorEntity):
    """Classify the current level, without inferring any actuator state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["refill_threshold", "between_thresholds", "stop_threshold"]
    _attr_native_value = None

    def __init__(self, entry: ConfigEntry, zone: Zone, tank: Tank) -> None:
        super().__init__(entry, zone, tank, "refill_preview")
        self._lower = Decimal(str(tank["thresholds"]["refill_lower_m"]))
        self._upper = Decimal(str(tank["thresholds"]["refill_upper_m"]))

    def _observe(self, level: Decimal | None) -> None:
        self._attr_available = level is not None
        if level is None:
            self._attr_native_value = None
        elif level <= self._lower:
            self._attr_native_value = "refill_threshold"
        elif level >= self._upper:
            self._attr_native_value = "stop_threshold"
        else:
            self._attr_native_value = "between_thresholds"
