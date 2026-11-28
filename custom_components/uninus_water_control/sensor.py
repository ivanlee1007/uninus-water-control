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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import DistanceConverter

from .inventory import DOMAIN, Tank, Zone, inventory, level_source
from .settings import IDENT, PAIRS, _binding, matches

LEVEL_UNITS = (UnitOfLength.METERS, UnitOfLength.CENTIMETERS, UnitOfLength.MILLIMETERS)
_METRE_SHIFTS = {
    unit: Decimal(str(DistanceConverter.convert(1, unit, UnitOfLength.METERS))).adjusted() for unit in LEVEL_UNITS
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Expose level and any configured read-only previews."""
    platform = async_get_current_platform()
    entry.runtime_data.settings.platforms[platform.domain] = platform
    async_add_entities(
        TankLevel(entry, zone, tank, zone_index, tank_index)
        for zone_index, zone in enumerate(inventory(entry))
        for tank_index, tank in enumerate(zone["tanks"])
    )
    async_add_entities(ManualStatus(role) for role in entry.runtime_data.roles.values())
    async_add_entities(
        RefillPreview(entry, zone, effective)
        for zone in inventory(entry)
        for tank in zone["tanks"]
        if "refill_lower_m" in (effective := entry.runtime_data.settings.with_thresholds(tank))["thresholds"]
    )


class ManualStatus(SensorEntity):
    """A command and HA feedback snapshot, never a claim of physical flow."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, role):
        self._role = role
        self._attr_unique_id = f"{role.owner.tank_id}_{role.name}_status"
        self._attr_translation_key = f"{role.name}_status"
        self._attr_translation_placeholders = dict(role.owner.labels)

    async def async_added_to_hass(self):
        self.async_on_remove(self._role.subscribe(self.async_write_ha_state))

    @property
    def native_value(self):
        return self._role.snapshot()["phase"]

    @property
    def extra_state_attributes(self):
        return {"uninus_water_manual": self._role.identity("status") | self._role.snapshot()}


@callback
def async_update_labels(entity: Entity, labels: dict[str, str]) -> bool:
    """Refresh generated labels while leaving the Registry's user name alone."""
    if dict(entity.translation_placeholders) == labels:
        return False
    entity._attr_translation_placeholders = dict(labels)
    # Core caches the formatted name independently of its placeholders.
    entity.__dict__.pop("name", None)
    if entity.registry_entry is not None:
        er.async_get(entity.hass).async_update_entity(entity.entity_id, original_name=entity.name)
    return True


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
            "uninus_water_settings_entry": entry.entry_id,
            "uninus_water_dashboard": {
                "version": 1,
                "zone": {"id": zone["id"], "name": zone["name"], "index": zone_index},
                "tank": {"id": tank["id"], "name": tank["name"], "index": tank_index},
            }
        }
        self._tank = tank
        self._refresh_display()
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

    def _refresh_display(self) -> bool:
        """Update only the public effective display fields."""
        policy = self._entry.runtime_data.settings.tank_view(self._tank)
        display = {"version": 1, **{kind: tuple(policy[kind]["effective"]) for kind in PAIRS
                                   if policy[kind]["effective"] is not None}}
        if policy["scale_max_m"] is not None:
            display["scale_max_m"] = policy["scale_max_m"]
        extra = self._attr_extra_state_attributes
        key = "uninus_water_display"
        old = extra.get(key)
        if len(display) > 1:
            extra[key] = display
        else:
            extra.pop(key, None)
        return old != extra.get(key)

    @callback
    def async_apply_topology(self, zone, tank, zone_index, tank_index):
        before = self._attr_extra_state_attributes["uninus_water_dashboard"]
        after = {"version": 1, "zone": {"id": zone["id"], "name": zone["name"], "index": zone_index},
                 "tank": {"id": tank["id"], "name": tank["name"], "index": tank_index}}
        source_changed = _binding(self._tank, "level_sensor") != _binding(tank, "level_sensor")
        self._tank = tank
        if before != after:
            self._attr_extra_state_attributes["uninus_water_dashboard"] = after
        if source_changed:
            self._async_refresh_source(level_source(er.async_get(self.hass), tank))
        labels_changed = async_update_labels(self, {"zone": zone["name"], "tank": tank["name"]})
        display_changed = self._refresh_display()
        if before != after or source_changed or display_changed or labels_changed:
            self.async_write_ha_state()

    @callback
    def _async_entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if (change is ConfigEntryChange.UPDATED and entry is self._entry
                and entry.state is ConfigEntryState.LOADED and self._refresh_display()):
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

    @callback
    def async_policy_changed(self, tank: Tank, zone: Zone | None = None) -> None:
        labels = {"zone": zone["name"] if zone else self.translation_placeholders["zone"], "tank": tank["name"]}
        labels_changed = async_update_labels(self, labels)
        policy_changed = (self._threshold_values(self._tank) != self._threshold_values(tank)
                          or _binding(self._tank, "level_sensor") != _binding(tank, "level_sensor"))
        self._tank = tank
        if policy_changed:
            self._configure_thresholds(tank)
            self._async_rebind(self._resolve_source())
        # A rename must not restart an in-progress alarm qualification timer.
        if policy_changed or labels_changed:
            self._async_queue_publish()

    def _threshold_values(self, tank: Tank) -> tuple:
        return tuple(tank["thresholds"][key] for key in self._policy_keys)

    def _configure_thresholds(self, tank):
        raise NotImplementedError

    def _observe(self, level: Decimal | None) -> None:
        raise NotImplementedError


class RefillPreview(ThresholdEntity, SensorEntity):
    """Classify the current level, without inferring any actuator state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["refill_threshold", "between_thresholds", "stop_threshold"]
    _attr_native_value = None
    _policy_keys = PAIRS["refill"]

    def __init__(self, entry: ConfigEntry, zone: Zone, tank: Tank) -> None:
        super().__init__(entry, zone, tank, "refill_preview")
        self._configure_thresholds(tank)

    def _configure_thresholds(self, tank):
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


async def async_reconcile_settings(hass, entry, settings):
    """Refresh only this entry's projections; do not reload platform owners."""
    from .binary_sensor import LowLevelAlarm
    from .button import ManualButton

    wanted = {"sensor": {}, "binary_sensor": {}, "button": {}}
    levels = {}
    for zi, zone in enumerate(inventory(entry)):
        for ti, tank in enumerate(zone["tanks"]):
            levels[tank["id"]] = (zone, tank, zi, ti)
            effective = settings.with_thresholds(tank)
            for domain, kind, cls, suffix in (
                ("sensor", "refill", RefillPreview, "refill_preview"),
                ("binary_sensor", "alarm", LowLevelAlarm, "low_level_alarm"),
            ):
                if PAIRS[kind][0] in effective["thresholds"]:
                    wanted[domain][f"{tank['id']}_{suffix}"] = (cls, zone, effective)
    registry = er.async_get(hass)
    for platform in settings.platforms.values():
        for entity in platform.entities.values():
            if isinstance(entity, (ManualStatus, ManualButton)) and async_update_labels(entity, entity._role.owner.labels):
                entity.async_write_ha_state()
        if platform.domain == "button":
            continue
        upcoming = wanted[platform.domain]
        suffix = "_refill_preview" if platform.domain == "sensor" else "_low_level_alarm"
        for row in er.async_entries_for_config_entry(registry, entry.entry_id):
            if row.platform != DOMAIN or row.domain != platform.domain:
                continue
            obsolete_preview = row.unique_id.endswith(suffix) and row.unique_id not in upcoming
            obsolete_level = platform.domain == "sensor" and matches(IDENT, row.unique_id) and row.unique_id not in levels
            if obsolete_preview or obsolete_level:
                if row.entity_id in platform.entities:
                    await platform.async_remove_entity(row.entity_id)
                registry.async_remove(row.entity_id)
        additions = []
        if platform.domain == "sensor":
            for ident, args in levels.items():
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, ident)
                if isinstance(entity := platform.entities.get(entity_id), TankLevel):
                    entity.async_apply_topology(*args)
                elif entity_id is None or not registry.async_get(entity_id).disabled_by:
                    additions.append(TankLevel(entry, *args))
        for ident, (cls, zone, tank) in upcoming.items():
            entity_id = registry.async_get_entity_id(platform.domain, DOMAIN, ident)
            entity = platform.entities.get(entity_id)
            if isinstance(entity, ThresholdEntity):
                entity.async_policy_changed(tank, zone)
            elif entity_id is None or not registry.async_get(entity_id).disabled_by:
                additions.append(cls(entry, zone, tank))
        if additions:
            await platform.async_add_entities(additions)
