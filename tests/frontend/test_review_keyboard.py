"""Trusted keyboard visibility through the shipped quick-parameter dialog."""

import json

import pytest

from test_dashboard_browser import browser_page
from test_c_quick_parameters import normal_card


@pytest.mark.parametrize('width,height', [(390, 844), (390, 600), (1100, 480)])
def test_scrolled_quick_fields_visible_with_forward_and_backward_tab(browser_page, width, height):
    page = browser_page
    page.set_viewport_size({'width': width, 'height': height})
    opener = normal_card(page)
    opener.click()
    scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    scale.fill('7.125')
    page.evaluate("""() => {
        window.panel = card.shadowRoot.querySelector('uninus-water-dashboard-quick-parameters');
        window.fieldNodes = [...panel.shadowRoot.querySelectorAll('input,select')];
    }""")

    def focused_field_visible():
        measured = page.evaluate("""() => {
            const root=panel.shadowRoot, el=root.activeElement;
            const r=el.getBoundingClientRect();
            const hit=root.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return {field:el.dataset.field || el.tagName, top:r.top, bottom:r.bottom,
                hit:hit?.tagName, visible:hit===el, connected:fieldNodes.every(n=>n.isConnected),
                retained:fieldNodes.every((n,i)=>root.querySelectorAll('input,select')[i]===n)};
        }""")
        print('KEYBOARD_FIELD', json.dumps({'width':width,'height':height,**measured}))
        assert measured['connected'] and measured['retained']
        assert measured['visible'], 'Trusted Tab focus must be hit-testable, not covered by the action panel'

    focused_field_visible()
    # Reverse from the scrolled bottom field through every preceding field;
    # then retrace with forward Tab, without script focus or scroll repair.
    for _ in range(6):
        page.keyboard.press('Shift+Tab')
        focused_field_visible()
    for _ in range(6):
        page.keyboard.press('Tab')
        focused_field_visible()
    assert scale.evaluate('el => el.getRootNode().activeElement === el')
    assert scale.input_value() == '7.125'
    page.keyboard.press('Escape')
    assert page.get_by_role('dialog').count() == 0
    assert opener.evaluate('el => el.getRootNode().activeElement === el')
    assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 0
    opener.click()
    assert scale.input_value() == ''
    scale.fill('6.25')
    page.get_by_role('dialog').get_by_role('button', name='取消', exact=True).click()
    assert page.get_by_role('dialog').count() == 0
    assert opener.evaluate('el => el.getRootNode().activeElement === el')
    assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 0
