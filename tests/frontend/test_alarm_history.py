"""Actual shipped Card in Chromium with held, counted history transport replies."""

from test_dashboard_browser import browser_page  # noqa: F401 (pytest fixture)


SETUP = """() => {
    window.requests = [];
    window.pending = [];
    window.transport = msg => { requests.push(msg); return new Promise((resolve, reject) => pending.push({resolve, reject})); };
    const entry = '01G00000000000000000000000';
    window.hass = {
        states: Object.fromEntries([1, 2].map(n => {
            const state = projection(n);
            state.attributes.uninus_water_settings_entry = entry;
            return [`sensor.test_${n}`, state];
        })),
        user: {id: id(10), is_admin:true, is_active:true},
        connected: true,
        callWS: transport,
        connection: {},
        callService: (...args) => activity.routes.push(['service', args]),
    };
    window.card = normalCard();
    card.setConfig({type:'custom:uninus-water-dashboard'});
    card.hass = hass;
    document.querySelector('#area').append(card);
    window.reply = (idx, status) => pending[idx].resolve({
        version:1, scope:id(30 + idx), status,
        window:{start_utc:'2026-01-01T00:00:00.000000Z',end_utc:'2026-01-02T00:00:00.000000Z'},
        coverage: status === 'ok' ? { start_utc:'2026-01-01T13:00:00.000000Z',end_utc:'2026-01-01T15:00:00.000000Z'} : null,
        events: status === 'ok' ? [{at_utc:'2026-01-01T15:00:00.000000Z',state:'on'}] : [], limited:false,
    });
}"""


def test_explicit_history_refresh_is_scoped_and_invalidated_on_target_authority_and_transport_changes(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    assert page.locator('.tank').count() == 2
    page.get_by_role('button', name='補水參數', exact=True).click()
    panel = page.get_by_role('region', name='告警輸出紀錄')
    assert panel.count() == 1, 'normal parameters tab must offer read-only alarm output history'
    pick = panel.get_by_label('選擇水桶')
    refresh = panel.get_by_role('button', name='重新整理紀錄')
    assert page.evaluate('requests.length') == 0
    refresh.click()
    refresh.evaluate('el => el.click()')
    assert page.evaluate('requests') == [{
        'type': 'uninus_water_control/alarm_history_get', 'version': 1,
        'entry_id': '01G00000000000000000000000', 'zone_id': '0' * 31 + '1', 'tank_id': '0' * 31 + '1',
    }]
    pick.select_option(label='Water zone · Tank 2')
    page.evaluate("reply(0, 'ok')")
    assert panel.locator('time').count() == 0
    refresh.click()
    page.evaluate("reply(1, 'no_binding')")
    panel.get_by_text('未設定告警輸出', exact=True).wait_for()
    assert page.evaluate('requests.at(-1).tank_id') == '0' * 31 + '2'

    refresh.click()
    page.evaluate("reply(2, 'ok')")
    row = panel.locator('li').filter(has_text='開啟')
    row.wait_for()
    assert row.locator('time').get_attribute('datetime') == '2026-01-01T15:00:00.000000Z'
    assert 'UTC' in panel.inner_text()
    refresh.click()
    page.evaluate('() => { hass.user.is_admin = false; card.hass = hass; }')
    assert not panel.is_visible()
    page.evaluate("reply(3, 'ok')")
    page.evaluate('() => { hass.user.is_admin = true; card.hass = hass; }')
    assert panel.locator('time').count() == 0
    refresh.click()
    page.evaluate('() => { hass.callWS = msg => transport(msg); card.hass = hass; }')
    page.evaluate("reply(4, 'ok')")
    assert panel.locator('time').count() == 0
    refresh.click()
    page.evaluate('() => { hass.connected = false; card.hass = hass; }')
    assert not panel.is_visible()
    page.evaluate("reply(5, 'ok')")
    page.evaluate('() => { hass.connected = true; card.hass = hass; }')
    assert panel.locator('time').count() == 0
    refresh.click()
    page.evaluate("() => { card.remove(); reply(6, 'ok'); }")
    page.evaluate("() => { document.querySelector('#area').append(card); }")
    assert panel.locator('time').count() == 0
    refresh.click()
    page.evaluate("reply(7, 'unavailable')")
    panel.get_by_text('沒有可驗證的告警輸出歷史', exact=True).wait_for()
    page.get_by_role('button', name='操作總覽', exact=True).click()
    assert page.locator('.tank:visible').count() == 2
    page.get_by_role('button', name='補水參數', exact=True).click()
    assert panel.get_by_text('沒有可驗證的告警輸出歷史', exact=True).count() == 0
    page.wait_for_timeout(500)
    assert page.evaluate('requests.length') == 8
    assert page.evaluate('activity.events') == []


def test_history_coverage_displays_full_precision_for_same_second_events(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.get_by_role('button', name='補水參數', exact=True).click()
    panel = page.get_by_role('region', name='告警輸出紀錄')
    panel.get_by_role('button', name='重新整理紀錄').click()
    page.evaluate("""() => pending[0].resolve({
        version:1, scope:id(30), status:'ok', limited:false,
        window:{start_utc:'2026-01-01T00:00:00.000000Z',end_utc:'2026-01-02T00:00:00.000000Z'},
        coverage:{start_utc:'2026-01-01T15:46:34.000001Z',end_utc:'2026-01-01T15:46:34.054613Z'},
        events:[{at_utc:'2026-01-01T15:46:34.054613Z',state:'off'},
                {at_utc:'2026-01-01T15:46:34.038580Z',state:'on'}],
    })""")
    times = panel.locator('time')
    times.first.wait_for()
    assert times.all_inner_texts() == [
        '2026-01-01 15:46:34.054613 UTC', '2026-01-01 15:46:34.038580 UTC',
    ]
    assert panel.get_by_text('已驗證涵蓋：', exact=False).inner_text() == (
        '已驗證涵蓋：2026-01-01 15:46:34.000001 UTC 至 2026-01-01 15:46:34.054613 UTC'
    )
