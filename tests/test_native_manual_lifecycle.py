"""Native Core lifecycle regressions with isolated simulated switch feedback."""

import asyncio
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState, OperationNotAllowed
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame

DOMAIN = "uninus_water_control"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
TANK = "lifecycle_tank"


class NativeManualLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in ("connect", "connect_ex", "sendto"):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        shutil.copytree(SOURCE, Path(self.directory) / "custom_components" / DOMAIN)
        await self.boot_core()
        self.depth = self.registry.async_get_or_create("sensor", "lifecycle_probe", "depth")
        self.output = self.registry.async_get_or_create("switch", "lifecycle_probe", "refill")
        self.seed_feedback()
        tank = {
            "id": TANK,
            "name": "Test tank",
            "level_sensor": self.depth.entity_id,
            "level_sensor_registry_id": self.depth.id,
            "refill_switch": self.output.entity_id,
            "refill_switch_registry_id": self.output.id,
            "alarm_switch": None,
        }
        self.entry = ConfigEntry(
            domain=DOMAIN,
            title="Test lifecycle entry",
            source="user",
            version=1,
            minor_version=1,
            discovery_keys={},
            subentries_data=[],
            unique_id=None,
            data={"zones": [{"id": "lifecycle_zone", "name": "Test zone", "tanks": [tank]}]},
            options={
                "manual": {
                    "tank_id": TANK,
                    "refill": {
                        "output": self.output.id,
                        "source": self.depth.id,
                        "max_runtime_seconds": 60,
                        "cutoff_confirmed": True,
                    },
                }
            },
        )
        await self.hass.config_entries.async_add(self.entry)
        await self.hass.async_block_till_done()
        self.assertIs(self.entry.state, ConfigEntryState.LOADED)
        self.assertTrue(self.status()["can_start"])
        self.assertEqual(self.effects, [])

    async def boot_core(self):
        self.hass = hass = HomeAssistant(self.directory)
        self.addAsyncCleanup(hass.async_stop)
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        hass.auth = await auth.auth_manager_from_config(hass, [], [])
        user = await hass.auth.async_create_user("Test operator", group_ids=["system-admin"])
        self.context = Context(user_id=user.id)
        await hass.config_entries.async_initialize()
        await hass.async_start()
        self.registry = er.async_get(hass)
        self.calls = asyncio.Queue()
        self.effects = []
        self.on_ack = asyncio.Event()
        self.on_ack.set()
        hass.services.async_register("switch", "turn_on", self.handle_switch)
        hass.services.async_register("switch", "turn_off", self.handle_switch)

    def seed_feedback(self):
        self.hass.states.async_set(self.depth.entity_id, "1.25", {"unit_of_measurement": "m"})
        self.hass.states.async_set(self.output.entity_id, "off")

    async def handle_switch(self, call: ServiceCall):
        entity_id = call.data["entity_id"]
        state = "on" if call.service == "turn_on" else "off"
        self.effects.append((entity_id, state))
        self.calls.put_nowait((entity_id, state))
        if state == "on":
            await self.on_ack.wait()
        self.hass.states.async_set(entity_id, state)

    async def press(self, action):
        entity_id = self.registry.async_get_entity_id("button", DOMAIN, f"{TANK}_refill_{action}")
        self.assertIsNotNone(entity_id)
        await self.hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True, context=self.context
        )

    def status(self):
        entity_id = self.registry.async_get_entity_id("sensor", DOMAIN, f"{TANK}_refill_status")
        return self.hass.states.get(entity_id).attributes["uninus_water_manual"]

    async def wait_status(self, **expected):
        async with asyncio.timeout(4):
            while not all(self.status()[key] == value for key, value in expected.items()):
                await asyncio.sleep(0.005)

    async def test_immediate_core_stop_enters_native_off_provider(self):
        await self.press("start")
        await self.wait_status(phase="running", actual="on")
        await self.press("stop")
        # No yield between observing the queued OFF and asking Core to stop.
        self.assertEqual(self.effects, [(self.output.entity_id, "on")])
        self.assertTrue(self.status()["pending_off"])
        await self.hass.async_stop()
        self.assertEqual(self.effects, [(self.output.entity_id, "on"), (self.output.entity_id, "off")])
        self.assertEqual(self.hass.states.get(self.output.entity_id).state, "off")
        self.assertEqual(self.status()["actual"], "off")
        self.assertFalse(self.status()["pending_off"])
        self.assertFalse(self.status()["can_start"])

    async def test_failed_native_reload_stays_restart_required_after_late_on_ack(self):
        entry_id = self.entry.entry_id
        target = self.output.entity_id
        runtime = self.entry.runtime_data
        self.on_ack.clear()
        try:
            await self.press("start")
            self.assertEqual(await asyncio.wait_for(self.calls.get(), 1), (target, "on"))
            self.assertTrue(self.status()["pending_on"])
            self.assertFalse(await self.hass.config_entries.async_reload(entry_id))
            self.assertIs(self.entry.state, ConfigEntryState.FAILED_UNLOAD)
            self.assertIs(self.entry.runtime_data, runtime)
            self.assertEqual(self.effects, [(target, "on"), (target, "off")])
            self.assertTrue(self.status()["pending_on"])
            with self.assertRaises(HomeAssistantError):
                await self.press("start")
        finally:
            self.on_ack.set()

        await self.wait_status(actual="off", pending_on=False, pending_off=False)
        self.assertEqual(self.effects, [(target, "on"), (target, "off"), (target, "off")])
        self.assertEqual(self.hass.states.get(target).state, "off")
        self.assertEqual(self.status()["phase"], "fault")
        self.assertEqual(self.status()["fault"], "restart_required")
        self.assertFalse(self.status()["can_start"])
        for action in ("start", "reset"):
            with self.assertRaisesRegex(HomeAssistantError, "Restart Home Assistant"):
                await self.press(action)
        with self.assertRaises(OperationNotAllowed):
            await self.hass.config_entries.async_reload(entry_id)
        self.assertIs(self.entry.state, ConfigEntryState.FAILED_UNLOAD)
        self.assertEqual(self.effects.count((target, "on")), 1)

        await self.hass.async_stop()
        await self.boot_core()
        self.entry = self.hass.config_entries.async_get_entry(entry_id)
        self.assertEqual(self.registry.async_get(target).id, self.output.id)
        self.seed_feedback()
        self.assertTrue(await self.hass.config_entries.async_setup(entry_id))
        await self.hass.async_block_till_done()
        self.assertIs(self.entry.state, ConfigEntryState.LOADED)
        self.assertIsNot(self.entry.runtime_data, runtime)
        self.assertEqual(self.status()["phase"], "idle")
        self.assertIsNone(self.status()["fault"])
        self.assertTrue(self.status()["can_start"])
        self.assertEqual(self.hass.states.get(target).state, "off")
        self.assertEqual(self.effects, [])

    async def test_core_stop_does_not_send_off_to_replacement_registry_row(self):
        target = self.output.entity_id
        await self.press("start")
        await self.wait_status(phase="running", actual="on")
        await self.press("stop")
        self.assertEqual(self.effects, [(target, "on")])
        self.assertTrue(self.status()["pending_off"])
        self.registry.async_remove(target)
        self.hass.states.async_remove(target)
        replacement = self.registry.async_get_or_create(
            "switch", "lifecycle_probe", "replacement", suggested_object_id=target.split(".", 1)[1]
        )
        self.assertEqual(replacement.entity_id, target)
        self.assertNotEqual(replacement.id, self.output.id)
        self.hass.states.async_set(target, "on")

        await self.hass.async_stop()
        self.assertEqual(self.effects, [(target, "on")])
        self.assertEqual(self.hass.states.get(target).state, "on")
        self.assertEqual(self.status()["actual"], "unknown")
        self.assertFalse(self.status()["pending_off"])
        self.assertFalse(self.status()["can_start"])
