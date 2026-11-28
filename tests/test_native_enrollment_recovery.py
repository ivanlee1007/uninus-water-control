"""Native Options and cold backup/recovery acceptance for topology enrollment."""
import asyncio
from contextlib import ExitStack
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import sys
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.util.file import WriteError

import test_native_topology_migration as migration
from water_settings_ws import compare, settings_client

DOMAIN = migration.DOMAIN
MARK = migration.MARK
STATUS = 'topology_status'


async def status(hass, entry):
    manager = hass.config_entries.options
    initial = await manager.async_init(entry.entry_id)
    if initial['type'] is FlowResultType.ABORT:
        return initial['reason']
    try:
        assert initial['type'] is FlowResultType.MENU and STATUS in initial['menu_options'], initial
        shown = await manager.async_configure(initial['flow_id'], {'next_step_id': STATUS})
        if shown['type'] is FlowResultType.ABORT:
            return shown['reason']
        assert shown['type'] is FlowResultType.FORM and not shown['data_schema'].schema, shown
        assert not shown.get('description_placeholders'), shown  # No token, IDs, or data.
        return shown['step_id']
    finally:
        if any(flow['flow_id'] == initial['flow_id'] for flow in manager.async_progress()):
            manager.async_abort(initial['flow_id'])


def restore(source, target):
    """Owned offline restore of the whole synthetic Core config directory."""
    shutil.copytree(source, target)


async def cold(root, entry_id, mode):
    assert version('homeassistant') == '2025.12.0'
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
    image = root / '.storage' / f'{DOMAIN}.settings.{entry_id}'
    core = image.with_name('core.config_entries')
    before = {p.name: migration.digest(p) for p in (image, core) if p.exists()}
    calls = []
    events = []
    try:
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await hass.config_entries.async_initialize()
        entry = hass.config_entries.async_get_entry(entry_id)
        migration.signals(hass, calls)
        await hass.async_start()
        with ExitStack() as fault:
            if mode in ('private_before', 'private_after', 'readback_unavailable', 'mark_lost', 'resume'):
                await instrument_preparation(hass, fault, mode, image, events)
            await hass.config_entries.async_setup(entry_id)
            await hass.async_block_till_done()
            if mode in ('private_before', 'readback_unavailable'):
                attempts = events.copy()
                reason = entry.error_reason_translation_key
                assert not await hass.config_entries.async_reload(entry_id)
                assert entry.error_reason_translation_key == reason and events == attempts, (
                    'A same-process reload started another preparation attempt or changed its verdict', attempts, events)
            if mode in ('prepare', 'private_after', 'resume'):
                await migration.disk_row(core, entry_id, lambda r: r['data'][MARK]['phase'] == 'owned')
            if mode == 'mark_lost':
                # Keep the genuine scoped failure through final shutdown saves.
                await hass.async_stop()
        module = Path(sys.modules[f'custom_components.{DOMAIN}'].__file__).resolve()
        assert module.is_relative_to(root)
        assert module.read_bytes() == (migration.legacy.SOURCE / '__init__.py').read_bytes()
        result = {'status': await status(hass, entry), 'state': entry.state.name,
                  'roles': sorted(entry.runtime_data.roles) if entry.state is ConfigEntryState.LOADED else [],
                  'rows': sorted((r.unique_id, r.id, r.entity_id) for r in er.async_entries_for_config_entry(er.async_get(hass), entry_id)),
                  'active_buttons': sorted(s.entity_id for s in hass.states.async_all('button') if s.state != 'unavailable')}
        if mode in ('readback_unavailable', 'prepare'):
            expected = ('topology_recovery_required' if mode == 'readback_unavailable'
                        else 'topology_restart_required')
            assert entry.state is ConfigEntryState.SETUP_ERROR and not hasattr(entry, 'runtime_data')
            assert entry.error_reason_translation_key == result['status'] == expected
            for name in ('config_api', 'config_flow', 'settings'):
                copied = Path(sys.modules[f'custom_components.{DOMAIN}.{name}'].__file__).resolve()
                assert copied == root / 'custom_components' / DOMAIN / f'{name}.py'
                assert copied.read_bytes() == (migration.legacy.SOURCE / f'{name}.py').read_bytes()
            request, _ = await settings_client(hass, entry)
            stable = {p: p.read_bytes() for p in (image, core)}
            attempts = events.copy()
            get = await request('get', type=f'{DOMAIN}/topology_get')
            save = await request('save', type=f'{DOMAIN}/topology_save', if_revision='1' * 64,
                                 if_source_snapshot='2' * 64, zones=[], remove={'zones': [], 'tanks': []}, changes={})
            assert all(r['type'] == 'result' and not r['success'] for r in (get, save)), (get, save)
            assert {p: p.read_bytes() for p in (image, core)} == stable and events == attempts and not calls
            codes = [r['error']['code'] for r in (get, save)]
            assert codes == [expected, expected], (result['status'], codes)
            result['topology_errors'] = codes
        if entry.state is ConfigEntryState.LOADED:
            request, _ = await settings_client(hass, entry)
            opened = await request('get')
            assert opened['success'], opened
            result['settings'] = opened['result']
        if mode == 'warm_status_loss':
            displaced = image.with_suffix('.fixture-held')
            image.rename(displaced)
            try:
                assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.settings.topology
                assert await status(hass, entry) == 'topology_recovery_required'
                assert not image.exists() and not calls
            finally:
                displaced.rename(image)
    finally:
        await hass.async_stop()
    after = {p.name: migration.digest(p) for p in (image, core) if p.exists()}
    assert not calls, calls
    if mode not in ('prepare', 'private_after', 'readback_unavailable', 'mark_lost', 'resume'):
        assert before == after, (mode, before, after)
    print('ENROLLMENT ' + json.dumps({'mode': mode, **result, 'before': before, 'after': after, 'calls': calls,
                                     'events': events, 'pid': os.getpid()}), flush=True)


async def instrument_preparation(hass, fault, mode, image, events):
    """Fault only the owned Store instances and observe the real mark boundary."""
    integration = await loader.async_get_integration(hass, DOMAIN)
    await integration.async_get_component()
    component = sys.modules[f'custom_components.{DOMAIN}.settings']
    initialize = component.Settings.__init__
    mark = component.mark_owned

    def observed_mark(hass, entry, requested):
        durable = json.loads(image.read_bytes())['data']
        assert durable['enrollment'] == requested['token'] and 'topology' in durable
        assert events[-1] == 'verified_readback', events
        assert entry.data[MARK]['phase'] == 'requested'
        events.append('mark_after_verification')
        return mark(hass, entry, requested)

    def instrument(self, hass, entry):
        initialize(self, hass, entry)
        write = self.store._write_prepared_data
        read = self._async_read_file

        def physical(mode_hint, data):
            events.append('private_replacement_attempt')
            if mode == 'private_before':
                raise WriteError('injected before private replacement')
            if mode == 'resume':
                raise AssertionError('verified prepared image must not be replayed')
            write(mode_hint, data)
            events.append('private_replaced')
            if mode == 'private_after':
                raise WriteError('injected after private replacement; acknowledgement lost')

        async def readback():
            if mode == 'readback_unavailable' and 'private_replaced' in events:
                events.append('readback_unavailable')
                raise component.SettingsError('settings_unavailable')
            result = await read()
            if result and 'topology' in result:
                events.append('verified_readback')
            return result

        fault.enter_context(patch.object(self.store, '_write_prepared_data', physical))
        fault.enter_context(patch.object(self, '_async_read_file', readback))

    fault.enter_context(patch.object(component.Settings, '__init__', instrument))
    fault.enter_context(patch.object(component, 'mark_owned', observed_mark))
    if mode == 'mark_lost':
        def fail_core_write(mode_hint, data):
            events.append('core_replacement_refused')
            assert 'mark_after_verification' in events and image.exists()
            raise WriteError('injected before Core phase replacement')
        fault.enter_context(patch.object(hass.config_entries._store, '_write_prepared_data', fail_core_write))


class TestNativeEnrollmentRecovery(IsolatedAsyncioTestCase):
    set_depth = migration.TestNativeTopologyMigration.set_depth
    setup_controller = migration.TestNativeTopologyMigration.setup_controller
    register_depth = migration.TestNativeTopologyMigration.register_depth
    option = migration.TestNativeTopologyMigration.option
    submit = migration.TestNativeTopologyMigration.submit
    edit_tank = migration.TestNativeTopologyMigration.edit_tank
    saved = migration.TestNativeTopologyMigration.saved
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp

    async def worker(self, script, root, entry_id, mode, prefix):
        proc = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).with_name(script).resolve()),
            str(root), entry_id, mode, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 65)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert proc.returncode == 0, (mode, proc.returncode)
        record, = [json.loads(line.removeprefix(prefix)) for line in log.splitlines() if line.startswith(prefix)]
        return record

    async def cold(self, root, entry, mode):
        return await self.worker(Path(__file__).name, root, entry.entry_id, mode, 'ENROLLMENT ')

    async def gui(self, root, entry_id, mode):
        return await self.worker('native_enrollment_options_gui.py', root, entry_id, mode, 'OPTIONS_GUI ')

    async def test_cold_preparation_verdict_agrees_in_native_configure_and_authenticated_topology(self):
        calls = []
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor='sensor.migration_depth')
        root = Path(self.hass.config.config_dir)
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        assert confirmed['type'] is FlowResultType.CREATE_ENTRY
        await self.hass.async_stop()
        assert not calls
        target = root.parent / (root.name + '-verdicts')
        self.addCleanup(shutil.rmtree, target, True)
        restore(root, target)
        pending = await self.cold(root, entry, 'prepare')
        assert pending['topology_errors'] == ['topology_restart_required'] * 2
        failed = await self.cold(target, entry, 'readback_unavailable')
        assert failed['topology_errors'] == ['topology_recovery_required'] * 2
        assert failed['events'] == ['private_replacement_attempt', 'private_replaced', 'readback_unavailable']
        assert failed['before']['core.config_entries'] == failed['after']['core.config_entries']

    async def test_enrollment_preparation_retries_only_unmarked_images_and_never_replays_verified_writes(self):
        calls = []
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor='sensor.migration_depth')
        root = Path(self.hass.config.config_dir)
        assert await status(self.hass, entry) == 'topology_not_requested'
        await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        await self.hass.async_stop()
        assert not calls
        image_name = f'{DOMAIN}.settings.{entry.entry_id}'
        assert not (root / '.storage' / image_name).exists()
        cases = root.parent / (root.name + '-faults')
        cases.mkdir()
        self.addCleanup(shutil.rmtree, cases, True)
        for mode in ('private_before', 'private_after', 'readback_unavailable', 'mark_lost'):
            target = cases / mode
            restore(root, target)
            record = await self.cold(target, entry, mode)
            sample = target / '.storage' / image_name
            row = await migration.disk_row(sample.with_name('core.config_entries'), entry.entry_id)
            assert not record['roles'] and not record['active_buttons']
            if mode == 'private_before':
                assert record['events'] == ['private_replacement_attempt']
                assert not sample.exists() and row['data'][MARK]['phase'] == 'requested'
                assert record['status'] == 'topology_restart_required'
                await self.cold(target, entry, 'prepare')
            elif mode == 'private_after':
                assert record['events'] == ['private_replacement_attempt', 'private_replaced',
                                            'verified_readback', 'mark_after_verification']
                assert row['data'][MARK]['phase'] == 'owned'
                assert record['status'] == 'topology_restart_required'
            else:
                assert row['data'][MARK]['phase'] == 'requested' and sample.exists()
                if mode == 'readback_unavailable':
                    assert record['events'][-1] == 'readback_unavailable'
                    assert 'mark_after_verification' not in record['events']
                else:
                    assert record['events'][-1] == 'core_replacement_refused'
                    assert record['status'] == 'topology_restart_required'
                original = sample.read_bytes()
                resume = await self.cold(target, entry, 'resume')
                assert resume['events'] == ['verified_readback', 'mark_after_verification']
                assert resume['status'] == 'topology_restart_required' and sample.read_bytes() == original
            owned = await self.cold(target, entry, 'owned')
            assert owned['status'] == 'topology_owned_active' and owned['pid'] != record['pid']

    @pytest.mark.skipif(any(find_spec(name) is None for name in ('playwright', 'hass_frontend', 'securetar', 'cronsim')),
                        reason='requires the pinned browser and native Backup dependencies')
    async def test_native_options_enrollment_and_native_archive_restore(self):
        calls = []
        source = self.register_depth('original', object_id='migration_depth')
        output = er.async_get(self.hass).async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        tank = entry.data['zones'][0]['tanks'][0]
        await self.saved(await self.edit_tank(entry, tank['id']), tank_name='Storage', level_sensor=source.entity_id,
                         refill_switch=output.entity_id, commission_refill=True, confirm_device_cutoff=True)
        request, _ = await settings_client(self.hass, entry)
        get = await request('get')
        assert (await request('save', **compare(get['result']), changes={'tanks': [{'id': tank['id'], 'scale_max_m': 6}]}))['success']
        await self.hass.async_stop()
        assert not calls
        root = Path(self.hass.config.config_dir)
        marker = root / 'configuration.yaml'
        marker.write_text('# synthetic before native backup\n')
        initial = await self.gui(root, entry.entry_id, 'initial')
        assert initial['warm_pending_ui'] and initial['ui_status'] == 'Topology transfer not requested'
        preparation = await self.gui(root, entry.entry_id, 'prepared')
        assert preparation['ui_status'] == 'pending'
        active = await self.gui(root, entry.entry_id, 'active')
        assert active['ui_status'] == 'Topology transfer complete' and active['roles'] == ['refill']
        assert active['rows'] == initial['rows'] == preparation['rows']
        image = root / '.storage' / f'{DOMAIN}.settings.{entry.entry_id}'
        full_image = image.read_bytes()
        core = image.with_name('core.config_entries')
        core_before = await migration.disk_row(core, entry.entry_id)
        image.unlink()
        marker.write_text('# synthetic failure-side settings to be replaced\n')
        recovery = await self.gui(root, entry.entry_id, 'recovery')
        assert recovery['ui_status'] == 'recovery' and recovery['native_restore_queued']
        assert not image.exists()
        restored = await self.gui(root, entry.entry_id, 'restored')
        assert restored['image'] == active['image'] and image.read_bytes() == full_image
        assert restored['roles'] == active['roles'] and restored['rows'] == active['rows']
        assert await migration.disk_row(core, entry.entry_id) == core_before
        assert marker.read_text() == '# synthetic before native backup\n'
        assert len({row['pid'] for row in (initial, preparation, active, recovery, restored)}) == 5

    async def test_status_follows_verified_cold_enrollment_and_matching_restore(self):
        calls = []
        source = self.register_depth('original', object_id='migration_depth')
        output = er.async_get(self.hass).async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        tank = entry.data['zones'][0]['tanks'][0]
        await self.saved(await self.edit_tank(entry, tank['id']), tank_name='Storage', level_sensor=source.entity_id,
                         refill_switch=output.entity_id, commission_refill=True, confirm_device_cutoff=True)
        root = Path(self.hass.config.config_dir)
        image = root / '.storage' / f'{DOMAIN}.settings.{entry.entry_id}'
        core = image.with_name('core.config_entries')
        request, _ = await settings_client(self.hass, entry)
        get = await request('get')
        stored = await request('save', **compare(get['result']), changes={'tanks': [{'id': tank['id'], 'scale_max_m': 6}]})
        assert stored['success'], stored
        assert await status(self.hass, entry) == 'topology_not_requested'
        assert (await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True))['type'] is FlowResultType.CREATE_ENTRY
        assert await status(self.hass, entry) == 'topology_restart_required'
        await self.hass.async_stop()
        assert not calls
        assert (await self.cold(root, entry, 'prepare'))['status'] == 'topology_restart_required'
        owned = await self.cold(root, entry, 'owned')
        assert owned['status'] == 'topology_owned_active' and owned['roles'] == ['refill']
        assert len(owned['active_buttons']) == 3
        assert owned['settings']['zones'][0]['tanks'][0]['scale_max_m'] == 6
        assert (await self.cold(root, entry, 'warm_status_loss'))['status'] == 'topology_owned_active'

        # Keep backup and restored roots as siblings, not nested copies of themselves.
        snapshots = root.parent / (root.name + '-snapshots')
        snapshots.mkdir()
        self.addCleanup(shutil.rmtree, snapshots, True)
        backup = snapshots / 'backup'
        restore(root, backup)
        for mode in ('restore', 'lost', 'corrupt', 'wrong_entry', 'wrong_token'):
            destination = snapshots / mode
            restore(backup, destination)
            private = destination / '.storage' / image.name
            if mode == 'lost':
                private.unlink()
            elif mode == 'corrupt':
                private.write_bytes(b'{corrupt data')
            elif mode in {'wrong_entry', 'wrong_token'}:
                raw = json.loads(private.read_bytes())
                # Correctly re-seal well-formed foreign inputs to isolate identity checks.
                payload = raw['data']
                payload['entry_id' if mode == 'wrong_entry' else 'enrollment'] = '0' * (26 if mode == 'wrong_entry' else 32)
                seal = sys.modules[f'custom_components.{DOMAIN}.settings']._digest
                payload.pop('integrity')
                payload['integrity'] = seal(payload)
                private.write_text(json.dumps(raw), encoding='utf-8')
            state = await self.cold(destination, entry, mode)
            assert state['rows'] == owned['rows']
            if mode == 'restore':
                assert state['status'] == 'topology_owned_active'
                assert state['settings'] == owned['settings'] and state['active_buttons'] == owned['active_buttons']
            else:
                assert state['status'] == 'topology_recovery_required', state
                assert not state['roles'] and not state['active_buttons']


if __name__ == '__main__':
    asyncio.run(cold(Path(sys.argv[1]), *sys.argv[2:]))
