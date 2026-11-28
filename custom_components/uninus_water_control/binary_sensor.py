"""Read-only delayed low-level alarm; it does not send notifications or commands."""

from datetime import datetime
from decimal import Decimal

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_call_later

from .inventory import Tank, Zone, inventory
from .sensor import ThresholdEntity
from .settings import PAIRS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Expose each independently configured alarm."""
    platform = async_get_current_platform()
    entry.runtime_data.settings.platforms[platform.domain] = platform
    async_add_entities(
        LowLevelAlarm(entry, zone, effective)
        for zone in inventory(entry)
        for tank in zone["tanks"]
        if "alarm_lower_m" in (effective := entry.runtime_data.settings.with_thresholds(tank))["thresholds"]
    )


class LowLevelAlarm(ThresholdEntity, BinarySensorEntity):
    """Alarm only after an uninterrupted low interval under this entity owner."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_is_on = False
    _policy_keys = PAIRS["alarm"]

    def __init__(self, entry: ConfigEntry, zone: Zone, tank: Tank) -> None:
        super().__init__(entry, zone, tank, "low_level_alarm")
        self._configure_thresholds(tank)
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._interval: object | None = None

    def _configure_thresholds(self, tank):
        self._lower = Decimal(str(tank["thresholds"]["alarm_lower_m"]))
        self._delay = tank["thresholds"]["alarm_delay_seconds"]

    def _observe(self, level: Decimal | None) -> None:
        self._attr_available = level is not None
        if level is None or level > self._lower:
            self._interval = None
            if self._cancel_timer is not None:
                self._cancel_timer()
                self._cancel_timer = None
            self._attr_is_on = False
        elif self._interval is None:
            interval = self._interval = object()

            @callback
            def expire(now: datetime) -> None:
                if not self._live or self._interval is not interval:
                    return
                self._cancel_timer = None
                if (source := self._resolve_source()) != self._source:
                    self._async_rebind(source)
                else:
                    self._attr_is_on = True
                self._async_queue_publish()

            self._cancel_timer = async_call_later(self.hass, self._delay, expire)