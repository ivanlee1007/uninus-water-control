"""Offline native regressions for conflict admission and selector identity."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4
from unittest import IsolatedAsyncioTestCase, TestCase

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

import test_native_actuation as actuation
import test_native_water_control as native
import test_native_topology_migration as migration
from water_settings_ws import compare, settings_client


class TestBindingConflicts(IsolatedAsyncioTestCase):
    asyncSetUp = actuation.NativeActuationTest.asyncSetUp
    boot_core = actuation.NativeActuationTest.boot_core
    make_output = actuation.NativeActuationTest.make_output
    set_depth = actuation.NativeActuationTest.set_depth
    release_ack = actuation.NativeActuationTest.release_ack
    handle_switch = actuation.NativeActuationTest.handle_switch
    configure = actuation.NativeActuationTest.configure
    submit_tank = actuation.NativeActuationTest.submit_tank
    tank_form = actuation.NativeActuationTest.tank_form
    tank_fields = actuation.NativeActuationTest.tank_fields
    press = actuation.NativeActuationTest.press
    next_call = actuation.NativeActuationTest.next_call
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit

    async def test_passive_add_cannot_disable_commissioned_refill(self):
        await self.configure(commission_refill=True, confirm_device_cutoff=True)
        entry = self.entry
        owner = entry.runtime_data
        self.assertTrue(owner.roles['refill'].can_start())
        before = entry.data, entry.options
        effects = list(self.effects)
        form = await self.submit(await self.option(entry, 'add_tank'), zone_id=entry.data['zones'][0]['id'])
        result = await self.submit(form, tank_name='Passive clone', level_sensor='sensor.backup', refill_switch=self.refill)
        self.assertEqual(result['type'], FlowResultType.FORM, result)
        self.assertEqual(result['errors'], {'refill_switch': 'shared_output'})
        self.assertIs(entry.data, before[0])
        self.assertIs(entry.options, before[1])
        self.assertIs(entry.runtime_data, owner)
        self.assertTrue(owner.roles['refill'].can_start())
        self.assertEqual(self.effects, effects)
        self.hass.config_entries.options.async_abort(form['flow_id'])
        self.assertIs(entry.options, before[1])
        await self.press('refill', 'start')
        await self.next_call(self.refill, 'on')
        await self.press('refill', 'stop')
        await self.next_call(self.refill, 'off')


class TestPassiveOutputIdentity(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeWaterControlTest.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit
    edit_tank = native.NativeWaterControlTest.edit_tank

    @staticmethod
    def suggestion(form, field):
        return next(key for key in form['data_schema'].schema if key == field).description.get('suggested_value')

    async def echoed_identity(self, operation, field):
        registry = er.async_get(self.hass)
        original = registry.async_get_or_create('switch', 'test_probe', 'original', suggested_object_id='passive')
        self.hass.states.async_set(original.entity_id, 'off')
        entry = None
        fields = {'tank_name': 'Corrected', 'level_sensor': 'sensor.backup', field: original.entity_id}
        if operation in ('user', 'add_zone'):
            fields['zone_name'] = 'New zone'
        if operation == 'user':
            manager = self.hass.config_entries.flow
            form = await manager.async_init(native.DOMAIN, context={'source': 'user'})
        else:
            manager = self.hass.config_entries.options
            entry = await self.setup_controller()
            before = entry.data, entry.options
            form = await self.option(entry, operation)
            zone = entry.data['zones'][0]
            if operation == 'add_tank':
                form = await self.submit(form, zone_id=zone['id'])
            elif operation == 'edit_tank':
                form = await self.submit(form, tank_id=zone['tanks'][0]['id'])
        invalid = await manager.async_configure(form['flow_id'], fields | {'tank_name': ' '})
        self.assertEqual(invalid['errors'], {'tank_name': 'blank_name'})
        self.assertEqual(self.suggestion(invalid, field), original.entity_id)
        moved = registry.async_update_entity(original.entity_id, new_entity_id='switch.original_moved')
        self.hass.states.async_remove(original.entity_id)
        replacement = registry.async_get_or_create('switch', 'test_probe', 'replacement', suggested_object_id='passive')
        self.assertEqual(replacement.entity_id, original.entity_id)
        self.hass.states.async_set(moved.entity_id, 'off')
        self.hass.states.async_set(replacement.entity_id, 'off')
        for _ in range(2):
            rejected = await manager.async_configure(form['flow_id'], fields)
            self.assertEqual(rejected['type'], FlowResultType.FORM, rejected)
            self.assertEqual(rejected['errors'], {field: 'output_source_changed'})
            self.assertIsNone(self.suggestion(rejected, field))
            if entry:
                self.assertIs(entry.data, before[0])
                self.assertIs(entry.options, before[1])
            else:
                self.assertEqual(self.hass.config_entries.async_entries(native.DOMAIN), [])
        saved = await manager.async_configure(form['flow_id'], fields | {field: moved.entity_id})
        self.assertEqual(saved['type'], FlowResultType.CREATE_ENTRY, saved)
        await self.hass.async_block_till_done()
        entry, = self.hass.config_entries.async_entries(native.DOMAIN)
        tank, = [t for z in (entry.options or entry.data)['zones'] for t in z['tanks'] if t['name'] == 'Corrected']
        self.assertEqual(tank[f'{field}_registry_id'], original.id)
        # An intentionally reopened form can explicitly choose the replacement.
        reopened = await self.edit_tank(entry, tank['id'])
        self.assertEqual(self.suggestion(reopened, field), moved.entity_id)
        result = await self.submit(reopened, **{k: v for k, v in fields.items() if k != 'zone_name'})
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        await self.hass.async_block_till_done()
        tank, = [t for z in entry.options['zones'] for t in z['tanks'] if t['name'] == 'Corrected']
        self.assertEqual(tank[f'{field}_registry_id'], replacement.id)

    async def test_legacy_name_only_output_does_not_suggest_a_new_registry_owner(self):
        entry = await self.setup_controller(refill_switch='switch.refill')
        tank = entry.data['zones'][0]['tanks'][0]
        self.assertNotIn('refill_switch_registry_id', tank)
        self.hass.states.async_remove('switch.refill')
        replacement = er.async_get(self.hass).async_get_or_create(
            'switch', 'test_probe', 'replacement', suggested_object_id='refill')
        self.assertEqual(replacement.entity_id, 'switch.refill')
        form = await self.edit_tank(entry, tank['id'])
        self.assertIsNone(self.suggestion(form, 'refill_switch'))
        before = entry.data, entry.options
        self.hass.config_entries.options.async_abort(form['flow_id'])
        self.assertIs(entry.data, before[0])
        self.assertIs(entry.options, before[1])
        form = await self.edit_tank(entry, tank['id'])
        result = await self.submit(form, tank_name='Explicit selection', level_sensor='sensor.depth',
                                   refill_switch=replacement.entity_id)
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        self.assertEqual(entry.options['zones'][0]['tanks'][0]['refill_switch_registry_id'], replacement.id)

    async def default_identity(self, field, consent_error=False):
        registry = er.async_get(self.hass)
        original = registry.async_get_or_create('switch', 'test_probe', 'original', suggested_object_id='passive')
        self.hass.states.async_set(original.entity_id, 'off')
        if consent_error and field == 'refill_switch':
            self.hass.states.async_remove('sensor.depth')
            registry.async_get_or_create('sensor', 'test_probe', 'depth', suggested_object_id='depth')
            self.set_depth('sensor.depth', '1.25', 'm')
        entry = await self.setup_controller(**({} if consent_error else {field: original.entity_id}))
        tank = entry.data['zones'][0]['tanks'][0]
        before = entry.data, entry.options
        fields = {'tank_name': 'Rename only', 'level_sensor': 'sensor.depth', field: original.entity_id}
        form = await self.edit_tank(entry, tank['id'])
        if consent_error:
            fields[f'commission_{field.removesuffix("_switch")}'] = True
            form = await self.submit(form, **fields)
            self.assertEqual(form['errors'], {'confirm_device_cutoff': 'cutoff_required'})
            fields['confirm_device_cutoff'] = True
        self.assertEqual(self.suggestion(form, field), original.entity_id)
        moved = registry.async_update_entity(original.entity_id, new_entity_id='switch.moved')
        self.hass.states.async_remove(original.entity_id)
        replacement = registry.async_get_or_create('switch', 'test_probe', 'replacement', suggested_object_id='passive')
        self.assertEqual(replacement.entity_id, original.entity_id)
        for _ in range(2):
            denied = await self.submit(form, **fields)
            self.assertEqual(denied['type'], FlowResultType.FORM, denied)
            self.assertEqual(denied['errors'], {field: 'output_source_changed'})
            self.assertEqual(self.suggestion(denied, field), None if consent_error else moved.entity_id)
            self.assertIs(entry.data, before[0])
            self.assertIs(entry.options, before[1])
        fields.pop(f'commission_{field.removesuffix("_switch")}', None)
        result = await self.submit(form, **(fields | {field: moved.entity_id}))
        self.assertEqual(result['type'], FlowResultType.CREATE_ENTRY, result)
        self.assertEqual(entry.options['zones'][0]['tanks'][0][f'{field}_registry_id'], original.id)


def _identity_case(operation, field):
    async def case(self):
        await self.echoed_identity(operation, field)
    return case


for _operation in ('user', 'add_zone', 'add_tank', 'edit_tank'):
    for _field in ('refill_switch', 'alarm_switch'):
        setattr(TestPassiveOutputIdentity, f'test_echo_{_operation}_{_field}', _identity_case(_operation, _field))


def _default_case(field, consent):
    async def case(self):
        await self.default_identity(field, consent)
    return case


for _field in ('refill_switch', 'alarm_switch'):
    for _consent in (False, True):
        setattr(TestPassiveOutputIdentity, f'test_default_{_field}_consent_{_consent}', _default_case(_field, _consent))


async def owned_conflicts(root, entry_id):
    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr, frame
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
        owner = entry.runtime_data
        assert owner.roles['refill'].can_start()
        image = Path(owner.settings.store.path)
        core = root / '.storage/core.config_entries'
        before = image.read_bytes(), core.read_bytes(), entry.data, entry.options
        request, _ = await settings_client(hass, entry)
        opened = await request('get', type=f'{native.DOMAIN}/topology_get')
        assert opened['success'], opened
        snapshot = opened['result']
        zones = deepcopy(snapshot['zones'])
        for zone in zones:
            for tank in zone['tanks']:
                tank.pop('protected')
        first, second = zones[0]['tanks']
        for field, error in [('level_sensor', 'shared_source'), ('refill_switch', 'shared_output')]:
            proposed = deepcopy(zones)
            proposed[0]['tanks'][1][field] = first[field]
            refused = await request('save', type=f'{native.DOMAIN}/topology_save', **compare(snapshot),
                                    zones=proposed, remove={'zones': [], 'tanks': []}, changes={})
            assert refused['error']['code'] == 'binding_conflict', refused
            menu = await bank.options.async_init(entry_id)
            form = await bank.options.async_configure(menu['flow_id'], {'next_step_id': 'edit_tank'})
            form = await bank.options.async_configure(form['flow_id'], {'tank_id': second['id']})
            fields = {'tank_name': 'Passive duplicate', 'level_sensor': 'sensor.backup'}
            fields[field] = first[field]['entity_id']
            denied = await bank.options.async_configure(form['flow_id'], fields)
            assert denied['type'] is FlowResultType.FORM and denied['errors'] == {field: error}, denied
            bank.options.async_abort(form['flow_id'])
            assert image.read_bytes() == before[0] and core.read_bytes() == before[1]
            assert entry.data is before[2] and entry.options is before[3] and entry.runtime_data is owner
            assert owner.roles['refill'].can_start() and calls == []
            current = await request('get', type=f'{native.DOMAIN}/topology_get')
            assert current['success'] and current['result'] == snapshot
        print('OWNED_CONFLICT_FENCE', json.dumps({'disk_unchanged': True, 'calls': calls, 'can_start': True}))
    finally:
        await hass.async_stop()


class TestOwnedBindingConflicts(IsolatedAsyncioTestCase):
    asyncSetUp = migration.TestNativeTopologyMigration.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    register_depth = native.NativeWaterControlTest.register_depth
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit
    saved = native.NativeWaterControlTest.saved
    edit_tank = native.NativeWaterControlTest.edit_tank
    _fresh = migration.TestNativeTopologyMigration._fresh

    async def test_owned_native_and_card_reject_passive_conflicts_without_writes(self):
        registry = er.async_get(self.hass)
        source = self.register_depth('original', object_id='migration_depth')
        self.hass.states.async_remove('sensor.backup')
        self.register_depth('backup', object_id='backup')
        output = registry.async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
        migration.signals(self.hass, [])
        entry = await self.setup_controller(level_sensor=source.entity_id, refill_switch=output.entity_id)
        zone = entry.data['zones'][0]
        await self.saved(await self.edit_tank(entry, zone['tanks'][0]['id']), tank_name='Protected',
                         level_sensor=source.entity_id, refill_switch=output.entity_id,
                         commission_refill=True, confirm_device_cutoff=True)
        form = await self.submit(await self.option(entry, 'add_tank'), zone_id=zone['id'])
        await self.saved(form, tank_name='Passive', level_sensor='sensor.backup')
        confirmed = await self.submit(await self.option(entry, migration.OPERATION), confirm_device_cutoff=True)
        self.assertEqual(confirmed['type'], FlowResultType.CREATE_ENTRY, confirmed)
        root = Path(self.hass.config.config_dir)
        await migration.disk_row(root / '.storage/core.config_entries', entry.entry_id,
                                 lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
        await self.hass.async_stop()
        await self._fresh(root, entry, 'prepare')
        child = await asyncio.create_subprocess_exec(sys.executable, '-B', str(Path(__file__).resolve()),
            'owned', str(root), entry.entry_id, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(child.communicate(), 60)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        log = output.decode('utf-8', errors='replace')
        print(log, end='')
        self.assertEqual(child.returncode, 0, log)
        self.assertIn('OWNED_CONFLICT_FENCE', log)


class TestBindingTranslations(TestCase):
    def test_config_and_options_have_all_binding_errors(self):
        for locale in ('en', 'zh-Hant'):
            document = json.loads((native.SOURCE / 'translations' / f'{locale}.json').read_text(encoding='utf-8'))
            for section in ('config', 'options'):
                for key in ('shared_output', 'shared_source', 'output_source_changed'):
                    self.assertTrue(document[section]['error'].get(key), (locale, section, key))


class TestLegacyBindingCompatibility(IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeWaterControlTest.asyncSetUp
    set_depth = native.NativeWaterControlTest.set_depth
    setup_controller = native.NativeWaterControlTest.setup_controller
    option = native.NativeWaterControlTest.option
    submit = native.NativeWaterControlTest.submit
    edit_tank = native.NativeWaterControlTest.edit_tank

    async def test_unchanged_legacy_sharing_survives_rename_but_new_sharing_is_rejected(self):
        registry = er.async_get(self.hass)
        self.hass.states.async_remove('sensor.depth')
        source = registry.async_get_or_create('sensor', 'test_probe', 'original_depth', suggested_object_id='depth')
        output = registry.async_get_or_create('switch', 'test_probe', 'original', suggested_object_id='passive')
        self.set_depth(source.entity_id, '1.25', 'm')
        self.hass.states.async_set(output.entity_id, 'off')
        entry = await self.setup_controller(refill_switch=output.entity_id)
        # Seed a preexisting legacy inventory, not a new writer bypass.
        zones = deepcopy(entry.data['zones'])
        original = zones[0]['tanks'][0]
        zones[0]['tanks'].append(dict(original, id=uuid4().hex, name='Existing shared tank'))
        self.hass.config_entries.async_update_entry(entry, options={'zones': zones})
        await self.hass.config_entries.async_reload(entry.entry_id)
        await self.hass.async_block_till_done()
        moved_source = registry.async_update_entity(source.entity_id, new_entity_id='sensor.moved')
        moved_output = registry.async_update_entity(output.entity_id, new_entity_id='switch.moved')
        form = await self.edit_tank(entry, original['id'])
        saved = await self.submit(form, tank_name='Metadata only', level_sensor=moved_source.entity_id,
                                  refill_switch=moved_output.entity_id)
        self.assertEqual(saved['type'], FlowResultType.CREATE_ENTRY, saved)
        await self.hass.async_block_till_done()
        before = entry.options
        form = await self.submit(await self.option(entry, 'add_tank'), zone_id=zones[0]['id'])
        denied = await self.submit(form, tank_name='New sharing', level_sensor=moved_source.entity_id,
                                   refill_switch=moved_output.entity_id)
        self.assertEqual(denied['type'], FlowResultType.FORM, denied)
        self.assertEqual(denied['errors'], {'level_sensor': 'shared_source', 'refill_switch': 'shared_output'})
        self.assertIs(entry.options, before)
        self.hass.config_entries.options.async_abort(form['flow_id'])
        # Distinct registry identities may intentionally reuse the vacated names.
        self.hass.states.async_remove(source.entity_id)
        self.hass.states.async_remove(output.entity_id)
        replacement_source = registry.async_get_or_create('sensor', 'test_probe', 'replacement_depth', suggested_object_id='depth')
        replacement_output = registry.async_get_or_create('switch', 'test_probe', 'replacement_output', suggested_object_id='passive')
        self.assertEqual(replacement_source.entity_id, source.entity_id)
        self.assertEqual(replacement_output.entity_id, output.entity_id)
        form = await self.edit_tank(entry, original['id'])
        saved = await self.submit(form, tank_name='Explicit independent mapping', level_sensor=replacement_source.entity_id,
                                  refill_switch=replacement_output.entity_id)
        self.assertEqual(saved['type'], FlowResultType.CREATE_ENTRY, saved)
        tank = entry.options['zones'][0]['tanks'][0]
        self.assertEqual(tank['level_sensor_registry_id'], replacement_source.id)
        self.assertEqual(tank['refill_switch_registry_id'], replacement_output.id)


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'owned':
    asyncio.run(owned_conflicts(Path(sys.argv[2]), sys.argv[3]))
