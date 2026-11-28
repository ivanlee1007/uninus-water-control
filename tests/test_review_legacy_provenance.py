"""Legacy source edits retain one Store's parameters through exact provenance."""
import sys
import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, main
import test_native_water_control as native
from water_settings_ws import settings_client, compare
from homeassistant.data_entry_flow import FlowResultType

class LegacyMatrix(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeWaterControlTest.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit
    saved = native.NativeWaterControlTest.saved
    edit_tank = native.NativeWaterControlTest.edit_tank

    async def prepared(self, use_common=False):
        entry = await self.setup_controller()
        request, _ = await settings_client(self.hass, entry)
        view = (await request('get'))['result']
        ident = view['zones'][0]['tanks'][0]['id']
        saved = await request('save', **compare(view), changes={
            'common': {'refill': [0.5, 3], 'alarm': [0.3, 7]},
            'tanks': [{'id':ident, 'scale_max_m':8,
                       'refill':{'use_common':use_common,'local':[0.4,2]},
                       'alarm':{'use_common':use_common,'local':[0.2,5]}}]})
        self.assertTrue(saved['success'], saved)
        return entry, request, saved['result'], ident

    async def retention(self, use_common):
        entry, request, before, ident = await self.prepared(use_common)
        path = Path(entry.runtime_data.settings.store.path)
        original = path.read_bytes()
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage rebound', level_sensor='sensor.backup')
        response = await request('get')
        self.assertTrue(response['success'], response)
        after = response['result']
        old, new = before['zones'][0]['tanks'][0], after['zones'][0]['tanks'][0]
        self.assertEqual(new['id'], ident)
        self.assertEqual(after['zones'][0]['id'], before['zones'][0]['id'])
        self.assertEqual(after['common'], before['common'])
        form = await self.submit(await self.option(entry,'tank_parameters'),tank_id=ident)
        fields = {str(k):k.description.get('suggested_value') for k in form['data_schema'].schema}
        self.hass.config_entries.options.async_abort(form['flow_id'])
        print('B1_MATRIX', json.dumps({'common_mode':use_common,'before':old,'after':new,'reopen':fields,
                                      'store_unchanged':path.read_bytes()==original}))
        for field in ('scale_max_m','refill','alarm'):
            with self.subTest(field=field): self.assertEqual(new[field],old[field])
        with self.subTest(reopen='scale'): self.assertEqual(fields['scale_max_m'],8)
        for kind in ('refill','alarm'):
            with self.subTest(reopen=kind): self.assertEqual(fields[kind+'_use_common'],use_common)

    async def test_local_scale_and_reopen_after_explicit_rebind(self):
        await self.retention(False)

    async def test_use_common_keeps_local_pairs_scale_and_reopen_after_explicit_rebind(self):
        await self.retention(True)

    async def test_cancel_rebind_draft_zero_options_or_settings_writes(self):
        entry, request, view, ident = await self.prepared()
        path = Path(entry.runtime_data.settings.store.path)
        before, options = path.read_bytes(), entry.options
        form = await self.edit_tank(entry,ident)
        self.hass.config_entries.options.async_abort(form['flow_id'])
        self.assertIs(entry.options,options)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual((await request('get'))['result'],view)

    async def test_locked_settings_reject_rebind_without_half_write(self):
        entry, request, view, ident = await self.prepared()
        settings=entry.runtime_data.settings
        path=Path(settings.store.path)
        before, options=path.read_bytes(),entry.options
        form=await self.edit_tank(entry,ident)
        async with settings.lock:
            rejected=await self.submit(form,tank_name='Rejected',level_sensor='sensor.backup')
        self.assertEqual(rejected['type'],FlowResultType.ABORT,rejected)
        self.assertEqual(rejected['reason'],'configuration_changed')
        self.assertIs(entry.options,options)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual((await request('get'))['result'],view)

    async def test_repeated_rebind_then_numeric_save_consumes_bridge_and_native_edit_prunes(self):
        entry, request, view, ident = await self.prepared(True)
        path = Path(entry.runtime_data.settings.store.path)
        original = path.read_bytes()
        for source in ('sensor.backup', 'sensor.third'):
            self.set_depth(source, '2', 'm')
            await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor=source)
            current = (await request('get'))['result']
            self.assertEqual(current['zones'][0]['tanks'][0], view['zones'][0]['tanks'][0])
            self.assertEqual(path.read_bytes(), original)
        bridge = entry.options['parameter_provenance'][ident]
        self.assertEqual(set(bridge), {'generation', 'anchor', 'target', 'source'})
        self.assertTrue(all(isinstance(v, str) for v in bridge.values()))
        result = await request('save', **compare(current), changes={'tanks':[{'id':ident, 'scale_max_m':9}]})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['result']['zones'][0]['tanks'][0]['refill'], view['zones'][0]['tanks'][0]['refill'])
        self.assertEqual(result['result']['zones'][0]['tanks'][0]['alarm'], view['zones'][0]['tanks'][0]['alarm'])
        settings = entry.runtime_data.settings
        tank = settings.zones[0]['tanks'][0]
        self.assertEqual(settings.data['tanks'][0]['anchor'], settings._anchor(tank))
        settled = path.read_bytes()
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor='sensor.third')
        self.assertNotIn('parameter_provenance', entry.options)
        self.assertEqual(path.read_bytes(), settled)

    async def test_passive_alias_replacement_cannot_reanchor_dormant_bridge(self):
        from homeassistant.helpers import entity_registry as er
        entry, request, view, ident = await self.prepared()
        registry = er.async_get(self.hass)
        source = registry.async_get_or_create('sensor', 'test_probe', 'backup', suggested_object_id='backup')
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor=source.entity_id)
        self.assertEqual((await request('get'))['result']['zones'][0]['tanks'][0]['scale_max_m'], 8)
        path = Path(entry.runtime_data.settings.store.path)
        original = path.read_bytes()
        registry.async_remove(source.entity_id)
        self.hass.states.async_remove(source.entity_id)
        replacement = registry.async_get_or_create('sensor', 'test_probe', 'replacement', suggested_object_id='backup')
        self.assertEqual(replacement.entity_id, source.entity_id)
        self.set_depth(replacement.entity_id, '3', 'm')
        self.assertIsNone((await request('get'))['result']['zones'][0]['tanks'][0]['scale_max_m'])
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor=replacement.entity_id)
        self.assertNotIn('parameter_provenance', entry.options)
        self.assertIsNone((await request('get'))['result']['zones'][0]['tanks'][0]['scale_max_m'])
        self.assertEqual(path.read_bytes(), original)

    async def test_failed_independent_read_and_cancelled_read_cannot_write_options(self):
        import asyncio
        from unittest.mock import patch
        entry, request, view, ident = await self.prepared()
        settings = entry.runtime_data.settings
        path = Path(settings.store.path)
        original, options = path.read_bytes(), entry.options
        SettingsError = sys.modules[type(settings).__module__].SettingsError
        form = await self.edit_tank(entry, ident)
        with patch.object(settings, '_async_read_file', side_effect=SettingsError('settings_unavailable')):
            rejected = await self.submit(form, tank_name='Storage', level_sensor='sensor.backup')
        self.assertEqual(rejected['type'], FlowResultType.ABORT, rejected)
        self.assertIs(entry.options, options)
        self.assertEqual(path.read_bytes(), original)
        form = await self.edit_tank(entry, ident)
        entered = asyncio.Event()
        async def held_read():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(settings, '_async_read_file', side_effect=held_read):
            task = asyncio.create_task(self.submit(form, tank_name='Storage', level_sensor='sensor.backup'))
            async with asyncio.timeout(3): await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(settings.lock.locked())
        self.assertIs(entry.options, options)
        self.assertEqual(path.read_bytes(), original)

    async def test_disabled_legacy_rebind_preserves_overlay_on_reenable(self):
        from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
        entry, request, view, ident = await self.prepared(True)
        path = Path(entry.runtime_data.settings.store.path)
        original = path.read_bytes()
        self.assertTrue(await self.hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER))
        self.assertEqual(entry.state, ConfigEntryState.NOT_LOADED)
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor='sensor.backup')
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(await self.hass.config_entries.async_set_disabled_by(entry.entry_id, None))
        await self.hass.async_block_till_done()
        observed = await request('get')
        self.assertTrue(observed['success'], observed)
        self.assertEqual(observed['result']['zones'][0]['tanks'][0], view['zones'][0]['tanks'][0])
        self.assertEqual(path.read_bytes(), original)

    async def test_failed_numeric_save_keeps_valid_bridge_and_parameters(self):
        from unittest.mock import patch
        from homeassistant.util.file import WriteError
        entry, request, view, ident = await self.prepared(True)
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor='sensor.backup')
        current = (await request('get'))['result']
        settings = entry.runtime_data.settings
        path = Path(settings.store.path)
        original, options = path.read_bytes(), entry.options
        with self.assertLogs('homeassistant.helpers.storage', level='ERROR') as diagnostics:
            with patch.object(settings.store, '_write_prepared_data', side_effect=WriteError('expected bridged numeric refusal')):
                rejected = await request('save', **compare(current), changes={'tanks':[{'id':ident, 'scale_max_m':9}]})
        self.assertFalse(rejected['success'], rejected)
        self.assertEqual(rejected['error']['code'], 'readback_required')
        self.assertIs(entry.options, options)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((await request('get'))['result'], current)
        self.assertTrue(any('expected bridged numeric refusal' in line for line in diagnostics.output))
        print('EXPECTED_NUMERIC_WRITE_REFUSAL', '\n'.join(diagnostics.output))

    async def test_source_change_during_provenance_read_is_rejected(self):
        import asyncio
        from unittest.mock import patch
        from homeassistant.helpers import entity_registry as er
        entry, request, view, ident = await self.prepared()
        registry = er.async_get(self.hass)
        source = registry.async_get_or_create('sensor', 'test_probe', 'backup', suggested_object_id='backup')
        settings = entry.runtime_data.settings
        path = Path(settings.store.path)
        original, options = path.read_bytes(), entry.options
        form = await self.edit_tank(entry, ident)
        entered, released = asyncio.Event(), asyncio.Event()
        read = settings._async_read_file
        async def held_read():
            data = await read()
            entered.set()
            await released.wait()
            return data
        with patch.object(settings, '_async_read_file', side_effect=held_read):
            task = asyncio.create_task(self.submit(form, tank_name='Storage', level_sensor=source.entity_id))
            try:
                async with asyncio.timeout(3): await entered.wait()
                registry.async_remove(source.entity_id)
                replacement = registry.async_get_or_create('sensor', 'test_probe', 'replacement', suggested_object_id='backup')
                self.assertNotEqual(source.id, replacement.id)
            finally:
                released.set()
            rejected = await task
        self.assertEqual(rejected['type'], FlowResultType.ABORT, rejected)
        self.assertEqual(rejected['reason'], 'configuration_changed')
        self.assertIs(entry.options, options)
        self.assertEqual(path.read_bytes(), original)

    async def test_bridge_is_consumed_before_owned_topology_validation(self):
        entry, request, view, ident = await self.prepared(True)
        await self.saved(await self.edit_tank(entry, ident), tank_name='Storage', level_sensor='sensor.backup')
        # Real preparer and native witness; fresh-process lifecycle is separate.
        result = await self.submit(await self.option(entry, 'enroll_topology'), confirm_device_cutoff=True)
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        Settings = type(entry.runtime_data.settings)
        prepared = Settings(self.hass, entry)
        await prepared._async_prepare_topology()
        data = await prepared._async_read_file()
        self.assertEqual(data['tanks'][0]['scale_max_m'], 8)
        self.assertEqual(data['tanks'][0]['refill'], {'use_common':True, 'local':[0.4,2]})
        self.assertEqual(data['tanks'][0]['alarm'], {'use_common':True, 'local':[0.2,5]})
        self.assertNotIn('parameter_provenance', entry.options)
        self.assertNotIn('parameter_provenance', data['topology'])
