"""Native Core processes across real inventory handoff and loss of the owned image."""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

import test_native_water_control as legacy
from water_settings_ws import compare, settings_client

DOMAIN = legacy.DOMAIN
MARK = 'topology_enrollment'
OPERATION = 'enroll_topology'


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def signals(hass, calls):
    hass.states.async_set('sensor.migration_depth', '1.25', {'unit_of_measurement': 'm'})
    hass.states.async_set('sensor.backup', '2.4', {'unit_of_measurement': 'm'})
    hass.states.async_set('switch.probe_output', 'off')

    @callback
    def record(call):
        calls.append([call.service, call.data.get('entity_id')])

    for action in ('turn_on', 'turn_off', 'toggle'):
        hass.services.async_register('switch', action, record)


async def disk_row(path, entry_id, predicate=lambda row: True):
    async with asyncio.timeout(14):
        while True:
            try:
                payload = path.read_bytes()
            except (FileNotFoundError, PermissionError):
                # A native atomic replacement can briefly deny a Windows read.
                # Keep the existing deadline and require a real valid row.
                pass
            else:
                for row in json.loads(payload)['data']['entries']:
                    if row['entry_id'] == entry_id and predicate(row):
                        return row
            await asyncio.sleep(.05)


async def options_attempt(hass, entry):
    bank = hass.config_entries
    with patch.object(bank, 'async_update_entry', wraps=bank.async_update_entry) as updates, \
         patch.object(bank, 'async_schedule_reload', wraps=bank.async_schedule_reload) as reloads:
        result = await bank.options.async_init(entry.entry_id)
        if result['type'] is FlowResultType.MENU:
            zone, = (entry.options if 'zones' in entry.options else entry.data)['zones']
            result = await bank.options.async_configure(result['flow_id'], {'next_step_id': 'add_tank'})
            if result['type'] is FlowResultType.FORM:
                result = await bank.options.async_configure(result['flow_id'], {'zone_id': zone['id']})
            if result['type'] is FlowResultType.FORM:
                result = await bank.options.async_configure(result['flow_id'], {
                    'tank_name': 'Blocked', 'level_sensor': 'sensor.backup'})
        if result['type'] in (FlowResultType.FORM, FlowResultType.MENU):
            bank.options.async_abort(result['flow_id'])
        return {'result': result['type'].value, 'updates': updates.call_count, 'reloads': reloads.call_count}


async def fresh(root, entry_id, kind):
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
    core = root / '.storage/core.config_entries'
    private = root / '.storage' / f'{DOMAIN}.settings.{entry_id}'
    before = {p.name: digest(p) for p in (core, private) if p.exists()}
    calls = []
    try:
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await hass.config_entries.async_initialize()
        entry = hass.config_entries.async_get_entry(entry_id)
        original_options = dict(entry.options)
        signals(hass, calls)
        if kind == 'prepare':
            unloaded = await options_attempt(hass, entry)
        await hass.async_start()
        await hass.config_entries.async_setup(entry_id)
        await hass.async_block_till_done()
        imported = Path(sys.modules[f'custom_components.{DOMAIN}'].__file__).resolve()
        assert imported == root / 'custom_components' / DOMAIN / '__init__.py'
        assert digest(imported) == digest(legacy.SOURCE / '__init__.py')
        if kind == 'prepare':
            marked = await disk_row(core, entry_id, lambda row: row['data'].get(MARK, {}).get('phase') == 'owned')
            assert marked['options'] == original_options
            await hass.config_entries.async_reload(entry_id)
            await hass.async_block_till_done()
            handoff = await options_attempt(hass, entry)
        if kind in ('lost', 'corrupt', 'mismatch'):
            assert entry.options == original_options
        owner = getattr(entry, 'runtime_data', None)
        request, _ = await settings_client(hass, entry)
        opened = await request('get')
        if opened['success']:
            policy = opened['result']['zones'][0]['tanks'][0]
        else:
            policy = {}
        saved = None
        if kind in ('prepare', 'lost', 'corrupt', 'mismatch'):
            saved = await request('save', if_revision='1' * 64, if_source_snapshot='2' * 64,
                                  changes={'tanks': []})
        rows = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        result = {'pid': os.getpid(), 'state': entry.state.name,
                  'roles': sorted(owner.roles) if owner else [],
                  'active_button_states': sorted(r.unique_id for r in rows if r.domain == 'button'
                      and (state := hass.states.get(r.entity_id)) is not None and state.state != 'unavailable'),
                  'output': owner.roles['refill'].binding.output if owner and 'refill' in owner.roles else None,
                  'zones': opened['result']['zones'] if opened['success'] else None,
                  'error': opened.get('error', {}).get('code'),
                  'save_error': saved.get('error', {}).get('code') if saved else None,
                  'policy': policy, 'projected': {r.unique_id: r.entity_id for r in rows if r.domain == 'sensor'
                      and (state := hass.states.get(r.entity_id)) is not None and state.state != 'unavailable'}}
        if kind == 'prepare':
            result.update(unloaded=unloaded, handoff=handoff)
        if kind == 'card_readback':
            # config_get is intentionally not a binding DTO. Independently
            # verify the enrolled topology and actual projection after boot.
            topology = await request('get', type=f'{DOMAIN}/topology_get')
            assert topology['success'], topology
            result['level_bindings'] = {
                t['id']: t['level_sensor'] for z in topology['result']['zones'] for t in z['tanks']}
            result['projected_values'] = {key: hass.states.get(entity).state
                                          for key, entity in result['projected'].items()}
    finally:
        await hass.async_stop()
    after = {p.name: digest(p) for p in (core, private) if p.exists()}
    if kind in ('lost', 'corrupt', 'mismatch'):
        assert before == after, (kind, before, after)
    assert calls == [], (kind, calls)
    print('FRESH_CORE', json.dumps(result, sort_keys=True), flush=True)


class TestNativeTopologyMigration(IsolatedAsyncioTestCase):
    set_depth = legacy.NativeWaterControlTest.set_depth
    setup_controller = legacy.NativeWaterControlTest.setup_controller
    register_depth = legacy.NativeWaterControlTest.register_depth
    edit_tank = legacy.NativeWaterControlTest.edit_tank
    option = legacy.NativeWaterControlTest.option
    submit = legacy.NativeWaterControlTest.submit
    saved = legacy.NativeWaterControlTest.saved

    async def asyncSetUp(self):
        await legacy.NativeWaterControlTest.asyncSetUp(self)

    async def _fresh(self, root, entry, kind):
        child = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            str(root), entry.entry_id, kind, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(child.communicate(), 40)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert child.returncode == 0, (kind, child.returncode, log)
        result, = [json.loads(line.removeprefix('FRESH_CORE ')) for line in log.splitlines()
                   if line.startswith('FRESH_CORE ')]
        assert result['pid'] != os.getpid()
        return result

    def _copy(self, root, name, *, image=None):
        dest = root.parent / name
        dest.mkdir()
        (dest / '.storage').mkdir()
        for name in ('core.config_entries', 'core.entity_registry'):
            shutil.copyfile(root / '.storage' / name, dest / '.storage' / name)
        if image:
            shutil.copyfile(root / '.storage' / image, dest / '.storage' / image)
        shutil.copytree(legacy.SOURCE, dest / 'custom_components' / DOMAIN)
        self.addCleanup(shutil.rmtree, dest)
        return dest

    async def test_missing_and_corrupt_private_image_never_reenable_legacy_roles(self):
        calls = []
        registry = er.async_get(self.hass)
        source = self.register_depth('test-depth', object_id='migration_depth')
        output = registry.async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        zone, = entry.data['zones']
        tank, = zone['tanks']
        form = await self.edit_tank(entry, tank['id'])
        rejected = await self.submit(form, tank_name='Storage', level_sensor=source.entity_id,
                                     refill_switch=output.entity_id, commission_refill=True)
        assert rejected['errors'] == {'confirm_device_cutoff': 'cutoff_required'}, rejected
        await self.saved(rejected, tank_name='Storage', level_sensor=source.entity_id,
                         refill_switch=output.entity_id, commission_refill=True, confirm_device_cutoff=True)
        assert sorted(entry.runtime_data.roles) == ['refill']
        root = Path(self.hass.config.config_dir)
        core = root / '.storage/core.config_entries'
        await disk_row(core, entry.entry_id, lambda row: row['options'].get('manual', {}).get('tank_id') == tank['id'])
        request, _ = await settings_client(self.hass, entry)
        opened = await request('get')
        assert opened['success'], opened
        saved = await request('save', **compare(opened['result']), changes={'tanks': [{'id': tank['id'], 'scale_max_m': 6}]})
        private = Path(entry.runtime_data.settings.store.path)
        assert saved['success'] and private.exists(), saved
        async with asyncio.timeout(14):
            while not core.with_name('core.entity_registry').exists() or b'_refill_start' not in core.with_name('core.entity_registry').read_bytes():
                await asyncio.sleep(.05)
        legacy_root = self._copy(root, f'{root.name}-legacy', image=private.name)
        menu = await self.hass.config_entries.options.async_init(entry.entry_id)
        enrolled = OPERATION in menu.get('menu_options', [])
        if enrolled:
            denied = await self.submit(await self.submit(menu, next_step_id=OPERATION), confirm_device_cutoff=False)
            assert denied['type'] is FlowResultType.FORM and denied['errors']
            submitted = await self.submit(denied, confirm_device_cutoff=True)
            assert submitted['type'] is FlowResultType.CREATE_ENTRY, submitted
            assert submitted['data'] is None
            await disk_row(core, entry.entry_id, lambda row: row['data'].get(MARK, {}).get('phase') == 'requested')
            await self.hass.config_entries.async_reload(entry.entry_id)
            await self.hass.async_block_till_done()
            assert entry.state is not ConfigEntryState.LOADED
            assert await options_attempt(self.hass, entry) == {'result': 'abort', 'updates': 0, 'reloads': 0}
        else:
            self.hass.config_entries.options.async_abort(menu['flow_id'])
        await self.hass.async_stop()
        assert calls == []
        if enrolled:
            pending = await self._fresh(root, entry, 'prepare')
            assert not pending['active_button_states'] and not pending['roles'] and pending['save_error']
            assert pending['unloaded'] == pending['handoff'] == {'result': 'abort', 'updates': 0, 'reloads': 0}
        missing = self._copy(root, f'{root.name}-lost')
        corrupt = self._copy(root, f'{root.name}-corrupt')
        mismatch = self._copy(root, f'{root.name}-mismatch', image=private.name)
        (corrupt / '.storage' / private.name).write_bytes(b'{injected corruption')
        image = mismatch / '.storage' / private.name
        foreign = json.loads(image.read_bytes())
        foreign['data']['entry_id'] = '0' * 26
        image.write_text(json.dumps(foreign), encoding='utf-8')
        after_loss = await self._fresh(missing, entry, 'lost')
        after_corruption = await self._fresh(corrupt, entry, 'corrupt')
        assert not {**{k: after_loss[k] for k in ('roles', 'active_button_states') if after_loss[k]},
                    **{f'corrupt_{k}': after_corruption[k] for k in ('roles', 'active_button_states') if after_corruption[k]}}, (
            'Fresh Core advertised legacy commissioned roles after loss/corruption of a physically verified private image', after_loss, after_corruption)
        wrong_entry = await self._fresh(mismatch, entry, 'mismatch')
        for refused in (after_loss, after_corruption, wrong_entry):
            assert not refused['active_button_states'] and not refused['roles'] and not refused['projected']
            assert refused['error'] and refused['save_error']
        assert enrolled, 'Only an actual native handoff can establish healthy migrated behavior'
        healthy = await self._fresh(root, entry, 'healthy')
        still_legacy = await self._fresh(legacy_root, entry, 'legacy')
        for active in (healthy, still_legacy):
            assert active['state'] == 'LOADED' and active['roles'] == ['refill']
            assert f"{tank['id']}_refill_start" in active['active_button_states']
            assert f"{tank['id']}_refill_stop" in active['active_button_states']
            assert active['output'] == output.id and active['zones'][0]['id'] == zone['id']
            assert active['policy']['id'] == tank['id'] and active['policy']['scale_max_m'] == 6
        assert healthy['projected'] == still_legacy['projected']
        assert json.loads(private.read_bytes())['data']['topology']['zones'][0]['tanks'][0]['level_sensor_registry_id'] == source.id

    async def test_native_options_does_not_write_when_unloaded(self):
        entry = await self.setup_controller()
        core = Path(self.hass.config.config_dir) / '.storage/core.config_entries'
        await disk_row(core, entry.entry_id)
        old = core.read_bytes()
        assert await self.hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED and not hasattr(entry, 'runtime_data')
        result = await options_attempt(self.hass, entry)
        assert result == {'result': 'abort', 'updates': 0, 'reloads': 0}, result
        assert core.read_bytes() == old


if __name__ == '__main__':
    asyncio.run(fresh(Path(sys.argv[1]), *sys.argv[2:]))
