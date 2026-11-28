"""Register the authenticated card-settings transport once per Core."""

import asyncio
from functools import partial

import voluptuous as vol
from homeassistant.components import websocket_api as ws
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import Unauthorized

from .inventory import DOMAIN
from .settings import ENTRY, KEY, TOKEN, SettingsError, matches
from .topology_lifecycle import MARK, enrollment_fence, valid_witness, witness


async def _authorize(hass, connection):
    current = await hass.auth.async_get_user(connection.user.id)
    token = hass.auth.async_get_refresh_token(connection.refresh_token_id)
    if (current is None or not current.is_active or not current.is_admin or current.system_generated
            or token is None or token.user is not current):
        raise Unauthorized
    return current


def _version(value):
    if type(value) is not int or value != 1:
        raise vol.Invalid("Unsupported version")
    return value


def _pattern(pattern):
    def validate(value):
        if not matches(pattern, value):
            raise vol.Invalid("Invalid settings identifier")
        return value
    return validate


async def _dispatch(hass, connection, msg):
    authorize = partial(_authorize, hass, connection)
    topology_get = msg["type"] == f"{DOMAIN}/topology_get"
    topology = topology_get or msg["type"] == f"{DOMAIN}/topology_save"

    async def owned(settings):
        # No suspension between the final current-principal check under the gate
        # and sending the resulting private data to this connection.
        try:
            result = await settings.async_request(authorize, msg)
        except Unauthorized:
            connection.send_error(msg["id"], "unauthorized", "Unauthorized")
        except SettingsError as err:
            code = str(err)
            if topology and code == "settings_unavailable" and settings.topology_busy:
                code = "topology_recovery_required"
            connection.send_error(msg["id"], code, code)
        else:
            connection.send_result(msg["id"], result)

    try:
        await authorize()
        entry = hass.config_entries.async_get_entry(msg["entry_id"])
        runtime = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
        if entry and entry.domain == DOMAIN and topology:
            if MARK not in entry.data:
                raise SettingsError("topology_not_enrolled")
            marked = witness(entry)
            if not valid_witness(marked):
                raise SettingsError("topology_recovery_required")
            if reason := enrollment_fence(hass, entry):
                raise SettingsError(reason)
            if marked["phase"] == "requested":
                raise SettingsError("topology_restart_required")
            busy = runtime and runtime.settings.topology_busy and not (topology_get and runtime.settings.indeterminate_topology)
            if busy or (runtime is None and entry.state is ConfigEntryState.SETUP_ERROR):
                raise SettingsError("topology_recovery_required")
        if runtime is None:
            raise SettingsError("entry_not_loaded")
        if topology_get:
            runtime.settings.require_live(reconcile=True)
        else:
            runtime.settings.require_live()
        await asyncio.shield(hass.async_create_task(owned(runtime.settings), "water card settings"))
    except SettingsError as err:
        connection.send_error(msg["id"], str(err), str(err))


_BASE = {vol.Required("version"): _version, vol.Required("entry_id"): _pattern(ENTRY)}


@ws.websocket_command({vol.Required("type"): f"{DOMAIN}/config_get", **_BASE})
@ws.require_admin
@ws.async_response
async def get_config(hass, connection, msg):
    await _dispatch(hass, connection, msg)


@ws.websocket_command({vol.Required("type"): f"{DOMAIN}/config_save", **_BASE,
                       vol.Required("if_revision"): _pattern(TOKEN), vol.Required("if_source_snapshot"): _pattern(TOKEN),
                       vol.Required("changes"): dict})
@ws.require_admin
@ws.async_response
async def save_config(hass, connection, msg):
    await _dispatch(hass, connection, msg)


@ws.websocket_command({vol.Required("type"): f"{DOMAIN}/topology_save", **_BASE,
                       vol.Required("if_revision"): _pattern(TOKEN), vol.Required("if_source_snapshot"): _pattern(TOKEN),
                       vol.Required("zones"): list, vol.Required("remove"): dict, vol.Required("changes"): dict})
@ws.require_admin
@ws.async_response
async def save_topology(hass, connection, msg):
    await _dispatch(hass, connection, msg)


@ws.websocket_command({vol.Required("type"): f"{DOMAIN}/topology_get", **_BASE})
@ws.require_admin
@ws.async_response
async def get_topology(hass, connection, msg):
    await _dispatch(hass, connection, msg)


@callback
def register(hass):
    if KEY not in hass.data:
        ws.async_register_command(hass, get_config)
        ws.async_register_command(hass, save_config)
        ws.async_register_command(hass, get_topology)
        ws.async_register_command(hass, save_topology)
        hass.data[KEY] = True
