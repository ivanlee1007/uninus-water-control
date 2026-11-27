"""P4 through real HA Options, registry, state machine, and elapsed timers."""

import asyncio
import json
import shutil
import socket
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

DOMAIN = "uninus_water_control"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
REFILL = {"refill_lower_m": 1.25, "refill_upper_m": 2.4}
ALARM = {"alarm_lower_m": 1.25, "alarm_delay_seconds": 1}


class NativeThresholdPreviewTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(SOURCE, self.root / "custom_components" / DOMAIN)
        for name in ("connect", "connect_ex", "sendto"):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
        await self.boot()
        self.probe = self.register_probe("primary", "depth")
        self.set_depth("125", "cm")
        flow = self.hass.config_entries.flow
        result = await flow.async_init(DOMAIN, context={"source": "user"})
        result = await flow.async_configure(result["flow_id"], {
            "zone_name": "North", "tank_name": "Storage", "level_sensor": self.probe,
            "refill_switch": "switch.synthetic_refill", "alarm_switch": "switch.synthetic_alarm",
        })
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        await self.hass.async_block_till_done()
        self.entry = result["result"]
        self.zone, = self.entry.data["zones"]
        self.tank = self.zone["tanks"][0]["id"]

    async def boot(self):
        self.hass = hass = HomeAssistant(str(self.root))
        self.addAsyncCleanup(hass.async_stop)
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await hass.config_entries.async_initialize()
        await hass.async_start()
        self.registry = er.async_get(hass)
        self.calls = []

        @callback
        def record(call):
            self.calls.append(call)

        for domain in ("switch", "homeassistant"):
            for service in ("turn_on", "turn_off", "toggle"):
                hass.services.async_register(domain, service, record)

    def register_probe(self, unique, object_id):
        return self.registry.async_get_or_create("sensor", "p4_synthetic", unique, suggested_object_id=object_id).entity_id

    def set_depth(self, value, unit="m", entity_id=None):
        self.hass.states.async_set(entity_id or self.probe, value, {"unit_of_measurement": unit})

    async def submit(self, step, **fields):
        return await self.hass.config_entries.options.async_configure(step["flow_id"], fields)

    async def select(self, operation, **target):
        menu = await self.hass.config_entries.options.async_init(self.entry.entry_id)
        self.assertIn(operation, menu["menu_options"])
        return await self.submit(await self.submit(menu, next_step_id=operation), **target)

    async def thresholds(self, tank=None):
        return await self.select("edit_thresholds", tank_id=tank or self.tank)

    async def save(self, form, **fields):
        result = await self.submit(form, **fields)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
        await self.hass.async_block_till_done()
        self.assertEqual(self.entry.state, ConfigEntryState.LOADED)

    def entity(self, domain, suffix, tank=None):
        return self.registry.async_get_entity_id(domain, DOMAIN, f"{tank or self.tank}_{suffix}")

    def assert_state(self, entity_id, expected):
        self.assertIsNotNone(entity_id)
        state = self.hass.states.get(entity_id)
        self.assertIsNotNone(state, entity_id)
        self.assertEqual(state.state, expected)
        return state

    async def wait_for_state(self, entity_id, expected):
        async with asyncio.timeout(3):
            while not (state := self.hass.states.get(entity_id)) or state.state != expected:
                await asyncio.sleep(0.01)

    async def test_native_refill_preview_persists_and_compares_both_inclusive_bounds(self):
        self.assertIsNone(self.entity("sensor", "refill_preview"))
        self.assertIsNone(self.entity("binary_sensor", "low_level_alarm"))
        await self.save(await self.thresholds(), **REFILL)
        entity = self.entity("sensor", "refill_preview")
        self.assertIsNone(self.entity("binary_sensor", "low_level_alarm"))
        self.assertEqual(self.entry.options["zones"][0]["tanks"][0]["thresholds"], REFILL)
        for value, unit, expected in (
            ("125", "cm", "refill_threshold"), ("1.2499", "m", "refill_threshold"),
            ("1251", "mm", "between_thresholds"), ("239.99", "cm", "between_thresholds"),
            ("2400", "mm", "stop_threshold"), ("2.4001", "m", "stop_threshold"),
            ("240", "%", "unavailable"),
        ):
            self.set_depth(value, unit)
            await self.hass.async_block_till_done()
            state = self.assert_state(entity, expected)
            self.assertEqual(state.attributes["uninus_water_thresholds"], {
                "version": 1, "zone_id": self.zone["id"], "tank_id": self.tank, "kind": "refill_preview",
            })
        self.assertEqual(self.calls, [])
        entry_id = self.entry.entry_id
        await self.hass.async_stop()
        store = json.loads((self.root / ".storage" / "core.config_entries").read_text(encoding="utf-8"))
        saved, = store["data"]["entries"]
        self.assertEqual(saved["options"]["zones"][0]["tanks"][0]["thresholds"], REFILL)
        await self.boot()
        self.entry = self.hass.config_entries.async_get_entry(entry_id)
        self.assertTrue(await self.hass.config_entries.async_setup(entry_id))
        await self.hass.async_block_till_done()
        self.assertEqual(self.entity("sensor", "refill_preview"), entity)
        self.assert_state(entity, "unavailable")
        self.set_depth("2400", "mm")
        await self.hass.async_block_till_done()
        self.assert_state(entity, "stop_threshold")
        self.assertEqual(self.calls, [])

    async def test_alarm_only_waits_for_continuous_equality_and_resets_on_same_turn_invalid(self):
        await self.save(await self.thresholds(), **ALARM)
        self.assertEqual(self.entry.options["zones"][0]["tanks"][0]["thresholds"], ALARM)
        self.assertIsNone(self.entity("sensor", "refill_preview"))
        entity = self.entity("binary_sensor", "low_level_alarm")
        self.assert_state(entity, "off")
        await asyncio.sleep(0.65)
        self.set_depth("125", "%")
        self.set_depth("1250", "mm")  # No yield between invalid and restored samples.
        await asyncio.sleep(0.5)  # Beyond old deadline, before the replacement timer.
        self.assert_state(entity, "off")
        await self.wait_for_state(entity, "on")
        self.set_depth("1.25001")
        await self.hass.async_block_till_done()
        self.assert_state(entity, "off")
        self.set_depth("nan", "m")
        await self.hass.async_block_till_done()
        state = self.assert_state(entity, "unavailable")
        self.assertEqual(state.attributes["uninus_water_thresholds"], {
            "version": 1, "zone_id": self.zone["id"], "tank_id": self.tank, "kind": "low_level_alarm",
        })
        self.assertEqual(self.calls, [])

    async def test_native_validation_rejects_partial_unordered_or_invalid_groups_without_saving(self):
        before = self.entry.options
        cases = (
            ({"refill_lower_m": 0}, "refill_upper_m", "threshold_pair_required"),
            ({"refill_lower_m": 1, "refill_upper_m": 1}, "refill_upper_m", "threshold_order"),
            ({"refill_lower_m": 2, "refill_upper_m": 1}, "refill_upper_m", "threshold_order"),
            ({"refill_lower_m": float("nan"), "refill_upper_m": 1}, "refill_lower_m", "finite_threshold_required"),
            ({"alarm_lower_m": 1}, "alarm_delay_seconds", "threshold_pair_required"),
            ({"alarm_delay_seconds": 1}, "alarm_lower_m", "threshold_pair_required"),
            (ALARM | {"alarm_lower_m": float("inf")}, "alarm_lower_m", "finite_threshold_required"),
            (ALARM | {"alarm_delay_seconds": 0}, "alarm_delay_seconds", "positive_whole_delay"),
            (ALARM | {"alarm_delay_seconds": 1.5}, "alarm_delay_seconds", "positive_whole_delay"),
        )
        for values, field, error in cases:
            with self.subTest(values=values):
                rejected = await self.submit(await self.thresholds(), **values)
                self.assertEqual(rejected["type"], FlowResultType.FORM)
                self.assertEqual(rejected["errors"], {field: error})
                self.assertIs(self.entry.options, before)
        with self.assertRaises(InvalidData):
            await self.submit(await self.thresholds(), **(ALARM | {"alarm_delay_seconds": True}))
        with self.assertRaises(InvalidData):
            await self.submit(await self.thresholds(), **(REFILL | {"refill_upper_m": 10**500}))
        self.assertIs(self.entry.options, before)
        self.assertEqual(self.calls, [])

    async def test_fractional_source_units_preserve_exact_boundary_and_nearby_values(self):
        self.set_depth("0.07", "cm")
        await self.save(await self.thresholds(), **{
            **ALARM, "alarm_lower_m": 0.0007, "refill_lower_m": 0.0007, "refill_upper_m": 0.0009,
        })
        preview = self.entity("sensor", "refill_preview")
        alarm = self.entity("binary_sensor", "low_level_alarm")
        self.assert_state(preview, "refill_threshold")
        await self.wait_for_state(alarm, "on")
        self.set_depth("0.07000000000000001", "cm")
        await self.hass.async_block_till_done()
        self.assert_state(preview, "between_thresholds")
        self.assert_state(alarm, "off")
        self.set_depth("0.9", "mm")
        await self.hass.async_block_till_done()
        self.assert_state(preview, "stop_threshold")

    async def add_peer(self):
        peer_source = self.register_probe("peer", "peer_depth")
        self.set_depth("2400", "mm", peer_source)
        await self.save(await self.select("add_tank", zone_id=self.zone["id"]),
                        tank_name="Peer", level_sensor=peer_source)
        return peer_source, self.entry.options["zones"][0]["tanks"][1]["id"]

    async def test_peers_rename_rebind_and_remove_without_transferring_registry_identity(self):
        peer_source, peer = await self.add_peer()
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        await self.save(await self.thresholds(peer), **(REFILL | ALARM))
        preview = self.entity("sensor", "refill_preview")
        peer_preview = self.entity("sensor", "refill_preview", peer)
        alarm = self.entity("binary_sensor", "low_level_alarm")
        peer_alarm = self.entity("binary_sensor", "low_level_alarm", peer)
        await self.wait_for_state(alarm, "on")
        self.assert_state(peer_preview, "stop_threshold")
        self.assert_state(peer_alarm, "off")
        self.registry.async_update_entity(self.probe, new_entity_id="sensor.renamed_depth")
        self.hass.states.async_remove(self.probe)  # Synthetic source has no platform to move its state.
        self.assertEqual(self.register_probe("impostor", "depth"), self.probe)
        self.set_depth("0")  # Old ID, now a different registry row.
        self.set_depth("2.4", entity_id="sensor.renamed_depth")
        await self.hass.async_block_till_done()
        self.assert_state(preview, "stop_threshold")
        self.assert_state(alarm, "off")
        self.registry.async_remove("sensor.renamed_depth")
        await self.hass.async_block_till_done()
        self.assert_state(preview, "unavailable")
        self.assert_state(alarm, "unavailable")
        self.assert_state(peer_alarm, "off")
        await self.save(await self.select("edit_tank", tank_id=self.tank), tank_name="Renamed", level_sensor=self.probe)
        self.assertEqual(self.entity("sensor", "refill_preview"), preview)
        self.assertEqual(self.entity("binary_sensor", "low_level_alarm"), alarm)
        self.assertEqual(self.entry.options["zones"][0]["tanks"][0]["thresholds"], REFILL | ALARM)
        self.assert_state(preview, "refill_threshold")
        self.assert_state(alarm, "off")
        self.set_depth("1", entity_id=peer_source)
        await self.hass.async_block_till_done()
        self.assert_state(peer_preview, "refill_threshold")
        await self.wait_for_state(peer_alarm, "on")
        await self.save(await self.select("remove_tank", tank_id=peer), confirm=True)
        self.assertIsNone(self.registry.async_get(peer_preview))
        self.assertIsNone(self.registry.async_get(peer_alarm))
        self.assertIsNone(self.hass.states.get(peer_alarm))
        await self.save(await self.thresholds(), **REFILL)
        self.assertIsNone(self.registry.async_get(alarm))
        self.assert_state(preview, "refill_threshold")
        self.assertEqual(self.calls, [])

    async def test_expired_callbacks_cannot_publish_after_native_reload_or_removal(self):
        await self.add_peer()
        module = import_module(f"custom_components.{DOMAIN}.binary_sensor")
        schedule = module.async_call_later
        jobs = []

        def capture(hass, delay, action):
            jobs.append(action)
            return schedule(hass, delay, action)

        with patch.object(module, "async_call_later", capture):
            await self.save(await self.thresholds(), **ALARM)
            alarm = self.entity("binary_sensor", "low_level_alarm")
            old, = jobs
            self.assertTrue(await self.hass.config_entries.async_reload(self.entry.entry_id))
            await self.hass.async_block_till_done()
            self.assertEqual(self.entity("binary_sensor", "low_level_alarm"), alarm)
            _, reloaded = jobs
            self.assert_state(alarm, "off")
            old(dt_util.utcnow())  # Replay an already dispatched callback from the retired owner.
            self.assert_state(alarm, "off")
            await self.save(await self.select("remove_tank", tank_id=self.tank), confirm=True)
            reloaded(dt_util.utcnow())
            self.assertIsNone(self.registry.async_get(alarm))
            self.assertIsNone(self.hass.states.get(alarm))
            self.assertEqual(self.calls, [])

    async def test_nested_restore_cannot_replay_an_older_invalid_frame_after_the_new_sample(self):
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        preview = self.entity("sensor", "refill_preview")
        alarm = self.entity("binary_sensor", "low_level_alarm")
        restored = False

        @callback
        def restore(event):
            nonlocal restored
            if not restored and event.data["new_state"].state == "unavailable":
                restored = True
                self.set_depth("1.25")

        @callback
        def is_preview(data):
            return data["entity_id"] in (preview, alarm) and data["new_state"] is not None

        # A native downstream callback reacting immediately to either preview.
        remove = self.hass.bus.async_listen(EVENT_STATE_CHANGED, restore, event_filter=is_preview)
        try:
            self.set_depth("unknown")
            await self.hass.async_block_till_done()
            # The restore listener may queue a second publication next turn.
            await asyncio.sleep(0)
            self.assertTrue(restored)
            self.assert_state(preview, "refill_threshold")
            self.assert_state(alarm, "off")
            await self.wait_for_state(alarm, "on")
        finally:
            remove()

    def listen_depth(self, receive):
        @callback
        def is_depth(data):
            return data["entity_id"] == self.probe and data["new_state"] is not None

        @callback
        def observe(event):
            receive(event.data["new_state"].state)

        self.addCleanup(self.hass.bus.async_listen(EVENT_STATE_CHANGED, observe, event_filter=is_depth))

    async def test_upstream_inline_restore_preserves_invalid_reset_and_latest_projection(self):
        def restore(state):
            if state == "unknown":
                self.set_depth("1.25")

        # Register before P4: its synchronous callbacks see the nested sample first.
        self.listen_depth(restore)
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        preview = self.entity("sensor", "refill_preview")
        alarm = self.entity("binary_sensor", "low_level_alarm")
        await self.wait_for_state(alarm, "on")
        seen = []
        self.listen_depth(seen.append)

        reset_at = self.hass.loop.time()
        self.set_depth("unknown")
        await self.hass.async_block_till_done()
        self.assertEqual(seen, ["1.25", "unknown"])
        self.assert_state(self.probe, "1.25")
        self.assert_state(preview, "refill_threshold")
        self.assert_state(alarm, "off")
        await self.wait_for_state(alarm, "on")
        self.assertGreaterEqual(self.hass.loop.time() - reset_at, ALARM["alarm_delay_seconds"] - 0.01)
        self.assertEqual(seen, ["1.25", "unknown"])  # No later source event repaired it.
        self.assertEqual(self.calls, [])

    async def test_upstream_inline_low_high_low_restarts_the_elapsed_alarm_interval(self):
        def restore(state):
            if state == "2.5":
                self.set_depth("1.25")

        self.set_depth("3")
        self.listen_depth(restore)
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        preview = self.entity("sensor", "refill_preview")
        alarm = self.entity("binary_sensor", "low_level_alarm")
        self.set_depth("1")
        await asyncio.sleep(0.55)
        self.assert_state(alarm, "off")
        seen = []
        self.listen_depth(seen.append)

        reset_at = self.hass.loop.time()
        self.set_depth("2.5")
        await self.hass.async_block_till_done()
        self.assertEqual(seen, ["1.25", "2.5"])
        self.assert_state(self.probe, "1.25")
        self.assert_state(preview, "refill_threshold")
        self.assert_state(alarm, "off")
        await self.wait_for_state(alarm, "on")
        self.assertGreaterEqual(self.hass.loop.time() - reset_at, ALARM["alarm_delay_seconds"] - 0.01)
        self.assertEqual(seen, ["1.25", "2.5"])
        self.assertEqual(self.calls, [])

    async def test_upstream_inline_latest_high_cannot_be_overwritten_by_an_older_low(self):
        def restore(state):
            if state == "1":
                self.set_depth("2.5")

        self.listen_depth(restore)
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        preview = self.entity("sensor", "refill_preview")
        alarm = self.entity("binary_sensor", "low_level_alarm")
        await self.wait_for_state(alarm, "on")
        seen = []
        self.listen_depth(seen.append)

        self.set_depth("1")
        await self.hass.async_block_till_done()
        self.assertEqual(seen, ["2.5", "1"])
        self.assert_state(self.probe, "2.5")
        self.assert_state(preview, "stop_threshold")
        self.assert_state(alarm, "off")
        await asyncio.sleep(ALARM["alarm_delay_seconds"] + 0.1)
        self.assert_state(alarm, "off")
        self.assertEqual(seen, ["2.5", "1"])
        self.assertEqual(self.calls, [])

    async def test_preview_does_not_commission_or_change_existing_manual_settings(self):
        switches = {}
        for role in ("refill", "alarm"):
            row = self.registry.async_get_or_create(
                "switch", "p4_synthetic", role, suggested_object_id=f"native_{role}",
            )
            switches[f"{role}_switch"] = row.entity_id
            self.hass.states.async_set(row.entity_id, "off")
        await self.save(
            await self.select("edit_tank", tank_id=self.tank), tank_name="Storage", level_sensor=self.probe,
            commission_refill=True, commission_alarm=True, confirm_device_cutoff=True, **switches,
        )
        manual = self.entry.options["manual"]
        buttons = [self.entity("button", f"{role}_start") for role in ("refill", "alarm")]
        self.assertTrue(all(buttons))
        self.assertIsNone(self.entity("sensor", "refill_preview"))
        self.assertIsNone(self.entity("binary_sensor", "low_level_alarm"))
        await self.save(await self.thresholds(), **(REFILL | ALARM))
        await self.wait_for_state(self.entity("binary_sensor", "low_level_alarm"), "on")
        self.assertEqual(self.entry.options["manual"], manual)
        self.assertEqual([self.entity("button", f"{role}_start") for role in ("refill", "alarm")], buttons)
        for entity_id in switches.values():
            self.assert_state(entity_id, "off")
        self.assertEqual(self.calls, [])
