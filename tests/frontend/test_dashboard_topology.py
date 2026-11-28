"""Shipped topology editor draft and exact wire in trusted Chromium."""
import pytest

from test_dashboard_browser import browser_page  # noqa: F401
from test_dashboard_editor import SETUP


BOOT = """() => {
  class Picker extends HTMLElement {
    constructor() {
      super();
      this.input = document.createElement('select');
      this.attachShadow({mode:'open'}).append(this.input);
      this.input.addEventListener('change', () => this.dispatchEvent(new CustomEvent('value-changed', {detail:{value:this.input.value}})));
    }
    set includeEntities(choices) {
      this.input.replaceChildren(...['', ...choices].map(value => new Option(value, value)));
    }
    set label(value) { this.input.setAttribute('aria-label', value); }
    set value(value) { this.input.value = value ?? ''; }
    set disabled(value) { this.input.disabled = value; }
  }
  customElements.define('ha-entity-picker', Picker);
  const [source, next] = ['sensor.original','sensor.second'].map((entity_id, i) => ({entity_id, registry_id:id(10+i)}));
  window.topology = {version:1,entry_id:entry,revision:server.revision,source_snapshot:server.source_snapshot,
    candidates:[source,next],zones:[{id:id(1),name:'東區',tanks:[{id:id(2),name:'試驗桶',level_sensor:source,
      refill_switch:null,alarm_switch:null,protected:false}]}]};
  const original = ws;
  hass.callWS = async msg => {
    if (!msg.type.includes('/topology_')) return original(msg);
    wire.push(structuredClone(msg));
    if (msg.type.endsWith('/topology_save')) {
      topology.zones = msg.zones.map(z => ({...z, tanks:z.tanks.map(t => ({...t,id:t.id ?? id(3), protected:false}))}));
      topology.revision = server.revision = '9'.repeat(64);
      topology.source_snapshot = server.source_snapshot = '8'.repeat(64);
      server.zones[0].tanks.push({id:id(3), name:'新水桶', scale_max_m:null,
        refill:{use_common:false,local:null,effective:null},alarm:{use_common:false,local:null,effective:null}});
    }
    return structuredClone(topology);
  };
  editor.hass = hass;
}"""


def test_add_sensor_only_draft_then_apply_once(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    opener = page.get_by_role('button', name='編輯區域與水桶', exact=True)
    assert opener.count() == 1, 'The native card editor must expose an ordinary topology draft entry point'
    add = page.get_by_role('button', name='新增水桶', exact=True)
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    names = page.get_by_role('textbox', name='水桶名稱', exact=True)
    for cancel in (True, False):
        opener.click()
        add.click()
        names.last.fill('新水桶')
        assert apply.is_disabled()
        page.locator('ha-entity-picker[data-role="level_sensor"]').last.get_by_role('combobox').select_option('sensor.second')
        assert apply.is_enabled()
        assert page.evaluate("!wire.some(x => x.type.endsWith('_save'))")
        if cancel:
            page.evaluate("() => { editor.remove(); window.editor = mount(); }")
    apply.click()
    page.get_by_text('已套用；不必再按外層儲存。', exact=True).wait_for()
    [write] = page.evaluate("wire.filter(x => x.type.endsWith('_save'))")
    assert write['type'].endswith('/topology_save')
    assert write['zones'][0]['tanks'][-1] == {'id': None, 'name': '新水桶', 'refill_switch': None,
        'alarm_switch': None, 'level_sensor': {'entity_id': 'sensor.second', 'registry_id': '0' * 31 + 'b'}}
    assert write['remove'] == {'zones': [], 'tanks': []}
    assert page.evaluate('activity.events') == []


def test_removing_zone_lists_exact_committed_tank_and_requires_confirmation(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate(BOOT)
    page.evaluate("""() => {
      topology.zones[0].tanks[0].protected = true;
      topology.zones.push({id:id(4),name:'東區',tanks:[{id:id(5),name:'試驗桶',protected:false,
        level_sensor:topology.candidates[1],refill_switch:null,alarm_switch:null}]});
      server.zones.push({id:id(4),name:'東區',tanks:[{...server.zones[0].tanks[0],id:id(5)}]});
      const real = hass.callWS;
      hass.callWS = async msg => {
        if (!msg.type.endsWith('/topology_save')) return real(msg);
        wire.push(structuredClone(msg));
        topology.zones.pop();
        server.zones.pop();
        topology.revision = server.revision = '9'.repeat(64);
        topology.source_snapshot = server.source_snapshot = '8'.repeat(64);
        throw {code:'readback_required'};
      };
      editor.hass = hass;
    }""")
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    remove = page.get_by_role('button', name='移除區域', exact=True)
    apply = page.get_by_role('button', name='套用整合設定', exact=True)
    assert remove.first.is_disabled()
    remove.last.click()
    assert apply.is_disabled()
    preview = page.get_by_role('group', name='移除範圍確認')
    assert preview.get_by_role('listitem').all_text_contents() == ['區域：東區（第 2 區）', '水桶：東區（第 2 區） · 試驗桶（第 1 桶）']
    assert page.evaluate("!wire.some(x => x.type.endsWith('_save'))")
    preview.get_by_role('checkbox').check()
    apply.click()
    page.get_by_text('無法確認是否已套用；請重新讀取並核對，不要重送草稿。', exact=True).wait_for()
    assert apply.is_disabled()
    assert preview.is_visible()
    page.get_by_role('button', name='重新讀取（捨棄草稿）', exact=True).click()
    page.get_by_role('textbox', name='區域名稱', exact=True).wait_for()
    assert page.get_by_role('textbox', name='區域名稱', exact=True).count() == 1
    [write] = page.evaluate("wire.filter(x => x.type.endsWith('_save'))")
    assert write['remove'] == {'zones': ['0' * 31 + '4'], 'tanks': ['0' * 31 + '5']}
    assert not page.get_by_text('已套用；不必再按外層儲存。', exact=True).count()


@pytest.mark.parametrize('code,status', [
    ('topology_not_enrolled', '尚未移轉'), ('topology_restart_required', '移轉尚未完成'),
    ('topology_recovery_required', '區桶設定需要復原'),
])
def test_topology_never_opens_an_unowned_or_quarantined_draft(browser_page, code, status):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""code => {
      if (code !== 'topology_not_enrolled') entries[0].state = 'setup_error';
      hass.callWS = async msg => {
        if (!msg.type.endsWith('/topology_get')) return ws(msg);
        wire.push(structuredClone(msg));
        throw {code};
      };
      editor.hass = hass;
    }""", code)
    page.get_by_role('button', name='編輯區域與水桶', exact=True).click()
    page.get_by_text(status, exact=False).wait_for()
    assert not page.get_by_role('button', name='新增水桶', exact=True).count()
    assert page.get_by_role('button', name='套用整合設定', exact=True).is_disabled()
    assert page.evaluate("!wire.some(x => x.type.endsWith('_save'))")
