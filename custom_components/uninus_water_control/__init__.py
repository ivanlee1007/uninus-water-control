"""Uninus Water Control: a read-only inventory and sensor platform."""

from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .inventory import DOMAIN, inventory

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Expose the single public module when Core has an HTTP server."""
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
def _async_prune_levels(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove registry records for deleted tanks, and only those we own."""
    active_ids = {tank["id"] for zone in inventory(entry) for tank in zone["tanks"]}
    registry = er.async_get(hass)
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        if row.platform == DOMAIN and row.domain == Platform.SENSOR and row.unique_id not in active_ids:
            registry.async_remove(row.entity_id)


@callback
def async_prune_disabled_levels(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reconcile committed options when a disabled entry will not set up again."""
    if entry.disabled_by and entry.state is ConfigEntryState.NOT_LOADED:
        _async_prune_levels(hass, entry)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reconcile the registry and load the current sensors."""
    _async_prune_levels(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the sensors and their source listeners before pruning stale rows."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    _async_prune_levels(hass, entry)
    return True
