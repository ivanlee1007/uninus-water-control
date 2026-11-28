"""Native protected commissioning and Card settings use one durable authority."""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

import test_native_topology_migration as migration
from water_settings_ws import compare, settings_client


async def protected(root, entry_id, output_id):
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
        assert entry.data[migration.MARK]['phase'] == 'owned'
        migration.signals(hass, calls)
        await hass.async_start()
        assert await bank.async_setup(entry_id)
        imported = Path(sys.modules[f'custom_components.{migration.DOMAIN}.config_flow'].__file__).resolve()
        assert imported == root / 'custom_components' / migration.DOMAIN / 'config_flow.py'
        assert migration.digest(imported) == migration.digest(migration.legacy.SOURCE / 'config_flow.py')
        owner = entry.runtime_data
        assert not owner.roles
        tank = entry.data['zones'][0]['tanks'][0]
        tank_id = tank['id']
        request, _ = await settings_client(hass, entry)
        opened = await request('get')
        assert opened['success'], opened

        async def native_form():
            menu = await bank.options.async_init(entry_id)
            if menu['type'] is not FlowResultType.MENU:
                return menu
            target = await bank.options.async_configure(menu['flow_id'], {'next_step_id': 'edit_tank'})
            return await bank.options.async_configure(target['flow_id'], {'tank_id': tank_id})

        def commissioning(confirm):
            return {'tank_name': 'Commissioned', 'level_sensor': 'sensor.migration_depth',
                    'refill_switch': 'switch.probe_output', 'commission_refill': True,
                    'confirm_device_cutoff': confirm, 'refill_max_runtime_seconds': 45}

        with patch.object(bank, 'async_update_entry', wraps=bank.async_update_entry) as updates, \
             patch.object(bank, 'async_reload', wraps=bank.async_reload) as reloads:
            native = await native_form()
            card = await request('save', **compare(opened['result']), changes={'tanks': [{'id': tank_id, 'scale_max_m': 9}]})
            assert card['success'], card
            prior = image.read_bytes()
            if native['type'] is FlowResultType.FORM:
                stale = await bank.options.async_configure(native['flow_id'], commissioning(True))
                assert stale['type'] is FlowResultType.ABORT and stale['reason'] == 'configuration_changed'
                assert image.read_bytes() == prior
            native = await native_form()
            if native['type'] is FlowResultType.FORM:
                denied = await bank.options.async_configure(native['flow_id'], commissioning(False))
                assert denied['errors'] == {'confirm_device_cutoff': 'cutoff_required'}, denied
                assert image.read_bytes() == prior
                result = await bank.options.async_configure(denied['flow_id'], commissioning(True))
            else:
                result = native
            recorded = json.loads(image.read_bytes())['data']['topology']['manual'].get('refill')
            assert recorded and recorded['output'] == output_id and recorded['max_runtime_seconds'] == 45, (
                'Native protected edit must persist approved commissioning in the same image used by Card Save', result, recorded)
            assert result['type'] is FlowResultType.CREATE_ENTRY and result['data'] is None, result
            assert updates.call_count == 0 and reloads.call_count == 1
            assert core.read_bytes() == core_before
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED and entry.runtime_data is not owner
            role = entry.runtime_data.roles['refill']
            assert role.binding.output == output_id and role.binding.seconds == 45
            assert role.can_start()
            stale_card = await request('save', **compare(card['result']), changes={'tanks': [{'id': tank_id, 'scale_max_m': 8}]})
            assert stale_card['error']['code'] == 'stale_revision', stale_card
            fresh = await request('get')
            actual = fresh['result']['zones'][0]['tanks'][0]
            assert actual['id'] == tank_id and actual['name'] == 'Commissioned' and actual['scale_max_m'] == 9
    finally:
        await hass.async_stop()
    assert calls == [] and core.read_bytes() == core_before
    print('SHARED_WRITER', json.dumps({'pid': os.getpid(), 'state': entry.state.name,
          'output_matches': role.binding.output == output_id, 'core_bytes_unchanged': True,
          'calls': calls, 'private_generation': json.loads(image.read_bytes())['data']['generation']}), flush=True)


class TestNativeTopologySharedWriter(IsolatedAsyncioTestCase):
    set_depth = migration.TestNativeTopologyMigration.set_depth
    setup_controller = migration.TestNativeTopologyMigration.setup_controller
    register_depth = migration.TestNativeTopologyMigration.register_depth
    option = migration.TestNativeTopologyMigration.option
    submit = migration.TestNativeTopologyMigration.submit
    _fresh = migration.TestNativeTopologyMigration._fresh
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp

    async def test_protected_native_commission_and_card_save_share_cas_and_image(self):
        calls = []
        source = self.register_depth('test-depth', object_id='migration_depth')
        output = er.async_get(self.hass).async_get_or_create('switch', 'test_probe', 'output',
                                                            suggested_object_id='probe_output')
        assert output.entity_id == 'switch.probe_output'
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        assert not entry.runtime_data.roles
        root = Path(self.hass.config.config_dir)
        core = root / '.storage/core.config_entries'
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        assert confirmed['type'] is FlowResultType.CREATE_ENTRY, confirmed
        await migration.disk_row(core, entry.entry_id, lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        assert not calls
        await self._fresh(root, entry, 'prepare')
        child = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            str(root), entry.entry_id, output.id, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(child.communicate(), 40)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert child.returncode == 0, (child.returncode, log)
        result, = [json.loads(line.removeprefix('SHARED_WRITER ')) for line in log.splitlines() if line.startswith('SHARED_WRITER ')]
        assert result['pid'] != os.getpid() and result['core_bytes_unchanged'] and result['calls'] == []


if __name__ == '__main__':
    asyncio.run(protected(Path(sys.argv[1]), *sys.argv[2:]))
