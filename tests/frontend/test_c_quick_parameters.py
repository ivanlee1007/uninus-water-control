"""Approved C entry points: display-only editor and per-tank operational dialog."""

from test_dashboard_browser import browser_page
from test_dashboard_editor import SETUP


def test_lovelace_editor_only_emits_display_config(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const cls = customElements.get('uninus-water-dashboard');
        window.editor = cls.getConfigElement();
        window.wire = [];
        editor.hass = {...snapshot({'sensor.p1': projection(1)}), user:{id:'admin',is_admin:true},
            connected:true,connection:{connected:true},callWS:async msg => {wire.push(msg); throw Error('private read');}};
        editor.setConfig({type:'custom:uninus-water-dashboard'});
        document.querySelector('#area').append(editor);
    }""")
    assert page.get_by_role('button', name='套用整合設定', exact=True).count() == 0
    assert page.get_by_role('spinbutton').count() == 0
    assert page.evaluate('wire') == []
    page.get_by_role('checkbox', name='顯示詳細狀態', exact=True).check()
    page.get_by_role('checkbox', name='Water zone', exact=True).check()
    events = page.evaluate("activity.events.filter(e => e[0] === 'config-changed')")
    assert events[-1][1]['config'] == {'type': 'custom:uninus-water-dashboard', 'show_details': True,
                                     'zone_ids': ['0' * 31 + '1'], 'tank_ids': [], 'display_mode': 'thin'}
    assert page.evaluate('wire') == []


def test_normal_card_opens_only_selected_tank_quick_parameters(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""() => {
        editor.remove(); window.card = normalCard(); card.setConfig({type:'custom:uninus-water-dashboard'});
        const one = projection(2); one.entity_id = 'sensor.p2';
        one.attributes.uninus_water_dashboard.zone.id = id(1);
        one.attributes.uninus_water_settings_entry = entry;
        const other = structuredClone(server.zones[0].tanks[0]); other.id = id(3); other.name='PRIVATE_OTHER_TANK';
        server.zones[0].tanks.push(other);
        card.hass = {...hass,states:{'sensor.p2':one}};
        document.querySelector('#area').append(card);
    }""")
    page.get_by_role('button', name='快捷參數', exact=True).click()
    modal = page.get_by_role('dialog', name='快捷參數', exact=True)
    assert modal.is_visible()
    scale = modal.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    scale.fill('2.75')
    assert modal.get_by_role('group', name='共用補水門檻', exact=True).count() == 0
    assert modal.get_by_text('PRIVATE_OTHER_TANK', exact=True).count() == 0
    assert modal.get_by_role('button', name='編輯區域與水桶', exact=True).count() == 0
    modal.get_by_role('button', name='儲存此桶參數', exact=True).click()
    modal.get_by_text('已儲存並重新讀回確認。', exact=True).wait_for()
    [write] = page.evaluate("wire.filter(m => m.type.endsWith('/config_save'))")
    assert write['changes'] == {'tanks':[{'id':'0'*31+'2','scale_max_m':2.75}]}
    assert page.evaluate("wire.slice(wire.findIndex(m=>m.type.endsWith('/config_save'))+1).some(m=>m.type.endsWith('/config_get'))")
    assert page.evaluate('activity.routes') == []
    modal.get_by_role('button', name='取消', exact=True).click()
    assert page.get_by_role('dialog').count() == 0


def test_display_selection_echo_reconfigures_existing_card_without_private_reads(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard(); card.setConfig({type:'custom:uninus-water-dashboard'});
        card.hass = snapshot({'sensor.p1':projection(1), 'sensor.p3':projection(3, 2)});
        document.querySelector('#area').append(card);
        window.original = card.shadowRoot.querySelector('.level');
        card.setConfig({type:'custom:uninus-water-dashboard',tank_ids:[id(1)],show_details:true});
    }""")
    assert page.locator('.tank').count() == 1
    assert page.locator('.tank .diagnostics').is_visible()
    assert page.evaluate("original === card.shadowRoot.querySelector('.level')")
    assert page.evaluate('activity.routes') == []


NORMAL = """() => {
    editor.remove(); window.card = normalCard(); card.setConfig({type:'custom:uninus-water-dashboard'});
    const one = projection(2,1,0); one.entity_id = 'sensor.p2';
    one.attributes.uninus_water_settings_entry = entry;
    window.visibleStates = {'sensor.p2': one};
    window.updateCard = (overrides = {}) => {card.hass = {...hass,states:visibleStates,...overrides};};
    updateCard(); document.querySelector('#area').append(card);
}"""


def normal_card(page):
    page.evaluate(SETUP)
    page.evaluate(NORMAL)
    return page.get_by_role('button', name='快捷參數', exact=True)


def test_quick_cancel_escape_draft_focus_and_responsive_geometry(browser_page):
    page = browser_page
    opener = normal_card(page)
    for width in (1100, 390):
        page.set_viewport_size({'width': width, 'height': 760})
        opener.click()
        scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
        scale.press_sequentially('2.')
        page.evaluate("""() => {
            window.input = card.shadowRoot.querySelector('uninus-water-dashboard-quick-parameters').shadowRoot.activeElement;
            window.blurs = 0; input.addEventListener('blur',()=>blurs++);
            visibleStates['sensor.p2'].state = '1.3'; updateCard(); updateCard();
        }""")
        page.keyboard.type('75')
        assert scale.input_value() == '2.75'
        assert page.evaluate('input.getRootNode().activeElement === input && blurs === 0')
        assert page.get_by_role('dialog').evaluate('(el) => {const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && el.scrollWidth <= el.clientWidth;}')
        page.keyboard.press('Escape')
        assert page.get_by_role('dialog').count() == 0
        assert opener.evaluate('el => el.getRootNode().activeElement === el')
        assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 0
        opener.click()
        assert scale.input_value() == ''
        page.get_by_role('dialog').get_by_role('button', name='取消', exact=True).click()


import pytest


@pytest.mark.parametrize('loss', ['demotion', 'principal', 'disconnect', 'source', 'preview', 'edit'])
def test_quick_owner_loss_purges_private_fields_and_never_writes(browser_page, loss):
    page = browser_page
    normal_card(page).click()
    scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    scale.fill('7.125')
    page.evaluate("""loss => {
        window.oldPanel = card.shadowRoot.querySelector('uninus-water-dashboard-quick-parameters');
        if (loss === 'demotion') {hass.user.is_admin=false; updateCard();}
        if (loss === 'principal') updateCard({user:{id:'other',is_admin:true}});
        if (loss === 'disconnect') updateCard({connected:false});
        if (loss === 'source') updateCard({states:{}});
        if (loss === 'preview') card.preview=true;
        if (loss === 'edit') card.editMode=true;
    }""", loss)
    assert page.get_by_role('dialog').count() == 0
    assert page.evaluate("oldPanel.shadowRoot.querySelectorAll('input,select').length === 0")
    assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 0


@pytest.mark.parametrize('accepted', [True, False])
def test_quick_close_reopen_keeps_unsettled_save_locked_and_ignores_late_reply(browser_page, accepted):
    page = browser_page
    opener = normal_card(page)
    page.evaluate("""accepted => {
        hass.callWS = async msg => {
            if (!msg.type.endsWith('/config_save')) return ws(msg);
            if (!accepted) {wire.push(msg); return new Promise((resolve,reject)=>{window.releaseSave=()=>reject({code:'stale_revision'});});}
            const result = await ws(msg);
            return new Promise(resolve=>{window.releaseSave=()=>resolve(result);});
        }; updateCard();
    }""", accepted)
    opener.click()
    scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    scale.fill('2.75')
    page.get_by_role('button', name='儲存此桶參數', exact=True).click()
    page.wait_for_function("typeof releaseSave === 'function'")
    page.get_by_role('dialog').get_by_role('button', name='取消', exact=True).click()
    opener.click()
    assert page.get_by_role('button', name='儲存此桶參數', exact=True).is_disabled()
    assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 1
    page.evaluate('releaseSave()')
    scale.wait_for()
    assert scale.input_value() == ('2.75' if accepted else '')
    assert page.get_by_text('已儲存並重新讀回確認。', exact=True).count() == 0
    assert page.evaluate("wire.filter(m=>m.type.endsWith('/config_save')).length") == 1



def test_detached_quick_requires_new_hass_before_any_replacement_read(browser_page):
    page=browser_page
    page.evaluate(SETUP)
    page.get_by_role('spinbutton',name='水位顯示量程上限（m）',exact=True).wait_for()
    page.evaluate("""()=>{
      editor.remove(); window.requestCount=wire.length;
      document.querySelector('#area').append(editor);
    }""")
    # Drain settled JS microtasks without polling a product timer.
    page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>resolve()))')
    assert page.evaluate('wire.length') == page.evaluate('requestCount')
    assert page.get_by_role('spinbutton').count()==0
    page.evaluate('editor.hass=hass')
    page.get_by_role('spinbutton',name='水位顯示量程上限（m）',exact=True).wait_for()
