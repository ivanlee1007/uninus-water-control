"""Run the commissioned controls through isolated Home Assistant Core."""

import asyncio
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame

DOMAIN = "uninus_water_control"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN


class NativeActuationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        shutil.copytree(SOURCE, Path(directory.name) / "custom_components" / DOMAIN)
        self.directory = directory.name
        await self.boot_core()
        depth = self.registry.async_get_or_create("sensor", "test_probe", "depth", suggested_object_id="depth")
        self.refill = self.make_output("refill")
        self.alarm = self.make_output("alarm")
        self.depth = depth.entity_id
        self.set_depth("1.25")

    async def boot_core(self):
        self.hass = hass = HomeAssistant(self.directory)
        self.addAsyncCleanup(hass.async_stop)
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        hass.auth = await auth.auth_manager_from_config(hass, [], [])
        actor = await hass.auth.async_create_user("Test operator", group_ids=["system-admin"])
        self.actor = Context(user_id=actor.id)
        await hass.config_entries.async_initialize()
        await hass.async_start()
        self.registry = er.async_get(hass)
        self.calls = asyncio.Queue()
        self.effects = []
        self.on_ack = asyncio.Event()
        self.on_ack.set()
        self.held_off = None
        self.off_ack = asyncio.Event()
        self.off_ack.set()
        self.drop_feedback = set()
        self.addAsyncCleanup(self.release_ack)
        hass.services.async_register("switch", "turn_on", self.handle_switch)
        hass.services.async_register("switch", "turn_off", self.handle_switch)

    def make_output(self, unique, object_id=None):
        row = self.registry.async_get_or_create("switch", "test_probe", unique, suggested_object_id=object_id or unique)
        self.hass.states.async_set(row.entity_id, "off")
        return row.entity_id

    def set_depth(self, state):
        self.hass.states.async_set(self.depth, state, {"unit_of_measurement": "m"})

    async def release_ack(self):
        self.on_ack.set()
        self.off_ack.set()

    async def handle_switch(self, call: ServiceCall):
        entity = call.data["entity_id"]
        value = "on" if call.service == "turn_on" else "off"
        self.effects.append((entity, value))
        self.calls.put_nowait((entity, value))
        if value == "on":
            await self.on_ack.wait()
        elif entity == self.held_off:
            await self.off_ack.wait()
        if (entity, value) not in self.drop_feedback:
            self.hass.states.async_set(entity, value)

    async def configure(self, **options):
        manager = self.hass.config_entries.flow
        form = await manager.async_init(DOMAIN, context={"source": "user"})
        result = await manager.async_configure(
            form["flow_id"],
            {
                "zone_name": "Test zone",
                "tank_name": "Test tank",
                "level_sensor": self.depth,
                "refill_switch": self.refill,
                "alarm_switch": self.alarm,
            },
        )
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
        self.entry = result["result"]
        self.tank = self.entry.data["zones"][0]["tanks"][0]["id"]
        await self.hass.async_block_till_done()
        self.assertEqual(self.entry.state, ConfigEntryState.LOADED)
        if options:
            result = await self.submit_tank(**options)
            self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
            await self.hass.async_block_till_done()
            self.assertEqual(self.entry.state, ConfigEntryState.LOADED)

    async def submit_tank(self, **options):
        form = await self.tank_form()
        return await self.hass.config_entries.options.async_configure(form["flow_id"], self.tank_fields(**options))

    async def tank_form(self):
        manager = self.hass.config_entries.options
        form = await manager.async_init(self.entry.entry_id)
        form = await manager.async_configure(form["flow_id"], {"next_step_id": "edit_tank"})
        return await manager.async_configure(form["flow_id"], {"tank_id": self.tank})

    def tank_fields(self, **options):
        return {
            "tank_name": "Test tank",
            "level_sensor": self.depth,
            "refill_switch": self.refill,
            "alarm_switch": self.alarm,
            **options,
        }

    async def press(self, role, action, context=None):
        entity = self.registry.async_get_entity_id("button", DOMAIN, f"{self.tank}_{role}_{action}")
        self.assertIsNotNone(entity, (role, action))
        await self.hass.services.async_call(
            "button", "press", {"entity_id": entity}, blocking=True, context=context or self.actor
        )

    async def next_call(self, entity, value):
        self.assertEqual(await asyncio.wait_for(self.calls.get(), 1), (entity, value))

    def status(self, role):
        entity = self.registry.async_get_entity_id("sensor", DOMAIN, f"{self.tank}_{role}_status")
        return self.hass.states.get(entity).attributes["uninus_water_manual"]

    async def wait_status(self, role, **expected):
        async with asyncio.timeout(4):
            while not all(self.status(role)[key] == value for key, value in expected.items()):
                await asyncio.sleep(0.005)

    async def test_manual_refill_start_then_stop(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        self.assertEqual(self.effects, [])
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        self.assertEqual(self.hass.states.get(self.refill).state, "on")
        await self.press("refill", "stop")
        await self.next_call(self.refill, "off")
        self.assertEqual(self.hass.states.get(self.refill).state, "off")
        self.assertEqual(self.hass.states.get(self.alarm).state, "off")

    async def test_independent_alarm_without_valid_level(self):
        await self.configure(commission_alarm=True, confirm_device_cutoff=True)
        self.set_depth("unavailable")
        self.assertEqual(self.effects, [])
        await self.press("alarm", "start")
        await self.next_call(self.alarm, "on")
        self.assertEqual(self.hass.states.get(self.refill).state, "off")
        await self.press("alarm", "stop")
        await self.next_call(self.alarm, "off")
        self.assertEqual(self.hass.states.get(self.alarm).state, "off")

    async def test_stop_beats_pending_on_and_late_on_is_closed(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        self.on_ack.clear()
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="starting", pending_on=True, actual="off")
        with self.assertRaises(HomeAssistantError):
            await self.press("refill", "start")
        await self.press("refill", "stop")
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="stopping", pending_on=True, actual="off")
        self.assertFalse(self.on_ack.is_set())
        self.on_ack.set()
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="idle", pending_on=False, pending_off=False, actual="off")
        self.assertEqual(self.effects, [(self.refill, "on"), (self.refill, "off"), (self.refill, "off")])

    async def test_invalid_level_event_revokes_even_when_immediately_restored(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on")
        self.set_depth("unavailable")
        self.set_depth("1.25")
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="fault", actual="off", fault="source_invalid")
        await self.press("refill", "reset")
        await self.wait_status("refill", phase="idle", can_start=True)
        self.assertEqual(self.effects, [(self.refill, "on"), (self.refill, "off")])

    async def test_unconfirmed_stop_never_projects_off(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        self.drop_feedback.add((self.refill, "off"))
        self.hass.states.async_set(self.refill, "unavailable")
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="stopping", actual="unknown", can_start=False)
        with self.assertRaises(HomeAssistantError):
            await self.press("refill", "reset")
        self.assertEqual(await asyncio.wait_for(self.calls.get(), 3), (self.refill, "off"))
        self.hass.states.async_set(self.refill, "off")
        await self.wait_status("refill", phase="fault", actual="off", fault="output_unknown")

    async def test_reused_output_id_is_never_given_off_authority(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        self.registry.async_remove(self.refill)
        self.hass.states.async_remove(self.refill)
        self.assertEqual(self.make_output("replacement", "refill"), self.refill)
        await self.wait_status("refill", phase="stopping", actual="unknown", fault="output_binding_changed")
        await self.press("refill", "stop")
        with self.assertRaises(HomeAssistantError):
            await self.press("refill", "start")
        self.assertEqual(self.effects, [(self.refill, "on")])

    async def test_core_read_only_user_and_system_call_cannot_start(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        reader = await self.hass.auth.async_create_user("Read only", group_ids=["system-read-only"])
        for context in (Context(user_id=reader.id), Context()):
            with self.assertRaises(Unauthorized):
                await self.press("refill", "start", context)
        self.assertEqual(self.effects, [])

    async def test_start_is_opt_in_cutoff_required_and_shared_output_rejected(self):
        await self.configure()
        self.assertIsNone(self.registry.async_get_entity_id("button", DOMAIN, f"{self.tank}_refill_start"))
        for values, error in (
            ({"commission_refill": True}, {"confirm_device_cutoff": "cutoff_required"}),
            (
                {"commission_refill": True, "confirm_device_cutoff": True, "alarm_switch": self.refill},
                {"refill_switch": "shared_output"},
            ),
        ):
            result = await self.submit_tank(**values)
            self.assertEqual(result["type"], FlowResultType.FORM)
            self.assertEqual(result["errors"], error)
            self.assertEqual(dict(self.entry.options), {})
        self.assertEqual(self.effects, [])

    async def test_max_runtime_sends_native_off(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True, refill_max_runtime_seconds=1)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", max_runtime_seconds=1)
        self.assertEqual(await asyncio.wait_for(self.calls.get(), 3), (self.refill, "off"))
        await self.wait_status("refill", phase="fault", actual="off", fault="runtime_limit")

    async def test_lost_on_report_does_not_claim_running(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        self.drop_feedback.add((self.refill, "on"))
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="starting", pending_on=False, actual="off")
        self.assertEqual(await asyncio.wait_for(self.calls.get(), 3), (self.refill, "off"))
        await self.wait_status("refill", phase="fault", actual="off", fault="on_unconfirmed")

    async def test_pending_alarm_off_does_not_block_refill_stop(self):
        await self.configure(commission_refill=True, commission_alarm=True, confirm_device_cutoff=True)
        await self.press("alarm", "start")
        await self.next_call(self.alarm, "on")
        self.on_ack.clear()
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        self.held_off = self.alarm
        self.off_ack.clear()
        await self.press("alarm", "stop")
        await self.next_call(self.alarm, "off")
        await self.press("refill", "stop")
        await self.next_call(self.refill, "off")
        self.on_ack.set()
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="idle", pending_on=False, actual="off")
        await self.wait_status("alarm", phase="stopping", pending_off=True, actual="on")
        self.off_ack.set()
        await self.wait_status("alarm", phase="idle", pending_off=False, actual="off")

    async def test_old_off_is_not_proof_after_lost_on_and_off_reports(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        self.drop_feedback.update({(self.refill, "on"), (self.refill, "off")})
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.press("refill", "stop")
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="stopping", pending_off=False, actual="unknown", can_start=False)
        self.hass.states.async_set(self.refill, "off")  # Same value, new HA feedback.
        await self.wait_status("refill", phase="idle", actual="off", can_start=True)

    async def test_commissioning_rejects_output_replaced_after_form_opens(self):
        await self.configure()
        form = await self.tank_form()
        moved = self.registry.async_update_entity(self.refill, new_entity_id="switch.refill_renamed")
        self.hass.states.async_remove(self.refill)
        self.make_output("replacement", "refill")
        fields = self.tank_fields(commission_refill=True, confirm_device_cutoff=True)
        manager = self.hass.config_entries.options
        for _ in range(2):
            result = await manager.async_configure(form["flow_id"], fields)
            self.assertEqual(result["errors"], {"refill_switch": "output_source_changed"})
            self.assertEqual(dict(self.entry.options), {})
        self.hass.states.async_set(moved.entity_id, "off")
        result = await manager.async_configure(form["flow_id"], fields | {"refill_switch": moved.entity_id})
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        await self.hass.async_block_till_done()
        await self.press("refill", "start")
        await self.next_call(moved.entity_id, "on")
        await self.press("refill", "stop")
        await self.next_call(moved.entity_id, "off")
        self.assertEqual(self.effects, [(moved.entity_id, "on"), (moved.entity_id, "off")])

    async def assert_cutoff_echo_keeps_identity(self, field, role):
        # The native unittest event loop is already running; deny external I/O
        # while exercising the real Options manager, registry and button service.
        for method in ("connect", "connect_ex", "sendto"):
            self.enterContext(patch.object(socket.socket, method, side_effect=AssertionError("network forbidden")))
        await self.configure()
        manager = self.hass.config_entries.options
        form = await self.tank_form()
        flow_id = form["flow_id"]
        original_data, original_options = self.entry.data, self.entry.options
        is_level = field == "level_sensor"
        domain, unit = ("sensor", {"unit_of_measurement": "m"}) if is_level else ("switch", {})
        intended = self.registry.async_get_or_create(domain, "test_probe", "intended", suggested_object_id="candidate")
        old_id = intended.entity_id
        self.hass.states.async_set(old_id, "1.5" if is_level else "off", unit)
        fields = self.tank_fields(**{field: old_id, f"commission_{role}": True})

        def suggestion(result):
            marker = next(key for key in result["data_schema"].schema if key == field)
            return marker.description["suggested_value"]

        error = await manager.async_configure(flow_id, fields)
        self.assertEqual(error["type"], FlowResultType.FORM)
        self.assertEqual(error["errors"], {"confirm_device_cutoff": "cutoff_required"})
        self.assertEqual(suggestion(error), old_id)

        moved = self.registry.async_update_entity(old_id, new_entity_id=f"{domain}.candidate_moved")
        self.assertEqual(moved.id, intended.id)
        self.hass.states.async_remove(old_id)
        self.hass.states.async_set(moved.entity_id, "1.5" if is_level else "off", unit)
        replacement = self.registry.async_get_or_create(
            domain, "test_probe", "replacement", suggested_object_id="candidate"
        )
        self.assertEqual(replacement.entity_id, old_id)
        self.assertNotEqual(replacement.id, intended.id)
        self.hass.states.async_set(old_id, "2.5" if is_level else "off", unit)

        confirmed = fields | {"confirm_device_cutoff": True}
        binding_key = "source" if is_level else "output"
        error_key = "level_source_changed" if is_level else "output_source_changed"
        for _ in range(2):
            result = await manager.async_configure(flow_id, confirmed)
            await self.hass.async_block_till_done()
            if result["type"] == FlowResultType.CREATE_ENTRY:
                # Capture the actual unsafe admission on RED, then stop it.
                self.assertEqual(self.entry.options["manual"][role][binding_key], replacement.id)
                output = self.refill if is_level else old_id
                await self.press(role, "start")
                await self.next_call(output, "on")
                await self.press(role, "stop")
                await self.next_call(output, "off")
            self.assertEqual(result["type"], FlowResultType.FORM, self.effects)
            self.assertEqual(result["errors"], {field: error_key})
            self.assertNotEqual(suggestion(result), old_id)
            self.assertIs(self.entry.data, original_data)
            self.assertIs(self.entry.options, original_options)
            self.assertIsNone(self.registry.async_get_entity_id("button", DOMAIN, f"{self.tank}_{role}_start"))
            self.assertEqual(self.effects, [])

        # Same flow can explicitly select the intended row's new ID. The old
        # string can be chosen for the replacement only in a fresh flow.
        for selection, row, target_flow in ((moved.entity_id, moved, flow_id), (old_id, replacement, None)):
            if target_flow is None:
                target_flow = (await self.tank_form())["flow_id"]
            result = await manager.async_configure(target_flow, confirmed | {field: selection})
            self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
            await self.hass.async_block_till_done()
            self.assertEqual(self.entry.state, ConfigEntryState.LOADED)
            self.assertEqual(self.entry.options["manual"][role][binding_key], row.id)
            output = self.refill if is_level else selection
            await self.press(role, "start")
            await self.next_call(output, "on")
            await self.press(role, "stop")
            await self.next_call(output, "off")
            await self.wait_status(role, phase="idle", actual="off", pending_on=False, pending_off=False)

    async def test_cutoff_error_echo_does_not_rebind_refill_level(self):
        await self.assert_cutoff_echo_keeps_identity("level_sensor", "refill")

    async def test_cutoff_error_echo_does_not_rebind_refill_output(self):
        await self.assert_cutoff_echo_keeps_identity("refill_switch", "refill")

    async def test_cutoff_error_echo_does_not_rebind_alarm_output(self):
        await self.assert_cutoff_echo_keeps_identity("alarm_switch", "alarm")

    async def test_reload_and_actual_restart_do_not_run_previous_command(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        self.assertTrue(await self.hass.config_entries.async_reload(self.entry.entry_id))
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="idle", actual="off")
        self.assertEqual(self.effects, [(self.refill, "on"), (self.refill, "off")])
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        entry_id = self.entry.entry_id
        original_row_id = self.registry.async_get(self.refill).id
        await self.hass.async_stop()
        await self.next_call(self.refill, "off")
        await self.boot_core()
        self.entry = self.hass.config_entries.async_get_entry(entry_id)
        self.assertEqual(self.registry.async_get(self.refill).id, original_row_id)
        self.set_depth("1.25")
        self.hass.states.async_set(self.refill, "on")  # Synthetic residual startup feedback.
        self.hass.states.async_set(self.alarm, "off")
        self.assertTrue(await self.hass.config_entries.async_setup(entry_id))
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="fault", actual="off", fault="unexpected_on")
        self.assertEqual(self.effects, [(self.refill, "off")])
