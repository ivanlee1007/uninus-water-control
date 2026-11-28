"""Private card settings through the registered Core WS and real AuthManager."""

import asyncio
from copy import deepcopy
import json
import sys
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant.const import EVENT_CALL_SERVICE

from water_settings_ws import compare, settings_client
from test_native_threshold_preview import NativeThresholdPreviewTest as PreviewFixture

# Use its real disposable integration setup without collecting its inherited tests here.
class SettingsFixture:
    asyncSetUp = PreviewFixture.asyncSetUp
    boot = PreviewFixture.boot
    register_probe = PreviewFixture.register_probe
    set_depth = PreviewFixture.set_depth
    entity = PreviewFixture.entity
    assert_state = PreviewFixture.assert_state
    wait_for_state = PreviewFixture.wait_for_state

    async def client(self, group="system-admin"):
        return await settings_client(self.hass, self.entry, group)

    compare = staticmethod(compare)


# Do not let pytest collect the aliased TestCase in addition to its own module.
del PreviewFixture


class TestNativeCardSettings(SettingsFixture, IsolatedAsyncioTestCase):
    async def test_saved_settings_are_read_back_and_reopened(self):
        request, _ = await self.client()
        initial = await request("get")
        self.assertTrue(initial["success"], initial)
        baseline = initial["result"]
        saved = await request("save", **self.compare(baseline), changes={
            "common": {"refill": [1.25, 2.4]},
            "tanks": [{"id": self.tank, "refill": {"use_common": True, "local": [0.5, 2]}, "scale_max_m": 3}],
        })
        self.assertTrue(saved["success"], saved)
        path = self.root / ".storage" / f"uninus_water_control.settings.{self.entry.entry_id}"
        self.assertTrue(path.is_file(), "admitted config_save did not persist the draft")
        reopened, _ = await self.client()
        fresh = (await reopened("get"))["result"]
        self.assertEqual(fresh, saved["result"])
        self.assertNotEqual(fresh["revision"], baseline["revision"])
        tank = fresh["zones"][0]["tanks"][0]
        self.assertEqual(tank["refill"], {"use_common": True, "local": [0.5, 2], "effective": [1.25, 2.4]})
        self.assertEqual(tank["scale_max_m"], 3)
        await self.wait_for_state(self.entity("sensor", "refill_preview"), "refill_threshold")
        self.assertEqual(self.calls, [])

    async def test_settings_reject_lossy_wire_numbers_before_persistence(self):
        from homeassistant.util.json import json_loads

        request, _ = await self.client()
        base = (await request("get"))["result"]
        saved = await request("save", **self.compare(base), changes={
            "common": {"refill": [0.1, 1.125], "alarm": [-0.05, 7]},
            "tanks": [{"id": self.tank, "scale_max_m": 2.75}],
        })
        self.assertTrue(saved["success"], saved)
        expected = saved["result"]
        self.assertEqual(expected["common"], {"refill": [0.1, 1.125], "alarm": [-0.05, 7]})
        self.assertEqual(expected["zones"][0]["tanks"][0]["scale_max_m"], 2.75)
        path = self.root / ".storage" / self.entry.runtime_data.settings.store.key
        physical = path.read_bytes()
        fresh, _ = await self.client()
        for raw in (
            '{"common":{"refill":[9007199254740992,9007199254740993]}}',
            '{"common":{"refill":[0,9007199254740993.0]}}',
            '{"tanks":[{"id":"TANK","refill":{"local":[-9007199254740993,-9007199254740992]}}]}',
            '{"tanks":[{"id":"TANK","scale_max_m":18446744073709551617}]}',
            '{"common":{"alarm":[9007199254740993,7]}}',
        ):
            with self.subTest(raw=raw):
                # Decode requests just as Core's WS HTTP transport does; orjson already
                # rounds decimal tokens and integers outside its machine-integer range.
                reply = await request("save", **self.compare(expected), changes=json_loads(raw.replace("TANK", self.tank)))
                self.assertEqual(self.error(reply), "invalid_format")
                self.assertEqual(path.read_bytes(), physical)
                self.assertEqual((await fresh("get"))["result"], expected)
        self.assertEqual(self.calls, [])

    @staticmethod
    def error(reply):
        assert reply["success"] is False and "result" not in reply, reply
        return reply["error"]["code"]

    async def test_two_clients_and_native_options_cannot_overwrite_inflight_save(self):
        one, _ = await self.client()
        two, _ = await self.client()
        base = (await one("get"))["result"]
        settings = self.entry.runtime_data.settings
        manager = self.hass.config_entries.options
        menu = await manager.async_init(self.entry.entry_id)
        self.assertNotIn("edit_thresholds", menu["menu_options"])
        self.assertNotIn("edit_display_scale", menu["menu_options"])
        hint = await manager.async_configure(menu["flow_id"], {"next_step_id": "card_settings"})
        self.assertEqual(hint["reason"], "card_settings_in_dashboard_editor")
        menu = await manager.async_init(self.entry.entry_id)
        target = await manager.async_configure(menu["flow_id"], {"next_step_id": "edit_zone"})
        rename = await manager.async_configure(target["flow_id"], {"zone_id": self.zone["id"]})
        entered, release = asyncio.Event(), asyncio.Event()
        save = settings.store.async_save

        async def held(data):
            entered.set()
            await release.wait()
            await save(data)

        with patch.object(settings.store, "async_save", held):
            a = asyncio.create_task(one("save", **self.compare(base), changes={
                "tanks": [{"id": self.tank, "scale_max_m": 2.5}],
            }))
            b = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(settings.lock.locked())
                b = asyncio.create_task(two("save", **self.compare(base), changes={
                    "tanks": [{"id": self.tank, "scale_max_m": 5}],
                }))
                refused = await manager.async_configure(rename["flow_id"], {"zone_name": "Stray edit"})
                self.assertEqual(refused["reason"], "configuration_changed")
            finally:
                release.set()
                first = await a
                if b:
                    second = await b
            self.assertTrue(first["success"], first)
            self.assertEqual(self.error(second), "stale_revision")
        self.assertEqual((await two("get"))["result"], first["result"])
        self.assertEqual(self.calls, [])

    async def test_source_row_rename_is_safe_but_removal_and_reuse_is_stale(self):
        request, _ = await self.client()
        base = (await request("get"))["result"]
        moved = "sensor.moved_depth"
        self.registry.async_update_entity(self.probe, new_entity_id=moved)
        self.register_probe("reuses_vacated_name", "depth")
        self.set_depth("1", entity_id=moved)
        fresh = (await request("get"))["result"]
        self.assertEqual(self.compare(fresh), self.compare(base))
        saved = await request("save", **self.compare(base), changes={"common": {"alarm": [1, 1]}})
        self.assertTrue(saved["success"], saved)
        path = self.root / ".storage" / self.entry.runtime_data.settings.store.key
        before = path.read_bytes()
        self.registry.async_remove(moved)
        self.register_probe("reuses_moved_name", "moved_depth")
        rejected = await request("save", **self.compare(saved["result"]), changes={"common": {"alarm": [3, 1]}})
        self.assertEqual(self.error(rejected), "stale_source")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    async def test_admin_checked_again_after_gate_and_non_admin_cannot_read(self):
        admin, user = await self.client()
        self.assertFalse(user.is_owner)
        reader, _ = await self.client("system-read-only")
        self.assertEqual(self.error(await reader("get")), "unauthorized")
        base = (await admin("get"))["result"]
        self.assertEqual(self.error(await reader("save", **self.compare(base), changes={})), "unauthorized")
        settings = self.entry.runtime_data.settings
        async with settings.lock:
            queued = asyncio.create_task(admin("save", **self.compare(base), changes={"common": {"alarm": [1, 1]}}))
            async with asyncio.timeout(2):
                while not settings.lock._waiters or not any(not waiter.done() for waiter in settings.lock._waiters):
                    await asyncio.sleep(0)
            await self.hass.auth.async_update_user(user, group_ids=["system-read-only"])
            self.assertFalse(user.is_admin)
        self.assertEqual(self.error(await queued), "unauthorized")
        self.assertFalse((self.root / ".storage" / settings.store.key).exists())
        self.assertEqual(self.calls, [])

    async def test_swallowed_store_failure_has_no_ack_and_fresh_read_recovers(self):
        from homeassistant.util.file import WriteError

        request, _ = await self.client()
        base = (await request("get"))["result"]
        settings = self.entry.runtime_data.settings
        original = settings.store._async_write_data
        written = []

        async def fail(data):
            written.append(data["data"]["generation"])
            raise WriteError("intentional settings test fault")

        with patch.object(settings.store, "_async_write_data", fail):
            reply = await request("save", **self.compare(base), changes={"common": {"alarm": [1, 1]}})
        self.assertEqual(self.error(reply), "readback_required")
        self.assertEqual(len(written), 1)
        self.assertEqual((await request("get"))["result"], base)
        self.assertFalse((self.root / ".storage" / settings.store.key).exists())
        self.assertEqual(settings.store._async_write_data, original)
        self.assertEqual(self.calls, [])

    async def test_missing_overlay_preserves_legacy_and_reupgrade_does_not_shadow_native_drift(self):
        request, _ = await self.client()
        tank = self.zone["tanks"][0]
        data = deepcopy(dict(self.entry.data))
        data["zones"][0]["tanks"][0]["thresholds"] = {
            "refill_lower_m": 0.2, "refill_upper_m": 1.8, "alarm_lower_m": 0.3, "alarm_delay_seconds": 17,
        }
        data["display_scales_m"] = {tank["id"]: 2}
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        base = (await request("get"))["result"]
        legacy = base["zones"][0]["tanks"][0]
        self.assertEqual(legacy["refill"], {"use_common": False, "local": [0.2, 1.8], "effective": [0.2, 1.8]})
        self.assertEqual(legacy["alarm"]["effective"], [0.3, 17])
        self.assertEqual(legacy["scale_max_m"], 2)
        applied = await request("save", **self.compare(base), changes={"tanks": [{"id": tank["id"], "scale_max_m": 3}]})
        self.assertTrue(applied["success"], applied)
        self.assertEqual(self.entry.data["display_scales_m"], {tank["id"]: 2})
        # Emulate a committed change made by a downgraded integration, which did
        # not see or overwrite the versioned private overlay.
        newer_legacy = deepcopy(dict(self.entry.data))
        newer_legacy["display_scales_m"][tank["id"]] = 4
        self.hass.config_entries.async_update_entry(self.entry, data=newer_legacy)
        await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()
        recovered = (await request("get"))["result"]["zones"][0]["tanks"][0]
        self.assertEqual(recovered["scale_max_m"], 4)
        self.assertEqual(recovered["refill"], legacy["refill"])
        self.assertEqual(self.calls, [])

    async def test_scale_only_preserves_legacy_alarm_above_write_cap(self):
        await self.assert_legacy_alarm_survives_scale_save(2_147_483_648.0)

    async def test_scale_only_preserves_legacy_alarm_above_int64(self):
        await self.assert_legacy_alarm_survives_scale_save(float(2**64))

    async def test_scale_only_preserves_maximum_finite_legacy_alarm(self):
        await self.assert_legacy_alarm_survives_scale_save(sys.float_info.max)

    async def assert_legacy_alarm_survives_scale_save(self, delay):
        # v0.2.0's NumberSelector coerced to float before the finite/whole check.
        self.assertTrue(delay >= 1 and delay.is_integer() and float(int(delay)) == delay)
        policy = {"alarm_lower_m": 0.3, "alarm_delay_seconds": delay}
        data = deepcopy(dict(self.entry.data))
        data["zones"][0]["tanks"][0]["thresholds"] = policy
        data["display_scales_m"] = {self.tank: 2.0}
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        self.assertTrue(await self.hass.config_entries.async_reload(self.entry.entry_id))
        await self.hass.async_block_till_done()
        self.set_depth("0.2")
        alarm_id = self.entity("binary_sensor", "low_level_alarm")
        await self.wait_for_state(alarm_id, "off")
        owner = self.entry.runtime_data
        alarm = owner.settings.platforms["binary_sensor"].entities[alarm_id]
        timer, interval = alarm._cancel_timer, alarm._interval
        self.assertIsNotNone(timer)
        self.assertIsNotNone(interval)
        source_data, source_options = self.entry.data, self.entry.options
        level = self.registry.async_get_entity_id("sensor", "uninus_water_control", self.tank)
        effects = []
        self.hass.bus.async_listen(EVENT_CALL_SERVICE, effects.append)
        request, _ = await self.client()
        before = await request("get")
        self.assertTrue(before["success"], before)
        base = before["result"]
        legacy = base["zones"][0]["tanks"][0]
        pair = [0.3, delay]
        self.assertEqual(legacy["alarm"], {"use_common": False, "local": pair, "effective": pair})
        path = self.root / ".storage" / owner.settings.store.key
        self.assertFalse(path.exists())
        saved = await request("save", **self.compare(base), changes={
            "tanks": [{"id": self.tank, "scale_max_m": 3.0}],
        })
        self.assertTrue(saved["success"], saved)
        snapshot = saved["result"]
        self.assertNotEqual(snapshot["revision"], base["revision"])
        self.assertEqual(snapshot["source_snapshot"], base["source_snapshot"])
        self.assertEqual(snapshot["zones"][0]["tanks"][0], {**legacy, "scale_max_m": 3.0})
        self.assertEqual((await request("get"))["result"], snapshot)
        physical = path.read_bytes()
        stored = json.loads(physical)["data"]["tanks"][0]
        self.assertEqual(stored["alarm"], {"use_common": False, "local": pair})
        self.assertIs(type(stored["alarm"]["local"][1]), float)
        self.assertIs(self.entry.runtime_data, owner)
        self.assertIs(self.entry.data, source_data)
        self.assertIs(self.entry.options, source_options)
        self.assertEqual(dict(self.entry.data), data)
        self.assertEqual(self.hass.states.get(level).attributes["uninus_water_display"], {
            "version": 1, "scale_max_m": 3.0, "alarm": tuple(pair),
        })
        self.assertIs(alarm._cancel_timer, timer)
        self.assertIs(alarm._interval, interval)

        # Explicit writes remain bounded, even echoing the exact same legacy value.
        for changes in (
            {"common": {"alarm": pair}},
            {"tanks": [{"id": self.tank, "scale_max_m": 99, "alarm": {"local": pair}}]},
        ):
            self.assertEqual(self.error(await request("save", **self.compare(snapshot), changes=changes)),
                             "invalid_format")
            self.assertEqual(path.read_bytes(), physical)
        await self.hass.async_block_till_done()
        self.assertEqual(effects, [])
        self.assertEqual(self.calls, [])

        entry_id = self.entry.entry_id
        await self.hass.async_stop()
        await self.boot()
        self.entry = self.hass.config_entries.async_get_entry(entry_id)
        self.set_depth("0.2")
        self.assertTrue(await self.hass.config_entries.async_setup(entry_id))
        await self.hass.async_block_till_done()
        restarted, _ = await self.client()
        received = await restarted("get")
        self.assertTrue(received["success"], received)
        self.assertEqual(received["result"], snapshot)
        self.assertEqual(dict(self.entry.data), data)
        self.assertEqual(self.hass.states.get(level).attributes["uninus_water_display"]["alarm"], tuple(pair))
        self.assertEqual(self.hass.states.get(level).attributes["uninus_water_display"]["scale_max_m"], 3.0)
        self.assert_state(alarm_id, "off")
        self.assertEqual(path.read_bytes(), physical)
        self.assertEqual(self.calls, [])

    async def test_update_restarts_only_alarm_with_changed_effective_pair(self):
        manager = self.hass.config_entries.options
        self.set_depth("0.3")
        peer_source = self.register_probe("peer", "peer_depth")
        self.set_depth("0.4", entity_id=peer_source)
        menu = await manager.async_init(self.entry.entry_id)
        flow = menu["flow_id"]
        await manager.async_configure(flow, {"next_step_id": "add_tank"})
        await manager.async_configure(flow, {"zone_id": self.zone["id"]})
        await manager.async_configure(flow, {"tank_name": "Peer", "level_sensor": peer_source})
        await self.hass.async_block_till_done()
        peer = self.entry.options["zones"][0]["tanks"][1]["id"]
        request, _ = await self.client()
        base = (await request("get"))["result"]
        saved = await request("save", **self.compare(base), changes={"common": {"alarm": [1, 60]}, "tanks": [
            {"id": self.tank, "alarm": {"use_common": True, "local": [2, 61]}},
            {"id": peer, "alarm": {"local": [1, 60]}},
        ]})
        self.assertTrue(saved["success"], saved)
        owner = self.entry.runtime_data
        platform = owner.settings.platforms["binary_sensor"]
        first = platform.entities[self.entity("binary_sensor", "low_level_alarm")]
        other = platform.entities[self.entity("binary_sensor", "low_level_alarm", tank=peer)]
        first_interval, second_interval, second_timer = first._interval, other._interval, other._cancel_timer
        self.assertIsNotNone(first_interval)
        self.assertIsNotNone(second_interval)
        saved = await request("save", **self.compare(saved["result"]), changes={"common": {"alarm": [1, 61]}})
        self.assertTrue(saved["success"], saved)
        result = saved["result"]["zones"][0]["tanks"]
        self.assertEqual(result[0]["alarm"], {"use_common": True, "local": [2, 61], "effective": [1, 61]})
        self.assertEqual(result[1]["alarm"]["effective"], [1, 60])
        self.assertIsNot(first._interval, first_interval)
        self.assertIs(other._interval, second_interval)
        self.assertIs(other._cancel_timer, second_timer)
        self.assertIs(self.entry.runtime_data, owner)
        self.assertEqual(self.calls, [])

    async def test_revocation_after_atomic_write_withholds_the_private_reply(self):
        request, user = await self.client()
        verifier, _ = await self.client()
        base = (await request("get"))["result"]
        settings = self.entry.runtime_data.settings
        entered, release = asyncio.Event(), asyncio.Event()
        save = settings.store.async_save

        async def held(data):
            await save(data)
            entered.set()
            await release.wait()

        with patch.object(settings.store, "async_save", held):
            pending = asyncio.create_task(request("save", **self.compare(base), changes={"common": {"alarm": [1, 1]}}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue((self.root / ".storage" / settings.store.key).is_file())
                await self.hass.auth.async_update_user(user, group_ids=["system-read-only"])
            finally:
                release.set()
                reply = await pending
        self.assertEqual(self.error(reply), "unauthorized")
        fresh = (await verifier("get"))["result"]
        self.assertEqual(fresh["common"]["alarm"], [1, 1])
        self.assertNotEqual(fresh["revision"], base["revision"])
        self.assertEqual(self.calls, [])
