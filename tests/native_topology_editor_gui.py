"""Native C display/Quick UI plus retained authenticated topology wire compatibility."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

# Reuse the product's native frontend harness and its only-loopback fixtures.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import verify_native_dashboard as gui
from playwright.async_api import async_playwright, expect

import test_native_topology_migration as migration


async def enter_dashboard_edit(page, fallback):
    label = await page.evaluate("document.querySelector('home-assistant').hass.localize('ui.panel.lovelace.menu.configure_ui')")
    tooltip = page.locator('hui-root ha-tooltip').filter(has_text=label)
    menu = page.locator('#dashboardmenu:visible')
    await expect(tooltip.or_(menu).first).to_be_attached()
    if label and await tooltip.count():
        ident = await tooltip.first.evaluate("el => el.for ?? el.getAttribute('for')")
        await page.locator(f'hui-root ha-icon-button#{ident}').click()
    else:
        await menu.click()
        await page.get_by_text(label or fallback, exact=True).click()


async def choose_entity(picker, entity_id):
    field = picker.locator('ha-picker-field')
    await expect(field).to_be_visible()
    # The native field delegates to an asynchronously rendered control. Await
    # that public focus operation before sending a trusted activation key.
    await field.evaluate('async el => { await el.updateComplete; await el.focus(); }')
    await picker.page.keyboard.press('Enter')
    search = picker.locator('ha-picker-combo-box input:visible').last
    await expect(search).to_be_editable()
    options = picker.locator('ha-combo-box-item:visible')
    await expect(options.first).to_be_visible()
    # HA may show the exact same entity in multiple recommendation sections.
    choice = options.filter(has_text=entity_id).first
    # Use the actual rendered option when available. Filtering while HA is
    # still virtualizing the initial list races its option/key computation.
    if not await choice.count():
        await search.fill(entity_id)
    await expect(choice).to_be_visible()
    await choice.click()
    await expect(picker.locator('ha-picker-combo-box')).to_have_count(0)
    await expect(picker).to_have_js_property('value', entity_id)


def observe_writes(page, writes):
    def record(raw):
        if isinstance(raw, str):
            msg = json.loads(raw)
            data = msg.get('service_data', {})
            if (msg.get('type') == 'call_service' and msg.get('domain') == 'system_log' and msg.get('service') == 'write'
                    and data.get('logger', '').startswith('frontend.js.modern.')
                    and "Cannot read properties of undefined (reading 'id')" in data.get('message', '')
                    and 'ha-picker-combo-box.ts' in data.get('message', '')):
                # Pinned frontend logs its own virtualizer error as a service
                # call. Keep its evidence, but don't classify it as a Card,
                # Lovelace, or switch writer. Picker value/closure and safe
                # persistence are still required independently.
                print('NATIVE_PICKER_DIAGNOSTIC known frontend virtualizer error (system_log.write)', flush=True)
                return
            if msg.get('type') in {f'{gui.DOMAIN}/config_save', f'{gui.DOMAIN}/topology_save', 'lovelace/config/save', 'call_service'}:
                writes.append(msg)
    page.on('websocket', lambda ws: ws.on('framesent', record))


async def run(root, entry_id):
    storage = root / '.storage'
    (storage / 'onboarding').write_text(json.dumps({
        'version': 4, 'minor_version': 1, 'key': 'onboarding',
        'data': {'done': ['user', 'core_config', 'integration', 'analytics']},
    }), encoding='utf-8')
    hass = gui.HomeAssistant(str(root))
    sys.path.insert(0, str(root))
    gui.loader.async_setup(hass)
    hass.config.skip_pip = True
    port = gui.loopback_port()
    url = f'http://127.0.0.1:{port}'
    core_path = storage / 'core.config_entries'
    image = storage / f'{gui.DOMAIN}.settings.{entry_id}'
    calls = []
    try:
        accounts = json.loads((storage / 'auth').read_bytes())['data']['users']
        sole_user, = (row['id'] for row in accounts if row['name'] == 'Separate test owner')
        config = {'homeassistant': {'auth_providers': [{'type': 'trusted_networks',
                  'trusted_networks': ['127.0.0.1/32'], 'trusted_users': {'127.0.0.1/32': [sole_user]},
                  'allow_bypass_login': True}]},
                  'http': {'server_host': '127.0.0.1', 'server_port': port}, 'frontend': {}}
        migration.signals(hass, calls)
        assert await gui.bootstrap.async_from_config_dict(config, hass) is hass
        user = await hass.auth.async_get_user(sole_user)
        assert user.is_admin
        await hass.async_start()
        await hass.async_block_till_done()
        entry = hass.config_entries.async_get_entry(entry_id)
        assert entry.state is gui.ConfigEntryState.LOADED, entry.state
        assert Path(sys.modules[f'custom_components.{gui.DOMAIN}'].__file__).resolve().is_relative_to(root)
        owner = entry.runtime_data
        existing_role = owner.roles['refill']
        assert existing_role.can_start(), existing_role.snapshot()
        asserted = (root / 'custom_components' / gui.DOMAIN / 'dashboard.js').read_bytes()
        assert asserted == (gui.ROOT / 'custom_components' / gui.DOMAIN / 'dashboard.js').read_bytes()
        before = image.read_bytes()
        registry = gui.er.async_get(hass)
        source = registry.async_get('sensor.backup')
        extra = registry.async_get_or_create('sensor', 'test_probe', 'browser_extra', suggested_object_id='browser_extra')
        spare = registry.async_get_or_create('switch', 'test_probe', 'browser_spare', suggested_object_id='browser_spare')
        hass.states.async_set(extra.entity_id, '0.84', {'unit_of_measurement': 'm'})
        hass.states.async_set(spare.entity_id, 'off')
        writes = []

        def core_entry_row():
            data = json.loads(core_path.read_bytes())['data']
            return next(row for row in data['entries'] if row['entry_id'] == entry_id)

        async def restrict(route):
            if route.request.url.startswith(url + '/'):
                await route.continue_()
            else:
                await route.abort()

        async with async_playwright() as playwright, await playwright.chromium.launch(
            executable_path=os.environ.get('WATER_CHROMIUM_EXECUTABLE'), headless=True,
        ) as browser:
            async with await browser.new_context(locale='en-US', viewport={'width': 1180, 'height': 920}) as ctx:
                await ctx.route('**/*', restrict)
                page = await ctx.new_page()
                page.set_default_timeout(9000)
                await page.add_init_script("""
                    window.waterEditorDialogEvents = [];
                    addEventListener('dialog-closed', e => waterEditorDialogEvents.push(['close', e.detail?.dialog, performance.now()]), true);
                """)
                observe_writes(page, writes)
                await gui.native_login(page, url)
                await page.evaluate(gui.SETTINGS_CALL, {'type': 'lovelace/resources/create', 'res_type': 'module', 'url': gui.URL_PATH})
                await page.evaluate(gui.SETTINGS_CALL, {'type': 'lovelace/config/save', 'config': {'title': gui.TITLE, 'views': [
                    {'title': gui.TITLE, 'path': 'topology-editor', 'type': 'panel', 'cards': [{'type': f'custom:{gui.CARD_TAG}'}]},
                ]}})
                dashboard = url + '/lovelace/topology-editor'
                async with page.expect_response(url + gui.URL_PATH) as asset:
                    await page.goto(dashboard, wait_until='domcontentloaded')
                assert (await asset.value).ok and await (await asset.value).body() == asserted
                shell = page.locator(f'hui-panel-view {gui.CARD_TAG}:visible')
                try:
                    await expect(shell.get_by_role('button', name='Storage · 1.25 m', exact=True)).to_be_visible(timeout=12000)
                except AssertionError:
                    print('NATIVE_GUI_DIAGNOSTIC', await page.evaluate("""() => {
                      const app = document.querySelector('home-assistant');
                      return {path:location.pathname, stateCount:Object.keys(app?.hass?.states ?? {}).length,
                        projections:Object.values(app?.hass?.states ?? {}).filter(s=>s.attributes.uninus_water_dashboard || s.attributes.uninus_water_manual)
                          .map(s=>[s.state,s.attributes.uninus_water_dashboard?.tank?.name,s.attributes.uninus_water_manual?.action]),
                        cardDefined:!!customElements.get('uninus-water-dashboard')};
                    }"""), await shell.count(), flush=True)
                    raise
                await expect(shell.get_by_role('button', name='手動補水', exact=True)).to_be_enabled()
                yaml_before = await page.evaluate(gui.SETTINGS_CALL, {'type': 'lovelace/config'})
                core_before = core_entry_row()
                writes.clear()
                # Lovelace has no operational/topology writer in architecture C.
                await enter_dashboard_edit(page, 'Edit dashboard')
                modal, display = await gui.open_card_editor(page, 'Edit')
                preview = modal.get_by_role('button', name='手動補水', exact=True)
                await expect(preview).to_be_disabled()
                await preview.dispatch_event('click')
                await expect(display.get_by_role('button', name='編輯區域與水桶', exact=True)).to_have_count(0)
                await expect(display.get_by_role('spinbutton')).to_have_count(0)
                await display.get_by_role('checkbox', name='顯示詳細狀態', exact=True).check()
                assert not writes and image.read_bytes() == before
                await modal.get_by_role('button', name='Cancel', exact=True).press('Enter')
                await expect(modal).to_have_count(0)
                assert not writes and image.read_bytes() == before
                await page.locator('hui-root .exit-edit-mode').click()
                await expect(shell.get_by_role('button', name='手動補水', exact=True)).to_be_enabled()

                # The old topology GUI has been retired, not secretly kept as an
                # operational editor. Preserve exact backend compatibility checks
                # through the authenticated native WS. Native Options picker CRUD
                # acceptance is a separate integration-owner gate.
                topology = await get_topology(page, entry_id)
                zones = writable_zones(topology)
                zones[0]['tanks'].append({'id': None, 'name': '新水桶',
                    'level_sensor': {'entity_id': source.entity_id, 'registry_id': source.id},
                    'refill_switch': None, 'alarm_switch': None})
                await save_topology(page, topology, zones)
                write, = writes
                assert write['type'] == f'{gui.DOMAIN}/topology_save' and write['entry_id'] == entry_id
                assert write['zones'][0]['tanks'][-1]['level_sensor'] == {'entity_id': source.entity_id, 'registry_id': source.id}
                stored = json.loads(image.read_bytes())['data']
                new = stored['topology']['zones'][0]['tanks'][-1]
                assert new['name'] == '新水桶' and new['refill_switch'] is None and new['alarm_switch'] is None
                assert new['level_sensor_registry_id'] == source.id and entry.runtime_data is owner and owner.roles['refill'] is existing_role
                assert not calls and core_entry_row() == core_before
                native = registry.async_get_entity_id('sensor', gui.DOMAIN, new['id'])
                assert hass.states.get(native).attributes['uninus_water_dashboard']['tank']['id'] == new['id']
                await expect(shell.get_by_role('button', name='新水桶 · 2.40 m', exact=True)).to_be_visible()
                assert await page.evaluate(gui.SETTINGS_CALL, {'type': 'lovelace/config'}) == yaml_before
                assert len(writes) == 1
                initial_write_count = len(writes)
                await topology_compatibility_crud(page, entry_id, image, new, extra, spare)
                # Registered topology writes refresh real native names, not only DTO labels.
                for zone in owner.zones:
                    for tank in zone['tanks']:
                        entity_id = registry.async_get_entity_id('sensor', gui.DOMAIN, tank['id'])
                        row = registry.async_get(entity_id)
                        expected_name = f"{zone['name']} · {tank['name']} water level"
                        assert row.original_name == expected_name
                        assert hass.states.get(entity_id).attributes['friendly_name'] == expected_name
                manual_row = registry.async_get(registry.async_get_entity_id('sensor', gui.DOMAIN, f"{owner.tank_id}_refill_status"))
                assert manual_row.original_name == '同名區 · Storage refill status'
                print('NATIVE_TOPOLOGY_NAMES_OK registered rename refreshes real Core names', flush=True)
                replacement = await same_name_replacement(page, entry_id, image, registry, hass, source, native)
                writes.clear()
                await revision_conflict(page, ctx, dashboard, image, entry, new['id'], writes)
                assert entry.runtime_data is owner and owner.roles['refill'] is existing_role and not calls
                assert core_entry_row() == core_before
        print('NATIVE_TOPOLOGY_GUI', json.dumps({'id': new['id'], 'entity_id': native, 'writes': initial_write_count,
              'replacement': replacement.id, 'calls': calls}), flush=True)
    finally:
        await hass.async_stop()


async def get_topology(page, entry_id):
    return await page.evaluate(gui.SETTINGS_CALL, {'type': f'{gui.DOMAIN}/topology_get', 'version': 1, 'entry_id': entry_id})


def writable_zones(topology):
    return [{'id': z['id'], 'name': z['name'], 'tanks': [
        {k: v for k, v in t.items() if k != 'protected'} for t in z['tanks']]} for z in topology['zones']]


async def save_topology(page, baseline, zones, remove=None):
    payload = {'type': f'{gui.DOMAIN}/topology_save', 'version': 1, 'entry_id': baseline['entry_id'],
        'if_revision': baseline['revision'], 'if_source_snapshot': baseline['source_snapshot'],
        'zones': zones, 'remove': remove or {'zones': [], 'tanks': []}, 'changes': {}}
    saved = await page.evaluate(gui.SETTINGS_CALL, payload)
    assert await get_topology(page, baseline['entry_id']) == saved
    return saved


async def topology_compatibility_crud(page, entry_id, image, existing, extra, spare):
    baseline = await get_topology(page, entry_id)
    zones = writable_zones(baseline)
    zones[0]['name'] = '同名區'
    zones[0]['tanks'][1]['name'] = '已改名桶'
    zones[0]['tanks'][1]['alarm_switch'] = {'entity_id': spare.entity_id, 'registry_id': spare.id}
    zones.append({'id': None, 'name': '同名區', 'tanks': [{'id': None, 'name': '已改名桶',
        'level_sensor': {'entity_id': extra.entity_id, 'registry_id': extra.id}, 'refill_switch': None, 'alarm_switch': None}]})
    baseline = await save_topology(page, baseline, zones)
    stored = json.loads(image.read_bytes())['data']['topology']['zones']
    assert [zone['name'] for zone in stored] == ['同名區', '同名區']
    assert stored[0]['tanks'][1]['id'] == existing['id'] and stored[0]['tanks'][1]['name'] == '已改名桶'
    assert stored[0]['tanks'][1]['alarm_switch_registry_id'] == spare.id
    second, = stored[1]['tanks']
    assert second['name'] == '已改名桶' and second['id'] != existing['id']
    zones = writable_zones(baseline)
    removed = zones.pop()
    exact_remove = {'zones': [removed['id']], 'tanks': [second['id']]}
    baseline = await save_topology(page, baseline, zones, exact_remove)
    assert json.loads(image.read_bytes())['data']['topology']['zones'] == stored[:1]
    zones = writable_zones(baseline)
    zones[0]['tanks'].append({'id': None, 'name': '暫存桶',
        'level_sensor': {'entity_id': extra.entity_id, 'registry_id': extra.id}, 'refill_switch': None, 'alarm_switch': None})
    baseline = await save_topology(page, baseline, zones)
    spare_tank = json.loads(image.read_bytes())['data']['topology']['zones'][0]['tanks'][-1]
    assert spare_tank['name'] == '暫存桶' and spare_tank['id'] != second['id']
    zones = writable_zones(baseline)
    zones[0]['tanks'].pop()
    await save_topology(page, baseline, zones, {'zones': [], 'tanks': [spare_tank['id']]})
    assert json.loads(image.read_bytes())['data']['topology']['zones'] == stored[:1]
    await expect(page.locator(f'hui-panel-view {gui.CARD_TAG}:visible').get_by_role('button', name='已改名桶 · 2.40 m', exact=True)).to_be_visible()
    print('NATIVE_TOPOLOGY_COMPATIBILITY_CRUD_OK', flush=True)


async def same_name_replacement(page, entry_id, image, registry, hass, source, projection):
    ident = source.entity_id
    initial = image.read_bytes()
    registry.async_remove(ident)
    hass.states.async_remove(ident)
    replacement = registry.async_get_or_create('sensor', 'test_probe', 'browser_replacement', suggested_object_id='backup')
    assert replacement.entity_id == ident and replacement.id != source.id
    hass.states.async_set(ident, '0.93', {'unit_of_measurement': 'm', 'friendly_name': '試驗替代水位'})
    async with asyncio.timeout(5):
        while hass.states.get(projection).state != 'unavailable':
            await asyncio.sleep(.01)
    assert image.read_bytes() == initial
    baseline = await get_topology(page, entry_id)
    assert baseline['zones'][0]['tanks'][-1]['level_sensor'] == {'entity_id': None, 'registry_id': source.id}
    assert baseline['zones'][0]['tanks'][0]['protected']
    # An explicit native authenticated compatibility request, not an alias fallback.
    zones = writable_zones(baseline)
    zones[0]['tanks'][-1]['level_sensor'] = {'entity_id': ident, 'registry_id': replacement.id}
    await save_topology(page, baseline, zones)
    old_tank, new_tank = json.loads(initial)['data']['topology']['zones'][0]['tanks']
    old_readback, new_readback = json.loads(image.read_bytes())['data']['topology']['zones'][0]['tanks']
    assert old_readback == old_tank and new_readback == new_tank | {'level_sensor_registry_id': replacement.id}
    assert hass.states.get(projection).state == '0.93'
    await expect(page.locator(f'hui-panel-view {gui.CARD_TAG}:visible').get_by_role('button', name='已改名桶 · 0.93 m', exact=True)).to_be_visible()
    return replacement


async def revision_conflict(page, ctx, dashboard, image, entry, tank_id, writes):
    modal, quick = await gui.open_quick_parameters(page, tank_id)
    draft = quick.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    await draft.fill('2.75')
    before = await gui.read_settings(page, entry)
    assert not writes
    other = await ctx.new_page()
    await gui.native_login(other, dashboard)
    latest = await gui.publish_settings(other, before, {'tanks': [{'id': tank_id, 'scale_max_m': 4.75}]})
    await other.close()
    assert latest['revision'] != before['revision'] and latest['source_snapshot'] == before['source_snapshot']
    physical = image.read_bytes()
    apply = quick.get_by_role('button', name='儲存此桶參數', exact=True)
    await apply.click()
    await expect(quick.get_by_text('設定已被變更', exact=False)).to_be_visible()
    await expect(apply).to_be_disabled()
    write, = writes
    assert write['type'] == f'{gui.DOMAIN}/config_save'
    assert (write['if_revision'], write['if_source_snapshot']) == (before['revision'], before['source_snapshot'])
    assert write['changes'] == {'tanks': [{'id': tank_id, 'scale_max_m': 2.75}]}
    assert image.read_bytes() == physical and await draft.input_value() == '2.75'
    review = quick.get_by_role('button', name='讀取目前設定（保留草稿）', exact=True)
    await review.click()
    panel = quick.get_by_role('region', name='整合目前設定（唯讀）', exact=True)
    await expect(panel.get_by_text('水位顯示量程：4.75 m', exact=True)).to_be_visible()
    # Bindings are deliberately not editable/read out through Quick parameters.
    topology = await get_topology(page, entry.entry_id)
    assert topology['zones'][0]['tanks'][-1]['level_sensor']['entity_id'] == 'sensor.backup'
    await expect(draft).to_have_value('2.75'); await expect(draft).to_be_disabled()
    assert len(writes) == 1 and image.read_bytes() == physical
    if screenshots := os.environ.get('WATER_NATIVE_CONFLICT_CAPTURE'):
        target = Path(screenshots)
        assert target.is_absolute(); target.mkdir(parents=True, exist_ok=True)
        for width, height, label in ((1180, 920, 'wide'), (390, 844, 'narrow')):
            await page.set_viewport_size({'width': width, 'height': height})
            current = panel.get_by_text('水位顯示量程：4.75 m', exact=True)
            await current.scroll_into_view_if_needed(); await expect(current).to_be_in_viewport()
            await page.screenshot(path=target / f'conflict-c-{label}.png', animations='disabled')
    await quick.get_by_role('button', name='重新讀取（捨棄草稿）', exact=True).click()
    await expect(draft).to_have_value('4.75'); await expect(panel).to_have_count(0); await expect(apply).to_be_disabled()
    assert len(writes) == 1 and image.read_bytes() == physical
    await modal.get_by_role('button', name='取消', exact=True).click()
    assert len(writes) == 1 and image.read_bytes() == physical
    print('NATIVE_CONFLICT_OK real Quick stale_revision, original CAS/draft preserved, explicit current review/discard, zero replay', flush=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
