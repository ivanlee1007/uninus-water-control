"""Real ConfigEntry handoff, registered Card transport and physical topology Store."""
import asyncio
import json
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

import test_native_topology_migration as migration
from water_settings_ws import compare, settings_client


def binding(row):
    return {'entity_id': row.entity_id, 'registry_id': row.id}


async def owned_case(root, entry_id):
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
    core = root / '.storage/core.config_entries'
    image = root / '.storage' / f'{migration.DOMAIN}.settings.{entry_id}'
    core_before = core.read_bytes()
    calls = []
    try:
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = bank = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await bank.async_initialize()
        entry = bank.async_get_entry(entry_id)
        migration.signals(hass, calls)
        await hass.async_start()
        assert await bank.async_setup(entry_id)
        owner = entry.runtime_data
        registry = er.async_get(hass)
        old_image = json.loads(image.read_bytes())['data']
        original, = old_image['topology']['zones']
        first, = original['tanks']
        request, _ = await settings_client(hass, entry)
        baseline = await request('get')
        assert baseline['success'], baseline
        changes = {'tanks': [{'id': first['id'], 'scale_max_m': 3}]}
        zones = [{'id': original['id'], 'name': original['name'], 'tanks': [
            {'id': first['id'], 'name': first['name'], 'level_sensor': binding(registry.async_get('sensor.migration_depth')),
             'refill_switch': None, 'alarm_switch': None},
            {'id': None, 'name': '新水桶', 'level_sensor': binding(registry.async_get('sensor.backup')),
             'refill_switch': None, 'alarm_switch': None},
        ]}]
        payload = dict(type=f'{migration.DOMAIN}/topology_save', **compare(baseline['result']), zones=zones,
                       remove={'zones': [], 'tanks': []}, changes=changes)
        saved = await request('save', **payload)
        assert saved['success'], saved  # Prove the authenticated, registered handler was admitted in RED.
        physical = json.loads(image.read_bytes())['data']
        existing, *added = physical['topology']['zones'][0]['tanks']
        assert [tank['name'] for tank in added] == ['新水桶'], 'admitted Apply did not durably add the selected tank'
        new, = added
        assert existing == first and new['level_sensor_registry_id'] == registry.async_get('sensor.backup').id
        assert new['id'] != first['id'] and len(new['id']) == 32
        assert entry.runtime_data is owner and core.read_bytes() == core_before
        entity_id = registry.async_get_entity_id('sensor', migration.DOMAIN, new['id'])
        state = hass.states.get(entity_id)
        assert state is not None and float(state.state) == 2.4
        assert state.attributes['uninus_water_dashboard']['tank']['name'] == '新水桶'
        fresh = await request('get', type=f'{migration.DOMAIN}/topology_get')
        assert fresh['success'] and fresh['result'] == saved['result']
        settings = await request('get')
        assert settings['result']['revision'] == fresh['result']['revision']
        assert settings['result']['zones'][0]['tanks'][0]['scale_max_m'] == 3
        assert (await request('save', **payload))['error']['code'] == 'stale_revision'
    finally:
        await hass.async_stop()
    assert calls == [] and core.read_bytes() == core_before
    print('CARD_TOPOLOGY', json.dumps({'new_id': new['id'], 'entity_id': entity_id}), flush=True)


class TestNativeCardTopology(IsolatedAsyncioTestCase):
    set_depth = migration.TestNativeTopologyMigration.set_depth
    setup_controller = migration.TestNativeTopologyMigration.setup_controller
    register_depth = migration.TestNativeTopologyMigration.register_depth
    option = migration.TestNativeTopologyMigration.option
    submit = migration.TestNativeTopologyMigration.submit
    _fresh = migration.TestNativeTopologyMigration._fresh
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp

    async def test_legacy_entry_cannot_write_topology(self):
        original = self.register_depth('original', object_id='migration_depth')
        migration.signals(self.hass, [])
        entry = await self.setup_controller(level_sensor=original.entity_id)
        request, _ = await settings_client(self.hass, entry)
        native = entry.runtime_data
        path = Path(native.settings.store.path)
        assert not path.exists()
        before = await request('get')
        assert before['success'], before
        status = await request('get', type=f'{migration.DOMAIN}/topology_get')
        assert status['error']['code'] == 'topology_not_enrolled'
        save = await request('save', type=f'{migration.DOMAIN}/topology_save', **compare(before['result']),
                             zones=[], remove={'zones': [], 'tanks': []}, changes={})
        assert save['error']['code'] == 'topology_not_enrolled'
        observed = await request('get')
        assert observed['success'] and observed['result'] == before['result']
        assert not path.exists() and entry.runtime_data is native

    async def test_apply_persists_and_exposes_new_tank_without_reload(self):
        original = self.register_depth('original', object_id='migration_depth')
        self.hass.states.async_remove('sensor.backup')
        self.register_depth('backup', object_id='backup')
        migration.signals(self.hass, [])
        entry = await self.setup_controller(level_sensor=original.entity_id)
        root = Path(self.hass.config.config_dir)
        core = root / '.storage/core.config_entries'
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        assert confirmed['type'] is FlowResultType.CREATE_ENTRY, confirmed
        await migration.disk_row(core, entry.entry_id, lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        await self._fresh(root, entry, 'prepare')
        proc = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            str(root), entry.entry_id, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 45)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert proc.returncode == 0, (proc.returncode, log)
        receipt, = [json.loads(line.removeprefix('CARD_TOPOLOGY ')) for line in log.splitlines() if line.startswith('CARD_TOPOLOGY ')]
        fresh = await self._fresh(root, entry, 'card_readback')
        assert fresh['projected'][receipt['new_id']] == receipt['entity_id']
        assert [tank['id'] for tank in fresh['zones'][0]['tanks']][1:] == [receipt['new_id']]


if __name__ == '__main__':
    asyncio.run(owned_case(Path(sys.argv[1]), sys.argv[2]))
