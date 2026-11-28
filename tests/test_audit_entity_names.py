"""Generated names and user Registry overrides on real isolated Core entities."""

from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

import test_dashboard_projection as projection
import test_native_manual_lifecycle as lifecycle
from unittest import IsolatedAsyncioTestCase


class TestAuditEntityNames(projection.DashboardProjectionTest):
    # Reuse only the source-relative isolated-Core harness, not its test methods.
    test_native_inventory_projects_only_layout_metadata_even_when_level_unavailable = None
    test_native_http_serves_only_the_dashboard_module_without_caching = None

    async def setup_level(self):
        hass = await self.start_core()
        registry = er.async_get(hass)
        source = registry.async_get_or_create('sensor', 'audit_probe', 'depth')
        hass.states.async_set(source.entity_id, '125', {'unit_of_measurement': 'cm'})
        flow = hass.config_entries.flow
        form = await flow.async_init(projection.DOMAIN, context={'source': 'user'})
        entry = (await self.save(hass, flow, form, zone_name='Old zone', tank_name='Old tank',
                                 level_sensor=source.entity_id))['result']
        module = import_module('custom_components.uninus_water_control.sensor')
        self.assertEqual(Path(module.__file__).resolve(), self.integration / 'sensor.py')
        zone = entry.data['zones'][0]
        tank = zone['tanks'][0]
        entity_id = registry.async_get_entity_id('sensor', projection.DOMAIN, tank['id'])
        entity = entry.runtime_data.settings.platforms['sensor'].entities[entity_id]
        return hass, registry, entry, entity, zone, tank

    async def test_native_level_rename_updates_generated_and_registry_names_not_custom_name(self):
        hass, registry, entry, entity, zone, tank = await self.setup_level()
        entity_id = entity.entity_id
        identity = registry.async_get(entity_id).id
        for index, custom_name in enumerate((None, 'User-owned title')):
            if custom_name:
                registry.async_update_entity(entity_id, name=custom_name)
                await hass.async_block_till_done()
            next_zone = dict(zone, name=f'New zone {index}')
            next_tank = dict(tank, name=f'New tank {index}')
            entity.async_apply_topology(next_zone, next_tank, 0, 0)
            await hass.async_block_till_done()
            state = hass.states.get(entity_id)
            row = registry.async_get(entity_id)
            generated = f'New zone {index} · New tank {index} water level'
            self.assertEqual(state.attributes['uninus_water_dashboard']['tank']['name'], next_tank['name'])
            self.assertEqual(state.attributes['friendly_name'], custom_name or generated)
            self.assertEqual(row.original_name, generated)
            self.assertEqual(row.name, custom_name)
            self.assertEqual(row.id, identity)
            self.assertEqual(entity.translation_placeholders, {'zone': next_zone['name'], 'tank': next_tank['name']})
            print('NATIVE_NAME', {'entity': entity_id, 'friendly_name': state.attributes['friendly_name'],
                                  'original_name': row.original_name, 'name': row.name})

    async def test_native_preview_names_follow_topology_without_resetting_observation(self):
        from water_settings_ws import compare, settings_client

        hass, registry, entry, _, zone, tank = await self.setup_level()
        request, _ = await settings_client(hass, entry)
        base = await request('get')
        self.assertTrue(base['success'], base)
        saved = await request('save', **compare(base['result']), changes={'tanks': [{
            'id': tank['id'], 'refill': {'local': [1.0, 2.0]}, 'alarm': {'local': [1.0, 30]},
        }]})
        self.assertTrue(saved['success'], saved)
        await hass.async_block_till_done()
        settings = entry.runtime_data.settings
        sensor = import_module('custom_components.uninus_water_control.sensor')
        rows = {row.unique_id: row for row in er.async_entries_for_config_entry(registry, entry.entry_id)}
        ids = [f"{tank['id']}_refill_preview", f"{tank['id']}_low_level_alarm"]
        before = {ident: rows[ident].id for ident in ids}
        for index, custom in enumerate((None, 'User preview title')):
            if custom:
                for ident in ids:
                    registry.async_update_entity(rows[ident].entity_id, name=custom)
                await hass.async_block_till_done()
            renamed = dict(zone, name=f'Renamed zone {index}', tanks=[dict(tank, name=f'Renamed tank {index}')])
            # Inject only the topology input at the inventory seam. EntityPlatform,
            # translations, registry/state writes and policy reconciliation are real Core.
            with patch.object(sensor, 'inventory', return_value=[renamed]):
                await sensor.async_reconcile_settings(hass, entry, settings)
            await hass.async_block_till_done()
            for ident, suffix in zip(ids, ['refill threshold preview', 'low-level alarm preview']):
                row = registry.async_get(rows[ident].entity_id)
                state = hass.states.get(row.entity_id)
                generated = f'Renamed zone {index} · Renamed tank {index} {suffix}'
                self.assertEqual(state.attributes['friendly_name'], custom or generated)
                self.assertEqual(row.original_name, generated)
                self.assertEqual(row.name, custom)
                self.assertEqual(row.id, before[ident])
                self.assertEqual(state.state, 'between_thresholds' if suffix == 'refill threshold preview' else 'off')
                print('PREVIEW_NAME', {'entity': row.entity_id, 'friendly_name': state.attributes['friendly_name'],
                                       'original_name': row.original_name, 'name': row.name})


class TestAuditManualNames(IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.NativeManualLifecycleTest.asyncSetUp
    boot_core = lifecycle.NativeManualLifecycleTest.boot_core
    seed_feedback = lifecycle.NativeManualLifecycleTest.seed_feedback
    handle_switch = lifecycle.NativeManualLifecycleTest.handle_switch
    status = lifecycle.NativeManualLifecycleTest.status

    async def test_commissioned_names_update_in_place_without_device_calls(self):
        sensor = import_module('custom_components.uninus_water_control.sensor')
        self.assertEqual(Path(sensor.__file__).resolve(), lifecycle.SOURCE / 'sensor.py')
        owner = self.entry.runtime_data
        initial_roles = dict(owner.roles)
        rows = [row for row in er.async_entries_for_config_entry(self.registry, self.entry.entry_id)
                if row.unique_id.endswith(('_refill_status', '_refill_start', '_refill_stop', '_refill_reset'))]
        self.assertEqual(len(rows), 4)
        suffix = {'status': 'refill status', 'start': 'start refill', 'stop': 'stop refill', 'reset': 'reset refill fault'}
        for index, custom in enumerate((None, 'User manual title')):
            if custom:
                for row in rows:
                    self.registry.async_update_entity(row.entity_id, name=custom)
                await self.hass.async_block_till_done()
            owner.labels.update(zone=f'Changed zone {index}', tank=f'Changed tank {index}')
            await sensor.async_reconcile_settings(self.hass, self.entry, owner.settings)
            await self.hass.async_block_till_done()
            for initial in rows:
                row = self.registry.async_get(initial.entity_id)
                state = self.hass.states.get(row.entity_id)
                action = row.unique_id.rsplit('_', 1)[1]
                generated = f'Changed zone {index} · Changed tank {index} {suffix[action]}'
                self.assertEqual(state.attributes['friendly_name'], custom or generated)
                self.assertEqual(row.original_name, generated)
                self.assertEqual(row.name, custom)
                self.assertEqual(row.id, initial.id)
                print('MANUAL_NAME', {'entity': row.entity_id, 'friendly_name': state.attributes['friendly_name'],
                                      'original_name': row.original_name, 'name': row.name})
            self.assertEqual(owner.roles, initial_roles)
            self.assertEqual(self.effects, [])
