"""Native topology-admission, durable-readback and projection boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.util.file import WriteError

from test_native_card_topology import binding
import test_native_topology_migration as migration
from water_settings_ws import compare, settings_client


async def run(root, entry_id):
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
        registry = er.async_get(hass)
        migration.signals(hass, calls)
        third = registry.async_get_or_create('sensor', 'test_probe', 'third', suggested_object_id='test_third')
        hass.states.async_set(third.entity_id, '0.6', {'unit_of_measurement': 'm'})
        optional = registry.async_get_or_create('switch', 'test_probe', 'passive', suggested_object_id='passive')
        hass.states.async_set(optional.entity_id, 'off')
        await hass.async_start()
        assert await bank.async_setup(entry_id)
        owner = entry.runtime_data
        role = owner.roles['refill']
        settings = owner.settings
        request, actor = await settings_client(hass, entry)
        other, _ = await settings_client(hass, entry, 'system-users')
        get = f'{migration.DOMAIN}/topology_get'
        save = f'{migration.DOMAIN}/topology_save'

        async def opened():
            result = await request('get', type=get)
            assert result['success'], result
            return result['result']

        def editable(snapshot):
            draft = deepcopy(snapshot['zones'])
            for z in draft:
                for t in z['tanks']:
                    del t['protected']
            return draft

        def save_request(snapshot, zones, remove=None):
            return dict(type=save, **compare(snapshot), zones=zones, changes={},
                        remove=remove or {'zones': [], 'tanks': []})

        def add(level, *, name='Storage', refill=None):
            return {'id': None, 'name': name, 'level_sensor': binding(level), 'refill_switch': refill, 'alarm_switch': None}

        def projection(tank_id):
            entity_id = registry.async_get_entity_id('sensor', migration.DOMAIN, tank_id)
            return hass.states.get(entity_id) if entity_id else None

        before = await opened()
        first = before['zones'][0]['tanks'][0]
        assert first['protected']
        assert (await other('get', type=get))['error']['code'] == 'unauthorized'
        zones = editable(before)
        zones[0]['tanks'][0]['level_sensor'] = binding(third)
        original = image.read_bytes()
        assert (await request('save', **save_request(before, zones)))['error']['code'] == 'protected_topology'
        assert image.read_bytes() == original and owner.roles['refill'] is role
        zones = editable(before)
        zones[0]['name'] = '同名區'
        zones[0]['tanks'][0]['name'] = '同名桶'
        zones.append({'id': None, 'name': '同名區', 'tanks': [add(third, name='同名桶', refill=binding(optional))]})
        outcome = await request('save', **save_request(before, zones))
        assert outcome['success'], outcome
        saved = outcome['result']
        existing, added = saved['zones']
        assert existing['id'] == before['zones'][0]['id'] and added['id'] != existing['id']
        existing_tank, = existing['tanks']
        added_tank, = added['tanks']
        assert existing_tank['id'] == first['id'] and added_tank['id'] != first['id']
        assert existing_tank['name'] == added_tank['name'] == '同名桶'
        assert projection(first['id']).attributes['uninus_water_dashboard']['zone']['name'] == '同名區'
        assert projection(added_tank['id']).state == '0.6' and owner.roles['refill'] is role and role.can_start()
        assert (await request('save', **compare(before), changes={}))['error']['code'] == 'stale_revision'

        # The saved name belongs to a different switch after rename/reuse. An
        # exact rebind on this passive tank must neither rewrite the sibling
        # role's stored binding nor affect the commissioned role.
        devices = dr.async_get(hass)
        device_a = devices.async_get_or_create(config_entry_id=entry_id, identifiers={('test_probe', 'A')})
        device_b = devices.async_get_or_create(config_entry_id=entry_id, identifiers={('test_probe', 'B')})
        registry.async_update_entity(optional.entity_id, new_entity_id='switch.passive_renamed', device_id=device_a.id)
        switched = registry.async_get(optional.id)
        hass.states.async_remove(optional.entity_id)
        hass.states.async_set(switched.entity_id, 'off')
        reused = registry.async_get_or_create('switch', 'test_probe', 'reused_passive',
                                              suggested_object_id='passive', device_id=device_b.id)
        alias = registry.async_get_or_create('switch', 'test_probe', 'alias_passive', device_id=device_a.id)
        assert switched.entity_id != reused.entity_id and reused.entity_id == optional.entity_id
        saved = await opened()
        prior = image.read_bytes()
        for conflicting in (switched, alias):
            zones = editable(saved)
            zones[1]['tanks'][0]['alarm_switch'] = binding(conflicting)
            refused = await request('save', **save_request(saved, zones))
            assert refused['error']['code'] == 'binding_conflict' and image.read_bytes() == prior

        from custom_components.uninus_water_control.manual import output_conflicts

        legacy = [{'tanks': [{'id': added_tank['id'], 'refill_switch': reused.entity_id}]}]
        assert output_conflicts(registry, legacy, added_tank['id'], 'alarm', reused)
        legacy[0]['tanks'][0]['refill_switch'] = switched.entity_id
        assert output_conflicts(registry, legacy, added_tank['id'], 'alarm', alias)
        zones = editable(saved)
        zones[1]['tanks'][0]['alarm_switch'] = binding(reused)
        accepted = await request('save', **save_request(saved, zones))
        assert accepted['success'], ('different registered device must not conflict at a stale saved name', accepted)
        physical = json.loads(image.read_bytes())['data']['topology']['zones'][1]['tanks'][0]
        assert (physical['refill_switch'], physical['refill_switch_registry_id']) == (reused.entity_id, switched.id)
        assert (physical['alarm_switch'], physical['alarm_switch_registry_id']) == (reused.entity_id, reused.id)
        assert owner.roles['refill'] is role and role.can_start() and not calls
        saved = accepted['result']

        zones = editable(saved)
        backup = registry.async_get('sensor.backup')
        zones[1]['tanks'][0]['level_sensor'] = binding(backup)
        stale = save_request(saved, zones)
        registry.async_remove(backup.entity_id)
        hass.states.async_remove('sensor.backup')
        replacement = registry.async_get_or_create('sensor', 'test_probe', 'replacement', suggested_object_id='backup')
        assert replacement.entity_id == backup.entity_id and replacement.id != backup.id
        hass.states.async_set(replacement.entity_id, '2.4', {'unit_of_measurement': 'm'})
        unchanged = image.read_bytes()
        assert (await request('save', **stale))['error']['code'] == 'stale_source'
        assert image.read_bytes() == unchanged
        saved = await opened()
        zones = editable(saved)
        zones[1]['tanks'][0]['level_sensor'] = binding(replacement)
        zones[1]['tanks'][0]['refill_switch'] = None
        rebound = await request('save', **save_request(saved, zones))
        assert rebound['success'], rebound
        assert rebound['result']['zones'][1]['tanks'][0]['id'] == added_tank['id']
        assert projection(added_tank['id']).state == '2.4'

        saved = rebound['result']
        prior = image.read_bytes()
        zones = editable(saved)[:1]
        assert (await request('save', **save_request(saved, zones)))['error']['code'] == 'removal_confirmation_required'
        assert image.read_bytes() == prior
        remove = {'zones': [added['id']], 'tanks': [added_tank['id']]}
        payload = save_request(saved, zones, remove)
        with patch.object(settings.store, '_write_prepared_data', side_effect=WriteError('isolated topology fixture')) as failed:
            result = await request('save', **payload)
        assert failed.call_count == 1 and result['error']['code'] == 'readback_required'
        assert image.read_bytes() == prior and projection(added_tank['id']) is not None and await opened() == saved
        entered, release = Event(), Event()
        write = settings.store._write_prepared_data

        def hold(*args):
            entered.set()
            assert release.wait(3)
            return write(*args)

        with patch.object(settings.store, '_write_prepared_data', side_effect=hold):
            task = asyncio.create_task(request('save', **payload))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert settings.lock.locked() and not task.done()
                assert image.read_bytes() == prior and projection(added_tank['id']) is not None
            finally:
                release.set()
            result = await task
        assert result['success'], result
        assert not projection(added_tank['id']) and len(json.loads(image.read_bytes())['data']['topology']['zones']) == 1
        assert entry.runtime_data is owner and owner.roles['refill'] is role and role.can_start()

        # Lose the acknowledgement after the real write, before any registry/state
        # publication; fresh authenticated Get must reconcile without a second write.
        saved = result['result']
        zones = editable(saved)
        zones[0]['name'] = '讀回復原'
        old_projection = projection(first['id']).attributes['uninus_water_dashboard']['zone']['name']
        read = settings._async_read_file
        read_count = 0

        async def lose_second_read():
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                raise OSError('fixture lost read acknowledgement')
            return await read()

        with patch.object(settings, '_async_read_file', side_effect=lose_second_read), \
             patch.object(settings.store, 'async_save', wraps=settings.store.async_save) as writes:
            unknown = await request('save', **save_request(saved, zones))
            assert unknown['error']['code'] == 'readback_required'
            assert projection(first['id']).attributes['uninus_water_dashboard']['zone']['name'] == old_projection
            assert json.loads(image.read_bytes())['data']['topology']['zones'][0]['name'] == '讀回復原'
            recovered = await opened()
            assert writes.call_count == 1
        assert recovered['zones'][0]['name'] == '讀回復原'
        assert projection(first['id']).attributes['uninus_water_dashboard']['zone']['name'] == '讀回復原'
        assert entry.runtime_data is owner and owner.roles['refill'] is role and role.can_start()

        policy = await request('save', **compare(recovered), changes={'tanks': [
            {'id': first['id'], 'alarm': {'local': [1.6, 45]}}
        ]})
        assert policy['success'], policy
        alarm_id = registry.async_get_entity_id('binary_sensor', migration.DOMAIN, f"{first['id']}_low_level_alarm")
        alarm = settings.platforms['binary_sensor'].entities[alarm_id]
        interval, alarm_timer = alarm._interval, alarm._cancel_timer
        assert interval is not None and alarm_timer is not None and hass.states.get(alarm_id).state == 'off'

        async def press(action, state):
            button = registry.async_get_entity_id('button', migration.DOMAIN, f"{first['id']}_refill_{action}")
            await hass.services.async_call('button', 'press', {'entity_id': button}, blocking=True,
                                           context=Context(user_id=actor.id))
            async with asyncio.timeout(4):
                while not calls or calls[-1][0] != f'turn_{state}':
                    await asyncio.sleep(.01)
                hass.states.async_set('switch.probe_output', state)
                while (role.actual != state or role.on_task or role.off_task
                       or (not role.running_seen if state == 'on' else not role.closed)):
                    await asyncio.sleep(.01)

        # Refer only to the fixture's synthetic switch; the editor has no output
        # service. Observe an already running role before the ordinary write.
        await press('start', 'on')
        assert role.requested and role.running_seen
        command_timer = role._runtime_timer
        active = await opened()
        zones = editable(active)
        zones[0]['tanks'].append(add(replacement, name='監看桶'))
        accepted = await request('save', **save_request(active, zones))
        assert accepted['success'], accepted
        assert role is owner.roles['refill'] and role.actual == 'on' and role._runtime_timer is command_timer
        assert settings.platforms['binary_sensor'].entities[alarm_id] is alarm
        assert alarm._interval is interval and alarm._cancel_timer is alarm_timer
        assert calls == [['turn_on', 'switch.probe_output']]
        await press('stop', 'off')
        assert role.closed

        # A lost owned image is distinct from a rejected draft or a recoverable
        # acknowledgement. Neither registered topology route may recreate it.
        last = accepted['result']
        image.unlink()
        quarantine = await request('get', type=get)
        assert quarantine['error']['code'] == 'topology_recovery_required'
        blocked = await request('save', **save_request(last, editable(last)))
        assert blocked['error']['code'] == 'topology_recovery_required'
        assert not image.exists() and role.closed and not role.can_start()
    finally:
        await hass.async_stop()
    assert calls == [['turn_on', 'switch.probe_output'], ['turn_off', 'switch.probe_output']]
    assert core.read_bytes() == core_before
    print('NATIVE_TOPOLOGY_BOUNDARIES_OK', flush=True)


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
