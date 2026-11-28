"""C architecture: native manager and authenticated HTTP Options contracts."""
import asyncio
import json
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType

import test_native_water_control as native
import test_native_topology_migration as migration
from water_settings_ws import settings_client, compare


class TestNativeCParameters(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeWaterControlTest.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit

    async def test_common_and_tank_parameters_use_settings_engine_and_reopen(self):
        entry = await self.setup_controller()
        request, _ = await settings_client(self.hass, entry)
        baseline = await request('get')
        self.assertTrue(baseline['success'], baseline)
        path = Path(entry.runtime_data.settings.store.path)
        self.assertFalse(path.exists())
        menu = await self.hass.config_entries.options.async_init(entry.entry_id)
        self.assertIn('common_parameters', menu['menu_options'])
        form = await self.submit(menu, next_step_id='common_parameters')
        cancelled = form['flow_id']
        self.hass.config_entries.options.async_abort(cancelled)
        self.assertFalse(path.exists())
        form = await self.option(entry, 'common_parameters')
        result = await self.submit(form, refill_enabled=True, refill_lower_m=0.4, refill_upper_m=1.8,
                                   alarm_enabled=True, alarm_lower_m=0.2, alarm_delay_seconds=8)
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        current = await request('get')
        self.assertTrue(current['success'], current)
        self.assertEqual(current['result']['common'], {'refill': [0.4, 1.8], 'alarm': [0.2, 8]})
        tank_id = entry.data['zones'][0]['tanks'][0]['id']
        form = await self.submit(await self.option(entry, 'tank_parameters'), tank_id=tank_id)
        result = await self.submit(form, refill_use_common=True, refill_enabled=False,
                                   alarm_use_common=False, alarm_enabled=True, alarm_lower_m=0.1,
                                   alarm_delay_seconds=5, scale_enabled=True, scale_max_m=4)
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        current = await request('get')
        tank = current['result']['zones'][0]['tanks'][0]
        self.assertTrue(tank['refill']['use_common'])
        self.assertEqual(tank['refill']['effective'], [0.4, 1.8])
        self.assertEqual(tank['alarm']['local'], [0.1, 5])
        self.assertEqual(tank['scale_max_m'], 4)
        reopened = await self.submit(await self.option(entry, 'tank_parameters'), tank_id=tank_id)
        fields = {str(key): key.description.get('suggested_value') for key in reopened['data_schema'].schema}
        self.assertTrue(fields['refill_use_common'])
        self.assertEqual(fields['scale_max_m'], 4)
        before = path.read_bytes()
        self.hass.config_entries.options.async_abort(reopened['flow_id'])
        self.assertEqual(path.read_bytes(), before)

    async def test_http_native_parameters_actor_revocation_unload_and_cancel(self):
        await self.http_parameters_contract(await self.setup_controller())

    async def http_parameters_contract(self, entry):
        from aiohttp.test_utils import TestClient, TestServer
        from homeassistant import auth
        from homeassistant.components.config.config_entries import OptionManagerFlowIndexView, OptionManagerFlowResourceView
        from homeassistant.components.http import HomeAssistantHTTP
        self.hass.auth = await auth.auth_manager_from_config(self.hass, [], [])
        await self.hass.auth.async_create_user('Separate owner')
        human = await self.hass.auth.async_create_user('Native admin', group_ids=['system-admin'])
        replacement = await self.hass.auth.async_create_user('Different admin', group_ids=['system-admin'])

        async def headers(user):
            refresh = await self.hass.auth.async_create_refresh_token(user, 'https://localhost/')
            return {'Authorization': 'Bearer ' + self.hass.auth.async_create_access_token(refresh)}

        human_headers = await headers(human)
        other_headers = await headers(replacement)
        http = HomeAssistantHTTP(self.hass, None, None, None, ['127.0.0.1'], 0, [], 'modern')
        await http.async_initialize(cors_origins=[], use_x_forwarded_for=False, login_threshold=-1,
                                    is_ban_enabled=False, use_x_frame_options=True)
        http.register_view(OptionManagerFlowIndexView(self.hass.config_entries.options))
        http.register_view(OptionManagerFlowResourceView(self.hass.config_entries.options))
        client = TestClient(TestServer(http.app, host='127.0.0.1'))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        url = '/api/config/config_entries/options/flow'

        async def post(path, data, actor=human_headers):
            response = await client.post(path, json=data, headers=actor)
            self.assertEqual(response.status, 200, await response.text())
            return await response.json()

        async def opened():
            menu = await post(url, {'handler': entry.entry_id})
            self.assertEqual(menu['type'], 'menu', menu)
            flow_url = url + '/' + menu['flow_id']
            form = await post(flow_url, {'next_step_id': 'common_parameters'})
            self.assertEqual(form['type'], 'form', form)
            flow = self.hass.config_entries.options._progress[menu['flow_id']]
            self.assertIs(flow._actor, human)
            return flow_url, flow

        async def inventory_form(operation, **target):
            menu = await post(url, {'handler': entry.entry_id})
            route = url + '/' + menu['flow_id']
            form = await post(route, {'next_step_id': operation})
            if target:
                form = await post(route, target)
            self.assertEqual(form['type'], 'form', form)
            return route

        async def saved_inventory(route, fields):
            result = await post(route, fields)
            self.assertEqual(result['type'], 'create_entry', result)
            await self.hass.async_block_till_done()

        initial_zones = json.loads(json.dumps(entry.runtime_data.zones))
        self.set_depth('sensor.http_zone', '1.5', 'm')
        self.set_depth('sensor.http_tank', '2', 'm')
        await saved_inventory(await inventory_form('add_zone'),
                              {'zone_name': 'HTTP zone', 'tank_name': 'HTTP tank', 'level_sensor': 'sensor.http_zone'})
        zone = entry.runtime_data.zones[-1]
        zone_id = zone['id']
        await saved_inventory(await inventory_form('edit_zone', zone_id=zone_id), {'zone_name': 'HTTP renamed'})
        await saved_inventory(await inventory_form('add_tank', zone_id=zone_id),
                              {'tank_name': 'HTTP second', 'level_sensor': 'sensor.http_tank'})
        second_id = entry.runtime_data.zones[-1]['tanks'][-1]['id']
        await saved_inventory(await inventory_form('edit_tank', tank_id=second_id),
                              {'tank_name': 'HTTP final', 'level_sensor': 'sensor.http_tank'})
        self.assertEqual(entry.runtime_data.zones[-1]['tanks'][-1]['name'], 'HTTP final')
        await saved_inventory(await inventory_form('remove_tank', tank_id=second_id), {'confirm': True})
        await saved_inventory(await inventory_form('remove_zone', zone_id=zone_id), {'confirm': True})
        self.assertEqual(entry.runtime_data.zones, initial_zones)

        path = Path(entry.runtime_data.settings.store.path)
        original_bytes = path.read_bytes() if path.exists() else None
        flow_url, _ = await opened()
        response = await client.delete(flow_url, headers=human_headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(path.read_bytes() if path.exists() else None, original_bytes)
        flow_url, flow = await opened()
        fields = {'refill_enabled': True, 'refill_lower_m': 0.4, 'refill_upper_m': 2,
                  'alarm_enabled': False}
        result = await post(flow_url, fields)
        self.assertEqual(result['type'], 'create_entry', result)
        physical = json.loads(path.read_bytes())['data']
        self.assertEqual(physical['common']['refill'], [0.4, 2])
        before = path.read_bytes()
        flow_url, flow = await opened()
        settings = entry.runtime_data.settings
        async with settings.lock:
            queued = asyncio.create_task(client.post(flow_url, json=fields | {'refill_upper_m': 3}, headers=human_headers))
            async with asyncio.timeout(3):
                while not settings.lock._waiters or not any(not waiter.done() for waiter in settings.lock._waiters):
                    await asyncio.sleep(0)
            self.hass.auth.async_remove_refresh_token(next(iter(human.refresh_tokens.values())))
        response = await queued
        denied = await response.json()
        self.assertEqual(path.read_bytes(), before, 'Revoked native request changed durable parameters after gate wait')
        self.assertEqual(denied['type'], 'abort', denied)
        self.assertEqual(denied['reason'], 'topology_admin_required')
        human_headers.update(await headers(human))
        flow_url, flow = await opened()
        invalid = await post(flow_url, fields | {'refill_upper_m': 0.1})
        self.assertEqual(invalid['errors'], {'base': 'invalid_parameters'})
        self.assertEqual(path.read_bytes(), before)
        await self.hass.auth.async_update_user(human, group_ids=['system-users'])
        denied = await client.post(flow_url, json=fields, headers=human_headers)
        self.assertEqual(denied.status, 401)  # Core maps Unauthorized to HTTP 401.
        self.assertEqual(path.read_bytes(), before)
        denied = await post(flow_url, fields, other_headers)
        self.assertEqual(denied['type'], 'abort', denied)
        self.assertEqual(denied['reason'], 'topology_admin_required')
        self.assertEqual(path.read_bytes(), before)
        await self.hass.auth.async_update_user(human, group_ids=['system-admin'])
        flow_url, flow = await opened()
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        denied = await post(flow_url, fields)
        self.assertEqual(denied['type'], 'abort', denied)
        self.assertEqual(denied['reason'], 'entry_not_loaded')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(hasattr(flow, '_parameter_snapshot'), 'Retired native flow retained private settings')

    async def test_native_parameter_stale_tokens_and_failed_readback_never_overwrite(self):
        from homeassistant.helpers import entity_registry as er
        from homeassistant.util.file import WriteError
        registry = er.async_get(self.hass)
        self.hass.states.async_remove('sensor.depth')
        source = registry.async_get_or_create('sensor', 'test_probe', 'depth', suggested_object_id='depth')
        self.set_depth(source.entity_id, '1.25', 'm')
        entry = await self.setup_controller()
        request, _ = await settings_client(self.hass, entry)
        original = await request('get')
        form = await self.option(entry, 'common_parameters')
        path = Path(entry.runtime_data.settings.store.path)
        fields = {'refill_enabled': True, 'refill_lower_m': 0.5, 'refill_upper_m': 2, 'alarm_enabled': False}
        saved = await request('save', **compare(original['result']), changes={'common': {'refill': [0.4, 3]}})
        self.assertTrue(saved['success'], saved)
        before = path.read_bytes()
        rejected = await self.submit(form, **fields)
        self.assertEqual(rejected['type'], FlowResultType.ABORT)
        self.assertEqual(rejected['reason'], 'configuration_changed')
        self.assertEqual(path.read_bytes(), before)
        form = await self.option(entry, 'common_parameters')
        registry.async_remove(source.entity_id)
        self.hass.states.async_remove(source.entity_id)
        replacement = registry.async_get_or_create('sensor', 'test_probe', 'replacement', suggested_object_id='depth')
        self.assertEqual(replacement.entity_id, source.entity_id)
        rejected = await self.submit(form, **fields)
        self.assertEqual(rejected['type'], FlowResultType.ABORT)
        self.assertEqual(rejected['reason'], 'configuration_changed')
        self.assertEqual(path.read_bytes(), before)
        form = await self.option(entry, 'common_parameters')
        store = entry.runtime_data.settings.store
        with patch.object(store, '_write_prepared_data', side_effect=WriteError('native C expected write refusal')) as write:
            rejected = await self.submit(form, **fields)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(rejected['type'], FlowResultType.ABORT)
        self.assertEqual(rejected['reason'], 'readback_required')
        self.assertEqual(path.read_bytes(), before)
        current = await request('get')
        self.assertTrue(current['success'], current)
        self.assertEqual(current['result']['common']['refill'], [0.4, 3])


async def owned_crud(root, entry_id):
    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
    sys.path.insert(0, str(root))
    hass = HomeAssistant(str(root))
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
        registry = er.async_get(hass)
        for name in ('third', 'fourth', 'fifth'):
            row = registry.async_get_or_create('sensor', 'test_probe', name, suggested_object_id=name)
            hass.states.async_set(row.entity_id, '1', {'unit_of_measurement': 'm'})
        core = root / '.storage/core.config_entries'
        before_core = core.read_bytes()
        image = Path(entry.runtime_data.settings.store.path)
        original = entry.runtime_data.zones[0]
        original_tank = original['tanks'][0]['id']

        async def option(operation, **target):
            menu = await bank.options.async_init(entry_id)
            assert menu['type'] is FlowResultType.MENU, menu
            form = await bank.options.async_configure(menu['flow_id'], {'next_step_id': operation})
            return await bank.options.async_configure(form['flow_id'], target) if target else form

        async def submit(form, **fields):
            return await bank.options.async_configure(form['flow_id'], fields)

        async def saved(form, **fields):
            result = await submit(form, **fields)
            assert result['type'] is FlowResultType.CREATE_ENTRY, result
            await hass.async_block_till_done()
            assert core.read_bytes() == before_core and not calls
            return result

        result = await submit(await option('add_zone'), zone_name='第二區', tank_name='同名', level_sensor='sensor.third')
        physical = json.loads(image.read_bytes())['data']['topology']['zones']
        assert len(physical) == 2 and physical[1]['name'] == '第二區', ('native add_zone did not persist', result, physical)
        assert result['type'] is FlowResultType.CREATE_ENTRY, result
        await hass.async_block_till_done()
        added_zone = physical[1]['id']
        await saved(await option('edit_zone', zone_id=added_zone), zone_name='正式區名')
        await saved(await option('add_tank', zone_id=original['id']), tank_name='同名', level_sensor='sensor.backup')
        zones = entry.runtime_data.zones
        added_tank = zones[0]['tanks'][1]['id']
        await saved(await option('edit_tank', tank_id=added_tank), tank_name='正式桶名', level_sensor='sensor.fourth')
        await saved(await option('common_parameters'), refill_enabled=True, refill_lower_m=0.5,
                    refill_upper_m=2, alarm_enabled=False)
        await saved(await option('tank_parameters', tank_id=added_tank), refill_use_common=True,
                    refill_enabled=False, alarm_use_common=False, alarm_enabled=False,
                    scale_enabled=True, scale_max_m=8)
        request, _ = await settings_client(hass, entry)
        viewed = await request('get')
        assert viewed['success'], viewed
        policy = viewed['result']['zones'][0]['tanks'][1]
        assert policy['refill']['effective'] == [0.5, 2] and policy['scale_max_m'] == 8
        await saved(await option('edit_tank', tank_id=added_tank), tank_name='正式桶名', level_sensor='sensor.fifth')
        current = await request('get')
        assert current['success'], current
        rebound = current['result']['zones'][0]['tanks'][1]
        assert rebound['scale_max_m'] == 8 and rebound['refill']['effective'] == [0.5, 2], (
            'Explicit native source edit erased the stable tank parameter record', rebound)
        assert entry.runtime_data.roles['refill'].can_start()
        pending = await option('remove_tank', tank_id=original_tank)
        before = image.read_bytes()
        rejected = await submit(pending, confirm=False)
        assert rejected['type'] is FlowResultType.FORM and image.read_bytes() == before
        bank.options.async_abort(pending['flow_id'])
        assert image.read_bytes() == before
        await saved(await option('remove_tank', tank_id=original_tank), confirm=True)
        assert entry.runtime_data.settings.manual == {} and not entry.runtime_data.roles
        await saved(await option('remove_zone', zone_id=added_zone), confirm=True)
        final = json.loads(image.read_bytes())['data']
        assert [t['id'] for z in final['topology']['zones'] for t in z['tanks']] == [added_tank]
        assert final['tanks'][0]['scale_max_m'] == 8
        assert not calls and core.read_bytes() == before_core
        # Reuse the real middleware/view contract against an owned entry too;
        # only fixture ownership changes, not the assertions or native transport.
        assertions = TestNativeCParameters()
        assertions.hass = hass
        cleanups = []
        assertions.addAsyncCleanup = lambda function: cleanups.append(function)
        try:
            await assertions.http_parameters_contract(entry)
        finally:
            for function in reversed(cleanups):
                await function()
        assert core.read_bytes() == before_core and not calls
        print('C_NATIVE_OWNED', json.dumps({'crud': True, 'parameters': True, 'http': True,
                                          'core_unchanged': True, 'calls': calls}))
    finally:
        await hass.async_stop()


class TestOwnedCOptions(IsolatedAsyncioTestCase):
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    register_depth = native.NativeWaterControlTest.register_depth
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit
    saved = native.NativeWaterControlTest.saved
    edit_tank = native.NativeWaterControlTest.edit_tank
    _fresh = migration.TestNativeTopologyMigration._fresh

    async def test_owned_native_full_inventory_and_parameters(self):
        from homeassistant.helpers import entity_registry as er
        source = self.register_depth('original', object_id='migration_depth')
        registry = er.async_get(self.hass)
        output = registry.async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        migration.signals(self.hass, [])
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        tank_id = entry.data['zones'][0]['tanks'][0]['id']
        await self.saved(await self.edit_tank(entry, tank_id), tank_name='Protected',
                         level_sensor=source.entity_id, refill_switch=output.entity_id,
                         commission_refill=True, confirm_device_cutoff=True)
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        self.assertEqual(confirmed['type'], FlowResultType.CREATE_ENTRY)
        root = Path(self.hass.config.config_dir)
        await migration.disk_row(root / '.storage/core.config_entries', entry.entry_id,
                                 lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        await self._fresh(root, entry, 'prepare')
        child = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            'owned', str(root), entry.entry_id, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(child.communicate(), 90)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        log = output.decode('utf-8', errors='replace')
        print(log, end='')
        self.assertEqual(child.returncode, 0, log)
        self.assertIn('C_NATIVE_OWNED', log)


class TestCNativeTranslations(TestCase):
    def test_parameter_fields_and_menu_are_localized(self):
        common = {'refill_enabled', 'refill_lower_m', 'refill_upper_m', 'alarm_enabled',
                  'alarm_lower_m', 'alarm_delay_seconds'}
        tank = common | {'refill_use_common', 'alarm_use_common', 'scale_enabled', 'scale_max_m'}
        for locale in ('en', 'zh-Hant'):
            options = json.loads((native.SOURCE / 'translations' / f'{locale}.json').read_text(encoding='utf-8'))['options']
            self.assertTrue(options['step']['init']['menu_options'].get('common_parameters'))
            self.assertTrue(options['step']['init']['menu_options'].get('tank_parameters'))
            for step_id, fields in [('common_parameters', common), ('save_parameters', tank)]:
                self.assertEqual(set(options['step'].get(step_id, {}).get('data', {})), fields)
            self.assertTrue(options['error'].get('invalid_parameters'))


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'owned':
    asyncio.run(owned_crud(Path(sys.argv[2]), sys.argv[3]))
