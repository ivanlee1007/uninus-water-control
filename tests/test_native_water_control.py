"""Exercise the integration through the real Home Assistant managers."""

from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import UUID

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.selector import SelectSelector
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

DOMAIN = "uninus_water_control"
SETUP = {"zone_name": "North", "tank_name": "Storage", "level_sensor": "sensor.depth"}
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN


class NativeWaterControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        if SOURCE.exists():
            shutil.copytree(SOURCE, root / "custom_components" / DOMAIN)
        self.hass = HomeAssistant(str(root))
        self.addAsyncCleanup(self.hass.async_stop)
        frame.async_setup(self.hass)
        loader.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        await self.hass.config_entries.async_initialize()
        await self.hass.async_start()
        self.set_depth("sensor.depth", "125", "cm")
        self.set_depth("sensor.backup", "2400", "mm")
        self.hass.states.async_set("switch.refill", "off")
        self.hass.states.async_set("switch.alarm", "off")

    def set_depth(self, entity_id, value, unit):
        self.hass.states.async_set(entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})

    async def setup_controller(self, **changes):
        flow = self.hass.config_entries.flow
        result = await flow.async_init(DOMAIN, context={"source": "user"})
        self.assertEqual(result["type"], FlowResultType.FORM, result)
        result = await flow.async_configure(result["flow_id"], SETUP | changes)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
        await self.hass.async_block_till_done()
        entry = result["result"]
        self.assertEqual(entry.state, ConfigEntryState.LOADED)
        return entry

    def owned_entities(self, entry):
        return er.async_entries_for_config_entry(er.async_get(self.hass), entry.entry_id)

    def assert_level(self, entity_id, expected):
        state = self.hass.states.get(entity_id)
        self.assertIsNotNone(state, entity_id)
        if expected == STATE_UNAVAILABLE:
            self.assertEqual(state.state, expected)
        else:
            self.assertEqual(float(state.state), expected)
            self.assertEqual(state.attributes[ATTR_UNIT_OF_MEASUREMENT], "m")

    async def test_setup_tracks_only_the_bound_level_in_metres(self):
        entry = await self.setup_controller(refill_switch="switch.refill", alarm_switch="switch.alarm")
        self.assertIn("zones", entry.data)
        zone, = entry.data["zones"]
        tank, = zone["tanks"]
        for record in (zone, tank):
            self.assertEqual(UUID(hex=record["id"]).hex, record["id"])
        self.assertEqual(tank["refill_switch"], "switch.refill")
        self.assertEqual(tank["alarm_switch"], "switch.alarm")
        entity, = self.owned_entities(entry)
        self.assertEqual(entity.domain, "sensor")
        self.assert_level(entity.entity_id, 1.25)
        for value, unit, expected in (
            ("1.5", "m", 1.5), ("800", "mm", 0.8), ("5", "%", STATE_UNAVAILABLE),
            ("inf", "m", STATE_UNAVAILABLE), ("nan", "cm", STATE_UNAVAILABLE),
            ("unknown", "m", STATE_UNAVAILABLE), ("2.5", "m", 2.5),
        ):
            self.set_depth("sensor.depth", value, unit)
            await self.hass.async_block_till_done()
            self.assert_level(entity.entity_id, expected)
        self.hass.states.async_remove("sensor.depth")
        await self.hass.async_block_till_done()
        self.assert_level(entity.entity_id, STATE_UNAVAILABLE)
        duplicate = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        self.assertEqual(duplicate["reason"], "already_configured")

    async def option(self, entry, operation):
        manager = self.hass.config_entries.options
        menu = await manager.async_init(entry.entry_id)
        self.assertEqual(menu["type"], FlowResultType.MENU, menu)
        return await manager.async_configure(menu["flow_id"], {"next_step_id": operation})

    async def submit(self, step, **fields):
        return await self.hass.config_entries.options.async_configure(step["flow_id"], fields)

    async def saved(self, step, **fields):
        result = await self.submit(step, **fields)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
        await self.hass.async_block_till_done()
        return result

    def register_depth(self, unique_id, object_id="probe_depth"):
        row = er.async_get(self.hass).async_get_or_create(
            "sensor", "test_probe", unique_id, suggested_object_id=object_id
        )
        self.assertEqual(row.entity_id, f"sensor.{object_id}")
        return row

    async def edit_tank(self, entry, tank_id):
        return await self.submit(await self.option(entry, "edit_tank"), tank_id=tank_id)

    def suggested_level(self, form):
        self.assertEqual(form["type"], FlowResultType.FORM, form)
        return next(key for key in form["data_schema"].schema if key == "level_sensor").description.get(
            "suggested_value"
        )

    async def assert_echoed_level_identity(self, operation):
        config = self.hass.config_entries
        source = self.register_depth("original")
        self.set_depth(source.entity_id, "1.5", "m")
        entry = None
        fields = {"tank_name": "Corrected tank", "level_sensor": source.entity_id}
        if operation in ("user", "add_zone"):
            fields["zone_name"] = "New zone"
        if operation == "user":
            manager = config.flow
            form = await manager.async_init(DOMAIN, context={"source": "user"})
            original_projections = set()
        else:
            manager = config.options
            entry = await self.setup_controller()
            original_data, original_options = entry.data, entry.options
            original_projections = {row.unique_id for row in self.owned_entities(entry)}
            zone, = entry.data["zones"]
            form = await self.option(entry, operation)
            if operation == "add_tank":
                form = await self.submit(form, zone_id=zone["id"])
            elif operation == "edit_tank":
                form = await self.submit(form, tank_id=zone["tanks"][0]["id"])

        invalid = await manager.async_configure(form["flow_id"], fields | {"tank_name": " "})
        self.assertEqual(invalid["errors"], {"tank_name": "blank_name"})
        self.assertEqual(self.suggested_level(invalid), source.entity_id)

        registry = er.async_get(self.hass)
        moved = registry.async_update_entity(source.entity_id, new_entity_id="sensor.original_depth")
        self.assertEqual(moved.id, source.id)
        self.hass.states.async_remove(source.entity_id)
        replacement = self.register_depth("replacement")
        self.assertNotEqual(replacement.id, source.id)
        self.set_depth(moved.entity_id, "1.5", "m")
        self.set_depth(replacement.entity_id, "9", "m")

        # First correct only the name; then forge the same stale selector value
        # after it has been dropped from the native form's suggestions.
        for _ in range(2):
            rejected = await manager.async_configure(invalid["flow_id"], fields)
            await self.hass.async_block_till_done()
            if rejected["type"] == FlowResultType.CREATE_ENTRY:
                committed, = config.async_entries(DOMAIN)
                tank, = (
                    tank for zone in (committed.options or committed.data)["zones"]
                    for tank in zone["tanks"] if tank["name"] == fields["tank_name"]
                )
                self.assertEqual(tank["level_sensor_registry_id"], replacement.id)
                projection, = (row for row in self.owned_entities(committed) if row.unique_id == tank["id"])
                self.assert_level(projection.entity_id, 9.0)
                self.fail(f"{operation}: correcting only the name bound the replacement and published 9 m")
            self.assertEqual(rejected["errors"], {"level_sensor": "level_source_changed"})
            self.assertEqual(self.suggested_level(rejected), "sensor.depth" if operation == "edit_tank" else None)
            if entry is None:
                self.assertEqual(config.async_entries(DOMAIN), [])
            else:
                self.assertIs(entry.data, original_data)
                self.assertIs(entry.options, original_options)
                self.assertEqual({row.unique_id for row in self.owned_entities(entry)}, original_projections)
                for row in self.owned_entities(entry):
                    self.assert_level(row.entity_id, 1.25)

        corrected = await manager.async_configure(rejected["flow_id"], fields | {"level_sensor": moved.entity_id})
        self.assertEqual(corrected["type"], FlowResultType.CREATE_ENTRY, corrected)
        await self.hass.async_block_till_done()
        committed, = config.async_entries(DOMAIN)
        tank, = (
            tank for zone in (committed.options or committed.data)["zones"]
            for tank in zone["tanks"] if tank["name"] == fields["tank_name"]
        )
        self.assertEqual(tank["level_sensor_registry_id"], source.id)
        self.assertEqual({row.unique_id for row in self.owned_entities(committed)}, original_projections | {tank["id"]})
        projection, = (row for row in self.owned_entities(committed) if row.unique_id == tank["id"])
        self.assert_level(projection.entity_id, 1.5)

    async def test_echoed_level_identity_initial_setup(self):
        await self.assert_echoed_level_identity("user")

    async def test_echoed_level_identity_add_zone(self):
        await self.assert_echoed_level_identity("add_zone")

    async def test_echoed_level_identity_add_tank(self):
        await self.assert_echoed_level_identity("add_tank")

    async def test_echoed_level_identity_edit_tank(self):
        await self.assert_echoed_level_identity("edit_tank")

    async def test_registered_level_follows_rename_instead_of_recycled_id(self):
        registry = er.async_get(self.hass)
        source = self.register_depth("original")
        self.set_depth(source.entity_id, "1", "m")
        entry = await self.setup_controller(level_sensor=source.entity_id)
        projection, = self.owned_entities(entry)
        zone, = entry.data["zones"]
        self.assert_level(projection.entity_id, 1.0)

        moved = registry.async_update_entity(source.entity_id, new_entity_id="sensor.original_depth")
        self.hass.states.async_remove(source.entity_id)
        replacement = self.register_depth("replacement")
        self.assertNotEqual(replacement.id, source.id)
        self.set_depth(moved.entity_id, "1.5", "m")
        self.set_depth(replacement.entity_id, "9", "m")
        await self.hass.async_block_till_done()
        self.assert_level(projection.entity_id, 1.5)
        self.assertTrue(await self.hass.config_entries.async_reload(entry.entry_id))
        await self.hass.async_block_till_done()
        self.assert_level(projection.entity_id, 1.5)

        rename_zone = await self.submit(await self.option(entry, "edit_zone"), zone_id=zone["id"])
        await self.saved(rename_zone, zone_name="South")
        edit = await self.edit_tank(entry, projection.unique_id)
        self.assertEqual(self.suggested_level(edit), moved.entity_id)
        await self.saved(edit, tank_name="Renamed", level_sensor=moved.entity_id, refill_switch="switch.refill")
        self.assertEqual(entry.options["zones"][0]["tanks"][0]["level_sensor_registry_id"], source.id)
        current, = self.owned_entities(entry)
        self.assertEqual((current.id, current.entity_id, current.unique_id),
                         (projection.id, projection.entity_id, projection.unique_id))
        self.assert_level(current.entity_id, 1.5)
        self.assertEqual(self.hass.states.get("switch.refill").state, "off")

    async def test_stale_registered_level_default_cannot_rebind_on_save(self):
        registry = er.async_get(self.hass)
        source = self.register_depth("original")
        entry = await self.setup_controller(level_sensor=source.entity_id)
        projection, = self.owned_entities(entry)
        edit = await self.edit_tank(entry, projection.unique_id)
        self.assertEqual(self.suggested_level(edit), source.entity_id)
        moved = registry.async_update_entity(source.entity_id, new_entity_id="sensor.original_depth")
        replacement = self.register_depth("replacement")
        self.set_depth(moved.entity_id, "1.5", "m")
        self.set_depth(replacement.entity_id, "9", "m")

        rejected = await self.submit(edit, tank_name="Rename only", level_sensor=source.entity_id)
        self.assertEqual(rejected["type"], FlowResultType.FORM)
        self.assertIn("level_sensor", rejected["errors"])
        self.assertFalse(entry.options)
        self.assertEqual(self.suggested_level(rejected), moved.entity_id)
        await self.saved(rejected, tank_name="Rename only", level_sensor=moved.entity_id)
        self.assert_level(projection.entity_id, 1.5)

    async def test_removed_registered_level_requires_explicit_rebind(self):
        registry = er.async_get(self.hass)
        source = self.register_depth("original")
        self.set_depth(source.entity_id, "1", "m")
        entry = await self.setup_controller(level_sensor=source.entity_id)
        projection, = self.owned_entities(entry)
        stale = await self.edit_tank(entry, projection.unique_id)
        registry.async_remove(source.entity_id)
        self.hass.states.async_remove(source.entity_id)
        replacement = self.register_depth("replacement")
        self.assertNotEqual(replacement.id, source.id)
        self.set_depth(replacement.entity_id, "9", "m")
        await self.hass.async_block_till_done()
        self.assert_level(projection.entity_id, STATE_UNAVAILABLE)
        self.assertTrue(await self.hass.config_entries.async_reload(entry.entry_id))
        await self.hass.async_block_till_done()
        self.assert_level(projection.entity_id, STATE_UNAVAILABLE)

        rejected = await self.submit(stale, tank_name="Rename only", level_sensor=source.entity_id)
        self.assertIn("level_sensor", rejected["errors"])
        self.assertIsNone(self.suggested_level(rejected))
        fresh = await self.edit_tank(entry, projection.unique_id)
        self.assertIsNone(self.suggested_level(fresh))
        await self.saved(fresh, tank_name="Storage", level_sensor=replacement.entity_id)
        self.assertEqual(entry.options["zones"][0]["tanks"][0]["level_sensor_registry_id"], replacement.id)
        self.assertEqual(self.owned_entities(entry)[0].id, projection.id)
        self.assert_level(projection.entity_id, 9.0)

    async def test_stale_registered_level_guard_survives_validation_rerender(self):
        registry = er.async_get(self.hass)
        source = self.register_depth("original")
        entry = await self.setup_controller(level_sensor=source.entity_id)
        projection, = self.owned_entities(entry)
        edit = await self.edit_tank(entry, projection.unique_id)
        self.assertEqual(self.suggested_level(edit), source.entity_id)
        invalid = await self.submit(edit, tank_name=" ", level_sensor="sensor.backup")
        self.assertEqual(invalid["errors"], {"tank_name": "blank_name"})
        self.assertEqual(self.suggested_level(invalid), "sensor.backup")

        moved = registry.async_update_entity(source.entity_id, new_entity_id="sensor.original_depth")
        replacement = self.register_depth("replacement")
        self.set_depth(moved.entity_id, "1.5", "m")
        self.set_depth(replacement.entity_id, "9", "m")
        for _ in range(2):
            rejected = await self.submit(edit, tank_name="Storage", level_sensor=source.entity_id)
            self.assertEqual(rejected["type"], FlowResultType.FORM)
            self.assertIn("level_sensor", rejected["errors"])
            self.assertFalse(entry.options)
            self.assertEqual(self.suggested_level(rejected), moved.entity_id)
        await self.saved(rejected, tank_name="Storage", level_sensor=moved.entity_id)
        self.assert_level(projection.entity_id, 1.5)

    async def test_unregistered_level_does_not_inherit_new_registry_row(self):
        self.set_depth("sensor.probe_depth", "1", "m")
        entry = await self.setup_controller(level_sensor="sensor.probe_depth")
        projection, = self.owned_entities(entry)
        self.assert_level(projection.entity_id, 1.0)
        self.hass.states.async_remove("sensor.probe_depth")
        replacement = self.register_depth("replacement")
        self.set_depth(replacement.entity_id, "9", "m")
        await self.hass.async_block_till_done()
        self.assert_level(projection.entity_id, STATE_UNAVAILABLE)
        fresh = await self.edit_tank(entry, projection.unique_id)
        self.assertIsNone(self.suggested_level(fresh))
        await self.saved(fresh, tank_name="Storage", level_sensor=replacement.entity_id)
        self.assert_level(projection.entity_id, 9.0)

    async def test_confirmed_deletion_cleans_registry_when_controller_is_disabled(self):
        entry = await self.setup_controller()
        # Duplicate labels remain valid; a newly shared source does not.
        self.set_depth("sensor.other_zone_depth", "125", "cm")
        await self.saved(await self.option(entry, "add_zone"), **(SETUP | {"level_sensor": "sensor.other_zone_depth"}))
        survivor, removed = entry.options["zones"]
        self.assertTrue(await self.hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER))
        self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
        self.assertEqual(len(self.owned_entities(entry)), 2)
        confirm = await self.submit(await self.option(entry, "remove_zone"), zone_id=removed["id"])
        await self.saved(confirm, confirm=True)
        self.assertEqual(entry.options["zones"], [survivor])
        self.assertEqual({row.unique_id for row in self.owned_entities(entry)}, {survivor["tanks"][0]["id"]})
        self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)

    async def test_options_edit_reload_stable_identity_and_stale_save(self):
        entry = await self.setup_controller(refill_switch="switch.refill", alarm_switch="switch.alarm")
        zone, = entry.data["zones"]
        tank, = zone["tanks"]
        before, = self.owned_entities(entry)
        stale = await self.submit(await self.option(entry, "edit_zone"), zone_id=zone["id"])
        edit = await self.submit(await self.option(entry, "edit_tank"), tank_id=tank["id"])
        await self.saved(edit, tank_name="Renamed", level_sensor="sensor.backup", alarm_switch="switch.alarm")
        current = self.hass.config_entries.async_get_entry(entry.entry_id)
        saved_zone, = current.options["zones"]
        saved_tank, = saved_zone["tanks"]
        self.assertEqual(saved_zone["id"], zone["id"])
        self.assertEqual(saved_tank, tank | {"name": "Renamed", "level_sensor": "sensor.backup", "refill_switch": None})
        after, = self.owned_entities(entry)
        self.assertEqual((after.entity_id, after.unique_id), (before.entity_id, before.unique_id))
        self.assertIn("Renamed", self.hass.states.get(after.entity_id).name)
        self.assert_level(after.entity_id, 2.4)
        self.set_depth("sensor.depth", "700", "cm")
        await self.hass.async_block_till_done()
        self.assert_level(after.entity_id, 2.4)
        self.set_depth("sensor.backup", "350", "cm")
        await self.hass.async_block_till_done()
        self.assert_level(after.entity_id, 3.5)
        rejected = await self.submit(stale, zone_name="Stale overwrite")
        self.assertEqual(rejected["reason"], "configuration_changed")
        self.assertEqual(current.options["zones"], [saved_zone])
        fresh = await self.submit(await self.option(entry, "edit_zone"), zone_id=zone["id"])
        await self.saved(fresh, zone_name="Renamed zone")
        self.assertEqual(current.options["zones"][0]["name"], "Renamed zone")
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.hass.states.async_remove("sensor.backup")
        await self.hass.async_block_till_done()
        # HA retains the registry row and may display it as unavailable when unloaded.
        unloaded = self.hass.states.get(after.entity_id)
        self.assertIn(None if unloaded is None else unloaded.state, (None, STATE_UNAVAILABLE))
        self.assertTrue(await self.hass.config_entries.async_setup(entry.entry_id))
        await self.hass.async_block_till_done()
        self.assert_level(after.entity_id, STATE_UNAVAILABLE)
        # Stop flushes the real Core config-entry store. Read it through a fresh manager.
        await self.hass.async_stop()
        restarted = HomeAssistant(self.directory.name)
        self.addAsyncCleanup(restarted.async_stop)
        restarted.config_entries = ConfigEntries(restarted, {})
        await restarted.config_entries.async_initialize()
        persisted = restarted.config_entries.async_get_entry(entry.entry_id)
        self.assertEqual(persisted.options, current.options)

    async def test_options_add_remove_duplicates_minimum_and_registry_cleanup(self):
        entry = await self.setup_controller()
        zone, = entry.data["zones"]
        tank, = zone["tanks"]
        for operation, fields, reason in (
            ("remove_zone", {"zone_id": zone["id"]}, "minimum_zones"),
            ("remove_tank", {"tank_id": tank["id"]}, "minimum_tanks"),
        ):
            rejected = await self.submit(await self.option(entry, operation), **fields)
            self.assertEqual(rejected["reason"], reason)
        # Duplicate labels remain valid; a newly shared source does not.
        self.set_depth("sensor.other_zone_depth", "125", "cm")
        await self.saved(await self.option(entry, "add_zone"), **(SETUP | {"level_sensor": "sensor.other_zone_depth"}))
        first, second = entry.options["zones"]
        self.assertEqual(first["name"], second["name"])
        self.assertNotEqual(first["id"], second["id"])
        choose = await self.option(entry, "add_tank")
        selector, = choose["data_schema"].schema.values()
        self.assertIsInstance(selector, SelectSelector)
        labels = [option["label"] for option in selector.config["options"]]
        self.assertEqual(len(set(labels)), 2)
        with self.assertRaises(InvalidData):
            await self.submit(choose, zone_id="0" * 32)
        add = await self.submit(choose, zone_id=first["id"])
        await self.saved(add, tank_name=tank["name"], level_sensor="sensor.backup")
        new_tank = entry.options["zones"][0]["tanks"][1]
        choose = await self.option(entry, "remove_tank")
        selector, = choose["data_schema"].schema.values()
        self.assertEqual(len({option["label"] for option in selector.config["options"]}), 3)
        with self.assertRaises(InvalidData):
            await self.submit(choose, tank_id="0" * 32)
        confirm = await self.submit(choose, tank_id=new_tank["id"])
        self.assertEqual(len(self.owned_entities(entry)), 3)
        unconfirmed = await self.submit(confirm, confirm=False)
        self.assertEqual(unconfirmed["type"], FlowResultType.FORM)
        self.assertEqual(len(self.owned_entities(entry)), 3)
        await self.saved(unconfirmed, confirm=True)
        self.assertEqual({row.unique_id for row in self.owned_entities(entry)}, {tank["id"], second["tanks"][0]["id"]})
        confirm = await self.submit(await self.option(entry, "remove_zone"), zone_id=second["id"])
        await self.saved(confirm, confirm=True)
        survivor, = self.owned_entities(entry)
        self.assertEqual(survivor.unique_id, tank["id"])
        self.assertEqual(entry.options["zones"], [zone])
        self.assertIsNotNone(self.hass.states.get(SETUP["level_sensor"]))

    async def test_native_validation_prevents_invalid_inventory(self):
        manager = self.hass.config_entries.flow
        step = await manager.async_init(DOMAIN, context={"source": "user"})
        for bad in ({"level_sensor": "invalid"}, {"refill_switch": "bad switch"}):
            with self.assertRaises(InvalidData):
                await manager.async_configure(step["flow_id"], SETUP | bad)
        for bad, field, error in (
            ({"tank_name": "  "}, "tank_name", "blank_name"),
            ({"level_sensor": "switch.alarm"}, "level_sensor", "invalid_sensor"),
            ({"level_sensor": "0" * 32}, "level_sensor", "invalid_sensor"),
            ({"alarm_switch": "sensor.depth"}, "alarm_switch", "invalid_switch"),
        ):
            result = await manager.async_configure(step["flow_id"], SETUP | bad)
            self.assertEqual(result["errors"], {field: error})
            self.assertEqual(self.hass.config_entries.async_entries(DOMAIN), [])
        entry = await self.setup_controller(refill_switch="switch.refill")
        tank_entity, = self.owned_entities(entry)
        add = await self.submit(await self.option(entry, "add_tank"), zone_id=entry.data["zones"][0]["id"])
        rejected = await self.submit(add, tank_name="cycle", level_sensor=tank_entity.entity_id)
        self.assertEqual(rejected["errors"], {"level_sensor": "own_level_sensor"})
        self.assertFalse(entry.options)
