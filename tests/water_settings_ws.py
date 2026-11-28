"""Fixture transport: genuine HA ActiveConnection and AuthManager with isolated users."""

import asyncio
import json
from logging import getLogger

from homeassistant import auth
from homeassistant.components.websocket_api.connection import ActiveConnection


async def settings_client(hass, entry, group="system-admin"):
    if not getattr(hass, "auth", None):
        hass.auth = await auth.auth_manager_from_config(hass, [], [])
        await hass.auth.async_create_user("Separate test owner")
    user = await hass.auth.async_create_user("Settings test", group_ids=[group])
    token = await hass.auth.async_create_refresh_token(user, "https://localhost/")
    queue = asyncio.Queue()
    connection = ActiveConnection(getLogger(__name__), hass, queue.put_nowait, user, token)

    async def request(command, **message):
        connection.async_handle({
            "id": connection.last_id + 1, "type": f"uninus_water_control/config_{command}", "version": 1,
            "entry_id": entry.entry_id, **message,
        })
        reply = await asyncio.wait_for(queue.get(), 4)
        return json.loads(reply) if isinstance(reply, (str, bytes)) else reply

    return request, user

def compare(base):
    return {"if_revision": base["revision"], "if_source_snapshot": base["source_snapshot"]}
