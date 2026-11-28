"""Chromium geometry and lifecycle of the shipped console with controlled HA state.

Not a native Home Assistant frontend test. Run explicitly under tests/frontend
with pytest, playwright==1.56.0 and `python -m playwright install chromium`.
Set P2_BROWSER_EXECUTABLE to use an existing Chromium-based browser instead.
"""

import json
import os
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
DASHBOARD = (
    Path(__file__).resolve().parents[2]
    / "custom_components/uninus_water_control/dashboard.js"
)


@pytest.fixture
def browser_page():
    launch = {}
    if executable := os.environ.get("P2_BROWSER_EXECUTABLE"):
        launch["executable_path"] = executable
    with playwright.sync_playwright() as runtime, runtime.chromium.launch(**launch) as browser:
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        page.set_default_timeout(2500)
        failures = []
        network = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        page.on("request", lambda request: network.append(request.url))
        page.route("**/*", lambda route: route.abort())
        page.set_content('<style>#area { width: 300px; }</style><main id="area"></main>')
        page.evaluate("""() => {
            window.activity = {loads: 0, routes: [], events: []};
            for (const name of ['hass-more-info', 'config-changed', 'call-service']) {
                document.addEventListener(name, (e) => activity.events.push([name, e.detail]));
            }
            window.loadCardHelpers = async () => { activity.loads++; throw new Error('unexpected helper'); };
            window.id = (n) => n.toString(16).padStart(32, '0');
            window.normalCard = () => {
                const card = document.createElement('uninus-water-dashboard');
                card.preview = false;
                card.editMode = false;
                return card;
            };
            window.projection = (tank, zone = 1, index = tank - 1, zoneIndex = zone - 1) => ({
                state: tank === 2 ? 'unavailable' : String(tank * 10),
                attributes: {
                    unit_of_measurement: 'm', friendly_name: 'DO NOT USE RAW NAME',
                    uninus_water_dashboard: {
                        version: 1, zone: {id: id(zone), name: 'Water zone', index: zoneIndex},
                        tank: {id: id(tank), name: 'Tank ' + tank, index},
                    },
                },
            });
            window.snapshot = (states, language = 'en') => ({
                states, language, locale: {language},
                callService: (...args) => activity.routes.push(['service', args]),
                callWS: (...args) => activity.routes.push(['ws', args]),
                fetchWithAuth: (...args) => activity.routes.push(['fetch', args]),
            });
        }""")


        # The new module is absent at the P1 baseline: assert visible behavior instead
        # of using file absence or undefined registration as the RED assertion.
        if DASHBOARD.is_file():
            page.add_script_tag(content=DASHBOARD.read_text(encoding="utf-8"), type="module")
            page.evaluate("customElements.whenDefined('uninus-water-dashboard').then(() => undefined)")
        yield page
        assert not failures
        assert not network
        assert page.evaluate("activity.routes") == []


def test_native_card_contract_and_container_geometry(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard();
        if (card.setConfig) card.setConfig({type: 'custom:uninus-water-dashboard'});
        window.states = Object.fromEntries([5, 2, 4, 1, 3].map(n => [`sensor.p${n}`, projection(n)]));
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    tiles = page.locator(".tank")
    try:
        tiles.first.wait_for()
    except playwright.TimeoutError:
        pass
    assert tiles.count() == 5, "five tank slots must be created from hass.states"
    assert page.locator(".zone-head h2").all_text_contents() == ["Water zone"]
    assert page.locator(".level").evaluate_all("els => els.map(el => el.getAttribute('aria-label'))") == [
        "Tank 1 · 10.00 m", "Tank 2 · 液位讀值不可用", "Tank 3 · 30.00 m", "Tank 4 · 40.00 m", "Tank 5 · 50.00 m"
    ]
    assert page.evaluate("""() => ({
        stub: card.constructor.getStubConfig(), grid: card.getGridOptions(),
        count: customCards.filter((item) => item.type === card.localName).length,
    })""") == {
        "stub": {"type": "custom:uninus-water-dashboard"},
        "grid": {"columns": "full", "rows": "auto", "min_columns": 3},
        "count": 1,
    }
    assert page.evaluate("activity.loads") == 0
    evidence = []
    evidence_dir = os.environ.get("P2_BROWSER_EVIDENCE")

    def measure(width, columns, zone_columns=1):
        page.evaluate("width => document.querySelector('#area').style.width = `${width}px`", width)
        geometry = page.evaluate("""() => {
            const zoneHosts = [...card.shadowRoot.querySelectorAll('[data-zone-id]')];
            const rect = (node) => {
                const {x, y, width, height, right} = node.getBoundingClientRect();
                return {x, y, width, height, right, overflow: node.scrollWidth - node.clientWidth};
            };
            return {viewport: innerWidth, card: rect(card), zones: zoneHosts.map(rect),
                tiles: [...zoneHosts[0].querySelectorAll('.tank')].map(rect)};
        }""")
        assert geometry["viewport"] == 1100
        assert geometry["card"]["width"] == width
        first = geometry["tiles"][0]
        assert len([t for t in geometry["tiles"] if t["y"] == first["y"]]) == columns
        assert len({z["x"] for z in geometry["zones"]}) == zone_columns
        assert all(0 < item["width"] and item["overflow"] == 0
                   for item in [geometry["card"], *geometry["zones"], *geometry["tiles"]])
        assert all(geometry["card"]["x"] <= t["x"] < t["right"] <= geometry["zones"][0]["right"]
                   for t in geometry["tiles"])
        if len(geometry["tiles"]) > columns:
            assert geometry["tiles"][columns]["x"] == first["x"]
            assert geometry["tiles"][columns]["y"] >= first["y"] + first["height"]
        evidence.append(geometry)

    for width, columns in ((300, 1), (344, 2), (512, 3), (692, 4)):
        measure(width, columns)
        if evidence_dir and width in (300, 692):
            page.locator("#area").screenshot(path=Path(evidence_dir) / f"shell-light-{width}.png")
    page.evaluate("""() => {
        card.hass = {...snapshot({...states, 'sensor.p6': projection(6, 2, 0)}), themes: {darkMode: true}};
    }""")
    page.locator(".tank").nth(5).wait_for()
    measure(690, 4)
    measure(740, 2, 2)
    if evidence_dir:
        directory = Path(evidence_dir)
        page.locator("#area").screenshot(path=directory / "shell-dark-740.png")
        (directory / "geometry.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_overview_summarizes_per_tank_previews_without_inventing_scale_or_output(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const preview = (entity_id, kind, state, tank) => ({entity_id, state,
            attributes: {uninus_water_thresholds: {version: 1, zone_id: id(1), tank_id: id(tank), kind}}});
        window.states = {
            'sensor.p1': projection(1), 'sensor.p2': projection(2), 'sensor.p3': projection(3),
            'sensor.band': preview('sensor.band', 'refill_preview', 'stop_threshold', 1),
            'binary_sensor.low': preview('binary_sensor.low', 'low_level_alarm', 'on', 1),
            'binary_sensor.unknown': preview('binary_sensor.unknown', 'low_level_alarm', 'unavailable', 2),
        };
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = {...snapshot(states, 'zh-Hant'), user: {is_admin: true}};
        document.querySelector('#area').append(card);
    }""")
    page.locator(".tank").first.wait_for()
    assert page.get_by_role("heading", name="補水控制", exact=True).is_visible()
    assert page.get_by_text("1 區 · 3 桶 · 水位單位 m", exact=True).is_visible()
    assert page.get_by_text("無告警輸出回報", exact=True).is_visible()
    assert page.locator("[data-low-level-active]").count() == 1
    assert page.get_by_role("meter").count() == 0
    first = page.locator(".tank").first
    first.locator(".diagnostics-toggle").click()
    assert first.get_by_role("button", name="低液位告警中", exact=True).is_visible()
    assert first.get_by_text("告警輸出：未提供", exact=True).is_visible()
    assert first.get_by_role("button", name="開啟告警", exact=True).is_disabled()
    assert first.get_by_text("未設定水位量程", exact=True).is_visible()
    page.get_by_role("button", name="補水參數", exact=True).click()
    assert page.get_by_role("link").count() == 0
    assert "編輯儀表板 → 編輯補水卡片" in page.get_by_role("region", name="補水參數").inner_text()
    assert page.locator("input,select").count() == 0
    page.evaluate("""() => {
        states['binary_sensor.low'] = {...states['binary_sensor.low'], state: 'off'};
        card.hass = {...snapshot(states, 'en'), user: {is_admin: false}};
    }""")
    assert page.get_by_role("link").count() == 0
    page.get_by_role("button", name="操作總覽", exact=True).click()
    assert page.locator("[data-low-level-active]").count() == 0
    assert first.get_by_role("button", name="低液位告警未觸發", exact=True).is_visible()


def test_reference_inventory_fits_three_tanks_per_zone_at_desktop_width(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const states = Object.fromEntries(Array.from({length: 8}, (_, i) =>
            [`sensor.p${i}`, projection(i + 1, i < 4 ? 1 : 2, i % 4)]));
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.locator(".tank").last.wait_for()
    for width, expected in ((984, 3), (1030, 3), (1060, 3), (390, 2)):
        rows = page.evaluate("""width => {
            document.querySelector('#area').style.width = `${width}px`;
            return [...card.shadowRoot.querySelectorAll('.zone')].map(zone => {
                const bounds = [...zone.querySelectorAll('.tank')].map(tank => tank.getBoundingClientRect());
                return bounds.filter(box => box.top === bounds[0].top).length;
            });
        }""", width)
        assert rows == [expected, expected], (width, rows)


def test_calibrated_level_and_threshold_summary_follow_current_projection(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.update = (reading, extras = {}) => {
            const entity = 'sensor.p1';
            const level = projection(1);
            level.state = reading;
            level.entity_id = entity;
            Object.assign(level.attributes, {unit_of_measurement: 'm', uninus_water_display: {
                version: 1, scale_max_m: 2.5, refill: [0.5, 1.5], alarm: [0.4, 5], ...extras,
            }});
            card.hass = snapshot({[entity]: level});
        };
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        update('2.5');
        document.querySelector('#area').append(card);
    }""")
    meter = page.get_by_role("meter")
    meter.wait_for()
    assert meter.get_attribute("aria-valuenow") == "2.5"
    assert meter.get_attribute("aria-valuemax") == "2.5"
    assert page.get_by_text("補水門檻　0.5／1.5 m", exact=True).is_visible()
    assert page.get_by_text("低液位告警　≤ 0.4 m · 5 秒", exact=True).is_visible()
    assert page.get_by_text("超出顯示量程", exact=True).count() == 0
    page.evaluate("update('2.51')")
    assert meter.get_attribute("aria-valuenow") == "2.5"
    assert page.get_by_text("超出顯示量程", exact=True).is_visible()
    page.evaluate("update('-0.01')")
    assert meter.get_attribute("aria-valuenow") == "0"
    page.evaluate("update('unavailable')")
    assert meter.count() == 0
    assert page.locator(".relation").get_by_text("液位讀值不可用", exact=True).is_visible()
    page.evaluate("update('1', {refill: [1.5, 0.5], version: 2})")
    assert meter.count() == 0
    assert page.get_by_text("補水門檻　0.5／1.5 m", exact=True).count() == 0


def test_mixed_thresholds_stay_on_their_own_validated_tanks(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const states = {};
        for (let i = 1; i <= 3; i++) {
            const entity = `sensor.p${i}`;
            const level = projection(i);
            level.entity_id = i === 3 ? 'sensor.mismatched' : entity;
            level.attributes.uninus_water_display = {version: 1, refill: [i, i + 1], alarm: [i, 5]};
            states[entity] = level;
        }
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.get_by_text("各桶補水門檻不同", exact=True).wait_for()
    assert page.get_by_text("各桶低液位告警設定不同", exact=True).is_visible()
    for i in (1, 2):
        tank = page.locator(f'[data-tank-id="{i:032x}"]')
        tank.locator(".diagnostics-toggle").click()
        assert tank.get_by_role("button", name="補水預覽無法取得", exact=True).is_disabled()
        assert tank.get_by_role("button", name="低液位告警預覽無法取得", exact=True).is_disabled()
    page.get_by_role("button", name="補水參數", exact=True).click()
    for i in (1, 2):
        assert page.get_by_text(f"補水門檻　{i}／{i+1} m", exact=True).is_visible()
        assert page.get_by_text(f"低液位告警　≤ {i} m · 5 秒", exact=True).is_visible()
    assert not page.get_by_text("補水門檻　3／4 m", exact=True).count()


def test_tank_readout_uses_a_vessel_without_claiming_a_measured_scale(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot({'sensor.p1': projection(1)});
        document.querySelector('#area').append(card);
    }""")
    readout = page.get_by_role("button", name="Tank 1 · 10.00 m", exact=True)
    readout.wait_for()
    page.locator(".tank .diagnostics-toggle").click()
    assert page.get_by_text("未設定水位量程", exact=True).is_visible()
    assert page.get_by_role("meter").count() == 0
    readout.click()
    assert page.evaluate("activity.events") == [["hass-more-info", {"entityId": "sensor.p1"}]]


def test_threshold_previews_are_tank_scoped_read_only_and_fail_closed(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        window.preview = (entity, kind, state, zone = 1, tank = 1, extra = {}) => ({
            entity_id: entity, state, attributes: {uninus_water_thresholds: {
                version: 1, zone_id: id(zone), tank_id: id(tank), kind, ...extra,
            }},
        });
        window.states = {
            'sensor.p1': projection(1), 'sensor.p2': projection(2, 2, 0),
            'sensor.renamed_one': preview('sensor.renamed_one', 'refill_preview', 'refill_threshold'),
            'binary_sensor.renamed_two': preview('binary_sensor.renamed_two', 'low_level_alarm', 'off'),
            'sensor.wrong_zone': preview('sensor.wrong_zone', 'refill_preview', 'stop_threshold', 2),
            'binary_sensor.extra_key': preview('binary_sensor.extra_key', 'low_level_alarm', 'on', 2, 2, {output: 'switch.x'}),
        };
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.locator(".tank").first.wait_for()
    first = page.locator('[data-tank-id="00000000000000000000000000000001"]')
    second = page.locator('[data-tank-id="00000000000000000000000000000002"]')
    refill = first.locator('[data-preview-kind="refill_preview"]')
    alarm = first.locator('[data-preview-kind="low_level_alarm"]')
    assert first.locator("[data-preview-kind]").count() == 2
    assert second.locator("[data-preview-kind]").count() == 0
    first.locator(".diagnostics-toggle").click()
    assert refill.inner_text() == "↓ 達補水低門檻"
    assert alarm.inner_text() == "低液位告警未觸發"
    page.keyboard.press("Tab")
    old = refill.element_handle()
    refill.focus()
    page.evaluate("""() => {
        window.previewBlurs = 0;
        card.shadowRoot.activeElement.addEventListener('blur', () => previewBlurs++);
        states['sensor.renamed_one'] = {...states['sensor.renamed_one'], state: 'stop_threshold'};
        states['binary_sensor.renamed_two'] = {...states['binary_sensor.renamed_two'], state: 'unavailable'};
        card.hass = snapshot({...states});
    }""")
    assert refill.evaluate("(el, old) => el === old && el.getRootNode().activeElement === el", old)
    assert page.evaluate("previewBlurs") == 0
    assert refill.inner_text() == "↑ 達補水高門檻"
    assert alarm.inner_text() == "低液位告警預覽無法取得"
    refill.click()
    alarm.click()
    assert page.evaluate("activity.events") == [
        ["hass-more-info", {"entityId": "sensor.renamed_one"}],
        ["hass-more-info", {"entityId": "binary_sensor.renamed_two"}],
    ]
    # The valid refill claimant must not win over even a malformed duplicate.
    # Other rejected wire frames must not attach to the other tank.
    page.evaluate("""() => {
        card.hass = snapshot({...states,
            'switch.lookalike': preview('switch.lookalike', 'refill_preview', 'refill_threshold'),
            'sensor.bad_version': preview('sensor.bad_version', 'refill_preview', 'refill_threshold', 2, 2, {version: 2}),
            'binary_sensor.bad_state': preview('binary_sensor.bad_state', 'low_level_alarm', 'unknown', 2, 2),
        });
    }""")
    assert refill.count() == 0
    assert alarm.count() == 1
    assert second.locator("[data-preview-kind]").count() == 0
    page.evaluate("""() => {
        const withoutExtra = {...states};
        delete withoutExtra['binary_sensor.extra_key'];
        card.hass = snapshot({...withoutExtra,
            'sensor.bad_version': preview('sensor.bad_version', 'refill_preview', 'refill_threshold', 2, 2, {version: 2}),
            'binary_sensor.mismatched_key': preview('binary_sensor.different_entity_id', 'low_level_alarm', 'on', 2, 2),
        }, 'zh-TW');
    }""")
    assert refill.inner_text() == "↑ 達補水高門檻"
    assert alarm.inner_text() == "低液位告警預覽無法取得"
    assert second.locator("[data-preview-kind]").count() == 0


def test_live_inventory_keeps_native_nodes_and_keyboard_focus(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard();
        window.states = {'sensor.p1': projection(1), 'sensor.p2': projection(2),
            'sensor.p3': projection(3, 2, 0)};
        document.querySelector('#area').append(card);
        card.hass = snapshot(states);
        card.setConfig({type: 'custom:uninus-water-dashboard'});
    }""")
    page.get_by_role("button", name="區域與水桶", exact=True).focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.evaluate("""() => {
        window.level = card.shadowRoot.querySelector('.level');
        window.zone = card.shadowRoot.querySelector('[data-zone-id]');
        window.heading = zone.querySelector('.zone-head h2');
        window.blurCount = 0;
        level.addEventListener('blur', () => blurCount++);
    }""")
    assert page.evaluate("card.shadowRoot.activeElement === level")
    page.evaluate("""() => {
        states = {...states, 'sensor.p1': {...states['sensor.p1'], state: '59'}};
        card.hass = snapshot(states);
        card.setConfig({...card.constructor.getStubConfig(), grid_options: {columns: 'full'}});
    }""")
    page.get_by_role("button", name="Tank 1 · 59.00 m").wait_for()
    assert page.evaluate("card.shadowRoot.querySelector('.level') === level && activity.loads === 0")
    page.evaluate("""() => {
        const next = structuredClone(states);
        for (const key of ['sensor.p1', 'sensor.p2']) {
            next[key].attributes.uninus_water_dashboard.zone.name = '<b>同名</b>';
            next[key].attributes.uninus_water_dashboard.zone.index = 1;
        }
        next['sensor.p1'].attributes.uninus_water_dashboard.tank.name = '<img src=/bad> 水桶';
        next['sensor.p1'].attributes.uninus_water_dashboard.tank.index = 2;
        next['sensor.p3'].attributes.uninus_water_dashboard.zone.index = 0;
        delete next['sensor.p2'];
        next['sensor.p4'] = projection(4, 1, 0, 1);
        next['sensor.p4'].attributes.uninus_water_dashboard.zone.name = '<b>同名</b>';
        window.latest = snapshot(next);
        card.hass = latest;
    }""")
    page.get_by_role("button", name="<img src=/bad> 水桶 · 59.00 m").wait_for()
    assert page.locator(".zone-head h2").all_text_contents() == ["Water zone", "<b>同名</b>"]
    assert page.locator(".level").evaluate_all("els => els.map(el => el.getAttribute('aria-label'))") == [
        "Tank 3 · 30.00 m", "Tank 4 · 40.00 m", "<img src=/bad> 水桶 · 59.00 m"
    ]
    assert page.evaluate("""() =>
        card.shadowRoot.querySelectorAll('[data-zone-id]')[1] === zone &&
        zone.querySelector('.zone-head h2') === heading &&
        zone.querySelectorAll('.level')[1] === level &&
        card.shadowRoot.activeElement === level && blurCount === 0
    """)
    page.keyboard.press("Enter")
    page.locator(f'[data-zone-id="{1:032x}"] .level').nth(1).click()
    assert page.evaluate("activity.events") == [
        ["hass-more-info", {"entityId": "sensor.p1"}],
        ["hass-more-info", {"entityId": "sensor.p1"}],
    ]
    assert page.locator("img").count() == 0


def test_disconnected_card_reads_latest_snapshot_without_ha_helpers(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        document.querySelector('#area').append(card);
    }""")
    assert page.get_by_role("status").inner_text() == "正在載入總覽卡片…"
    page.evaluate("() => { card.hass = snapshot({}, 'zh-TW'); }")
    assert page.get_by_role("status").inner_text() == "沒有可顯示的水桶。"
    page.evaluate("""() => {
        card.remove();
        card.hass = snapshot({'sensor.p2': projection(2)});
        document.querySelector('#area').append(card);
        card.hass = snapshot({'sensor.p3': projection(3)});
    }""")
    page.get_by_role("button", name="Tank 3 · 30.00 m").wait_for()
    assert page.locator(".tank").count() == 1
    assert page.evaluate("activity.loads") == 0
    assert page.get_by_text("正在載入總覽卡片…", exact=True).count() == 0
    page.evaluate("() => { card.hass = snapshot({}); }")
    assert page.get_by_role("status").inner_text() == "沒有可顯示的水桶。"
    assert page.locator(".tank").count() == 0


def test_duplicate_labels_keep_distinct_tiles_through_resize_and_availability(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.states = {'sensor.p1': projection(1), 'sensor.p2': projection(2),
                         'sensor.p3': projection(3, 2, 0)};
        for (const state of Object.values(states)) {
            state.attributes.uninus_water_dashboard.tank.name = 'Same tank';
        }
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.get_by_role("button", name="Same tank · 液位讀值不可用").wait_for()
    assert page.locator(".zone-head h2").all_text_contents() == ["Water zone", "Water zone"]
    page.locator(".tank .level").first.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.evaluate("""() => {
        window.retained = [...card.shadowRoot.querySelectorAll('.level')];
        window.middleButton = retained[1];
        window.blurs = 0;
        middleButton.addEventListener('blur', () => blurs++);
    }""")
    assert page.evaluate("card.shadowRoot.activeElement === middleButton")
    page.evaluate("""() => {
        document.querySelector('#area').style.width = '740px';
        const next = {...states, 'sensor.renamed': {...states['sensor.p2'], state: '24'}};
        delete next['sensor.p2'];
        card.hass = snapshot(next);
    }""")
    assert page.locator(".level").evaluate_all("els => els.map(el => el.getAttribute('aria-label'))") == [
        "Same tank · 10.00 m", "Same tank · 24.00 m", "Same tank · 30.00 m"
    ]
    assert page.evaluate("""() => {
        const zones = [...card.shadowRoot.querySelectorAll('[data-zone-id]')];
        return innerWidth === 1100 && zones[0].offsetLeft < zones[1].offsetLeft &&
            [...card.shadowRoot.querySelectorAll('.level')].every((tile, i) => tile === retained[i]) &&
            card.shadowRoot.activeElement === middleButton && blurs === 0;
    }""")
    page.keyboard.press("Enter")
    assert page.evaluate("activity.events") == [["hass-more-info", {"entityId": "sensor.renamed"}]]


def test_malformed_or_conflicting_projections_cannot_create_tiles(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const states = {};
        const add = (key, tank, zone, tankIndex, zoneIndex, edit = () => {}) => {
            const state = projection(tank, zone, tankIndex, zoneIndex);
            edit(state.attributes.uninus_water_dashboard);
            states[key] = state;
        };
        add('sensor.good', 1, 1, 7, 4);
        add('sensor.wrong_version', 2, 1, 8, 4, (m) => { m.version = '1'; });
        add('sensor.wrong_uuid', 3, 1, 9, 4, (m) => { m.tank.id = '0'.repeat(31) + 'A'; });
        add('sensor.wrong_index', 4, 1, 10, 4, (m) => { m.tank.index = -1; });
        add('sensor.wrong_name', 5, 1, 11, 4, (m) => { m.zone.name = []; });
        add('switch.not_a_level', 6, 1, 12, 4);
        add('sensor.malformed_key!', 7, 1, 13, 4);
        add('sensor.zone_conflict_first', 8, 2, 0, 5);
        add('sensor.zone_conflict_second', 9, 2, 1, 5, (m) => { m.zone.name = 'conflict'; });
        add('sensor.duplicate_id_first', 10, 3, 0, 6);
        add('sensor.duplicate_id_second', 10, 4, 0, 7);
        add('sensor.duplicate_slot_first', 11, 5, 0, 8);
        add('sensor.duplicate_slot_second', 12, 5, 0, 8);
        add('sensor.duplicate_zone_slot_first', 13, 6, 0, 9);
        add('sensor.duplicate_zone_slot_second', 14, 7, 0, 9);
        states['sensor.friendly_only'] = {state: '25', attributes: {friendly_name: 'Water zone Tank 1'}};
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.get_by_role("button", name="Tank 1 · 10.00 m", exact=True).wait_for()
    assert page.locator(".zone-head h2").all_text_contents() == ["Water zone"]
    assert page.locator(".level").evaluate_all("els => els.map(el => el.getAttribute('aria-label'))") == ["Tank 1 · 10.00 m"]
    page.locator(".level").click()
    assert page.evaluate("activity.events") == [["hass-more-info", {"entityId": "sensor.good"}]]


def test_manual_controls_confirm_start_keep_stop_and_refuse_stale_bindings(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.manual = (role, action, overrides = {}) => {
            const entity_id = `${action === 'status' ? 'sensor' : 'button'}.${role}_${action}`;
            return {entity_id, state: action === 'status' ? overrides.phase ?? 'idle' : 'unknown',
                attributes: {uninus_water_manual: {version: 1, tank_id: id(1), role, action,
                    ...(action === 'status' ? {phase: 'idle', requested: 'off', actual: 'off',
                        pending_on: false, pending_off: false, fault: null, max_runtime_seconds: 60,
                        can_start: true} : {}), ...overrides}}};
        };
        window.states = {'sensor.p1': projection(1), 'sensor.p2': projection(2)};
        for (const role of ['refill', 'alarm']) {
            for (const action of ['status', 'start', 'stop', 'reset']) {
                const state = manual(role, action);
                states[state.entity_id] = state;
            }
        }
        window.calls = [];
        window.service = (...args) => {
            calls.push(args);
            return new Promise((resolve, reject) => calls[calls.length - 1].settle = {resolve, reject});
        };
        window.push = (language = 'en') => {
            card.hass = {...snapshot({...states}, language), connected: true, callService: service};
        };
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        push();
        document.querySelector('#area').append(card);
    }""")
    tank = page.locator(f'[data-tank-id="{1:032x}"]')
    refill = tank.locator('[data-manual-role="refill"]')
    alarm = tank.locator('[data-manual-role="alarm"]')
    start = refill.locator('[data-action="start"]')
    stop = refill.locator('[data-action="stop"]')
    reset = refill.locator('[data-action="reset"]')
    dialog = page.get_by_role("dialog")
    start.click()
    assert dialog.is_visible()
    assert page.evaluate("calls.length") == 0
    page.keyboard.press("Escape")
    assert not dialog.is_visible()
    assert start.evaluate("b => b === b.getRootNode().activeElement")
    assert reset.is_hidden()
    details = tank.locator(".diagnostics")
    tank.locator(".diagnostics-toggle").click()
    assert details.get_by_text("要求：關", exact=True).count() == 2
    assert page.locator(f'[data-tank-id="{2:032x}"]').get_by_role("button", name="手動補水", exact=True).is_disabled()
    start.click()
    dialog.get_by_role("button", name="確認開始", exact=True).click()
    assert page.evaluate("calls") == [["button", "press", {}, {"entity_id": "button.refill_start"}]]
    assert tank.get_by_text("補水輸出：關", exact=True).is_visible()
    assert start.is_disabled()
    page.evaluate("""() => {
        states['button.renamed_refill_start'] = {...states['button.refill_start'], entity_id: 'button.renamed_refill_start'};
        delete states['button.refill_start'];
        push();
    }""")
    assert start.is_disabled(), "renaming an entity cannot retire its unresolved logical Start"
    assert stop.is_enabled()
    before = stop.bounding_box()
    original_stop = stop.element_handle()
    stop.focus()
    page.evaluate("""() => {
        states['sensor.refill_status'] = manual('refill', 'status', {
            phase: 'starting', requested: 'on', pending_on: true, can_start: false});
        push();
    }""")
    assert stop.evaluate("b => b === b.getRootNode().activeElement")
    assert stop.evaluate("(node, old) => node === old", original_stop)
    assert stop.bounding_box() == before
    assert tank.get_by_text("啟動中", exact=True).is_visible()
    assert refill.get_by_text("待處理：開", exact=True).is_visible()
    assert details.get_by_text("要求：開", exact=True).is_visible()
    alarm.get_by_role("button", name="開啟告警", exact=True).click()
    assert dialog.is_visible()
    stop.click()
    assert not dialog.is_visible(), "cross-role confirmation must not block Stop during ON"
    assert page.evaluate("calls[1]") == ["button", "press", {}, {"entity_id": "button.refill_stop"}]
    assert stop.is_disabled()
    alarm.get_by_role("button", name="開啟告警", exact=True).click()
    page.evaluate("""() => {
        states['button.alarm_duplicate'] = {...states['button.alarm_start'], entity_id: 'button.alarm_duplicate'};
        push();
    }""")
    assert not dialog.is_visible()
    assert alarm.get_by_role("button", name="開啟告警", exact=True).is_disabled()
    assert page.evaluate("calls.length") == 2
    page.evaluate("""() => {
        delete states['button.alarm_duplicate'];
        states['sensor.refill_status'] = manual('refill', 'status', {
            phase: 'fault', fault: 'on_unconfirmed', can_start: false});
        calls[0].settle.reject(new Error('raw private error'));
        calls[1].settle.resolve();
        push();
    }""")
    assert not page.get_by_text("raw private error", exact=False).count()
    reset.focus()
    reset.click()
    assert page.evaluate("calls[2]") == ["button", "press", {}, {"entity_id": "button.refill_reset"}]
    assert stop.evaluate("b => b === b.getRootNode().activeElement")
    page.evaluate("""() => {
        states['button.alarm_start'] = manual('alarm', 'start', {version: '1'});
        push('zh-Hant');
    }""")
    assert alarm.get_by_role("button", name="開啟告警", exact=True).is_disabled()
    assert tank.get_by_text("補水輸出：關", exact=True).is_visible()
    page.evaluate("""() => {
        calls[2].settle.resolve();
        states['sensor.refill_status'] = manual('refill', 'status', {
            phase: 'fault', fault: 'restart_required', can_start: false});
        push('zh-Hant');
    }""")
    assert refill.get_by_text("請重新啟動 Home Assistant", exact=False).is_visible()
    assert refill.get_by_role("button", name="清除故障", exact=True).is_disabled()
    assert start.is_disabled()
    assert refill.get_by_role("button", name="停止補水", exact=True).is_enabled()
    page.evaluate("""() => {
        states['sensor.alarm_status'] = {...states['sensor.alarm_status'], state: 'unavailable'};
        push();
    }""")
    assert tank.get_by_text("告警輸出：未知", exact=True).is_visible()
    assert alarm.get_by_role("button", name="關閉告警", exact=True).is_enabled()
    assert alarm.locator('[data-action="reset"]').is_hidden()
    page.evaluate("""() => {
        const old = states['button.alarm_stop'];
        states['button.alarm_bad'] = {...old, entity_id: 'switch.bad'};
        push();
    }""")
    assert alarm.get_by_role("button", name="關閉告警", exact=True).is_disabled()
    assert page.evaluate("calls.length") == 3


def test_approved_console_has_chinese_tabs_and_two_truthful_action_slots(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const entity_id = 'sensor.p1';
        const state = projection(1);
        state.entity_id = entity_id;
        state.state = '1.23';
        const {zone, tank} = state.attributes.uninus_water_dashboard;
        zone.name = '東區';
        tank.name = '東 1 桶';
        state.attributes.unit_of_measurement = 'm';
        state.attributes.uninus_water_display = {version: 1, scale_max_m: 2.5, refill: [0.5, 1.5]};
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = {...snapshot({[entity_id]: state}, 'en-US'), user: {is_admin: true}};
        document.querySelector('#area').append(card);
    }""")
    assert page.get_by_role("heading", name="補水控制", exact=True).is_visible()
    total = page.get_by_role("button", name="操作總覽", exact=True)
    parameters = page.get_by_role("button", name="補水參數", exact=True)
    devices = page.get_by_role("button", name="區域與水桶", exact=True)
    assert total.get_attribute("aria-pressed") == "true"
    tank = page.locator(".tank")
    assert tank.get_by_role("button", name="手動補水", exact=True).is_disabled()
    assert tank.get_by_role("button", name="開啟告警", exact=True).is_disabled()
    assert tank.get_by_text("補水輸出：未提供", exact=True).is_visible()
    toggle = tank.get_by_role("button", name="未提供：詳細狀態", exact=True)
    assert toggle.get_attribute("aria-expanded") == "false"
    assert toggle.evaluate("b => b.parentElement.tagName === 'HEADER'")
    initial_height = tank.bounding_box()["height"]
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert tank.get_by_text("水位量程：0–2.5 m", exact=True).is_visible()
    assert tank.bounding_box()["height"] > initial_height
    toggle.click()
    assert tank.bounding_box()["height"] == initial_height
    tank.get_by_role("button", name="東 1 桶 · 1.23 m", exact=True).click()
    assert page.evaluate("activity.events") == [["hass-more-info", {"entityId": "sensor.p1"}]]
    old = tank.element_handle()
    parameters.click()
    assert parameters.get_attribute("aria-pressed") == "true"
    assert page.get_by_role("heading", name="各桶門檻與顯示量程", exact=True).is_visible()
    assert page.get_by_role("region", name="補水參數", exact=True).get_by_text("水位量程：0–2.5 m", exact=True).is_visible()
    assert not tank.is_visible()
    devices.click()
    assert page.get_by_role("heading", name="區域與水桶清單", exact=True).is_visible()
    assert not page.get_by_role("heading", name="各桶門檻與顯示量程", exact=True).is_visible()
    assert page.get_by_text("東 1 桶", exact=True).last.is_visible()
    total.click()
    assert tank.evaluate("(node, old) => node === old", old)


def test_rejected_start_with_unchanged_feedback_leaves_a_guarded_stop_available(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const metadata = {version: 1, tank_id: id(1), role: 'refill'};
        const status = {phase: 'idle', requested: 'off', actual: 'off', pending_on: false,
            pending_off: false, fault: null, max_runtime_seconds: 60, can_start: true};
        const wire = (entity_id, state, action, fields = {}) => ({entity_id, state,
            attributes: {uninus_water_manual: {...metadata, action, ...fields}}});
        const states = {'sensor.p1': projection(1),
            'sensor.refill_status': wire('sensor.refill_status', 'idle', 'status', status),
            'button.refill_start': wire('button.refill_start', 'unknown', 'start'),
            'button.refill_stop': wire('button.refill_stop', 'unknown', 'stop')};
        window.calls = [];
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        card.hass = {...snapshot(states), connected: true, callService: (...args) => {
            calls.push(args);
            if (calls.length === 1) return new Promise((resolve, reject) => { window.rejectStart = reject; });
        }};
        document.querySelector('#area').append(card);
    }""")
    start = page.get_by_role("button", name="手動補水", exact=True)
    stop = page.get_by_role("button", name="停止補水", exact=True)
    start.click()
    page.get_by_role("button", name="確認開始", exact=True).click()
    assert stop.is_enabled()
    page.evaluate("() => { rejectStart(new Error('transport interrupted')); }")
    assert page.get_by_text("未確認按鈕請求。", exact=True).is_visible()
    assert stop.is_visible() and stop.is_enabled()
    assert page.get_by_text("補水輸出：關", exact=True).is_visible()
    stop.click()
    assert page.evaluate("calls") == [
        ["button", "press", {}, {"entity_id": "button.refill_start"}],
        ["button", "press", {}, {"entity_id": "button.refill_stop"}],
    ]


@pytest.fixture
def remap_page(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const idle = {phase: 'idle', requested: 'off', actual: 'off', pending_on: false,
            pending_off: false, fault: null, max_runtime_seconds: 60, can_start: true};
        const wire = (entity_id, action) => ({entity_id, state: action === 'status' ? 'idle' : 'unknown',
            attributes: {uninus_water_manual: {version: 1, tank_id: id(1), role: 'refill', action,
                ...(action === 'status' ? idle : {})}}});
        window.states = {'sensor.p1': projection(1),
            'sensor.refill_status': wire('sensor.refill_status', 'status'),
            'button.refill_start': wire('button.refill_start', 'start'),
            'button.refill_stop': wire('button.refill_stop', 'stop')};
        window.calls = [];
        window.replies = [];
        const service = (...args) => {
            calls.push(args);
            return new Promise((resolve, reject) => replies.push({resolve, reject}));
        };
        window.push = ({visible = states, connected = true, isAdmin = true} = {}) => {
            card.hass = {...snapshot({...visible}), connected, user: {id: 'fixture-user', is_admin: isAdmin},
                callService: service};
        };
        window.rename = (from, to) => {
            states[to] = {...states[from], entity_id: to};
            delete states[from];
            push();
        };
        window.publishStatus = (fields) => {
            const current = states['sensor.refill_status'];
            states[current.entity_id] = {...current, state: fields.phase ?? idle.phase,
                attributes: {uninus_water_manual: {...current.attributes.uninus_water_manual, ...idle, ...fields}}};
            push();
        };
        window.settle = async (index, reject = false) => {
            if (reject) replies[index].reject(new Error('private error from retired mapping'));
            else replies[index].resolve();
            await new Promise(requestAnimationFrame);
        };
        window.card = normalCard();
        card.setConfig(card.constructor.getStubConfig());
        push();
        document.querySelector('#area').append(card);
    }""")
    return page


def manual_target(entity):
    return ["button", "press", {}, {"entity_id": entity}]


def refill_actions(page):
    panel = page.locator('[data-manual-role="refill"]')
    return panel.locator('[data-action="start"]'), panel.locator('[data-action="stop"]')


def confirm_refill(page, start):
    start.click()
    page.get_by_role("button", name="確認開始", exact=True).click()


def test_start_rename_then_original_rejection_keeps_current_guarded_stop(remap_page):
    page = remap_page
    start, stop = refill_actions(page)
    confirm_refill(page, start)
    assert page.evaluate("calls") == [manual_target("button.refill_start")]
    assert stop.is_visible() and stop.is_enabled() and start.is_disabled()
    retained = stop.element_handle()
    stop.focus()
    page.evaluate("rename('button.refill_start', 'button.renamed_start')")
    assert stop.is_visible() and stop.is_enabled() and start.is_disabled()
    page.evaluate("settle(0, true)")
    assert stop.is_visible() and stop.is_enabled(), "ambiguous Start must still present the current Stop"
    assert start.is_hidden() and start.is_disabled()
    assert stop.evaluate("(b, old) => b === old && b === b.getRootNode().activeElement", retained)
    assert page.get_by_text("補水輸出：關", exact=True).is_visible()
    assert page.get_by_text("未確認按鈕請求。", exact=True).count() == 0
    assert page.get_by_text("private error", exact=False).count() == 0
    page.evaluate("rename('button.refill_stop', 'button.renamed_stop')")
    assert stop.evaluate("(b, old) => b === old && b === b.getRootNode().activeElement", retained)
    stop.click()
    assert stop.is_visible() and stop.is_disabled() and start.is_disabled()
    page.evaluate("settle(1, true)")
    assert stop.is_enabled() and start.is_disabled()
    stop.click()
    page.evaluate("settle(2)")
    assert stop.is_hidden() and start.is_visible() and start.is_enabled()
    assert page.locator(".level").evaluate("b => b === b.getRootNode().activeElement")
    assert page.get_by_text("補水輸出：關", exact=True).is_visible()
    confirm_refill(page, start)
    assert page.evaluate("calls") == [
        manual_target("button.refill_start"), manual_target("button.renamed_stop"),
        manual_target("button.renamed_stop"), manual_target("button.renamed_start"),
    ]
    page.evaluate("settle(3)")


@pytest.mark.parametrize("gap", ["detach", "offline", "permissions", "duplicate-status", "duplicate-stop"])
def test_uncertain_start_survives_lost_visibility_without_using_stale_routes(remap_page, gap):
    page = remap_page
    start, stop = refill_actions(page)
    confirm_refill(page, start)
    page.evaluate("""gap => {
        if (gap === 'detach') card.remove();
        if (gap === 'offline') push({connected: false});
        if (gap === 'permissions') push({visible: {}, isAdmin: false});
        if (gap.includes('duplicate')) {
            const key = gap === 'duplicate-status' ? 'sensor.refill_status' : 'button.refill_stop';
            const duplicate = key.replace('refill_', 'ambiguous_');
            push({visible: {...states, [duplicate]: {...states[key], entity_id: duplicate}}});
        }
    }""", gap)
    if gap in ("detach", "permissions"):
        assert start.count() == stop.count() == 0
    else:
        assert start.is_disabled() and stop.is_disabled()
    page.evaluate("settle(0, true)")
    assert page.evaluate("calls") == [manual_target("button.refill_start")]
    page.evaluate("""gap => {
        if (gap === 'detach') document.querySelector('#area').append(card);
        push({isAdmin: gap !== 'permissions'});
    }""", gap)
    assert start.is_hidden() and start.is_disabled() and stop.is_visible() and stop.is_enabled()
    assert page.get_by_text("未確認按鈕請求。", exact=True).count() == 0
    stop.click()
    page.evaluate("settle(1)")
    assert start.is_visible() and start.is_enabled()
    assert page.evaluate("calls") == [manual_target("button.refill_start"), manual_target("button.refill_stop")]


def test_old_stop_reply_cannot_clear_uncertainty_after_stop_remap_or_before_start_settlement(remap_page):
    page = remap_page
    start, stop = refill_actions(page)
    confirm_refill(page, start)
    stop.click()
    page.evaluate("settle(1)")
    page.evaluate("settle(0, true)")
    assert start.is_disabled() and stop.is_visible() and stop.is_enabled()
    stop.click()
    page.evaluate("rename('button.refill_stop', 'button.renamed_stop')")
    assert stop.is_disabled(), "Stop remap cannot release the unresolved logical Stop"
    page.evaluate("settle(2)")
    assert start.is_disabled() and stop.is_visible() and stop.is_enabled()
    stop.click()
    page.evaluate("settle(3)")
    assert start.is_enabled() and stop.is_hidden()
    assert page.evaluate("calls") == [
        manual_target("button.refill_start"), manual_target("button.refill_stop"),
        manual_target("button.refill_stop"), manual_target("button.renamed_stop"),
    ]


def test_only_authoritative_output_cycle_retires_uncertainty_without_stop(remap_page):
    page = remap_page
    start, stop = refill_actions(page)
    confirm_refill(page, start)
    page.evaluate("settle(0, true)")
    page.evaluate("publishStatus({can_start: false})")
    page.evaluate("publishStatus({})")
    assert start.is_disabled() and stop.is_visible() and stop.is_enabled()
    page.evaluate("publishStatus({phase: 'starting', requested: 'on', pending_on: true, can_start: false})")
    assert start.is_disabled() and stop.is_visible() and stop.is_enabled()
    page.evaluate("publishStatus({phase: 'stopping', pending_off: true, can_start: false})")
    assert start.is_disabled() and stop.is_visible() and stop.is_enabled()
    page.evaluate("publishStatus({})")
    assert start.is_visible() and start.is_enabled() and stop.is_hidden()
    assert page.evaluate("calls") == [manual_target("button.refill_start")]
