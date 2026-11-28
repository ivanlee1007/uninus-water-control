"""Actual native Options and Core Backup API on an owned loopback server."""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
from tempfile import TemporaryDirectory, gettempdir

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import verify_native_dashboard as gui
from homeassistant.backup_restore import RESTORE_BACKUP_RESULT_FILE, restore_backup
from homeassistant.const import RESTART_EXIT_CODE
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from playwright.async_api import async_playwright, expect

import test_native_topology_migration as migration

DOMAIN = gui.DOMAIN
OWNER = 'Separate test owner'
WS = gui.SETTINGS_CALL
PATH = f'/config/integrations/integration/{DOMAIN}'


async def seed(hass):
    reg = er.async_get(hass)
    hass.states.async_remove('sensor.migration_depth')
    hass.states.async_remove('switch.probe_output')
    source = reg.async_get_or_create('sensor', 'test_probe', 'original', suggested_object_id='migration_depth')
    output = reg.async_get_or_create('switch', 'test_probe', 'output', suggested_object_id='probe_output')
    assert (source.entity_id, output.entity_id) == ('sensor.migration_depth', 'switch.probe_output')
    hass.states.async_set('sensor.migration_depth', '1.25', {'unit_of_measurement': 'm'})
    hass.states.async_set('switch.probe_output', 'off')
    flow = hass.config_entries.flow
    start = await flow.async_init(DOMAIN, context={'source': 'user'})
    created = await flow.async_configure(start['flow_id'], {'zone_name': 'North', 'tank_name': 'Storage',
        'level_sensor': 'sensor.migration_depth', 'refill_switch': 'switch.probe_output'})
    entry = created['result']
    await hass.async_block_till_done()
    mgr = hass.config_entries.options
    start = await mgr.async_init(entry.entry_id)
    step = await mgr.async_configure(start['flow_id'], {'next_step_id': 'edit_tank'})
    tank = entry.data['zones'][0]['tanks'][0]
    step = await mgr.async_configure(step['flow_id'], {'tank_id': tank['id']})
    actual = await mgr.async_configure(step['flow_id'], {'tank_name': 'Storage', 'level_sensor': 'sensor.migration_depth',
        'refill_switch': 'switch.probe_output', 'commission_refill': True, 'confirm_device_cutoff': True})
    assert actual['type'].value == 'create_entry', actual
    await hass.async_block_till_done()
    return entry


async def configure(page, label='Configure'):
    button = page.get_by_role('button', name=label, exact=True)
    await expect(button).to_be_visible(timeout=14000)
    await button.press('Enter')
    return page.locator('dialog-data-entry-flow')


def restore_owned_test_settings(root):
    """Use native restore where it runs, exact offline archive contents on Windows."""
    assert root.resolve().is_relative_to(Path(gettempdir()).resolve())
    if sys.platform != 'win32':
        assert restore_backup(str(root)), root
        assert json.loads((root / RESTORE_BACKUP_RESULT_FILE).read_bytes())['success']
        return 'native_core_startup'

    # Core 2025.12's securetar 2025.2.1 rejects even backup.json on Windows:
    # Path('/fake').resolve().relative_to('/fake') compares drive paths to a
    # drive-less path. Do not patch it or manufacture a native restore result.
    # Extract this test's unencrypted, Core-generated config snapshot offline.
    request = json.loads((root / '.HA_RESTORE').read_bytes())
    archive, = (root / 'backups').glob('*.tar')
    assert Path(request['path']).resolve() == archive.resolve() and request['password'] is None
    assert request['restore_homeassistant'] is True and request['restore_database'] is False
    with TemporaryDirectory() as destination, tarfile.open(archive) as outer:
        generated = outer.getmember('homeassistant.tar.gz')
        assert generated.isfile()
        with outer.extractfile(generated) as compressed, tarfile.open(fileobj=compressed) as inner:
            members = inner.getmembers()
            assert all(m.name.startswith('data') and (m.isfile() or m.isdir()) for m in members)
            inner.extractall(destination, members=members, filter='data')
        restored = Path(destination) / 'data'
        assert (restored / '.storage/core.config_entries').is_file()
        assert (restored / 'custom_components' / DOMAIN / 'manifest.json').is_file()
        for path in root.iterdir():
            if path.name != 'backups':
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(restored, root, dirs_exist_ok=True)
    return 'offline_exact_core_archive_config'


async def run(root, entry_id, mode):
    assert version('homeassistant') == '2025.12.0'
    storage = root / '.storage'
    restore_method = None
    if mode in ('restored', 'old_restored', 'snapshot_restored'):
        restore_method = restore_owned_test_settings(root)
    (storage / 'onboarding').write_text(json.dumps({
        'version': 4, 'minor_version': 1, 'key': 'onboarding',
        'data': {'done': ['user', 'core_config', 'integration', 'analytics']},
    }), encoding='utf-8')
    hass = gui.HomeAssistant(str(root))
    gui.loader.async_setup(hass)
    hass.config.skip_pip = True
    sys.path.insert(0, str(root))
    port = gui.loopback_port()
    url = f'http://127.0.0.1:{port}'
    calls = []
    core = storage / 'core.config_entries'
    try:
        users = json.loads((storage / 'auth').read_bytes())['data']['users']
        user_id, = (row['id'] for row in users if row['name'] == OWNER)
        config = {'homeassistant': {'auth_providers': [{'type': 'trusted_networks',
            'trusted_networks': ['127.0.0.1/32'], 'trusted_users': {'127.0.0.1/32': [user_id]},
            'allow_bypass_login': True}]}, 'http': {'server_host': '127.0.0.1', 'server_port': port},
            'frontend': {}, 'config': {}, 'backup': {}}
        migration.signals(hass, calls)
        assert await gui.bootstrap.async_from_config_dict(config, hass) is hass
        await hass.async_start()
        await hass.async_block_till_done()
        entry = await seed(hass) if mode == 'v1_seed' else hass.config_entries.async_get_entry(entry_id)
        imported = Path(sys.modules[f'custom_components.{DOMAIN}'].__file__).resolve()
        assert imported.is_relative_to(root)
        entry_id = entry.entry_id
        image = storage / f'{DOMAIN}.settings.{entry_id}'
        record = {'mode': mode, 'entry_id': entry_id, 'state': entry.state.name, 'version': entry.version,
                  'import_init_sha256': migration.digest(imported), 'has_mark': migration.MARK in entry.data}
        if restore_method:
            record['restore_method'] = restore_method
        if entry.state is gui.ConfigEntryState.LOADED:
            record['roles'] = sorted(entry.runtime_data.roles)
        record['rows'] = sorted((r.unique_id, r.id, r.entity_id) for r in er.async_entries_for_config_entry(er.async_get(hass), entry_id))
        if image.exists():
            record['image'] = migration.digest(image)
        async with async_playwright() as p, await p.chromium.launch(
            executable_path=os.environ.get('WATER_CHROMIUM_EXECUTABLE'), headless=True,
        ) as browser, await browser.new_context(locale='en-US') as ctx:
            async def only_local(route):
                if route.request.url.startswith(url + '/'):
                    await route.continue_()
                else:
                    await route.abort()
            await ctx.route('**/*', only_local)
            page = await ctx.new_page()
            page.set_default_timeout(9000)
            await gui.native_login(page, url + PATH)
            if mode in ('initial', 'prepared', 'active', 'recovery', 'restored'):
                dialog = await configure(page)
                if mode in ('initial', 'active', 'restored'):
                    choice = dialog.get_by_text('Topology transfer status', exact=True)
                    try:
                        await choice.click()
                    except Exception:
                        print('OPTIONS_UI_DIAGNOSTIC', await page.evaluate("""() => {
                            const scan = root => [...root.querySelectorAll('*')].flatMap(e =>
                                [...(/DIALOG|FLOW/.test(e.tagName) ? [[e.tagName, e.innerText, e.shadowRoot?.textContent?.slice(0,400)]] : []),
                                 ...(e.shadowRoot ? scan(e.shadowRoot) : [])]);
                            return [location.pathname, scan(document)];
                        }"""), flush=True)
                        raise
                    title = 'Topology transfer not requested' if mode == 'initial' else 'Topology transfer complete'
                    await expect(dialog.locator('[slot="title"]').filter(has_text=title)).to_be_visible()
                    detail = 'verified Home Assistant settings backup' if mode == 'initial' else 'create and download a matching Home Assistant settings backup now'
                    await expect(dialog.get_by_text(detail, exact=False)).to_be_visible()
                    record['ui_status'] = title
                    if mode == 'initial':
                        await dialog.get_by_role('button', name='Next', exact=True).click()
                        await dialog.get_by_text('Transfer topology to the integration store', exact=True).click()
                        await dialog.locator('ha-switch').click()
                        await dialog.get_by_role('button', name='Submit', exact=True).click()
                        await migration.disk_row(core, entry_id, lambda row: row['data'].get(migration.MARK, {}).get('phase') == 'requested')
                        await expect(dialog.get_by_text('Options successfully saved.', exact=True)).to_be_visible()
                        await gui.native_login(page, url + PATH)
                        dialog = await configure(page)
                        await expect(dialog.get_by_text('Topology transfer is not complete.', exact=False)).to_be_visible()
                        record['warm_pending_ui'] = True
                elif mode == 'prepared':
                    await expect(dialog.get_by_text('Topology transfer is not complete.', exact=False)).to_be_visible()
                    record['ui_status'] = 'pending'
                else:
                    await expect(dialog.get_by_text('An administrator must restore the matching Home Assistant settings backup', exact=False)).to_be_visible()
                    record['ui_status'] = 'recovery'
            if mode in ('active', 'v1_seed', 'preenroll_archive'):
                await migration.disk_row(core, entry_id, lambda row: row['version'] == entry.version and bool(row['options'].get('manual')))
                await hass.async_block_till_done()
                name = 'Synthetic ' + mode
                prior_files = set((root / 'backups').glob('*.tar'))
                generated = await page.evaluate(WS, {'type': 'backup/generate', 'agent_ids': ['backup.local'],
                    'include_homeassistant': True, 'include_database': False, 'name': name, 'password': None})
                async with asyncio.timeout(23):
                    while True:
                        backups = await page.evaluate(WS, {'type': 'backup/info'})
                        archive = next((b for b in backups['backups'] if b['name'] == name), None)
                        if backups['state'] == 'idle' and archive is not None:
                            break
                        await asyncio.sleep(.1)
                path, = set((root / 'backups').glob('*.tar')) - prior_files
                record['archive_name'] = path.name
                record['archive_sha256'] = migration.digest(path)
                record['backup_id'] = archive['backup_id']
                assert bool(generated)
            if mode in ('recovery', 'rollback'):
                sources = await page.evaluate(WS, {'type': 'backup/info'})
                prior, = sources['backups']
                # The real admin API queues restore and requests Core restart; the
                # next owned process consumes the request with the Core CLI's hook.
                reply = await page.evaluate("""async msg => {
                    try { await document.querySelector('home-assistant').hass.callWS(msg); return true; }
                    catch (err) { return err?.code ?? false; }
                }""", {'type': 'backup/restore', 'agent_id': 'backup.local',
                    'backup_id': prior['backup_id'], 'restore_homeassistant': True, 'restore_database': False})
                async with asyncio.timeout(9):
                    while hass.state is not CoreState.stopped:
                        await asyncio.sleep(.05)
                # A genuine graceful restart can end WebSocket transport before its ACK.
                assert hass.exit_code == RESTART_EXIT_CODE, reply
                request = json.loads((root / '.HA_RESTORE').read_bytes())
                assert request['restore_homeassistant'] is True and request['restore_database'] is False
                archive, = (root / 'backups').glob('*.tar')
                assert Path(request['path']).resolve() == archive.resolve()
                record['native_restore_queued'] = True
                record['native_restart_code'] = hass.exit_code
                record['restore_ws_reply'] = reply
    finally:
        await hass.async_stop()
    assert not calls, calls
    print('OPTIONS_GUI ' + json.dumps({**record, 'calls': calls, 'pid': os.getpid()}), flush=True)


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), *sys.argv[2:]))
