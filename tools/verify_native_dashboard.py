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
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "uninus_water_control"
TITLE = "水量總覽驗證"
CARD_TAG = "uninus-water-dashboard"
URL_PATH = "/uninus_water_control/dashboard.js"


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


async def run_browser(hass, entry, base_url: str, screenshot_dir: Path | None, login_only: bool) -> None:
    async with async_playwright() as playwright:
        exe = os.environ.get("WATER_CHROMIUM_EXECUTABLE")
        browser = await playwright.chromium.launch(executable_path=exe, headless=True)
        async with await browser.new_context(viewport={"width": 1180, "height": 900}) as context:
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
            page.on("pageerror", lambda error: errors.append(str(error)))
            await native_login(page, base_url)
            if login_only:
                print("PASS: fresh native Home Assistant frontend and localhost-only trusted session")
                return
            native_hass = "document.querySelector('home-assistant').hass"
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
            assert not errors, (errors, await page.evaluate("window.waterTestUncaught"))
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
        try:
            assert await bootstrap.async_from_config_dict(core_config, hass) is hass
            assert "frontend" in hass.config.components and "lovelace" in hass.config.components
            owner = await hass.auth.async_create_user("Local Water Test", group_ids=[GROUP_ID_ADMIN], local_only=True)
            assert owner.is_admin
            if not login_only:
                for i in range(1, 7):
                    hass.states.async_set(f"sensor.test_depth_{i}", str(110 + 15 * i), {"unit_of_measurement": "cm"})
                entry = await native_entry(hass)
                for pos in range(2, 6):
                    await native_add_tank(hass, entry, pos)
            else:
                entry = None
            await hass.async_start()
            await run_browser(hass, entry, f"http://127.0.0.1:{port}", screenshots, login_only)
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-only", action="store_true", help="Check loopback HA frontend boot only")
    parser.add_argument("--screenshots", type=Path, help="Explicit destination for optional PNGs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("aiohttp.access").disabled = True
    asyncio.run(main(args.login_only, args.screenshots))
