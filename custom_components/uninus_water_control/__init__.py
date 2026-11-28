"""Uninus Water Control: inventory, monitoring and commissioned manual control."""

from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er

from . import alarm_history
from .config_api import register
from .inventory import DOMAIN, inventory
from .manual import ACTIONS, ROLES, ManualController
from .settings import Settings, SettingsError
from .topology_lifecycle import MARK, VERSION

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Expose the single public module when Core has an HTTP server."""
    register(hass)
    alarm_history.register(hass)
    if hass.http is not None:
        from aiohttp import web
        from homeassistant.helpers.http import HomeAssistantView

        class DashboardModuleView(HomeAssistantView):
            url = f"/{DOMAIN}/dashboard.js"
            name = f"{DOMAIN}:dashboard"
            requires_auth = False

            async def get(self, request: web.Request) -> web.FileResponse:
                return web.FileResponse(
                    Path(__file__).with_name("dashboard.js"),
                    headers={"Content-Type": "text/javascript", "Cache-Control": "no-store"},
                )

        hass.http.register_view(DashboardModuleView)
    return True


@callback
def _async_prune_levels(hass: HomeAssistant, entry: ConfigEntry, settings: Settings | None = None) -> None:
    """Remove only our registry records for projections no longer configured."""
    if MARK in entry.data and (settings is None or settings.topology is None or settings.topology_busy):
        # Pending and recovery states retain registry identity for restoration.
        return
    tanks = [tank for zone in (settings.zones if settings else inventory(entry)) for tank in zone["tanks"]]
    active_ids = {tank["id"] for tank in tanks}
    # Without an active settings owner, only remove rows for retired tanks.
    projected = [settings.with_thresholds(t) if settings else t for t in tanks]
    previews = {f"{t['id']}_refill_preview" for t in projected if settings is None or t["thresholds"].get("refill_lower_m") is not None}
    alarms = {f"{t['id']}_low_level_alarm" for t in projected if settings is None or t["thresholds"].get("alarm_lower_m") is not None}
    manual = settings.manual if settings else entry.options.get("manual") or {}
    tank_id = manual.get("tank_id")
    roles = [role for role in ROLES if tank_id in active_ids and manual.get(role)]
    buttons = {f"{tank_id}_{role}_{action}" for role in roles for action in ACTIONS}
    active_ids.update(f"{tank_id}_{role}_status" for role in roles)
    registry = er.async_get(hass)
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        if row.platform == DOMAIN and (
            (row.domain == Platform.SENSOR and row.unique_id not in active_ids | previews)
            or (row.domain == Platform.BINARY_SENSOR and row.unique_id not in alarms)
            or (row.domain == Platform.BUTTON and row.unique_id not in buttons)
        ):
            registry.async_remove(row.entity_id)


@callback
def async_prune_disabled_levels(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reconcile committed options when a disabled entry will not set up again."""
    if entry.disabled_by and entry.state is ConfigEntryState.NOT_LOADED:
        _async_prune_levels(hass, entry)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Introduce the lifecycle schema without implicitly enrolling old entries."""
    if entry.version == 1 and MARK not in entry.data:
        hass.config_entries.async_update_entry(entry, version=VERSION)
        return True
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reconcile the registry and load the current sensors."""
    settings = Settings(hass, entry)
    try:
        await settings.async_initialize()
    except SettingsError as err:
        restart = str(err) == "topology_restart_required"
        message = ("Water topology handoff awaits a fresh Home Assistant process. Keep the independent cutoff in effect."
                   if restart else "Water topology recovery required. Output identity and physical state are unverified. "
                   "Use the independent device cutoff; restore the matching backup before restarting.")
        raise ConfigEntryError(message, translation_domain=DOMAIN, translation_key=str(err)) from None
    _async_prune_levels(hass, entry, settings)
    entry.runtime_data = ManualController(hass, entry, settings)
    platforms = [*PLATFORMS, Platform.BUTTON] if entry.runtime_data.roles else PLATFORMS
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    entry.runtime_data.activate()
    alarm_history.attach(hass, entry, entry.runtime_data)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the sensors and their source listeners before pruning stale rows."""
    runtime = entry.runtime_data
    alarm_history.detach(hass, runtime)
    if not await runtime.async_close():
        # Core has already entered UNLOAD_IN_PROGRESS and a false result is
        # terminal (FAILED_UNLOAD). Keep the old owner for late OFF cleanup.
        runtime.require_restart()
        return False
    async with runtime.settings.lock:
        platforms = [*PLATFORMS, Platform.BUTTON] if runtime.roles else PLATFORMS
        if not await hass.config_entries.async_unload_platforms(entry, platforms):
            runtime.require_restart()
            return False
        runtime.detach()
        _async_prune_levels(hass, entry, runtime.settings)
    return True
