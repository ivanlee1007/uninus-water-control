"""Native regression coverage for setup ownership and OFF recovery."""

import asyncio
import inspect
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

import test_native_actuation as native
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError


class AuditActuationRecoveryTest(unittest.IsolatedAsyncioTestCase):
    """Reuse native hardware/Core helpers without collecting their tests twice."""

    boot_core = native.NativeActuationTest.boot_core
    make_output = native.NativeActuationTest.make_output
    set_depth = native.NativeActuationTest.set_depth
    release_ack = native.NativeActuationTest.release_ack
    handle_switch = native.NativeActuationTest.handle_switch
    configure = native.NativeActuationTest.configure
    submit_tank = native.NativeActuationTest.submit_tank
    tank_form = native.NativeActuationTest.tank_form
    tank_fields = native.NativeActuationTest.tank_fields
    press = native.NativeActuationTest.press
    next_call = native.NativeActuationTest.next_call
    status = native.NativeActuationTest.status
    wait_status = native.NativeActuationTest.wait_status

    async def asyncSetUp(self):
        expected = Path(__file__).resolve().parents[1] / "custom_components" / native.DOMAIN
        self.assertEqual(native.SOURCE, expected)
        for method in ("connect", "connect_ex", "sendto"):
            self.enterContext(patch.object(socket.socket, method, side_effect=AssertionError("network forbidden")))
        await native.NativeActuationTest.asyncSetUp(self)

    def assert_import_origin(self, runtime):
        # Core's custom component module cache must refer to this worktree, or
        # its byte-exact fixture copy, never the historical audit checkout.
        origin = Path(inspect.getfile(type(runtime))).resolve()
        expected = native.SOURCE / "manual.py"
        fixture = Path(self.directory).resolve() / "custom_components" / native.DOMAIN / "manual.py"
        self.assertIn(origin, (expected.resolve(), fixture))
        self.assertEqual(origin.read_bytes(), expected.read_bytes())

    async def test_stalled_off_is_cancelled_then_retried_without_overlap(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        runtime = self.entry.runtime_data
        self.assert_import_origin(runtime)
        module = inspect.getmodule(type(runtime))
        self.enterContext(patch.object(module, "OFF_CALL_SECONDS", 0.1, create=True))
        self.enterContext(patch.object(module, "RETRY_SECONDS", 0.05))
        release = asyncio.Event()
        cancelled = asyncio.Event()
        attempts = 0
        active = 0
        max_active = 0

        async def off(call):
            nonlocal attempts, active, max_active
            attempts += 1
            active += 1
            max_active = max(max_active, active)
            self.effects.append((call.data["entity_id"], "off"))
            self.calls.put_nowait((call.data["entity_id"], "off"))
            try:
                if attempts == 1:
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                self.hass.states.async_set(call.data["entity_id"], "off")
            finally:
                active -= 1

        self.hass.services.async_register("switch", "turn_off", off)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on")
        try:
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            first = runtime.roles["refill"].off_task
            for _ in range(5):
                await self.press("refill", "stop")
            self.assertIs(runtime.roles["refill"].off_task, first)
            self.assertEqual(attempts, 1)
            self.assertEqual(self.status("refill")["phase"], "stopping")
            self.assertFalse(self.status("refill")["can_start"])
            await asyncio.sleep(0.3)
            self.assertTrue(cancelled.is_set(), "stalled native OFF needs a finite cancellation deadline")
            await self.next_call(self.refill, "off")
            await self.wait_status("refill", actual="off", pending_off=False, phase="fault")
            self.assertEqual(attempts, 2)
            self.assertEqual(max_active, 1, "retry must not overlap an unresolved provider")
            self.assertTrue(first.done())
            await self.press("refill", "reset")
            await self.press("refill", "start")
            await self.next_call(self.refill, "on")
            await self.wait_status("refill", phase="running", actual="on")
            release.set()  # A cancelled old OFF cannot later close the new run.
            await asyncio.sleep(0.15)
            self.assertEqual(self.status("refill")["actual"], "on")
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            await self.wait_status("refill", phase="idle", actual="off", pending_off=False)
        finally:
            release.set()

    async def test_cancel_resistant_off_retains_owner_and_bounded_close(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        runtime = self.entry.runtime_data
        self.assert_import_origin(runtime)
        module = inspect.getmodule(type(runtime))
        self.enterContext(patch.object(module, "OFF_CALL_SECONDS", 0.1))
        self.enterContext(patch.object(module, "RETRY_SECONDS", 0.05))
        self.enterContext(patch.object(module, "CLOSE_SECONDS", 0.15))
        release = asyncio.Event()
        cancelled = asyncio.Event()
        attempts = 0

        async def off(call):
            nonlocal attempts
            attempts += 1
            self.effects.append((call.data["entity_id"], "off"))
            self.calls.put_nowait((call.data["entity_id"], "off"))
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            self.hass.states.async_set(call.data["entity_id"], "off")

        self.hass.services.async_register("switch", "turn_off", off)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on")
        try:
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            task = runtime.roles["refill"].off_task
            async with asyncio.timeout(1):
                await cancelled.wait()
            for _ in range(10):
                await self.press("refill", "stop")
            self.assertEqual(attempts, 1)
            self.assertIs(runtime.roles["refill"].off_task, task)
            self.assertFalse(task.done())
            self.assertEqual(self.status("refill")["phase"], "stopping")
            self.assertEqual(self.status("refill")["actual"], "on")
            self.assertTrue(self.status("refill")["pending_off"])
            self.assertFalse(self.status("refill")["can_start"])
            for action in ("start", "reset"):
                with self.assertRaises(HomeAssistantError):
                    await self.press("refill", action)
            # A close deadline bounds the join, never discards an unresolved
            # native provider or hands ownership to a replacement controller.
            async with asyncio.timeout(0.8):
                self.assertFalse(await runtime.async_close())
            self.assertIs(runtime.roles["refill"].off_task, task)
            self.assertEqual(attempts, 1)
        finally:
            release.set()
        await self.wait_status("refill", phase="fault", pending_off=False, actual="off")
        self.assertTrue(task.done())
        self.assertEqual(attempts, 1)
        self.assertFalse(self.status("refill")["can_start"])

    async def test_late_on_during_held_off_requires_another_off_pass(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        runtime = self.entry.runtime_data
        self.assert_import_origin(runtime)
        self.on_ack.clear()
        self.held_off = self.refill
        self.off_ack.clear()
        try:
            await self.press("refill", "start")
            await self.next_call(self.refill, "on")
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            task = runtime.roles["refill"].off_task
            self.on_ack.set()
            await self.wait_status("refill", pending_on=False, pending_off=True, actual="on", phase="stopping")
            self.assertIs(runtime.roles["refill"].off_task, task)
            self.assertTrue(runtime.roles["refill"]._off_again)
            for _ in range(5):
                await self.press("refill", "stop")
            self.assertEqual(self.effects, [(self.refill, "on"), (self.refill, "off")])
        finally:
            self.on_ack.set()
            self.off_ack.set()
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="idle", pending_on=False, pending_off=False, actual="off")
        self.assertEqual(self.effects, [(self.refill, "on"), (self.refill, "off"), (self.refill, "off")])

    async def test_default_deadline_retries_stalled_native_off(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        self.assert_import_origin(self.entry.runtime_data)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on")
        self.held_off = self.refill
        self.off_ack.clear()
        try:
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            first = self.entry.runtime_data.roles["refill"].off_task
            for _ in range(5):
                await self.press("refill", "stop")
            self.assertEqual(await asyncio.wait_for(self.calls.get(), 5), (self.refill, "off"))
            self.assertTrue(first.done())
            self.assertTrue(self.status("refill")["pending_off"])
            self.assertEqual(self.status("refill")["phase"], "stopping")
            self.assertEqual(self.status("refill")["actual"], "on")
            self.assertFalse(self.status("refill")["can_start"])
            self.assertEqual(self.effects.count((self.refill, "off")), 2)
        finally:
            self.off_ack.set()
        await self.wait_status("refill", phase="fault", actual="off", pending_off=False)

    async def test_core_stop_bounds_retries_and_keeps_unconfirmed_truth(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        runtime = self.entry.runtime_data
        self.assert_import_origin(runtime)
        module = inspect.getmodule(type(runtime))
        self.enterContext(patch.object(module, "OFF_CALL_SECONDS", 0.1))
        self.enterContext(patch.object(module, "RETRY_SECONDS", 0.05))
        self.enterContext(patch.object(module, "CLOSE_SECONDS", 0.25))
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on")
        self.held_off = self.refill
        self.off_ack.clear()
        try:
            await self.press("refill", "stop")
            await self.next_call(self.refill, "off")
            async with asyncio.timeout(1.5):
                await self.hass.async_stop()
            count = self.effects.count((self.refill, "off"))
            self.assertGreaterEqual(count, 2)
            await asyncio.sleep(0.2)
            self.assertEqual(self.effects.count((self.refill, "off")), count)
            self.assertTrue(runtime.retiring)
            self.assertIsNone(runtime.roles["refill"]._retry_timer)
            self.assertIsNone(runtime.roles["refill"].off_task)
            self.assertEqual(runtime.roles["refill"].snapshot()["phase"], "stopping")
            self.assertEqual(self.hass.states.get(self.refill).state, "on")
            self.assertFalse(runtime.roles["refill"].closed)
        finally:
            self.off_ack.set()

    async def test_failed_platform_setup_retires_owner_before_retry(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        failed_owners = []

        async def fail_forward(manager, entry, platforms):
            failed_owners.append(entry.runtime_data)
            raise ConfigEntryError("injected platform setup failure")

        with patch.object(ConfigEntries, "async_forward_entry_setups", fail_forward):
            self.assertFalse(await self.hass.config_entries.async_reload(self.entry.entry_id))
        self.assertIs(self.entry.state, ConfigEntryState.SETUP_ERROR)
        failed = failed_owners[0]
        self.assert_import_origin(failed)
        self.assertTrue(failed.retiring, "failed setup must retire its controller")
        self.assertEqual(failed._unsubs, [])
        self.assertIsNone(failed.roles["refill"]._unsub_state)
        self.assertIsNone(failed.roles["refill"]._unsub_report)
        self.assertIsNone(failed.roles["refill"].off_task)
        self.assertTrue(await self.hass.config_entries.async_reload(self.entry.entry_id))
        self.assertIsNot(self.entry.runtime_data, failed)
        await self.press("refill", "start")
        await self.next_call(self.refill, "on")
        await self.wait_status("refill", phase="running", actual="on", fault=None)
        await asyncio.sleep(0.05)
        self.assertEqual(self.effects, [(self.refill, "on")], "retired setup owner must not send competing OFF")
        self.assertFalse(failed.roles["refill"].requested)
        await self.press("refill", "stop")
        await self.next_call(self.refill, "off")
        await self.wait_status("refill", phase="idle", pending_off=False, actual="off")
