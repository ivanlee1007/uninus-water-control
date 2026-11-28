"""Actual loopback Home Assistant frontend through native Lovelace Edit card."""
import asyncio
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

import test_native_topology_migration as migration


class TestNativeLovelaceTopology(IsolatedAsyncioTestCase):
    set_depth = migration.TestNativeTopologyMigration.set_depth
    setup_controller = migration.TestNativeTopologyMigration.setup_controller
    register_depth = migration.TestNativeTopologyMigration.register_depth
    option = migration.TestNativeTopologyMigration.option
    submit = migration.TestNativeTopologyMigration.submit
    edit_tank = migration.TestNativeTopologyMigration.edit_tank
    saved = migration.TestNativeTopologyMigration.saved
    _fresh = migration.TestNativeTopologyMigration._fresh
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp

    async def _run_case(self, script):
        original = self.register_depth('original', object_id='migration_depth')
        self.hass.states.async_remove('sensor.backup')
        self.register_depth('backup', object_id='backup')
        output = er.async_get(self.hass).async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        calls = []
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=original.entity_id, refill_switch=output.entity_id)
        tank_id = entry.data['zones'][0]['tanks'][0]['id']
        await self.saved(await self.edit_tank(entry, tank_id), tank_name='Storage', level_sensor=original.entity_id,
                         refill_switch=output.entity_id, commission_refill=True, confirm_device_cutoff=True)
        root = Path(self.hass.config.config_dir)
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        assert confirmed['type'] is FlowResultType.CREATE_ENTRY, confirmed
        await migration.disk_row(root / '.storage/core.config_entries', entry.entry_id,
                                 lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        assert not calls
        await self._fresh(root, entry, 'prepare')
        proc = await asyncio.create_subprocess_exec(sys.executable, '-B',
            str(Path(__file__).with_name(script)), str(root), entry.entry_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 120)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert proc.returncode == 0, (proc.returncode, log)
        return root, entry, tank_id, log

    async def test_durable_crud_and_protection_boundaries(self):
        _, _, _, log = await self._run_case('native_topology_boundaries.py')
        assert 'NATIVE_TOPOLOGY_BOUNDARIES_OK' in log

    @pytest.mark.skipif(find_spec('playwright') is None or find_spec('hass_frontend') is None,
                        reason='requires the pinned browser test dependencies')
    async def test_add_from_native_lovelace_dialog_then_fresh_core(self):
        root, entry, tank_id, log = await self._run_case('native_topology_editor_gui.py')
        receipt, = [json.loads(line.removeprefix('NATIVE_TOPOLOGY_GUI ')) for line in log.splitlines()
                    if line.startswith('NATIVE_TOPOLOGY_GUI ')]
        fresh = await self._fresh(root, entry, 'card_readback')
        assert fresh['projected'][receipt['id']] == receipt['entity_id']
        assert [tank['id'] for tank in fresh['zones'][0]['tanks']] == [tank_id, receipt['id']]
        assert fresh['level_bindings'][receipt['id']] == {'entity_id': 'sensor.backup', 'registry_id': receipt['replacement']}
        assert fresh['projected_values'][receipt['id']] == '2.4'
        assert fresh['zones'][0]['tanks'][-1]['scale_max_m'] == 4.75
        assert fresh['roles'] == ['refill'] and receipt['writes'] == 1 and receipt['calls'] == []
