"""Read-only alarm history over authenticated Core WS and actual SQLite Recorder."""

import asyncio
import json
import logging
import socket
import tempfile
from functools import partial
from importlib import import_module
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import uuid4

from homeassistant import auth, bootstrap, loader
from homeassistant.components import recorder
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.util.dt import utcnow

DOMAIN = 'uninus_water_control'
COMMAND = f'{DOMAIN}/alarm_history_get'
ALARM = 'switch.synthetic_alarm'
EXCLUDED = 'switch.excluded_alarm'


class TestNativeAlarmHistory(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='water-alarm-history-')))
        self.hass = hass = HomeAssistant(str(self.root))
        self.addAsyncCleanup(self.shutdown)
        for name in ('connect', 'connect_ex', 'sendto'):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError('network forbidden')))
        loader.async_setup(hass)
        hass.config.skip_pip = True
        conf = {'recorder': {'auto_purge': False, 'commit_interval': 1, 'exclude': {'entities': [EXCLUDED]}}}
        hass.config_entries = ConfigEntries(hass, conf)
        await bootstrap.async_load_base_functionality(hass)
        async_initialize_recorder(hass)
        from homeassistant.setup import async_setup_component
        self.assertTrue(await async_setup_component(hass, 'recorder', conf))
        await hass.async_start()
        self.rec = recorder.get_instance(hass)
        self.assertTrue(await asyncio.wait_for(asyncio.shield(self.rec.async_db_ready), 10))
        self.registry = er.async_get(hass)
        for idx, entity_id in enumerate((ALARM, EXCLUDED)):
            self.registry.async_get_or_create('switch', 'water_history_fixture', f'bind_{idx}',
                                              suggested_object_id=entity_id.partition('.')[2])
        self.service_calls = []

        @callback
        def observe(event):
            self.service_calls.append((event.data['domain'], event.data['service']))

        hass.bus.async_listen(EVENT_CALL_SERVICE, observe)
        self.assertTrue(await async_setup_component(hass, DOMAIN, {}))
        self.assertEqual(Path(import_module(f'custom_components.{DOMAIN}').__file__).resolve(),
                         Path(__file__).resolve().parents[1] / 'custom_components' / DOMAIN / '__init__.py')
        hass.auth = await auth.auth_manager_from_config(hass, [], [])
        await hass.auth.async_create_user('Separate native owner')
        self.entry, self.zone, self.tank = await self.make_entry(ALARM)
        self.cut = utcnow()

    async def shutdown(self):
        await asyncio.wait_for(self.hass.async_stop(), 15)
        self.assertEqual(self.hass.state, CoreState.stopped)
        self.assertFalse(getattr(self, 'rec', None) and self.rec.is_alive())

    async def make_entry(self, entity_id=None, *, protected=None, registered=True):
        number = len(self.hass.config_entries.async_entries(DOMAIN))
        probe = f'sensor.alarm_history_depth_{number}'
        self.hass.states.async_set(probe, '1', {'unit_of_measurement': 'm'})
        data = {'zone_name': f'Zone {number}', 'tank_name': f'Tank {number}', 'level_sensor': probe}
        if entity_id:
            data['alarm_switch'] = entity_id
        if number == 0:
            manager = self.hass.config_entries.flow
            form = await manager.async_init(DOMAIN, context={'source': 'user'})
            response = await manager.async_configure(form['flow_id'], data)
            self.assertEqual(response['type'], FlowResultType.CREATE_ENTRY, response)
            entry = response['result']
        else:
            # The UI is single-entry; a second native config entry here tests
            # scope isolation without changing the native user flow.
            tank = {'id': uuid4().hex, 'name': data['tank_name'], 'level_sensor': probe,
                    'refill_switch': None, 'alarm_switch': entity_id}
            if entity_id and registered:
                tank['alarm_switch_registry_id'] = self.registry.async_get(entity_id).id
            options = {} if protected is None else {'manual': {'tank_id': tank['id'], 'alarm': {
                'output': tank['alarm_switch_registry_id'], 'max_runtime_seconds': 60, 'cutoff_confirmed': protected,
            }}}
            entry = ConfigEntry(domain=DOMAIN, title=data['zone_name'], source='user', version=1,
                                minor_version=1, discovery_keys={}, subentries_data=[], unique_id=None,
                                data={'zones': [{'id': uuid4().hex, 'name': data['zone_name'], 'tanks': [tank]}]}, options=options)
            await self.hass.config_entries.async_add(entry)
        await self.hass.async_block_till_done()
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        zone, = entry.data['zones']
        tank, = zone['tanks']
        return entry, zone['id'], tank['id']

    async def client(self, group='system-admin'):
        user = await self.hass.auth.async_create_user('History test', group_ids=[group])
        token = await self.hass.auth.async_create_refresh_token(user, 'https://localhost/')
        connection = ActiveConnection(logging.getLogger(__name__), self.hass, lambda _: None, user, token)
        replies = {}

        def deliver(value):
            message = json.loads(value) if isinstance(value, (str, bytes)) else value
            if message['id'] in replies:
                replies[message['id']].set_result(message)

        connection.send_message = deliver

        async def get(target=None, **extra):
            entry, zone, tank = target or (self.entry, self.zone, self.tank)
            sequence = connection.last_id + 1
            replies[sequence] = self.hass.loop.create_future()
            try:
                connection.async_handle({'id': sequence, 'type': COMMAND, 'version': 1,
                                         'entry_id': entry.entry_id, 'zone_id': zone, 'tank_id': tank, **extra})
                return await asyncio.wait_for(replies[sequence], 6)
            finally:
                replies.pop(sequence, None)

        return get, user

    async def commit(self):
        completion = self.hass.loop.create_future()
        self.rec.queue_task(SynchronizeTask(completion))
        await asyncio.wait_for(completion, 8)

    async def state(self, value, attributes=None, *, entity_id=ALARM):
        self.hass.states.async_set(entity_id, value, attributes or {})
        await self.hass.async_block_till_done()
        await self.commit()
        return self.hass.states.get(entity_id).last_changed.isoformat(timespec='microseconds').replace('+00:00', 'Z')

    async def real_rows(self, entity=ALARM):
        return (await self.rec.async_add_executor_job(partial(
            state_changes_during_period, self.hass, self.cut, utcnow(), entity_id=entity,
            no_attributes=True, include_start_time_state=False, limit=20))).get(entity, [])

    def result(self, response, status):
        self.assertTrue(response['success'], response)
        result = response['result']
        self.assertEqual(result['status'], status, response)
        self.assertEqual(set(result), {'version', 'scope', 'status', 'window', 'coverage', 'events', 'limited'})
        self.assertRegex(result['scope'], '^[a-f0-9]{32}$')
        self.assertEqual(result['version'], 1)
        self.assertEqual(set(result['window']), {'start_utc', 'end_utc'})
        self.assertNotIn('switch.', json.dumps(result))
        return result

    def error(self, response, code):
        self.assertFalse(response['success'], response)
        self.assertNotIn('result', response)
        self.assertEqual(response['error']['code'], code, response)

    async def test_registered_read_matches_real_recorder_transitions_and_never_dispatches(self):
        get, user = await self.client()
        self.assertTrue(user.is_admin and not user.is_owner)
        store = Path(self.entry.runtime_data.settings.store.path)
        before_options = self.entry.options
        self.service_calls.clear()
        baseline = await self.state('off')
        self.assertEqual([r.state for r in await self.real_rows()], ['off'])
        empty = self.result(await get(), 'no_events')
        self.assertEqual(empty['events'], [])
        self.assertEqual(empty['coverage'], {'start_utc': baseline, 'end_utc': baseline})
        on = await self.state('on')
        self.assertEqual(await self.state('on', {'changed_attributes_only': True}), on)
        off = await self.state('off')
        self.assertEqual([r.state for r in await self.real_rows()], ['off', 'on', 'off'])
        done = self.result(await get(), 'ok')
        self.assertEqual(done['scope'], empty['scope'])
        self.assertEqual(done['events'], [{'at_utc': off, 'state': 'off'}, {'at_utc': on, 'state': 'on'}])
        self.assertEqual(done['coverage'], {'start_utc': baseline, 'end_utc': off})
        self.assertFalse(done['limited'])
        self.assertIs(self.entry.options, before_options)
        self.assertFalse(store.exists())
        self.assertEqual(self.service_calls, [])

    async def test_gaps_and_registry_changes_cannot_manufacture_transitions(self):
        get, _ = await self.client()
        await self.state('off')
        await self.state('on')
        old = self.result(await get(), 'ok')['scope']
        await self.hass.services.async_call('recorder', 'disable', blocking=True)
        self.assertFalse(self.rec.enabled)
        await self.state('unavailable')
        self.assertEqual([r.state for r in await self.real_rows()], ['off', 'on'])
        self.result(await get(), 'unavailable')
        await self.hass.services.async_call('recorder', 'enable', blocking=True)
        self.assertTrue(self.rec.enabled)
        await self.state('off')
        renewed = self.result(await get(), 'no_events')
        self.assertNotEqual(renewed['scope'], old)
        # Gap with no switch event must still retire the previous hardware sample.
        await self.hass.services.async_call('recorder', 'disable', blocking=True)
        await self.hass.services.async_call('recorder', 'enable', blocking=True)
        await self.state('on')
        self.result(await get(), 'no_events')
        await self.state('unavailable')
        self.result(await get(), 'unavailable')
        await self.state('off')
        self.result(await get(), 'no_events')
        renamed = 'switch.same_row_renamed'
        self.registry.async_update_entity(ALARM, new_entity_id=renamed)
        await self.hass.async_block_till_done()
        self.registry.async_get_or_create('switch', 'water_history_fixture', 'different_row',
                                          suggested_object_id=ALARM.partition('.')[2])
        await self.state('on')
        self.result(await get(), 'unavailable')
        await self.state('off', entity_id=renamed)
        self.result(await get(), 'no_events')
        await self.state('on', entity_id=renamed)
        self.result(await get(), 'ok')
        self.registry.async_remove(renamed)
        await self.hass.async_block_till_done()
        self.registry.async_get_or_create('switch', 'water_history_fixture', 'reuse_renamed',
                                          suggested_object_id=renamed.partition('.')[2])
        await self.state('on', entity_id=renamed)
        self.result(await get(), 'unavailable')
        self.assertEqual(self.service_calls, [('recorder', 'disable'), ('recorder', 'enable'),
                                              ('recorder', 'disable'), ('recorder', 'enable')])

    async def test_non_admin_and_exact_owned_projection_and_output_are_independently_checked(self):
        get, user = await self.client()
        reader, _ = await self.client('system-read-only')
        none = await self.make_entry()
        excluded = await self.make_entry(EXCLUDED)
        await self.state('off', entity_id=EXCLUDED)
        self.assertEqual(await self.real_rows(EXCLUDED), [])
        self.error(await reader(), 'unauthorized')
        self.error(await get(entity_id=ALARM), 'invalid_format')
        self.error(await get(zone_id=none[1]), 'unknown_tank')
        self.error(await get(tank_id=none[2]), 'unknown_tank')
        unbound = self.result(await get(none), 'no_binding')
        self.assertEqual(self.result(await get(none), 'no_binding')['scope'], unbound['scope'])
        unavailable = self.result(await get(excluded), 'unavailable')
        self.assertNotEqual(unavailable['scope'], unbound['scope'])
        self.assertFalse(unavailable['events'])
        await self.state('off')
        projection = self.registry.async_get_entity_id('sensor', DOMAIN, self.tank)
        check = user.permissions.check_entity
        for denied in (projection, ALARM):
            with self.subTest(denied=denied), patch.object(user.permissions, 'check_entity', side_effect=lambda entity, key: entity != denied and check(entity, key)):
                self.error(await get(), 'unauthorized')
        self.result(await get(), 'no_events')
        self.assertEqual(self.service_calls, [])

    async def test_held_real_recorder_read_limits_connection_and_rechecks_demotion_or_stale_binding(self):
        await self.state('off')
        await self.state('on')
        get, user = await self.client()
        other, _ = await self.client()
        self.result(await get(), 'ok')
        native = self.rec.async_add_executor_job
        for cause in ('demotion', 'rename', 'unload'):
            with self.subTest(cause=cause):
                reached, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def hold(job, *args):
                    value = await native(job, *args)
                    calls.append(value)
                    reached.set()
                    await release.wait()
                    return value

                with patch.object(self.rec, 'async_add_executor_job', hold):
                    pending = asyncio.create_task(get())
                    try:
                        await asyncio.wait_for(reached.wait(), 3)
                        self.error(await get(), 'history_busy')
                        if cause == 'demotion':
                            await self.hass.auth.async_update_user(user, group_ids=['system-read-only'])
                        elif cause == 'rename':
                            self.registry.async_update_entity(ALARM, new_entity_id='switch.moved')
                        else:
                            self.assertTrue(await self.hass.config_entries.async_unload(self.entry.entry_id))
                    finally:
                        release.set()
                        received = await pending
                self.assertEqual(len(calls), 1)
                self.error(received, {'demotion': 'unauthorized', 'rename': 'stale_history', 'unload': 'entry_not_loaded'}[cause])
                if cause == 'demotion':
                    self.result(await other(), 'ok')
                    get = other
                if cause == 'rename':
                    await self.state('off', entity_id='switch.moved')
                    await self.state('on', entity_id='switch.moved')
        self.assertEqual(self.service_calls, [])

    async def test_native_query_and_returned_recent_edges_are_bounded(self):
        from homeassistant.components.recorder import history

        get, _ = await self.client()
        stamps = []
        for i in range(270):
            value = 'on' if i % 2 else 'off'
            self.hass.states.async_set(ALARM, value)
            stamp = self.hass.states.get(ALARM).last_changed.isoformat(timespec='microseconds').replace('+00:00', 'Z')
            stamps.append({'at_utc': stamp, 'state': value})
        await self.commit()
        self.assertEqual(len({row['at_utc'] for row in stamps}), 270)
        with patch.object(history, 'state_changes_during_period', wraps=history.state_changes_during_period) as query:
            result = self.result(await get(), 'ok')
            query.assert_called_once()
            self.assertEqual(query.call_args.kwargs, {'entity_id': ALARM, 'no_attributes': True,
                                                       'include_start_time_state': False, 'limit': 257})
        self.assertTrue(result['limited'])
        self.assertEqual(result['events'], list(reversed(stamps[-50:])))
        self.assertEqual(result['coverage'], {'start_utc': stamps[-256]['at_utc'], 'end_utc': stamps[-1]['at_utc']})
        self.assertEqual(self.service_calls, [])

    async def test_named_only_and_unmapped_protected_alarm_refuse_same_recorder_evidence(self):
        get, user = await self.client()
        # Inventory, native protected roles and Recorder share the same source
        # rows; only the currently mapped protected scope can use its evidence.
        protected_output = self.registry.async_get_or_create('switch', 'water_history_fixture', 'protected',
                                                              suggested_object_id='protected_history_alarm').entity_id
        await self.state('off', entity_id=protected_output)
        protected = await self.make_entry(protected_output, protected=True)
        invalid = await self.make_entry(protected_output, protected=False)
        legacy = await self.make_entry(protected_output, registered=False)
        self.assertIn('alarm', protected[0].runtime_data.roles)
        self.assertNotIn('alarm', invalid[0].runtime_data.roles)
        self.result(await get(protected), 'unavailable')
        self.hass.states.async_set(protected_output, 'off', force_update=True)
        await self.commit()
        self.result(await get(protected), 'no_events')
        self.result(await get(invalid), 'unavailable')
        self.result(await get(legacy), 'unavailable')
        with patch.object(user.permissions, 'check_entity', side_effect=lambda entity, _: entity != protected_output):
            self.error(await get(invalid), 'unauthorized')
        self.assertEqual(self.service_calls, [])
