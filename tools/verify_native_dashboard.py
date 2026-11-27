"""Exercise the actual bundled Home Assistant frontend on disposable localhost only.

Run with the repo's Python 3.13 test environment after installing Playwright and
HA's matching frontend; set WATER_CHROMIUM_EXECUTABLE for a local Chromium fork.
This fixture has no connection to any existing HA host or browser profile.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import json
import logging
import os
import shutil
import socket
from tempfile import TemporaryDirectory

# HA's bootstrap imports LiteLLM indirectly; skip its optional remote price map.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "uninus_water_control"
TITLE = "水量總覽驗證"
CARD_TAG = "uninus-water-dashboard"
URL_PATH = "/uninus_water_control/dashboard.js"
MANUAL = "uninus_water_manual"
OUTPUTS = {role: f"switch.browser_{role}" for role in ("refill", "alarm")}


class SyntheticOutputs:
    """Registry-bound outputs, deliberately separate service receipt and HA feedback."""

    def __init__(self, hass):
        self.hass = hass
        self.calls = asyncio.Queue()
        self.alarm_on = asyncio.Event()
        registry = er.async_get(hass)
        for role, entity_id in OUTPUTS.items():
            row = registry.async_get_or_create(
                "switch", "browser_probe", role, suggested_object_id=f"browser_{role}"
            )
            assert row.entity_id == entity_id
            hass.states.async_set(entity_id, "off")
        row = registry.async_get_or_create(
            "sensor", "browser_probe", "first_depth", suggested_object_id="test_depth_1"
        )
        assert row.entity_id == "sensor.test_depth_1"
        for service in ("turn_on", "turn_off"):
            hass.services.async_register("switch", service, self.handle)

    async def handle(self, call):
        entity_id = call.data["entity_id"]
        assert entity_id in OUTPUTS.values()
        state = "on" if call.service == "turn_on" else "off"
        self.calls.put_nowait((entity_id, state))
        if entity_id == OUTPUTS["alarm"] and state == "on":
            await self.alarm_on.wait()
        self.hass.states.async_set(entity_id, state)

    async def expect(self, role, state):
        assert await asyncio.wait_for(self.calls.get(), 4) == (OUTPUTS[role], state)


async def native_commission(hass, entry):
    """Drive real HA Options FlowManager and verify persisted native setup."""
    manager = hass.config_entries.options
    menu = await manager.async_init(entry.entry_id)
    select = await manager.async_configure(menu["flow_id"], {"next_step_id": "edit_tank"})
    tank = entry.options.get("zones", entry.data["zones"])[0]["tanks"][0]
    form = await manager.async_configure(select["flow_id"], {"tank_id": tank["id"]})
    result = await manager.async_configure(form["flow_id"], {
        "tank_name": "一號水桶", "level_sensor": "sensor.test_depth_1",
        "refill_switch": OUTPUTS["refill"], "alarm_switch": OUTPUTS["alarm"],
        "commission_refill": True, "commission_alarm": True, "confirm_device_cutoff": True,
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert entry.options["manual"] == {
        "tank_id": tank["id"],
        **{
            role: {"output": registry.async_get(entity_id).id,
                   "source": registry.async_get("sensor.test_depth_1").id if role == "refill" else None,
                   "cutoff_confirmed": True, "max_runtime_seconds": 60}
            for role, entity_id in OUTPUTS.items()
        },
    }
    native = {}
    for state in hass.states.async_all():
        identity = state.attributes.get(MANUAL)
        if not identity:
            continue
        assert identity["version"] == 1 and identity["tank_id"] == tank["id"], identity
        key = identity["role"], identity["action"]
        assert key not in native
        native[key] = state.entity_id
    assert set(native) == {(role, action) for role in OUTPUTS for action in ("status", "start", "stop", "reset")}
    return tank["id"], native


def loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stage_fixture(root: Path) -> None:
    shutil.copytree(ROOT / "custom_components" / DOMAIN, root / "custom_components" / DOMAIN,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    storage = root / ".storage"
    storage.mkdir()
    # Only skip unrelated onboarding steps in this fresh fixture. Auth and all
    # Lovelace writes below use the actual registered HA frontend/WS path.
    (storage / "onboarding").write_text(json.dumps({
        "version": 4, "minor_version": 1, "key": "onboarding",
        "data": {"done": ["user", "core_config", "integration", "analytics"]},
    }), encoding="utf-8")


async def native_entry(hass):
    setup = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(setup["flow_id"], {
        "zone_name": "育苗區", "tank_name": "一號水桶", "level_sensor": "sensor.test_depth_1",
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state == ConfigEntryState.LOADED, entry.state
    return entry


async def native_add_tank(hass, entry, number: int) -> None:
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    select = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "add_tank"})
    zone = next(iter(entry.options.get("zones", entry.data["zones"])))
    form = await hass.config_entries.options.async_configure(select["flow_id"], {"zone_id": zone["id"]})
    result = await hass.config_entries.options.async_configure(form["flow_id"], {
        "tank_name": f"第 {number} 桶", "level_sensor": f"sensor.test_depth_{number}",
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


async def native_login(page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "() => !!document.querySelector('home-assistant')?.hass?.user?.is_admin",
        timeout=12000,
    )


async def verify_manual(page, shell, hass, entry, outputs, calls, screenshot_dir):
    await expect(shell.locator("[data-manual-role]")).to_have_count(0)
    tank_id, native = await native_commission(hass, entry)
    tank = shell.locator(f'[data-tank-id="{tank_id}"]')
    refill = tank.locator('[data-manual-role="refill"]')
    alarm = tank.locator('[data-manual-role="alarm"]')
    dialog = shell.get_by_role("dialog")
    await expect(shell.locator("[data-manual-role]")).to_have_count(2)
    await expect(shell.locator(f'[data-tank-id]:not([data-tank-id="{tank_id}"]) [data-manual-role]')).to_have_count(0)
    for width in (260, 380, 740):
        await shell.evaluate("(el, w) => { el.style.width = `${w}px`; }", width)
        assert await tank.evaluate("""el => {
            const {left, right} = el.getBoundingClientRect();
            return [...el.querySelectorAll('.manual, .manual ha-button')].every(node => {
                const r = node.getBoundingClientRect();
                return r.width > 0 && r.left >= left && r.right <= right && node.scrollWidth <= node.clientWidth + 1;
            });
        }"""), width
        if screenshot_dir is not None and width == 380:
            await shell.screenshot(path=screenshot_dir / "native-manual-idle-380.png")
    assert not calls and outputs.calls.empty()

    async def start(panel):
        button = panel.get_by_role("button", name="Start", exact=True)
        assert await panel.locator("ha-button").count() == 3
        assert await panel.evaluate("el => !!customElements.get('ha-button') && el.querySelector('ha-button').matches(':defined')")
        await button.click()
        await expect(dialog).to_be_visible()
        await dialog.get_by_role("button", name="Confirm start").click()

    async def feedback(panel, role, actual):
        await expect(panel.get_by_text(f"HA feedback: {actual.title()}", exact=True)).to_be_visible()
        assert hass.states.get(OUTPUTS[role]).state == actual
        status = hass.states.get(native[(role, "status")]).attributes[MANUAL]
        assert status["actual"] == actual, status
        return status

    refill_start = refill.get_by_role("button", name="Start", exact=True)
    await refill_start.click()
    await expect(dialog).to_be_visible()
    if screenshot_dir is not None:
        await shell.screenshot(path=screenshot_dir / "native-manual-confirmation-740.png")
    await page.keyboard.press("Escape")
    await expect(dialog).not_to_be_visible()
    assert not calls and outputs.calls.empty()
    await expect(refill_start).to_be_focused()
    await start(refill)
    await outputs.expect("refill", "on")
    await expect(refill.get_by_text("Running", exact=True)).to_be_visible()
    assert (await feedback(refill, "refill", "on"))["requested"] == "on"
    await feedback(alarm, "alarm", "off")
    if screenshot_dir is not None:
        await shell.screenshot(path=screenshot_dir / "native-manual-running-740.png")
    await refill.get_by_role("button", name="Stop", exact=True).click()
    await outputs.expect("refill", "off")
    await expect(refill.get_by_text("Idle", exact=True)).to_be_visible()
    assert (await feedback(refill, "refill", "off"))["requested"] == "off"

    await start(alarm)
    await outputs.expect("alarm", "on")
    await expect(alarm.get_by_text("Starting", exact=True)).to_be_visible()
    status = await feedback(alarm, "alarm", "off")
    assert status["requested"] == "on" and status["pending_on"], status
    await feedback(refill, "refill", "off")
    await refill_start.click()
    await expect(dialog).to_be_visible()
    await alarm.get_by_role("button", name="Stop", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await outputs.expect("alarm", "off")
    await expect(alarm.get_by_text("Stopping", exact=True)).to_be_visible()
    assert hass.states.get(native[("alarm", "status")]).attributes[MANUAL]["pending_on"]
    outputs.alarm_on.set()
    await outputs.expect("alarm", "off")
    await expect(alarm.get_by_text("Idle", exact=True)).to_be_visible()
    await feedback(alarm, "alarm", "off")
    await feedback(refill, "refill", "off")

    reset = refill.get_by_role("button", name="Clear fault", exact=True)
    await expect(reset).to_be_disabled()
    hass.states.async_set(OUTPUTS["refill"], "on")  # Simulate unrequested feedback, not a browser command.
    await outputs.expect("refill", "off")
    await expect(refill.get_by_text("Fault: Unexpected ON", exact=True)).to_be_visible()
    await feedback(refill, "refill", "off")
    await reset.click()
    await expect(refill.get_by_text("Idle", exact=True)).to_be_visible()
    assert hass.states.get(native[("refill", "status")]).attributes[MANUAL]["fault"] is None
    assert outputs.calls.empty()
    assert [(m.get("domain"), m.get("service"), m.get("target"), m.get("service_data")) for m in calls] == [
        ("button", "press", {"entity_id": native[(role, action)]}, {})
        for role, action in (("refill", "start"), ("refill", "stop"), ("alarm", "start"), ("alarm", "stop"), ("refill", "reset"))
    ], calls
    print("PASS: native Options commissioning readback; bundled HA Start/Stop/Reset clicks; exact button.press targets; independent synthetic feedback and alarm stop during held ON; no other tank controls")


async def run_browser(hass, entry, outputs, base_url: str, screenshot_dir: Path | None, login_only: bool) -> None:
    async with async_playwright() as playwright:
        exe = os.environ.get("WATER_CHROMIUM_EXECUTABLE")
        browser = await playwright.chromium.launch(executable_path=exe, headless=True)
        async with await browser.new_context(viewport={"width": 1180, "height": 900}, locale="en-US") as context:
            external_requests: list[str] = []
            errors: list[str] = []
            async def restrict(route) -> None:
                if route.request.url.startswith(base_url + "/"):
                    await route.continue_()
                else:
                    external_requests.append(route.request.url.split("?", 1)[0])
                    await route.abort()
            await context.route("**/*", restrict)
            await context.add_init_script("""
                window.waterTestUncaught = [];
                window.addEventListener('unhandledrejection', e => {
                    const r=e.reason;
                    window.waterTestUncaught.push({code:r?.code, message:r?.message, name:r?.name});
                });
            """)
            page = await context.new_page()
            calls: list[dict] = []

            def record_frame(payload):
                if isinstance(payload, str):
                    message = json.loads(payload)
                    if message.get("type") == "call_service":
                        calls.append(message)

            page.on("websocket", lambda ws: ws.on("framesent", record_frame))
            page.on("pageerror", lambda error: errors.append(str(error)))
            await native_login(page, base_url)
            if login_only:
                print("PASS: fresh native Home Assistant frontend and localhost-only trusted session")
                return
            resource = await page.evaluate("""async (args) => {
                const hass = document.querySelector('home-assistant').hass;
                return hass.callWS({type: 'lovelace/resources/create', res_type:'module', url:args});
            }""", URL_PATH)
            assert resource["url"] == URL_PATH and resource["type"] == "module", resource
            await page.evaluate("""async ([title, tag]) => {
                const {hass} = document.querySelector('home-assistant');
                await hass.callWS({type:'lovelace/config/save', config:{title, views:[{
                    title, path: 'water-test', type:'panel', cards:[{type:'custom:' + tag}]
                }]}});
            }""", [TITLE, CARD_TAG])
            await page.goto(base_url + "/lovelace/water-test", wait_until="domcontentloaded")
            shell = page.locator(CARD_TAG)
            tiles = shell.locator("hui-tile-card")
            headings = shell.locator("hui-heading-card")
            await tiles.first.wait_for()
            assert await tiles.count() == 5
            assert await headings.count() == 1
            await tiles.first.get_by_text("1.25 m", exact=False).wait_for()
            await tiles.first.get_by_text("一號水桶", exact=True).wait_for()
            await tiles.first.locator("ha-tile-icon").click(timeout=8000)
            details = page.locator("ha-more-info-dialog ha-dialog[open]")
            await details.wait_for(state="attached", timeout=10000)
            await page.keyboard.press("Escape")
            await details.wait_for(state="detached", timeout=10000)
            before = await tiles.first.element_handle()
            hass.states.async_set("sensor.test_depth_1", "183", {"unit_of_measurement": "cm"})
            await tiles.first.get_by_text("1.83 m", exact=False).wait_for()
            assert await tiles.first.evaluate("(tile, old) => tile === old", before)
            await native_add_tank(hass, entry, 6)
            await page.wait_for_function("""tag => {
                function count(node) {
                   return (node.querySelectorAll('hui-tile-card').length || 0) +
                      [...node.querySelectorAll('*')].reduce((n, it) => n + (it.shadowRoot ? count(it.shadowRoot) : 0), 0);
                }
                return count(document) === 6;
            }""", arg=CARD_TAG)
            geometry = []
            for width in (260, 380, 530, 740, 1020):
                await shell.evaluate("(el, w) => { el.style.width = `${w}px`; }", width)
                positions = await tiles.evaluate_all("list => list.map(el => {const r=el.getBoundingClientRect(); return [r.x,r.y,r.width,r.right]})")
                first = sum(abs(x[1] - positions[0][1]) < 1 for x in positions)
                bounds = await shell.bounding_box()
                assert bounds and all(x[0] >= bounds["x"] - 1 and x[3] <= bounds["x"] + bounds["width"] + 1 for x in positions), (width, positions, bounds)
                geometry.append((width, first))
                if screenshot_dir is not None and width in (380, 740):
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    await shell.screenshot(path=screenshot_dir / f"native-water-{width}.png")
            assert set(groups for _, groups in geometry) >= {1, 2, 3, 4}, geometry
            await shell.evaluate("el => { el.style.width = '740px'; }")
            await verify_manual(page, shell, hass, entry, outputs, calls, screenshot_dir)
            rejections = await page.evaluate("window.waterTestUncaught")
            assert not errors and not rejections, (errors, rejections)
            print(f"PASS: native resource/dashboard WS readback, Heading, Tile, More info, live level and inventory change; same viewport grid {geometry}; external requests blocked={len(external_requests)}")


async def main(login_only: bool, screenshots: Path | None) -> None:
    with TemporaryDirectory(prefix="water-native-browser-") as temp:
        root = Path(temp)
        stage_fixture(root)
        hass = HomeAssistant(str(root))
        loader.async_setup(hass)
        hass.config.skip_pip = True
        port = loopback_port()
        auth_provider = {"type": "trusted_networks", "trusted_networks": ["127.0.0.1/32"], "allow_bypass_login": True}
        core_config = {"homeassistant": {"auth_providers": [auth_provider]},
                       "http": {"server_host": "127.0.0.1", "server_port": port},
                       "frontend": {}, "history": {}, "recorder": {}}
        outputs = None
        try:
            assert await bootstrap.async_from_config_dict(core_config, hass) is hass
            assert "frontend" in hass.config.components and "lovelace" in hass.config.components
            owner = await hass.auth.async_create_user("Local Water Test", group_ids=[GROUP_ID_ADMIN], local_only=True)
            assert owner.is_admin
            if not login_only:
                outputs = SyntheticOutputs(hass)
                for i in range(1, 7):
                    hass.states.async_set(f"sensor.test_depth_{i}", str(110 + 15 * i), {"unit_of_measurement": "cm"})
                entry = await native_entry(hass)
                for pos in range(2, 6):
                    await native_add_tank(hass, entry, pos)
            else:
                entry = None
            await hass.async_start()
            await run_browser(hass, entry, outputs, f"http://127.0.0.1:{port}", screenshots, login_only)
        finally:
            if outputs:
                outputs.alarm_on.set()
            await hass.async_stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-only", action="store_true", help="Check loopback HA frontend boot only")
    parser.add_argument("--screenshots", type=Path, help="Explicit destination for optional PNGs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("aiohttp.access").disabled = True
    asyncio.run(main(args.login_only, args.screenshots))
