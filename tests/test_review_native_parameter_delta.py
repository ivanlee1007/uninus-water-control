"""Q1: real native Options submits deltas against its frozen Settings Get."""
from copy import deepcopy
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType

import test_native_water_control as native
from water_settings_ws import settings_client, compare


class TestNativeParameterDelta(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeWaterControlTest.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit

    async def legacy(self, delay=2147483648.0, magnitude=0.3):
        entry = await self.setup_controller()
        data = deepcopy(dict(entry.data))
        tank = data['zones'][0]['tanks'][0]
        tank['thresholds'] = {'alarm_lower_m': magnitude, 'alarm_delay_seconds': delay,
                              'refill_lower_m': -magnitude, 'refill_upper_m': magnitude}
        data['display_scales_m'] = {tank['id']: 2.0}
        self.hass.config_entries.async_update_entry(entry, data=data)
        self.assertTrue(await self.hass.config_entries.async_reload(entry.entry_id))
        await self.hass.async_block_till_done()
        request, _ = await settings_client(self.hass, entry)
        return entry, tank['id'], request

    def shown(self, form):
        return {str(k): k.description['suggested_value'] for k in form['data_schema'].schema
                if k.description.get('suggested_value') is not None}

    async def opened(self, entry, tank_id=None):
        if tank_id is None:
            return await self.option(entry, 'common_parameters')
        return await self.submit(await self.option(entry, 'tank_parameters'), tank_id=tank_id)

    async def save_delta(self, entry, form, fields, expected):
        settings = entry.runtime_data.settings
        messages = []
        original = settings.async_request

        async def observe(authorize, message):
            messages.append(deepcopy(message))
            return await original(authorize, message)

        with patch.object(settings, 'async_request', observe):
            result = await self.submit(form, **fields)
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        self.assertEqual([m['type'].rsplit('/', 1)[1] for m in messages], ['config_save', 'config_get'])
        self.assertEqual(messages[0]['changes'], expected)
        return messages[0]

    async def test_scale_only_preserves_overcap_local_alarm(self):
        entry, ident, request = await self.legacy()
        before = (await request('get'))['result']
        form = await self.opened(entry, ident)
        message = await self.save_delta(entry, form, self.shown(form) | {'scale_max_m': 3.0},
                                       {'tanks': [{'id': ident, 'scale_max_m': 3.0}]})
        self.assertEqual(compare(before), {k: message[k] for k in compare(before)})
        after = (await request('get'))['result']['zones'][0]['tanks'][0]
        self.assertEqual(after['alarm'], before['zones'][0]['tanks'][0]['alarm'])
        self.assertEqual(after['scale_max_m'], 3.0)
        disk = Path(entry.runtime_data.settings.store.path).read_bytes()
        self.assertIn(b'2147483648.0', disk)

    async def test_finite_legacy_magnitudes_preserved_by_scale_only(self):
        entry, ident, request = await self.legacy(sys.float_info.max, sys.float_info.max)
        form = await self.opened(entry, ident)
        await self.save_delta(entry, form, self.shown(form) | {'scale_max_m': 3.0},
                              {'tanks': [{'id': ident, 'scale_max_m': 3.0}]})
        tank = (await request('get'))['result']['zones'][0]['tanks'][0]
        self.assertEqual(tank['refill']['local'], [-sys.float_info.max, sys.float_info.max])
        self.assertEqual(tank['alarm']['local'], [sys.float_info.max, sys.float_info.max])

    async def test_mode_only_retains_local_and_can_return_from_inheritance(self):
        entry, ident, request = await self.legacy()
        base = (await request('get'))['result']
        self.assertTrue((await request('save', **compare(base), changes={'common': {'alarm': [0.2, 10]}}))['success'])
        for inherit in (True, False):
            form = await self.opened(entry, ident)
            await self.save_delta(entry, form, self.shown(form) | {'alarm_use_common': inherit},
                                  {'tanks': [{'id': ident, 'alarm': {'use_common': inherit}}]})
            tank = (await request('get'))['result']['zones'][0]['tanks'][0]
            self.assertEqual(tank['alarm']['local'], [0.3, 2147483648.0])
            self.assertEqual(tank['alarm']['effective'], [0.2, 10] if inherit else [0.3, 2147483648.0])

    async def test_unrelated_local_pair_edit_is_whole_pair_and_disable_is_none(self):
        entry, ident, request = await self.legacy()
        form = await self.opened(entry, ident)
        # Omitted fields retain snapshot values, not implicit disabled/default values.
        await self.save_delta(entry, form, {'refill_upper_m': 4.0},
                              {'tanks': [{'id': ident, 'refill': {'local': [-0.3, 4.0]}}]})
        form = await self.opened(entry, ident)
        await self.save_delta(entry, form, {'alarm_enabled': False},
                              {'tanks': [{'id': ident, 'alarm': {'local': None}}]})
        tank = (await request('get'))['result']['zones'][0]['tanks'][0]
        self.assertIsNone(tank['alarm']['local'])
        self.assertEqual(tank['refill']['local'], [-0.3, 4.0])

    async def test_common_preserves_existing_finite_numbers(self):
        entry = await self.setup_controller()
        request, _ = await settings_client(self.hass, entry)
        before = (await request('get'))['result']
        self.assertTrue((await request('save', **compare(before), changes={'common': {'refill': [0, 2]}}))['success'])
        # Explicit fixture: a supported old stored common pair, not a new API edit.
        settings = entry.runtime_data.settings
        image = deepcopy(settings.data)
        image['common']['refill'] = [-sys.float_info.max, sys.float_info.max]
        await settings.store.async_save(image)
        self.assertTrue((await request('get'))['success'])
        form = await self.opened(entry)
        await self.save_delta(entry, form, self.shown(form) | {
            'alarm_enabled': True, 'alarm_lower_m': 0.2, 'alarm_delay_seconds': 10},
            {'common': {'alarm': [0.2, 10]}})
        current = (await request('get'))['result']
        self.assertEqual(current['common']['refill'], image['common']['refill'])
        form = await self.opened(entry)
        await self.save_delta(entry, form, {'refill_enabled': False}, {'common': {'refill': None}})

    async def test_new_oversized_edit_rejected_and_error_retains_original_cas(self):
        entry, ident, request = await self.legacy(100.0)
        form = await self.opened(entry, ident)
        baseline = (await request('get'))['result']
        path = Path(entry.runtime_data.settings.store.path)
        before = path.read_bytes() if path.exists() else None
        invalid = await self.submit(form, **(self.shown(form) | {'alarm_delay_seconds': 2147483648.0}))
        self.assertEqual(invalid['type'], FlowResultType.FORM, invalid)
        self.assertEqual(invalid['errors'], {'base': 'invalid_parameters'})
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        self.assertEqual(self.shown(invalid)['alarm_delay_seconds'], 2147483648.0)
        self.assertTrue((await request('save', **compare(baseline), changes={'tanks': [{'id': ident, 'scale_max_m': 4}]}))['success'])
        physical = path.read_bytes()
        rejected = await self.submit(invalid, alarm_delay_seconds=101)
        self.assertEqual(rejected['type'], FlowResultType.ABORT, rejected)
        self.assertEqual(rejected['reason'], 'configuration_changed')
        self.assertEqual(path.read_bytes(), physical)

    async def test_normal_control_noop_and_cancellation(self):
        entry, ident, request = await self.legacy(100.0)
        form = await self.opened(entry, ident)
        await self.save_delta(entry, form, self.shown(form) | {'scale_max_m': 3.0},
                              {'tanks': [{'id': ident, 'scale_max_m': 3.0}]})
        form = await self.opened(entry, ident)
        await self.save_delta(entry, form, self.shown(form), {})
        form = await self.opened(entry)
        await self.save_delta(entry, form, {}, {})
        path = Path(entry.runtime_data.settings.store.path)
        before = path.read_bytes()
        snapshot = (await request('get'))['result']
        for tank in (ident, None):
            form = await self.opened(entry, tank)
            self.hass.config_entries.options.async_abort(form['flow_id'])
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual((await request('get'))['result'], snapshot)
