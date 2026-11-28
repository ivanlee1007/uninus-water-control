"""Owned topology loss after load: preserve the exact Stop, reject another Start."""
import asyncio
from importlib.metadata import version
import json
from pathlib import Path
import sys
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame

import test_native_topology_migration as migration
from test_native_enrollment_recovery import status
from water_settings_ws import settings_client


async def warm(root, entry_id, *, configure=False):
    assert version('homeassistant') == '2025.12.0'
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
    calls = []
    off_dispatched = asyncio.Event()
    hold_off_feedback = False
    try:
        frame.async_setup(hass)
        loader.async_setup(hass)
        hass.config_entries = bank = ConfigEntries(hass, {})
        await dr.async_load(hass)
        await er.async_load(hass)
        await bank.async_initialize()
        entry = bank.async_get_entry(entry_id)
        assert entry.data[migration.MARK]['phase'] == 'owned'
        migration.signals(hass, [])

        @callback
        def apply(call):
            target = call.data['entity_id']
            calls.append((call.service, target))
            assert target == 'switch.probe_output'
            if hold_off_feedback and call.service == 'turn_off':
                off_dispatched.set()
                return
            hass.states.async_set(target, 'on' if call.service == 'turn_on' else 'off', context=call.context)

        for action in ('turn_on', 'turn_off'):
            hass.services.async_remove('switch', action)
            hass.services.async_register('switch', action, apply)
        await hass.async_start()
        assert await bank.async_setup(entry_id)
        await hass.async_block_till_done()
        imported = Path(sys.modules[f'custom_components.{migration.DOMAIN}.button'].__file__).resolve()
        assert imported == root / 'custom_components' / migration.DOMAIN / 'button.py'
        assert migration.digest(imported) == migration.digest(migration.legacy.SOURCE / 'button.py')
        for name in ('settings', 'config_flow', 'sensor'):
            copied = Path(sys.modules[f'custom_components.{migration.DOMAIN}.{name}'].__file__).resolve()
            assert copied == root / 'custom_components' / migration.DOMAIN / f'{name}.py'
            assert migration.digest(copied) == migration.digest(migration.legacy.SOURCE / f'{name}.py')
        request, user = await settings_client(hass, entry)
        role = entry.runtime_data.roles['refill']
        image = Path(entry.runtime_data.settings.store.path)
        core = root / '.storage/core.config_entries'
        core_before = core.read_bytes()
        buttons = {row.unique_id.rsplit('_', 1)[-1]: row.entity_id for row in
                   er.async_entries_for_config_entry(er.async_get(hass), entry_id) if row.domain == 'button'}

        async def press(name):
            await hass.services.async_call('button', 'press', {'entity_id': buttons[name]}, blocking=True,
                                           context=Context(user_id=user.id))

        async def await_output(expected):
            async with asyncio.timeout(5):
                while role.actual != expected or role.on_task or role.off_task:
                    await asyncio.sleep(.01)

        assert role.can_start() and calls == []
        if configure:
            registry = er.async_get(hass)

            def rows():
                return {(r.id, r.entity_id, r.unique_id) for r in er.async_entries_for_config_entry(registry, entry_id)}

            def projected():
                entity_id = registry.async_get_entity_id('sensor', migration.DOMAIN, f'{role.owner.tank_id}_refill_status')
                return hass.states.get(entity_id).attributes['uninus_water_manual']

            expected_rows = rows()
            original = image.read_bytes()
            settings_module = sys.modules[f'custom_components.{migration.DOMAIN}.settings']
            for kind in ('missing', 'mismatched'):
                assert await status(hass, entry) == 'topology_owned_active'
                assert projected()['can_start'] is True and projected()['actual'] == 'off'
                if kind == 'missing':
                    image.unlink()
                else:
                    # Structurally valid but wrong enrollment; keep its seal valid.
                    foreign = json.loads(original)
                    payload = foreign['data']
                    payload['enrollment'] = '0' * 32 if payload['enrollment'] != '0' * 32 else '1' * 32
                    payload.pop('integrity')
                    payload['integrity'] = settings_module._digest(payload)
                    image.write_text(json.dumps(foreign), encoding='utf-8')
                observed_bytes = image.read_bytes() if image.exists() else None
                assert await status(hass, entry) == 'topology_recovery_required'
                # Read the cached native state immediately. Neither a Card Get
                # nor an attempted Start may quarantine first and mask this defect.
                assert projected()['can_start'] is False, f'Configure reported {kind} image but still advertised Start'
                assert projected()['actual'] == 'off' and entry.runtime_data.roles['refill'] is role
                assert calls == [] and core.read_bytes() == core_before
                assert (image.read_bytes() if image.exists() else None) == observed_bytes
                if kind == 'missing':
                    assert not await bank.async_reload(entry_id)
                    assert entry.state is ConfigEntryState.SETUP_ERROR and rows() == expected_rows
                    assert not image.exists() and calls == []
                image.write_bytes(original)  # Exact disposable fixture; not a product restore path.
                assert await bank.async_reload(entry_id)
                await hass.async_block_till_done()
                old_role, role = role, entry.runtime_data.roles['refill']
                assert role is not old_role and rows() == expected_rows and calls == []
                assert image.read_bytes() == original

        await press('start')
        await await_output('on')
        assert role.requested and role.running_seen
        image.unlink()
        if configure:
            assert await status(hass, entry) == 'topology_recovery_required'
            assert projected()['can_start'] is False and projected()['actual'] == 'on'
            assert entry.runtime_data.roles['refill'] is role and role.running_seen and role.requested
            assert calls == [('turn_on', 'switch.probe_output')] and not image.exists()
            hold_off_feedback = True
        await press('stop')
        if configure:
            await asyncio.wait_for(off_dispatched.wait(), 4)
            assert projected()['actual'] == 'on' and not role.closed
            assert calls == [('turn_on', 'switch.probe_output'), ('turn_off', 'switch.probe_output')]
            hass.states.async_set('switch.probe_output', 'off')
            await asyncio.wait_for(role.settled.wait(), 4)
        await await_output('off')
        assert role.closed and not role.requested
        bound = [('turn_on', 'switch.probe_output'), ('turn_off', 'switch.probe_output')]
        assert calls == bound
        denied = None
        try:
            await press('start')
        except HomeAssistantError as err:
            denied = str(err)
        if not denied:
            await await_output('on')
        assert denied and calls == bound, ('Missing owned image admitted another Start after exact Stop', denied, calls)
        result = await request('get')
        assert not result['success'] and not role.can_start() and not image.exists()
        if configure:
            image.write_bytes(original)
            assert await bank.async_reload(entry_id)
            await hass.async_block_till_done()
            owner = entry.runtime_data
            settings = owner.settings
            corrupt = b'{invalid disposable fixture'
            image.write_bytes(corrupt)
            entered, release = asyncio.Event(), asyncio.Event()
            read = settings._async_read_file

            async def hold_failed_read():
                try:
                    return await read()
                except settings_module.SettingsError:
                    entered.set()
                    await release.wait()
                    raise

            with patch.object(settings, '_async_read_file', side_effect=hold_failed_read):
                showing = asyncio.create_task(status(hass, entry))
                unloading = None
                try:
                    await asyncio.wait_for(entered.wait(), 4)
                    unloading = asyncio.create_task(bank.async_unload(entry_id))
                    async with asyncio.timeout(4):
                        while not owner.retiring:
                            await asyncio.sleep(.01)
                    assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS and entry.runtime_data is owner
                finally:
                    release.set()
                    verdict = await asyncio.wait_for(showing, 5)
                    if unloading is not None:
                        assert await asyncio.wait_for(unloading, 5)
            assert verdict == 'entry_not_loaded' and not settings.topology_busy
            assert entry.state is ConfigEntryState.NOT_LOADED and not hasattr(entry, 'runtime_data')
            assert image.read_bytes() == corrupt and calls == bound
    finally:
        await hass.async_stop()
    assert core.read_bytes() == core_before and calls == bound
    print('WARM_LOSS', json.dumps({'isolated_bound_start_stop': calls, 'new_start': 'denied'}), flush=True)


class TestNativeTopologyWarmLoss(migration.IsolatedAsyncioTestCase):
    set_depth = migration.TestNativeTopologyMigration.set_depth
    setup_controller = migration.TestNativeTopologyMigration.setup_controller
    register_depth = migration.TestNativeTopologyMigration.register_depth
    edit_tank = migration.TestNativeTopologyMigration.edit_tank
    option = migration.TestNativeTopologyMigration.option
    saved = migration.TestNativeTopologyMigration.saved
    submit = migration.TestNativeTopologyMigration.submit
    _fresh = migration.TestNativeTopologyMigration._fresh
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp

    async def test_loss_after_load_preserves_exact_stop_but_cannot_start_again(self):
        await self.run_warm_case()

    async def test_configure_withdraws_cached_start_on_owned_image_loss_preserving_exact_stop(self):
        await self.run_warm_case('configure')

    async def run_warm_case(self, *mode):
        source = self.register_depth('test-depth', object_id='migration_depth')
        output = er.async_get(self.hass).async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        calls = []
        migration.signals(self.hass, calls)
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        tank = entry.data['zones'][0]['tanks'][0]
        await self.saved(await self.edit_tank(entry, tank['id']), tank_name='Storage', level_sensor=source.entity_id,
                         refill_switch=output.entity_id, commission_refill=True, confirm_device_cutoff=True)
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        assert confirmed['data'] is None
        root = Path(self.hass.config.config_dir)
        core = root / '.storage/core.config_entries'
        await migration.disk_row(core, entry.entry_id, lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        assert calls == []
        await self._fresh(root, entry, 'prepare')
        child = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            str(root), entry.entry_id, *mode, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(child.communicate(), 40)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        log = out.decode('utf-8', errors='replace')
        print(log, end='', flush=True)
        assert child.returncode == 0, (child.returncode, log)


if __name__ == '__main__':
    asyncio.run(warm(Path(sys.argv[1]), sys.argv[2], configure=sys.argv[3:] == ['configure']))
