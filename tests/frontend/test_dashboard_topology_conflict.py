"""Trusted Chromium interaction with the shipped editor and a controlled peer."""
import pytest

from test_dashboard_browser import browser_page  # noqa: F401
from test_dashboard_editor import SETUP
from test_dashboard_topology import BOOT


@pytest.mark.parametrize(('key', 'notice'), [
    ('revision', '設定已被變更'), ('source_snapshot', '水桶來源已變更'),
])
def test_conflict_reviews_current_values_without_replacing_draft_or_cas(browser_page, key, notice):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    page.evaluate("""key => {
        const original = hass.callWS;
        hass.callWS = async msg => {
            if (!msg.type.endsWith('/topology_save')) return original(msg);
            wire.push(structuredClone(msg));
            topology[key] = server[key] = 'd'.repeat(64);
            server.zones[0].tanks[0].scale_max_m = 4;
            topology.zones[0].tanks[0].name = server.zones[0].tanks[0].name = '目前水桶';
            const current = {entity_id:'sensor.current_depth', registry_id:id(12)};
            topology.candidates.push(current);
            topology.zones[0].tanks[0].level_sensor = current;
            throw {code:key === 'revision' ? 'stale_revision' : 'stale_source'};
        };
        editor.hass = hass;
    }""", key)
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    name = page.get_by_role('textbox', name='水桶名稱', exact=True)
    scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    name.fill('尚未套用草稿')
    scale.fill('2.75')
    draft = name.element_handle()
    apply.click()
    page.get_by_role('status').get_by_text(notice, exact=False).wait_for()
    panel = page.get_by_role('region', name='整合目前設定（唯讀）', exact=True)
    page.get_by_role('button', name='讀取目前設定（保留草稿）', exact=True).click(timeout=1800)
    panel.wait_for()
    assert '目前水桶' in panel.inner_text() and 'sensor.current_depth' in panel.inner_text()
    assert '4 m' in panel.inner_text() and '尚未套用草稿' not in panel.inner_text()
    assert name.evaluate('(el, draft) => el === draft', draft)
    assert (name.input_value(), scale.input_value()) == ('尚未套用草稿', '2.75')
    assert name.is_disabled() and apply.is_disabled()
    [write] = page.evaluate("wire.filter(x => x.type.endsWith('_save'))")
    assert (write['if_revision'], write['if_source_snapshot']) == ('a' * 64, 'b' * 64)
    assert write['zones'][0]['tanks'][0]['level_sensor']['entity_id'] == 'sensor.original'
    page.get_by_role('button', name='重新讀取（捨棄草稿）', exact=True).click()
    name.wait_for()
    assert name.input_value() == '目前水桶' and scale.input_value() == '4'
    assert not panel.count() and apply.is_disabled()
    assert page.evaluate("wire.filter(x => x.type.endsWith('_save')).length") == 1


@pytest.mark.parametrize(('key', 'notice'), [
    ('source_snapshot', '水桶來源已變更'), ('revision', '設定已被變更'),
])
def test_reconnect_split_read_keeps_draft_and_offers_fresh_conflict_review(browser_page, key, notice):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    name = page.get_by_role('textbox', name='水桶名稱', exact=True)
    name.fill('待核對的草稿')
    draft = name.element_handle()
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    assert apply.is_enabled()
    page.evaluate("""key => {
        const prior = hass.callWS;
        editor.hass = {...hass, connection:{connected:false}, connected:false};
        let splitReads = 2;
        editor.hass = {...hass, connection:{connected:true}, callWS:async msg => {
            const dto = await prior(msg);
            if (msg.type.endsWith('/topology_get') && splitReads) {
                --splitReads;
                // An actual peer change in the interval between the two reads.
                topology[key] = server[key] = (splitReads ? 'd' : 'e').repeat(64);
                const current = {entity_id:'sensor.current_depth', registry_id:id(12)};
                if (!topology.candidates.some(row => row.registry_id === current.registry_id)) {
                    topology.candidates.push(current);
                }
                topology.zones[0].tanks[0].level_sensor = current;
            }
            return dto;
        }};
    }""", key)
    status = page.get_by_role('status')
    review = page.get_by_role('button', name='讀取目前設定（保留草稿）', exact=True)
    panel = page.get_by_role('region', name='整合目前設定（唯讀）', exact=True)
    status.get_by_text(notice, exact=False).wait_for(timeout=2000)
    assert name.is_visible() and name.is_disabled() and apply.is_disabled()
    assert name.input_value() == '待核對的草稿' and name.evaluate('(el, draft) => el === draft', draft)
    assert not panel.count()
    review.click()
    status.get_by_text(notice, exact=False).wait_for(timeout=2000)
    assert not panel.count() and name.is_visible() and apply.is_disabled()
    review.click()
    panel.get_by_text('sensor.current_depth', exact=False).wait_for()
    assert name.input_value() == '待核對的草稿' and apply.is_disabled()
    page.get_by_role('button', name='重新讀取（捨棄草稿）', exact=True).click()
    name.wait_for()
    assert name.input_value() == '試驗桶' and not panel.count()
    assert not page.evaluate("wire.some(x => x.type.endsWith('_save'))")


@pytest.mark.parametrize('loss', ['demotion', 'denial'])
def test_held_conflict_review_does_not_publish_after_authority_loss(browser_page, loss):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    page.evaluate("""loss => {
        const original = hass.callWS;
        hass.callWS = async msg => {
            if (msg.type.endsWith('/topology_save')) {
                wire.push(structuredClone(msg));
                throw {code:'stale_source'};
            }
            const response = await original(msg);
            if (!msg.type.endsWith('/topology_get') ||
                wire.filter(x => x.type.endsWith('/topology_get')).length === 1) return response;
            response.zones[0].tanks[0].name = '不應公開的目前水桶';
            return new Promise((resolve, reject) => { window.releaseConflict = () =>
                loss === 'denial' ? reject({code:'unauthorized'}) : resolve(response); });
        };
        editor.hass = hass;
    }""", loss)
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    name = page.get_by_role('textbox', name='水桶名稱', exact=True)
    name.fill('不應保留的草稿')
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    apply.click()
    page.get_by_text('水桶來源已變更', exact=False).wait_for()
    review = page.get_by_role('button', name='讀取目前設定（保留草稿）', exact=True)
    review.click(timeout=1800)
    page.wait_for_function("typeof releaseConflict === 'function'")
    if loss == 'demotion':
        page.evaluate("editor.hass = {...hass, user:{...hass.user,is_admin:false}}")
        assert not name.count()
    page.evaluate('releaseConflict()')
    page.get_by_text('只有整合管理員可以讀取及套用設定。', exact=True).wait_for()
    assert not name.count() and not review.is_visible()
    assert not page.get_by_role('region', name='整合目前設定（唯讀）', exact=True).count()
    assert '不應公開的目前水桶' not in page.locator('uninus-water-dashboard-editor').inner_text()
    assert apply.is_disabled() and page.evaluate("wire.filter(x => x.type.endsWith('_save')).length") == 1


def test_missing_optional_binding_can_be_explicitly_cleared_without_rebinding_level(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    page.evaluate("""() => {
        const tank = topology.zones[0].tanks[0];
        tank.alarm_switch = {entity_id:null, registry_id:id(13)};
        const original = hass.callWS;
        hass.callWS = async msg => {
            if (!msg.type.endsWith('/topology_save')) return original(msg);
            wire.push(structuredClone(msg));
            const [zone] = msg.zones;
            topology.zones = [{...zone,tanks:[{...zone.tanks[0],protected:false}]}];
            topology.revision = server.revision = 'd'.repeat(64);
            topology.source_snapshot = server.source_snapshot = 'e'.repeat(64);
            return structuredClone(topology);
        };
        editor.hass = hass;
    }""")
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    page.get_by_text('告警開關（選填）：現有綁定無法使用。', exact=True).wait_for()
    assert apply.is_disabled()
    clear = page.get_by_role('button', name='清除無法使用的告警開關（選填）', exact=True)
    clear.click(timeout=1800)
    assert apply.is_enabled() and not page.evaluate("wire.some(x => x.type.endsWith('_save'))")
    apply.click()
    page.get_by_text('已套用；不必再按外層儲存。', exact=True).wait_for()
    write, = page.evaluate("wire.filter(x => x.type.endsWith('_save'))")
    tank = write['zones'][0]['tanks'][0]
    assert tank['alarm_switch'] is None and tank['level_sensor']['entity_id'] == 'sensor.original'
