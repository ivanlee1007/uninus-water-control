"""C boundary: topology belongs to native Options, never the Card editor.

Mutation/Registry acceptance remains in test_native_card_topology and native
Options suites. This module covers the replacement public display boundary.
"""
import pytest

from test_dashboard_browser import browser_page  # noqa: F401
from test_dashboard_editor import SETUP


def test_display_selection_cancel_does_not_enroll_or_add_a_tank(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""() => {
      editor.remove(); wire.length=0;
      editor = customElements.get('uninus-water-dashboard').getConfigElement();
      const state = projection(2,1,0);
      editor.hass={...hass,states:{'sensor.p2':state}};
      editor.setConfig({type:'custom:uninus-water-dashboard'});
      document.querySelector('#area').append(editor);
    }""")
    page.get_by_role('checkbox', name='Water zone · Tank 2', exact=True).check()
    assert page.evaluate('wire') == []
    assert page.get_by_role('button', name='新增水桶', exact=True).count() == 0
    assert page.get_by_role('button', name='編輯區域與水桶', exact=True).count() == 0
    page.evaluate("editor.remove()")
    assert page.evaluate('wire') == []
    [event] = page.evaluate("activity.events.filter(e=>e[0]==='config-changed')")
    assert event[1]['config']['tank_ids'] == ['0'*31+'2']
    assert page.evaluate('server.zones[0].tanks.length') == 1


def test_no_public_topology_mutator_or_binding_picker_in_quick_panel(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.get_by_role('spinbutton', name='水位顯示量程上限（m）').wait_for()
    assert page.get_by_role('textbox', name='水桶名稱', exact=True).count() == 0
    assert page.get_by_role('button', name='移除區域', exact=True).count() == 0
    assert page.locator('ha-entity-picker,ha-selector').count() == 0
    assert page.evaluate("!wire.some(m=>m.type.includes('/topology_') || m.type.endsWith('_save'))")


@pytest.mark.parametrize('code,status', [
    ('topology_not_enrolled', '尚未移轉'), ('topology_restart_required', '移轉尚未完成'),
    ('topology_recovery_required', '區桶設定需要復原'),
])
def test_unavailable_settings_never_open_a_writable_quick_draft(browser_page, code, status):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""code => {
      editor.remove();
      hass.callWS=async msg=>{
        if (!msg.type.endsWith('/config_get')) return ws(msg);
        wire.push(msg); throw {code};
      };
      editor=mount();
    }""", code)
    page.get_by_text(status, exact=False).wait_for()
    assert page.get_by_role('spinbutton').count() == 0
    assert page.get_by_role('button', name='儲存此桶參數', exact=True).is_disabled()
    assert page.evaluate("!wire.some(x => x.type.endsWith('_save') || x.type.includes('/topology_'))")
