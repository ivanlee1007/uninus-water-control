"""Chromium geometry and lifecycle of the shipped shell with controlled card helpers.

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
        page.set_content("""<style>
            body { background: var(--primary-background-color); color: var(--primary-text-color);
                   font: 16px system-ui; }
            #area { width: 300px; }
            .dark { --primary-background-color: #14191e; --primary-text-color: #eee;
                    --fixture-tile-surface: #2e3740; }
            p, h2 { margin: 0; }
            fixture-heading { display: block; font-size: 20px; font-weight: 600; }
            fixture-tile button { display: block; width: 100%; min-height: 56px;
                  border: 1px solid #888; border-radius: 12px; color: inherit;
                  background: var(--fixture-tile-surface, #ecf3fa); text-align: start;
                  overflow-wrap: anywhere; }
            fixture-tile button + button { min-height: 0; }
        </style><main id="area"></main>""")
        page.evaluate("""() => {
            window.activity = {loads: 0, cards: [], config: 0, routes: [], events: []};
            for (const name of ['hass-more-info', 'config-changed', 'call-service']) {
                document.addEventListener(name, (e) => activity.events.push([name, e.detail]));
            }
            class Heading extends HTMLElement {
                setConfig(config) { activity.config++; this.config = config; this.textContent = config.heading; }
            }
            class Tile extends HTMLElement {
                constructor() {
                    super();
                    this.attachShadow({mode: 'open'});
                    const style = document.createElement('style');
                    style.textContent = `:host { display:block } button {
                        width: 100%; min-height: 44px; border-radius: 12px; color: inherit;
                        font: inherit; font-size: 14px; overflow-wrap: anywhere; text-align: start;
                        border: 1px solid #888; background: var(--fixture-tile-surface, #ecf3fa); }
                        .icon { min-height: 20px; margin-top: 2px; font-size: 12px; }`;
                    this.shadowRoot.append(style);
                    for (const [part, action] of [['body', 'tap_action'], ['icon', 'icon_tap_action']]) {
                        const button = document.createElement('button');
                        button.className = part;
                        button.addEventListener('click', () => {
                            if (this.config[action]?.action === 'more-info') {
                                button.dispatchEvent(new CustomEvent('hass-more-info', {
                                    detail: {entityId: this.config.entity}, bubbles: true, composed: true,
                                }));
                            }
                        });
                        this.shadowRoot.append(button);
                    }
                    this.shadowRoot.querySelector('.icon').textContent = 'More info';
                }
                setConfig(config) { activity.config++; this.config = config; this.render(); }
                set hass(value) { this.latest = value; this.render(); }
                render() {
                    if (!this.config || !this.latest) return;
                    const reading = this.latest.states[this.config.entity]?.state;
                    this.shadowRoot.querySelector('.body').textContent = `${this.config.name} · ${reading}`;
                }
            }
            customElements.define('fixture-heading', Heading);
            customElements.define('fixture-tile', Tile);
            window.helpers = {createCardElement(config) {
                activity.cards.push(config);
                const card = document.createElement(`fixture-${config.type}`);
                card.setConfig(config);
                return card;
            }};
            window.loadCardHelpers = async () => { activity.loads++; return helpers; };
            window.id = (n) => n.toString(16).padStart(32, '0');
            window.projection = (tank, zone = 1, index = tank - 1, zoneIndex = zone - 1) => ({
                state: tank === 2 ? 'unavailable' : String(tank * 10),
                attributes: {
                    friendly_name: 'DO NOT USE RAW NAME',
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
        window.card = document.createElement('uninus-water-dashboard');
        if (card.setConfig) card.setConfig({type: 'custom:uninus-water-dashboard'});
        window.states = Object.fromEntries([5, 2, 4, 1, 3].map(n => [`sensor.p${n}`, projection(n)]));
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    tiles = page.locator("fixture-tile")
    try:
        tiles.first.wait_for()
    except playwright.TimeoutError:
        pass
    assert tiles.count() == 5, "five native tile slots must be created from hass.states"
    assert page.locator("fixture-heading").all_text_contents() == ["Water zone"]
    assert page.locator("fixture-tile .body").all_text_contents() == [
        "Tank 1 · 10", "Tank 2 · unavailable", "Tank 3 · 30", "Tank 4 · 40", "Tank 5 · 50"
    ]
    assert page.evaluate("""() => ({
        stub: card.constructor.getStubConfig(), grid: card.getGridOptions(),
        count: customCards.filter((item) => item.type === card.localName).length,
    })""") == {
        "stub": {"type": "custom:uninus-water-dashboard"},
        "grid": {"columns": "full", "rows": "auto", "min_columns": 3},
        "count": 1,
    }
    assert page.evaluate("activity.cards") == [
        {"type": "heading", "heading": "Water zone"},
        *[
            {"type": "tile", "entity": f"sensor.p{n}", "name": f"Tank {n}",
             "tap_action": {"action": "more-info"}, "icon_tap_action": {"action": "more-info"}}
            for n in range(1, 6)
        ],
    ]
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
                tiles: [...zoneHosts[0].querySelectorAll('fixture-tile')].map(rect)};
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
        document.body.classList.add('dark');
        card.hass = snapshot({...states, 'sensor.p6': projection(6, 2, 0)});
    }""")
    page.locator("fixture-tile").nth(5).wait_for()
    measure(690, 4)
    measure(720, 2, 2)
    if evidence_dir:
        directory = Path(evidence_dir)
        page.locator("#area").screenshot(path=directory / "shell-dark-720.png")
        (directory / "geometry.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_live_inventory_keeps_native_nodes_and_keyboard_focus(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.card = document.createElement('uninus-water-dashboard');
        window.states = {'sensor.p1': projection(1), 'sensor.p2': projection(2),
            'sensor.p3': projection(3, 2, 0)};
        document.querySelector('#area').append(card);
        card.hass = snapshot(states);
        card.setConfig({type: 'custom:uninus-water-dashboard'});
    }""")
    page.locator("fixture-tile .body").first.wait_for()
    page.keyboard.press("Tab")
    page.evaluate("""() => {
        window.tile = card.shadowRoot.querySelector('fixture-tile');
        window.body = tile.shadowRoot.querySelector('.body');
        window.zone = card.shadowRoot.querySelector('[data-zone-id]');
        window.heading = zone.querySelector('fixture-heading');
        window.blurCount = 0;
        body.addEventListener('blur', () => blurCount++);
        window.beforeConfig = activity.config;
    }""")
    assert page.evaluate("tile.shadowRoot.activeElement === body")
    page.evaluate("""() => {
        states = {...states, 'sensor.p1': {...states['sensor.p1'], state: '59'}};
        window.latest = snapshot(states);
        card.hass = latest;
        card.setConfig({...card.constructor.getStubConfig(), grid_options: {columns: 'full'}});
    }""")
    page.get_by_role("button", name="Tank 1 · 59").wait_for()
    assert page.evaluate("card.shadowRoot.querySelector('fixture-tile') === tile"), "live telemetry must retain native Tile"
    assert page.evaluate("activity.config === beforeConfig && activity.loads === 1")
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
    page.get_by_role("button", name="<img src=/bad> 水桶 · 59").wait_for()
    assert page.locator("fixture-heading").all_text_contents() == ["Water zone", "<b>同名</b>"]
    assert page.locator("fixture-tile .body").all_text_contents() == [
        "Tank 3 · 30", "Tank 4 · 40", "<img src=/bad> 水桶 · 59"
    ]
    assert page.evaluate("""() =>
        card.shadowRoot.querySelectorAll('[data-zone-id]')[1] === zone &&
        zone.querySelector('fixture-heading') === heading &&
        zone.querySelectorAll('fixture-tile')[1] === tile &&
        tile.latest === latest && tile.shadowRoot.activeElement === body && blurCount === 0
    """)
    page.keyboard.press("Enter")
    zone_tiles = page.locator(f'[data-zone-id="{1:032x}"] fixture-tile')
    zone_tiles.nth(1).get_by_role("button", name="More info").click()
    assert page.evaluate("activity.events") == [
        ["hass-more-info", {"entityId": "sensor.p1"}],
        ["hass-more-info", {"entityId": "sensor.p1"}],
    ]
    assert page.locator("img").count() == 0


@pytest.mark.parametrize("old_result", ["late-success", "early-rejection"])
def test_disconnected_helper_flight_cannot_touch_reconnected_card(browser_page, old_result):
    page = browser_page
    page.evaluate("""() => {
        window.flights = [];
        window.loadCardHelpers = () => new Promise((resolve, reject) => flights.push({resolve, reject}));
        window.card = document.createElement('uninus-water-dashboard');
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot({'sensor.p1': projection(1)});
        document.querySelector('#area').append(card);
    }""")
    page.wait_for_function("flights.length === 1")
    page.evaluate("""() => {
        card.remove();
        card.hass = snapshot({'sensor.p2': projection(2)});
        document.querySelector('#area').append(card);
        card.hass = snapshot({'sensor.p3': projection(3)});
    }""")
    page.wait_for_function("flights.length === 2")
    if old_result == "early-rejection":
        page.evaluate("""async () => {
            flights[0].reject(new Error('old private error'));
            await new Promise(requestAnimationFrame);
        }""")
        assert page.get_by_role("status").inner_text() == "Loading dashboard cards…"
    page.evaluate("""() => { flights[1].resolve(helpers); }""")
    page.get_by_role("button", name="Tank 3 · 30").wait_for()
    if old_result == "late-success":
        page.evaluate("""async () => {
            flights[0].resolve({createCardElement() { throw new Error('retired helper used'); }});
            await new Promise(requestAnimationFrame);
        }""")
    assert page.locator("fixture-tile .body").all_text_contents() == ["Tank 3 · 30"]
    assert page.evaluate("activity.cards.length === 2 && flights.length === 2")


def test_bridge_notices_are_localized_and_do_not_poll(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.flights = [];
        window.loadCardHelpers = () => new Promise((resolve, reject) => flights.push({resolve, reject}));
        window.card = document.createElement('uninus-water-dashboard');
        card.setConfig(card.constructor.getStubConfig());
        document.querySelector('#area').append(card);
    }""")
    notice = page.get_by_role("status")
    assert notice.inner_text() == "Loading dashboard cards…"
    page.evaluate("() => { card.hass = snapshot({}, 'zh-TW'); }")
    assert notice.inner_text() == "沒有可顯示的水桶。"
    assert page.evaluate("flights.length") == 0
    page.evaluate("() => { card.hass = snapshot({'sensor.p2': projection(2)}, 'zh-Hant'); }")
    page.wait_for_function("flights.length === 1")
    assert notice.inner_text() == "正在載入總覽卡片…"
    page.evaluate("() => { flights[0].reject(new Error('private error')); }")
    page.get_by_text("無法載入總覽卡片。", exact=True).wait_for()
    page.evaluate("() => { card.hass = snapshot({'sensor.p2': projection(2)}); }")
    assert notice.inner_text() == "Dashboard cards could not be loaded."
    assert page.evaluate("flights.length") == 1
    page.evaluate("() => { card.remove(); document.querySelector('#area').append(card); }")
    page.wait_for_function("flights.length === 2")
    page.evaluate("() => { flights[1].resolve(helpers); }")
    page.get_by_role("button", name="Tank 2 · unavailable").wait_for()
    assert notice.count() == 0
    page.evaluate("() => { card.hass = snapshot({}); }")
    assert notice.inner_text() == "No tanks to display."
    assert page.locator("fixture-tile").count() == 0


def test_duplicate_labels_keep_distinct_tiles_through_resize_and_availability(browser_page):
    page = browser_page
    page.evaluate("""() => {
        window.states = {'sensor.p1': projection(1), 'sensor.p2': projection(2),
                         'sensor.p3': projection(3, 2, 0)};
        for (const state of Object.values(states)) {
            state.attributes.uninus_water_dashboard.tank.name = 'Same tank';
        }
        window.card = document.createElement('uninus-water-dashboard');
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.get_by_role("button", name="Same tank · unavailable").wait_for()
    assert page.locator("fixture-heading").all_text_contents() == ["Water zone", "Water zone"]
    for _ in range(3):
        page.keyboard.press("Tab")
    page.evaluate("""() => {
        window.retained = [...card.shadowRoot.querySelectorAll('fixture-tile')];
        window.middleButton = retained[1].shadowRoot.querySelector('.body');
        window.blurs = 0;
        middleButton.addEventListener('blur', () => blurs++);
    }""")
    assert page.evaluate("retained[1].shadowRoot.activeElement === middleButton")
    page.evaluate("""() => {
        document.querySelector('#area').style.width = '720px';
        const next = {...states, 'sensor.renamed': {...states['sensor.p2'], state: '24'}};
        delete next['sensor.p2'];
        card.hass = snapshot(next);
    }""")
    assert page.locator("fixture-tile .body").all_text_contents() == [
        "Same tank · 10", "Same tank · 24", "Same tank · 30"
    ]
    assert page.evaluate("""() => {
        const zones = [...card.shadowRoot.querySelectorAll('[data-zone-id]')];
        return innerWidth === 1100 && zones[0].offsetLeft < zones[1].offsetLeft &&
            [...card.shadowRoot.querySelectorAll('fixture-tile')].every((tile, i) => tile === retained[i]) &&
            retained[1].shadowRoot.activeElement === middleButton && blurs === 0;
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
        window.card = document.createElement('uninus-water-dashboard');
        card.setConfig(card.constructor.getStubConfig());
        card.hass = snapshot(states);
        document.querySelector('#area').append(card);
    }""")
    page.get_by_role("button", name="Tank 1 · 10", exact=True).wait_for()
    assert page.locator("fixture-heading").all_text_contents() == ["Water zone"]
    assert page.locator("fixture-tile .body").all_text_contents() == ["Tank 1 · 10"]
    assert page.evaluate("activity.cards.filter(({type}) => type === 'tile').map(({entity}) => entity)") == [
        "sensor.good"
    ]
