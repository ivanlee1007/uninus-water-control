"""Verify the dashboard contract on isolated Home Assistant Core."""

from importlib import import_module
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from aiohttp import ClientSession, ClientTimeout
from homeassistant import auth, loader
from homeassistant.config_entries import HANDLERS, ConfigEntries, ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.setup import async_setup_component

DOMAIN = "uninus_water_control"
ATTR_DASHBOARD = "uninus_water_dashboard"
SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN


class DashboardProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.integration = self.root / "custom_components" / DOMAIN
        shutil.copytree(SOURCE, self.integration, ignore=shutil.ignore_patterns("__pycache__"))

        # Core's namespace import can otherwise reuse the checkout or prior fixture.
        namespace = import_module("custom_components")
        prefix = f"custom_components.{DOMAIN}"
        def integration_modules():
            return {name: module for name, module in sys.modules.copy().items()
                    if name == prefix or name.startswith(f"{prefix}.")}

        original = integration_modules()
        original_handler = HANDLERS.get(DOMAIN)
        original_binding = getattr(namespace, DOMAIN, None)
        for name in original:
            del sys.modules[name]
        if original_binding is not None:
            delattr(namespace, DOMAIN)

        def restore_integration_imports():
            for name in integration_modules():
                del sys.modules[name]
            sys.modules.update(original)
            # Flow handlers are global too: retaining the temporary class pairs
            # its old SettingsError with a different restored module instance.
            if original_handler is None:
                HANDLERS.pop(DOMAIN, None)
            else:
                HANDLERS[DOMAIN] = original_handler
            if original_binding is not None:
                setattr(namespace, DOMAIN, original_binding)
            elif hasattr(namespace, DOMAIN):
                delattr(namespace, DOMAIN)

        self.addCleanup(restore_integration_imports)
        self.enterContext(patch.object(namespace, "__path__", [str(self.integration.parent)]))

    async def start_core(self, *, http=False):
        hass = HomeAssistant(str(self.root))
        self.addAsyncCleanup(hass.async_stop)
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await hass.config_entries.async_initialize()
        if http:
            hass.auth = await auth.auth_manager_from_config(hass, [], [])
            self.assertTrue(await async_setup_component(hass, "http", {
                "http": {"server_host": "127.0.0.1"},
            }))
            # The YAML schema rejects zero; the native listener accepts it and avoids port races.
            hass.http.server_port = 0
            self.assertTrue(await async_setup_component(hass, DOMAIN, {}))
            loaded = sys.modules[f"custom_components.{DOMAIN}"]
            self.assertEqual(Path(loaded.__file__).resolve(), self.integration / "__init__.py")
        await hass.async_start()
        if http:
            await hass.async_block_till_done()
        return hass

    async def save(self, hass, manager, form, **fields):
        result = await manager.async_configure(form["flow_id"], fields)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY, result)
        await hass.async_block_till_done()
        return result

    async def choose(self, manager, entry, operation, **selection):
        menu = await manager.async_init(entry.entry_id)
        self.assertEqual(menu["type"], FlowResultType.MENU, menu)
        form = await manager.async_configure(menu["flow_id"], {"next_step_id": operation})
        self.assertEqual(form["type"], FlowResultType.FORM, form)
        if selection:
            form = await manager.async_configure(form["flow_id"], selection)
            self.assertEqual(form["type"], FlowResultType.FORM, form)
        return form

    def assert_projected(self, hass, entry, inventory, readings):
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        rows = {row.unique_id: row for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
        expected_ids = {tank["id"] for zone in inventory for tank in zone["tanks"]}
        self.assertEqual(set(rows), expected_ids)
        self.assertEqual(
            {state.entity_id for state in hass.states.async_all() if ATTR_DASHBOARD in state.attributes},
            {row.entity_id for row in rows.values()},
        )
        seen = set()
        for zone_index, zone in enumerate(inventory):
            self.assertEqual(UUID(hex=zone["id"]).hex, zone["id"])
            self.assertNotIn(zone["id"], seen)
            seen.add(zone["id"])
            for tank_index, tank in enumerate(zone["tanks"]):
                self.assertEqual(UUID(hex=tank["id"]).hex, tank["id"])
                self.assertNotIn(tank["id"], seen)
                seen.add(tank["id"])
                row = rows[tank["id"]]
                self.assertEqual((row.domain, row.platform), ("sensor", DOMAIN))
                state = hass.states.get(row.entity_id)
                self.assertIsNotNone(state, row.entity_id)
                expected_state = readings[tank["id"]]
                if expected_state == STATE_UNAVAILABLE:
                    self.assertEqual(state.state, STATE_UNAVAILABLE)
                else:
                    self.assertEqual(float(state.state), expected_state)
                self.assertEqual(dict(state.attributes), {
                    "uninus_water_settings_entry": entry.entry_id,
                    ATTR_DASHBOARD: {
                        "version": 1,
                        "zone": {"id": zone["id"], "name": zone["name"], "index": zone_index},
                        "tank": {"id": tank["id"], "name": tank["name"], "index": tank_index},
                    },
                    "device_class": "distance",
                    "friendly_name": f"{zone['name']} · {tank['name']} water level",
                    "state_class": "measurement",
                    ATTR_UNIT_OF_MEASUREMENT: "m",
                })
        return {unique: (row.id, row.entity_id) for unique, row in rows.items()}

    async def test_native_inventory_projects_only_layout_metadata_even_when_level_unavailable(self):
        hass = await self.start_core()
        self.assertIsNone(hass.http)
        source = er.async_get(hass).async_get_or_create("sensor", "test_probe", "private-level")
        hass.states.async_set(source.entity_id, "125", {ATTR_UNIT_OF_MEASUREMENT: "cm"})
        hass.states.async_set("sensor.backup", "2400", {ATTR_UNIT_OF_MEASUREMENT: "mm"})
        hass.states.async_set("switch.refill", "on")
        hass.states.async_set("switch.alarm", "off")
        fields = {
            "zone_name": "North",
            "tank_name": "Storage",
            "level_sensor": source.entity_id,
            "refill_switch": "switch.refill",
            "alarm_switch": "switch.alarm",
        }
        flow = hass.config_entries.flow
        form = await flow.async_init(DOMAIN, context={"source": "user"})
        entry = (await self.save(hass, flow, form, **fields))["result"]
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        options = hass.config_entries.options
        # Keep duplicate labels, not newly shared private sources/outputs.
        hass.states.async_set("sensor.other_zone_depth", "125", {ATTR_UNIT_OF_MEASUREMENT: "cm"})
        await self.save(hass, options, await self.choose(options, entry, "add_zone"),
                        **({key: value for key, value in fields.items() if key not in ("refill_switch", "alarm_switch")}
                           | {"level_sensor": "sensor.other_zone_depth"}))
        north, other = entry.options["zones"]
        await self.save(hass, options, await self.choose(options, entry, "add_tank", zone_id=north["id"]),
                        tank_name=fields["tank_name"], level_sensor="sensor.backup")
        north, other = entry.options["zones"]
        first, second = north["tanks"]
        third, = other["tanks"]
        reading = {first["id"]: 1.25, second["id"]: 2.4, third["id"]: 1.25}
        ids = self.assert_projected(hass, entry, [north, other], reading)

        await self.save(hass, options, await self.choose(options, entry, "edit_zone", zone_id=other["id"]),
                        zone_name="South <water>")
        await self.save(hass, options, await self.choose(options, entry, "edit_tank", tank_id=second["id"]),
                        tank_name="Secondary", level_sensor="sensor.backup")
        self.assertEqual(self.assert_projected(hass, entry, entry.options["zones"], reading), ids)

        hass.states.async_set(source.entity_id, "5", {ATTR_UNIT_OF_MEASUREMENT: "%"})
        hass.states.async_remove("sensor.other_zone_depth")
        hass.states.async_remove("sensor.backup")
        await hass.async_block_till_done()
        self.assertEqual(self.assert_projected(hass, entry, entry.options["zones"],
                                              dict.fromkeys(reading, STATE_UNAVAILABLE)), ids)
        hass.states.async_set("sensor.backup", "3", {ATTR_UNIT_OF_MEASUREMENT: "m"})
        await hass.async_block_till_done()
        reading = dict.fromkeys(reading, STATE_UNAVAILABLE) | {second["id"]: 3.0}
        self.assertEqual(self.assert_projected(hass, entry, entry.options["zones"], reading), ids)

        await self.save(hass, options, await self.choose(options, entry, "remove_tank", tank_id=first["id"]),
                        confirm=True)
        await self.save(hass, options, await self.choose(options, entry, "remove_zone", zone_id=north["id"]),
                        confirm=True)
        self.assertEqual(self.assert_projected(hass, entry, entry.options["zones"], reading),
                         {third["id"]: ids[third["id"]]})
        self.assertEqual(hass.states.get("switch.refill").state, "on")
        self.assertEqual(hass.states.get("switch.alarm").state, "off")

        # Read back the durable options and entity identity with a new Core instance.
        await hass.async_stop()
        restarted = await self.start_core()
        persisted = restarted.config_entries.async_get_entry(entry.entry_id)
        self.assertEqual(persisted.options, entry.options)
        self.assertTrue(await restarted.config_entries.async_setup(persisted.entry_id))
        await restarted.async_block_till_done()
        self.assertEqual(self.assert_projected(restarted, persisted, persisted.options["zones"], reading),
                         {third["id"]: ids[third["id"]]})

    async def test_native_http_serves_only_the_dashboard_module_without_caching(self):
        module = self.integration / "dashboard.js"
        shipped = module.read_bytes()
        self.assertTrue(shipped)
        hass = await self.start_core(http=True)
        [(host, port)] = hass.http.runner.addresses
        self.assertEqual(host, "127.0.0.1")
        prefix = f"http://{host}:{port}/{DOMAIN}"
        url = f"{prefix}/dashboard.js"
        async with ClientSession(timeout=ClientTimeout(total=5)) as client:
            async with client.get(url) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(response.content_type, {"text/javascript", "application/javascript"})
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                self.assertEqual(await response.read(), shipped)

            # A new build must work at the same URL without changing Lovelace resources.
            replacement = shipped + b"\n// isolated replacement build\n"
            module.write_bytes(replacement)
            for target in (url, f"{url}?v=2"):
                async with client.get(target) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), replacement)

            for suffix in ("", "/", "/manifest.json", "/sensor.py", "/dashboard.js/manifest.json"):
                async with client.get(f"{prefix}{suffix}") as response:
                    self.assertEqual(response.status, 404, suffix)
            async with client.post(url) as response:
                self.assertEqual(response.status, 405)
            self.assertFalse((self.root / ".storage" / "lovelace_resources").exists())
