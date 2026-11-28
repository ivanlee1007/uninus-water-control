"""C Quick parameter conflict review; no public topology mutation surface."""
import pytest

from test_dashboard_browser import browser_page  # noqa: F401
from test_dashboard_editor import SETUP


@pytest.mark.parametrize(('key', 'notice'), [
    ('revision', '設定已被變更'), ('source_snapshot', '水桶來源已變更'),
])
def test_conflict_reviews_current_values_without_replacing_draft_or_cas(browser_page, key, notice):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""key => {
        hass.callWS = async msg => {
            if (!msg.type.endsWith('/config_save')) return ws(msg);
            wire.push(structuredClone(msg));
            server[key]='d'.repeat(64);
            server.zones[0].tanks[0].scale_max_m=4;
            server.zones[0].tanks[0].name='目前水桶';
            throw {code:key==='revision'?'stale_revision':'stale_source'};
        }; editor.hass=hass;
    }""", key)
    scale = page.get_by_role('spinbutton', name='水位顯示量程上限（m）', exact=True)
    apply = page.get_by_role('button', name='儲存此桶參數', exact=True)
    scale.fill('2.75')
    draft = scale.element_handle()
    apply.click()
    page.get_by_role('status').get_by_text(notice, exact=False).wait_for()
    panel = page.get_by_role('region', name='整合目前設定（唯讀）', exact=True)
    page.get_by_role('button', name='讀取目前設定（保留草稿）', exact=True).click()
    panel.wait_for()
    assert '目前水桶' in panel.inner_text() and '4 m' in panel.inner_text()
    assert scale.input_value() == '2.75' and scale.evaluate('(el,old)=>el===old', draft)
    assert scale.is_disabled() and apply.is_disabled()
    [write] = page.evaluate("wire.filter(x=>x.type.endsWith('_save'))")
    assert (write['if_revision'],write['if_source_snapshot']) == ('a'*64,'b'*64)
    assert write['changes'] == {'tanks':[{'id':'0'*31+'2','scale_max_m':2.75}]}
    assert 'zones' not in write and 'remove' not in write
    page.get_by_role('button', name='重新讀取（捨棄草稿）', exact=True).click()
    scale.wait_for()
    assert scale.input_value() == '4' and not panel.count() and apply.is_disabled()
    assert page.evaluate("wire.filter(x=>x.type.endsWith('_save')).length") == 1


@pytest.mark.parametrize(('key','notice'), [('source_snapshot','水桶來源已變更'),('revision','設定已被變更')])
def test_reconnect_current_read_keeps_draft_and_original_cas(browser_page, key, notice):
    page=browser_page
    page.evaluate(SETUP)
    scale=page.get_by_role('spinbutton',name='水位顯示量程上限（m）',exact=True)
    scale.fill('2.75')
    draft=scale.element_handle()
    page.evaluate("""key=>{
      editor.hass={...hass,connection:{connected:false},connected:false};
      server[key]='d'.repeat(64); server.zones[0].tanks[0].scale_max_m=4;
      editor.hass={...hass,connection:{connected:true}};
    }""",key)
    page.get_by_role('status').get_by_text(notice,exact=False).wait_for()
    apply=page.get_by_role('button',name='儲存此桶參數',exact=True)
    assert scale.is_visible() and scale.is_disabled() and apply.is_disabled()
    assert scale.input_value()=='2.75' and scale.evaluate('(el,old)=>el===old',draft)
    panel=page.get_by_role('region',name='整合目前設定（唯讀）',exact=True)
    page.get_by_role('button',name='讀取目前設定（保留草稿）',exact=True).click()
    panel.get_by_text('水位顯示量程：4 m',exact=True).wait_for()
    assert scale.input_value()=='2.75' and apply.is_disabled()
    page.get_by_role('button',name='重新讀取（捨棄草稿）',exact=True).click()
    scale.wait_for()
    assert scale.input_value()=='4' and not panel.count()
    assert not page.evaluate("wire.some(x=>x.type.endsWith('_save'))")


@pytest.mark.parametrize('loss',['demotion','denial'])
def test_held_conflict_review_does_not_publish_after_authority_loss(browser_page,loss):
    page=browser_page
    page.evaluate(SETUP)
    page.evaluate("""loss=>{
      hass.callWS=async msg=>{
        if(msg.type.endsWith('/config_save')){wire.push(msg);throw {code:'stale_source'};}
        const response=await ws(msg);
        if(!msg.type.endsWith('/config_get') || !wire.some(m=>m.type.endsWith('/config_save')))return response;
        response.zones[0].tanks[0].name='不應公開的目前水桶';
        return new Promise((resolve,reject)=>{window.releaseConflict=()=>loss==='denial'?reject({code:'unauthorized'}):resolve(response);});
      };editor.hass=hass;
    }""",loss)
    scale=page.get_by_role('spinbutton',name='水位顯示量程上限（m）',exact=True)
    scale.fill('2.75')
    apply=page.get_by_role('button',name='儲存此桶參數',exact=True)
    apply.click()
    page.get_by_text('水桶來源已變更',exact=False).wait_for()
    review=page.get_by_role('button',name='讀取目前設定（保留草稿）',exact=True)
    review.click()
    page.wait_for_function("typeof releaseConflict==='function'")
    if loss=='demotion':
        page.evaluate("editor.hass={...hass,user:{...hass.user,is_admin:false}}")
        assert not scale.count()
    page.evaluate('releaseConflict()')
    page.get_by_text('只有整合管理員可以讀取及套用設定。',exact=True).wait_for()
    assert not scale.count() and not review.is_visible()
    assert not page.get_by_role('region',name='整合目前設定（唯讀）',exact=True).count()
    assert '不應公開的目前水桶' not in page.locator('uninus-water-dashboard-quick-parameters').inner_text()
    assert apply.is_disabled() and page.evaluate("wire.filter(x=>x.type.endsWith('_save')).length")==1


def test_quick_save_cannot_clear_optional_binding_or_rebind_level(browser_page):
    page=browser_page
    page.evaluate(SETUP)
    page.get_by_role('spinbutton',name='水位顯示量程上限（m）',exact=True).fill('2.75')
    assert page.locator('ha-entity-picker').count()==0
    page.get_by_role('button',name='儲存此桶參數',exact=True).click()
    page.get_by_text('已儲存並重新讀回確認。',exact=True).wait_for()
    [write]=page.evaluate("wire.filter(x=>x.type.endsWith('_save'))")
    assert write['changes']=={'tanks':[{'id':'0'*31+'2','scale_max_m':2.75}]}
    assert 'zones' not in write and 'remove' not in write
    assert not page.evaluate("wire.some(x=>x.type.includes('/topology_'))")
