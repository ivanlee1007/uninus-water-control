"""Source-bound Chromium regressions for display and private DOM consistency."""

import pytest

from test_dashboard_browser import browser_page


@pytest.mark.parametrize("unit,raw,expected", [
    ("m", "1.2", "1.20"), ("cm", "120", "1.20"), ("mm", "1200", "1.20"),
    ("km", ".0012", "1.20"), ("in", "50", "1.27"), ("ft", "5", "1.524"),
    ("yd", "2", "1.8288"), ("mi", ".001", "1.609344"), ("nmi", ".001", "1.852"),
    ("cm", "120.00000000000001", "1.2000000000000001"),
    ("mm", "1.2000000000000001e3", "1.2000000000000001"),
])
def test_distance_display_gauge_and_policy_use_metres(browser_page, unit, raw, expected):
    page = browser_page
    page.evaluate("""([unit, raw]) => {
        window.card = normalCard(); card.setConfig(card.constructor.getStubConfig());
        const state = projection(1); state.entity_id = 'sensor.p1'; state.state = raw;
        state.attributes.unit_of_measurement = unit;
        state.attributes.uninus_water_display = {version: 1, scale_max_m: 2, alarm: [1.2, 5]};
        card.hass = snapshot({'sensor.p1': state}); document.querySelector('#area').append(card);
    }""", [unit, raw])
    assert page.locator(".level").get_attribute("aria-label") == f"Tank 1 · {expected} m"
    assert float(page.get_by_role("meter").get_attribute("aria-valuenow")) == float(expected)
    relation = page.locator(".relation").inner_text()
    assert ("達告警低門檻" in relation) == (float(expected) == 1.2)
    assert ("低於告警門檻" in relation) == (float(expected) < 1.2)


@pytest.mark.parametrize("unit,raw", [("ft", "NaN"), ("mi", "Infinity"), ("m", "1e309"),
                                        ("km", "1e308"), ("cm", " 120"), ("%", "50")])
def test_invalid_or_overflowed_distance_is_unavailable(browser_page, unit, raw):
    page = browser_page
    page.evaluate("""([unit, raw]) => {
        window.card = normalCard(); card.setConfig(card.constructor.getStubConfig());
        const state = projection(1); state.entity_id = 'sensor.p1'; state.state = raw;
        state.attributes.unit_of_measurement = unit;
        state.attributes.uninus_water_display = {version: 1, scale_max_m: 2};
        card.hass = snapshot({'sensor.p1': state}); document.querySelector('#area').append(card);
    }""", [unit, raw])
    assert page.locator('.level').get_attribute('aria-label') == 'Tank 1 · 液位讀值不可用'
    assert page.get_by_role('meter').count() == 0


@pytest.mark.parametrize("transition", ["principal", "same_user_demotion", "source_removed", "source_replaced"])
def test_hidden_inventory_is_purged_on_authority_or_readability_change(browser_page, transition):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard(); card.setConfig(card.constructor.getStubConfig());
        const state = projection(1); state.entity_id = 'sensor.p1';
        state.attributes.uninus_water_dashboard.zone.name = 'PRIVATE_ZONE';
        state.attributes.uninus_water_dashboard.tank.name = 'PRIVATE_TANK';
        window.oldStates = {'sensor.p1': state};
        window.user = {id: 'owner', is_admin: true, is_active: true};
        window.service = () => undefined;
        window.update = (states, currentUser = user) => {
            card.hass = {...snapshot(states), user: currentUser, callService: service};
        };
        update(oldStates); document.querySelector('#area').append(card);
    }""")
    for name in ['補水參數', '區域與水桶', '操作總覽']:
        page.get_by_role('button', name=name, exact=True).click()
    assert page.evaluate("card.shadowRoot.textContent.includes('PRIVATE_TANK')")
    page.evaluate("""transition => {
        const currentUser = transition === 'principal' ? {id: 'reader', is_admin: false, is_active: true} :
            transition === 'same_user_demotion' ? {...user, is_admin: false} : user;
        const next = transition === 'source_replaced' ? {'sensor.p2': projection(2)} : {};
        update(next, currentUser);
    }""", transition)
    assert page.evaluate("!card.shadowRoot.textContent.includes('PRIVATE_TANK') && !card.shadowRoot.textContent.includes('PRIVATE_ZONE')")
    for selector in ['#parameters .settings-row', '#devices .settings-row']:
        assert page.locator(selector).count() == (1 if transition == 'source_replaced' else 0)


def test_normal_telemetry_keeps_nodes_focus_and_hidden_names_current(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard(); card.setConfig(card.constructor.getStubConfig());
        window.state = projection(1); state.entity_id = 'sensor.p1';
        window.service = () => undefined;
        window.update = () => {card.hass = {...snapshot({'sensor.p1': state}),
            user: {id: 'owner', is_admin: true, is_active: true}, callService: service};};
        update(); document.querySelector('#area').append(card);
    }""")
    for name in ['補水參數', '區域與水桶', '操作總覽']:
        page.get_by_role('button', name=name, exact=True).click()
    page.locator('.level').focus()
    page.evaluate("""() => {
        window.original = card.shadowRoot.querySelector('.level'); window.blurs = 0;
        original.addEventListener('blur', () => blurs++);
        window.rows = [...card.shadowRoot.querySelectorAll('.settings-row')];
        state.state = '1.5'; state.attributes.uninus_water_dashboard.tank.name = 'RENAMED';
        update(); update(); update();
    }""")
    assert page.evaluate("original === card.shadowRoot.activeElement && blurs === 0")
    assert page.evaluate("rows.every(row => row.isConnected) && rows.every(row => row.querySelector('strong').textContent === 'RENAMED')")



def test_closed_manual_confirmation_does_not_retain_former_tank_private_name(browser_page):
    page=browser_page
    page.evaluate("""() => {
      window.card=normalCard(); card.setConfig(card.constructor.getStubConfig());
      window.states={'sensor.p1':projection(1)};
      states['sensor.p1'].attributes.uninus_water_dashboard.tank.name='PRIVATE_CONFIRMATION';
      for(const action of ['status','start','stop','reset']) {
        const entity_id=(action==='status'?'sensor':'button')+'.refill_'+action;
        states[entity_id]={entity_id,state:action==='status'?'idle':'unknown',attributes:{uninus_water_manual:{
          version:1,tank_id:id(1),role:'refill',action,...(action==='status'?{phase:'idle',requested:'off',actual:'off',
          pending_on:false,pending_off:false,fault:null,max_runtime_seconds:60,can_start:true}:{})}}};
      }
      window.service=()=>undefined;
      window.update=()=>{card.hass={...snapshot(states),callService:service};};
      update(); document.querySelector('#area').append(card);
    }""")
    page.get_by_role('button',name='手動補水',exact=True).click()
    page.get_by_role('dialog').get_by_role('button',name='取消',exact=True).click()
    page.evaluate("states['sensor.p1'].attributes.uninus_water_dashboard.tank.name='CURRENT_TANK'; update();")
    assert page.evaluate("!card.shadowRoot.textContent.includes('PRIVATE_CONFIRMATION')")
    assert page.evaluate('activity.routes') == []
