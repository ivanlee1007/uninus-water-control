"""Source-rebind authority boundary through real decorated native HTTP views."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, main
import test_native_water_control as native
from water_settings_ws import settings_client, compare

class RebindAuthority(IsolatedAsyncioTestCase):
    asyncSetUp=native.NativeWaterControlTest.asyncSetUp
    set_depth=native.NativeWaterControlTest.set_depth
    setup_controller=native.NativeWaterControlTest.setup_controller

    async def test_rebind_actor_replacement_and_demotion_zero_writes(self):
        from aiohttp.test_utils import TestClient, TestServer
        from homeassistant import auth
        from homeassistant.components.http import HomeAssistantHTTP
        from homeassistant.components.config.config_entries import OptionManagerFlowIndexView, OptionManagerFlowResourceView
        entry=await self.setup_controller()
        request,_=await settings_client(self.hass,entry)
        current=(await request('get'))['result']
        ident=current['zones'][0]['tanks'][0]['id']
        saved=await request('save',**compare(current),changes={'tanks':[{'id':ident,'scale_max_m':8}]})
        self.assertTrue(saved['success'],saved)
        path=Path(entry.runtime_data.settings.store.path)
        before,options=path.read_bytes(),entry.options
        self.hass.auth=await auth.auth_manager_from_config(self.hass,[],[])
        await self.hass.auth.async_create_user('Separate owner')
        human=await self.hass.auth.async_create_user('Admin',group_ids=['system-admin'])
        other=await self.hass.auth.async_create_user('Other admin',group_ids=['system-admin'])
        async def headers(user):
            refresh=await self.hass.auth.async_create_refresh_token(user,'https://localhost/')
            return {'Authorization':'Bearer '+self.hass.auth.async_create_access_token(refresh)}
        human_headers,other_headers=await headers(human),await headers(other)
        http=HomeAssistantHTTP(self.hass,None,None,None,['127.0.0.1'],0,[],'modern')
        await http.async_initialize(cors_origins=[],use_x_forwarded_for=False,login_threshold=-1,
                                   is_ban_enabled=False,use_x_frame_options=True)
        http.register_view(OptionManagerFlowIndexView(self.hass.config_entries.options))
        http.register_view(OptionManagerFlowResourceView(self.hass.config_entries.options))
        client=TestClient(TestServer(http.app,host='127.0.0.1'))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        url='/api/config/config_entries/options/flow'
        async def post(route,data,actor=human_headers):
            response=await client.post(route,json=data,headers=actor)
            self.assertEqual(response.status,200,await response.text())
            return await response.json()
        async def form():
            menu=await post(url,{'handler':entry.entry_id})
            route=url+'/'+menu['flow_id']
            await post(route,{'next_step_id':'edit_tank'})
            selected=await post(route,{'tank_id':ident})
            self.assertEqual(selected['step_id'],'save_tank',selected)
            return route
        fields={'tank_name':'Unauthorized rebound','level_sensor':'sensor.backup'}
        denied=await post(await form(),fields,other_headers)
        self.assertEqual(denied['type'],'abort',denied)
        self.assertEqual(denied['reason'],'topology_admin_required')
        self.assertIs(entry.options,options)
        self.assertEqual(path.read_bytes(),before)
        import asyncio
        from unittest.mock import patch
        route=await form()
        settings=entry.runtime_data.settings
        original_read=settings._async_read_file
        entered,released=asyncio.Event(),asyncio.Event()
        async def held_read():
            data=await original_read()
            entered.set()
            await released.wait()
            return data
        with patch.object(settings,'_async_read_file',side_effect=held_read):
            queued=asyncio.create_task(client.post(route,json=fields,headers=human_headers))
            try:
                async with asyncio.timeout(3): await entered.wait()
                self.hass.auth.async_remove_refresh_token(next(iter(human.refresh_tokens.values())))
            finally:
                released.set()
            response=await queued
        self.assertEqual(response.status,200)
        denied=await response.json()
        self.assertEqual(denied['type'],'abort',denied)
        self.assertEqual(denied['reason'],'topology_admin_required')
        self.assertIs(entry.options,options)
        self.assertEqual(path.read_bytes(),before)
        human_headers.update(await headers(human))
        route=await form()
        await self.hass.auth.async_update_user(human,group_ids=['system-users'])
        denied=await client.post(route,json=fields,headers=human_headers)
        self.assertEqual(denied.status,401)
        self.assertIs(entry.options,options)
        self.assertEqual(path.read_bytes(),before)
        print('B1_REBIND_AUTHORITY actor replacement abort; demotion HTTP 401; Options identity and Settings bytes unchanged')



import asyncio
import json
from unittest.mock import patch
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.file import WriteError

class OptionsDurabilityBoundary(IsolatedAsyncioTestCase):
    asyncSetUp=native.NativeWaterControlTest.asyncSetUp
    set_depth=native.NativeWaterControlTest.set_depth
    setup_controller=native.NativeWaterControlTest.setup_controller
    option=native.NativeWaterControlTest.option
    submit=native.NativeWaterControlTest.submit
    edit_tank=native.NativeWaterControlTest.edit_tank

    async def test_native_success_does_not_prove_options_durable(self):
        entry=await self.setup_controller()
        from water_settings_ws import settings_client, compare
        request,_=await settings_client(self.hass,entry)
        view=(await request('get'))['result']
        ident=view['zones'][0]['tanks'][0]['id']
        saved=await request('save',**compare(view),changes={'tanks':[{'id':ident,'scale_max_m':8}]})
        self.assertTrue(saved['success'],saved)
        private=Path(entry.runtime_data.settings.store.path)
        numeric_before=private.read_bytes()
        bank=self.hass.config_entries
        store=bank._store
        await store.async_save(bank._data_to_save())
        path=Path(store.path)
        baseline=path.read_bytes()
        tank_id=entry.runtime_data.zones[0]['tanks'][0]['id']
        form=await self.edit_tank(entry,tank_id)
        attempted=asyncio.Event()
        def refuse(*args,**kwargs):
            self.hass.loop.call_soon_threadsafe(attempted.set)
            raise WriteError('B1 expected isolated Options write refusal')
        with self.assertLogs('homeassistant.helpers.storage',level='ERROR') as diagnostics:
            with patch.object(store,'_write_prepared_data',side_effect=refuse):
                result=await self.submit(form,tank_name='Storage rebound',level_sensor='sensor.backup')
                self.assertEqual(result['type'],FlowResultType.CREATE_ENTRY,result)
                self.assertEqual(entry.options['zones'][0]['tanks'][0]['level_sensor'],'sensor.backup')
                self.assertEqual(path.read_bytes(),baseline,'Options acknowledgement preceded physical persistence')
                async with asyncio.timeout(5): await attempted.wait()
                await self.hass.async_block_till_done()
                self.assertEqual(path.read_bytes(),baseline)
                self.assertEqual(private.read_bytes(),numeric_before)
                observed=await request('get')
                self.assertTrue(observed['success'],observed)
                self.assertEqual(observed['result']['zones'][0]['tanks'][0]['scale_max_m'],8)
        print('EXPECTED_CORE_WRITE_REFUSAL', '\n'.join(diagnostics.output))
        print('B1_OPTIONS_DURABILITY',json.dumps({'native_result':str(result['type']),
              'memory_source':entry.options['zones'][0]['tanks'][0]['level_sensor'],
              'durable_unchanged_after_injected_write_failure':path.read_bytes()==baseline,
              'expected_error_observed':any('B1 expected isolated Options write refusal' in line for line in diagnostics.output)}))
        self.assertTrue(any('B1 expected isolated Options write refusal' in line for line in diagnostics.output))
