"""Exercise the actual bundled Home Assistant frontend on disposable localhost only.

Run with the repo's Python 3.13 test environment after installing Playwright and
HA's matching frontend; set WATER_CHROMIUM_EXECUTABLE for a local Chromium fork.
This fixture has no connection to any existing HA host or browser profile.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import importlib.metadata
import json
import logging
from math import ceil
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

# HA's bootstrap imports LiteLLM indirectly; skip its optional remote price map.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components.recorder import get_instance as get_recorder
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, ServiceRegistry, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "uninus_water_control"
TITLE = "水量總覽驗證"
CARD_TAG = "uninus-water-dashboard"
EDITOR_TAG = f"{CARD_TAG}-editor"
URL_PATH = "/uninus_water_control/dashboard.js"
MANUAL = "uninus_water_manual"
PREVIEW = "uninus_water_thresholds"
OUTPUTS = {role: f"switch.browser_{role}" for role in ("refill", "alarm")}
PASSIVE_ALARM = "switch.browser_passive_history"
ALARM_HISTORY_GET = f"{DOMAIN}/alarm_history_get"


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
        for index in (1, 3):
            row = registry.async_get_or_create(
                "sensor", "browser_probe", f"depth_{index}", suggested_object_id=f"test_depth_{index}"
            )
            assert row.entity_id == f"sensor.test_depth_{index}"
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


async def native_add_tank(hass, entry, number: int, alarm=None) -> None:
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    select = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "add_tank"})
    zone = next(iter(entry.options.get("zones", entry.data["zones"])))
    form = await hass.config_entries.options.async_configure(select["flow_id"], {"zone_id": zone["id"]})
    result = await hass.config_entries.options.async_configure(form["flow_id"], {
        "tank_name": f"第 {number} 桶", "level_sensor": f"sensor.test_depth_{number}",
        **({"alarm_switch": alarm} if alarm else {}),
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


REFERENCE_LEVELS = ("1.23", "0.37", "1.67", "0.88", "1.13", "1.41", "0.63", "1.30")


async def seed_reference_layout(hass, entry):
    """Only the disposable synthetic inventory is shaped like the reference."""
    from custom_components.uninus_water_control.inventory import DISPLAY_SCALES, new_tank, new_zone

    options = deepcopy(dict(entry.options))
    first = options["zones"][0]
    first["name"] = "東區"
    del first["tanks"][4:]
    registry = er.async_get(hass)
    second = new_zone(registry, {"zone_name": "西區", "tank_name": "西 1 桶", "level_sensor": "sensor.test_depth_7"})
    second["tanks"].extend(new_tank(registry, {"tank_name": f"西 {i - 6} 桶", "level_sensor": f"sensor.test_depth_{i}"})
                           for i in (8, 9, 10))
    options["zones"].append(second)
    scales = {}
    for zone_label, zone, source_start, samples in (("東", first, 1, REFERENCE_LEVELS[:4]),
                                                      ("西", second, 7, REFERENCE_LEVELS[4:])):
        for index, (tank, sample) in enumerate(zip(zone["tanks"], samples, strict=True)):
            tank["name"] = f"{zone_label} {index + 1} 桶"
            scales[tank["id"]] = 2.5  # Explicit display scale for this fixture only.
            tank["thresholds"] = {"refill_lower_m": 0.5, "refill_upper_m": 1.5,
                                  "alarm_lower_m": 0.4, "alarm_delay_seconds": 1}
            hass.states.async_set(f"sensor.test_depth_{source_start + index}", sample, {"unit_of_measurement": "m"})
    commissioned, east3 = first["tanks"][0], first["tanks"][2]
    source = registry.async_get("sensor.test_depth_3")
    assert source is not None
    for role in OUTPUTS:
        for suffix in ("switch", "switch_registry_id"):
            key = f"{role}_{suffix}"
            if key in commissioned:
                east3[key] = commissioned.pop(key)
    east3["level_sensor_registry_id"] = source.id
    options["manual"]["tank_id"] = east3["id"]
    options["manual"]["refill"]["source"] = source.id
    original_manual = deepcopy(options["manual"])
    assert hass.config_entries.async_update_entry(entry, options=options, data={**entry.data, DISPLAY_SCALES: scales})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED and entry.options["manual"] == original_manual
    levels = [hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, tank["id"]))
              for zone in options["zones"] for tank in zone["tanks"]]
    assert [float(state.state) for state in levels] == [float(value) for value in REFERENCE_LEVELS], levels


SETTINGS_CALL = "msg => document.querySelector('home-assistant').hass.callWS(msg)"


async def read_settings(page, entry):
    return await page.evaluate(SETTINGS_CALL, {"type": f"{DOMAIN}/config_get", "version": 1, "entry_id": entry.entry_id})


async def publish_settings(page, current, changes):
    return await page.evaluate(SETTINGS_CALL, {
        "type": f"{DOMAIN}/config_save", "version": 1, "entry_id": current["entry_id"],
        "if_revision": current["revision"], "if_source_snapshot": current["source_snapshot"], "changes": changes,
    })


async def verify_browser_scale(options, base_url, shell, entry):
    """Exercise the private RPC over native browser auth, not a mock UI."""
    await native_login(options, base_url)
    current = await read_settings(options, entry)
    target = current["zones"][0]["tanks"][0]
    assert target["scale_max_m"] is None
    prior_options, owner = entry.options, entry.runtime_data
    result = await publish_settings(options, current, {"tanks": [{"id": target["id"], "scale_max_m": 2.5}]})
    assert result["revision"] != current["revision"]
    await expect(shell.get_by_role("meter", name="水位量程：0–2.5 m")).to_have_attribute("aria-valuemax", "2.5")
    await native_login(options, base_url)
    assert await read_settings(options, entry) == result
    assert entry.options is prior_options and entry.runtime_data is owner
    assert not await options.evaluate("window.waterTestUncaught")
    await options.close()
    print("PASS: native browser-authenticated scale settings RPC, fresh readback and live measured meter")


async def native_login(page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "() => !!document.querySelector('home-assistant')?.hass?.user?.is_admin",
        timeout=12000,
    )


async def verify_manual(page, shell, hass, entry, outputs, calls, screenshot_dir):
    await expect(shell.get_by_role("button", name="手動補水", exact=True).first).to_be_disabled()
    tank_id, native = await native_commission(hass, entry)
    tank = shell.locator(f'[data-tank-id="{tank_id}"]')
    refill = tank.locator('[data-manual-role="refill"]')
    alarm = tank.locator('[data-manual-role="alarm"]')
    dialog = shell.get_by_role("dialog")
    await expect(tank.get_by_role("button", name="手動補水", exact=True)).to_be_enabled()
    await expect(tank.get_by_role("button", name="開啟告警", exact=True)).to_be_enabled()
    other = shell.locator(f'[data-tank-id]:not([data-tank-id="{tank_id}"])')
    for button in await other.get_by_role("button", name="手動補水", exact=True).all():
        await expect(button).to_be_disabled()
    for button in await other.get_by_role("button", name="開啟告警", exact=True).all():
        await expect(button).to_be_disabled()
    for width in (260, 380, 740):
        await shell.evaluate("(el, w) => { el.style.width = `${w}px`; }", width)
        assert await tank.evaluate("""el => {
            const {left, right} = el.getBoundingClientRect();
            return [...el.querySelectorAll('.action-slot, .action-slot button:not([hidden])')].every(node => {
                const r = node.getBoundingClientRect();
                return r.width > 0 && r.left >= left && r.right <= right && node.scrollWidth <= node.clientWidth + 1;
            });
        }"""), width
        if screenshot_dir is not None and width == 380:
            await shell.screenshot(path=screenshot_dir / "native-manual-idle-380.png")
    assert not calls and outputs.calls.empty()

    async def start(panel):
        button = panel.locator('[data-action="start"]')
        assert await panel.locator("button").count() == 3
        await button.click()
        await expect(dialog).to_be_visible()
        await dialog.get_by_role("button", name="確認開始").click()

    async def feedback(panel, role, actual):
        label = {"refill": "補水輸出", "alarm": "告警輸出"}[role]
        await expect(tank.get_by_text(f"{label}：{'開' if actual == 'on' else '關'}", exact=True)).to_be_visible()
        assert hass.states.get(OUTPUTS[role]).state == actual
        status = hass.states.get(native[(role, "status")]).attributes[MANUAL]
        assert status["actual"] == actual, status
        return status

    refill_start = refill.get_by_role("button", name="手動補水", exact=True)
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
    await expect(tank.get_by_text("補水中", exact=True)).to_be_visible()
    assert (await feedback(refill, "refill", "on"))["requested"] == "on"
    await feedback(alarm, "alarm", "off")
    if screenshot_dir is not None:
        await shell.screenshot(path=screenshot_dir / "native-manual-running-740.png")
    await refill.get_by_role("button", name="停止補水", exact=True).click()
    await outputs.expect("refill", "off")
    await expect(tank.get_by_text("待命", exact=True)).to_be_visible()
    assert (await feedback(refill, "refill", "off"))["requested"] == "off"

    await tank.locator(".diagnostics-toggle").click()
    alarm_details = tank.locator('[data-control-details="alarm"]')
    await start(alarm)
    await outputs.expect("alarm", "on")
    await expect(alarm_details.get_by_text("狀態：啟動中", exact=True)).to_be_visible()
    status = await feedback(alarm, "alarm", "off")
    assert status["requested"] == "on" and status["pending_on"], status
    await feedback(refill, "refill", "off")
    await refill_start.click()
    await expect(dialog).to_be_visible()
    await alarm.get_by_role("button", name="關閉告警", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await outputs.expect("alarm", "off")
    await expect(alarm_details.get_by_text("狀態：停止中", exact=True)).to_be_visible()
    assert hass.states.get(native[("alarm", "status")]).attributes[MANUAL]["pending_on"]
    outputs.alarm_on.set()
    await outputs.expect("alarm", "off")
    await expect(alarm_details.get_by_text("狀態：待命", exact=True)).to_be_visible()
    await feedback(alarm, "alarm", "off")
    await feedback(refill, "refill", "off")

    reset = refill.get_by_role("button", name="清除故障", exact=True)
    await expect(reset).not_to_be_visible()
    hass.states.async_set(OUTPUTS["refill"], "on")  # Simulate unrequested feedback, not a browser command.
    await outputs.expect("refill", "off")
    await expect(refill.get_by_text("故障：非預期開啟", exact=True)).to_be_visible()
    await feedback(refill, "refill", "off")
    await expect(refill.get_by_role("button", name="停止補水", exact=True)).to_be_enabled()
    await reset.focus()
    await reset.click()
    await expect(tank.get_by_text("待命", exact=True)).to_be_visible()
    assert hass.states.get(native[("refill", "status")]).attributes[MANUAL]["fault"] is None
    await expect(tank.locator(".level")).to_be_focused()
    await tank.locator(".diagnostics-toggle").click()
    assert outputs.calls.empty()
    assert [(m.get("domain"), m.get("service"), m.get("target"), m.get("service_data")) for m in calls] == [
        ("button", "press", {"entity_id": native[(role, action)]}, {})
        for role, action in (("refill", "start"), ("refill", "stop"), ("alarm", "start"), ("alarm", "stop"), ("refill", "reset"))
    ], calls
    print("PASS: native Options commissioning readback; bundled HA Start/Stop/Reset clicks; exact button.press targets; independent synthetic feedback and alarm stop during held ON; other tank controls visibly disabled")


async def verify_threshold_previews(page, options, shell, hass, entry, outputs, calls, base_url, screenshot_dir):
    """Use authenticated browser settings RPC; previews only open native More info."""
    expected_settings = {"refill": [1.25, 1.85], "alarm": [1.2, 4]}
    zone = entry.options["zones"][0]
    tank_id = zone["tanks"][0]["id"]
    options_before, owner = entry.options, entry.runtime_data
    tank = shell.locator(f'[data-tank-id="{tank_id}"]')
    refill = tank.locator('[data-preview-kind="refill_preview"]')
    alarm = tank.locator('[data-preview-kind="low_level_alarm"]')
    await expect(shell.locator("[data-preview-kind]")).to_have_count(0)
    core_calls = []

    original_call = ServiceRegistry.async_call

    async def record_service(registry, domain, service, *args, **kwargs):
        # Observe attempts before schema and lookup so even an absent notifier counts.
        if registry is hass.services and domain.lower() in {"switch", "notify", "persistent_notification"}:
            core_calls.append((domain, service))
        return await original_call(registry, domain, service, *args, **kwargs)

    observer = patch.object(ServiceRegistry, "async_call", record_service)
    browser_call_count = len(calls)

    source = "sensor.test_depth_1"

    async def refill_state(value, label):
        await expect(refill).to_have_text(label)
        assert hass.states.get(native["refill_preview"]).state == value

    async def assert_preview_labels_fit():
        for tile in (refill, alarm):
            bounds = await tile.evaluate("el => [el.textContent, el.clientWidth, el.scrollWidth]")
            assert 0 < bounds[1] and bounds[2] <= bounds[1] + 1, bounds

    async def alarm_state(value, label):
        await expect(alarm).to_have_text(label)
        assert hass.states.get(native["low_level_alarm"]).state == value
        await expect(tank.get_by_text("告警輸出：關", exact=True)).to_be_visible()
        assert hass.states.get(OUTPUTS["alarm"]).state == "off"

    observer.start()
    try:
        await native_login(options, base_url)
        original = await read_settings(options, entry)
        assert all(original["zones"][0]["tanks"][0][kind]["effective"] is None for kind in expected_settings)
        saved = await publish_settings(options, original, {"tanks": [{
            "id": tank_id, **{kind: {"local": pair} for kind, pair in expected_settings.items()},
        }]})
        assert saved["revision"] != original["revision"]
        assert entry.state == ConfigEntryState.LOADED
        assert entry.options is options_before and entry.runtime_data is owner
        assert all(saved["zones"][0]["tanks"][0][kind]["effective"] == pair for kind, pair in expected_settings.items())
        assert all(t["refill"]["effective"] is None and t["alarm"]["effective"] is None for t in saved["zones"][0]["tanks"][1:])
        native = {}
        for kind, domain in (("refill_preview", "sensor"), ("low_level_alarm", "binary_sensor")):
            entity = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{tank_id}_{kind}")
            assert entity and hass.states.get(entity).attributes[PREVIEW] == {
                "version": 1, "zone_id": zone["id"], "tank_id": tank_id, "kind": kind,
            }
            native[kind] = entity
        await tank.locator(".diagnostics-toggle").click()
        await expect(refill).to_be_visible()
        await expect(alarm).to_be_visible()
        await expect(shell.locator("[data-preview-kind]")).to_have_count(2)

        # A fresh frontend connection must read the same durable configuration.
        await native_login(options, base_url)
        assert await read_settings(options, entry) == saved
        assert not await options.evaluate("window.waterTestUncaught")
        await options.close()

        await refill_state("between_thresholds", "水位在補水門檻內")
        await alarm_state("off", "低液位告警未觸發")
        tile_before = await refill.element_handle()
        icon = refill
        await icon.focus()
        await expect(icon).to_be_focused()
        for reading, label, state in (
            ("1250.1", "水位在補水門檻內", "between_thresholds"),
            ("1250", "↓ 達補水低門檻", "refill_threshold"),
            ("1849.9", "水位在補水門檻內", "between_thresholds"),
            ("1850", "↑ 達補水高門檻", "stop_threshold"),
        ):
            hass.states.async_set(source, reading, {"unit_of_measurement": "mm"})
            await refill_state(state, label)
            if reading == "1250" and screenshot_dir is not None:
                await shell.screenshot(path=screenshot_dir / "native-p4-refill-boundary.png")
            await assert_preview_labels_fit()
            assert await refill.evaluate("(el, previous) => el === previous", tile_before)
            await expect(icon).to_be_focused()

        # At equality, the alarm is still OFF before the continuous delay.
        start = monotonic()
        hass.states.async_set(source, "120", {"unit_of_measurement": "cm"})
        await refill_state("refill_threshold", "↓ 達補水低門檻")
        await alarm_state("off", "低液位告警未觸發")
        assert monotonic() - start < 4
        await alarm_state("on", "低液位告警中")
        assert monotonic() - start >= 4
        if screenshot_dir is not None:
            await shell.screenshot(path=screenshot_dir / "native-p4-delayed-alarm.png")

        await alarm.click()
        details = page.locator("ha-more-info-dialog ha-dialog[open]")
        await details.wait_for(state="attached")
        if screenshot_dir is not None:
            await page.screenshot(path=screenshot_dir / "native-p4-alarm-more-info.png")
        await page.keyboard.press("Escape")
        await details.wait_for(state="detached")
        hass.states.async_set(source, "121", {"unit_of_measurement": "cm"})
        await alarm_state("off", "低液位告警未觸發")
        hass.states.async_set(source, "unavailable")
        await refill_state("unavailable", "補水預覽無法取得")
        await alarm_state("unavailable", "低液位告警預覽無法取得")
        hass.states.async_set(source, "121", {"unit_of_measurement": "cm"})
        await alarm_state("off", "低液位告警未觸發")
        await refill_state("refill_threshold", "↓ 達補水低門檻")
        for width in (260, 380, 740):
            await shell.evaluate("(el, w) => { el.style.width = `${w}px`; }", width)
            assert await tank.evaluate("""el => {
                const tank = el.getBoundingClientRect();
                return [...el.querySelectorAll('[data-preview-kind]')].every(tile => {
                    const r = tile.getBoundingClientRect();
                    return r.width > 0 && r.left >= tank.left && r.right <= tank.right;
                });
            }"""), width
            await assert_preview_labels_fit()
            if screenshot_dir is not None and width == 380:
                await shell.screenshot(path=screenshot_dir / "native-p4-recovered-380.png")
        await hass.async_block_till_done()
        assert not core_calls and len(calls) == browser_call_count and outputs.calls.empty(), (
            core_calls, calls[browser_call_count:],
        )
        print("PASS: browser-authenticated settings RPC and fresh readback; live refill boundaries, delayed preview alarm, unavailable and recovery; retained preview node/focus; native More info; zero preview browser service, switch or notification calls")
    except Exception:
        if screenshot_dir is not None and not options.is_closed():
            await options.screenshot(path=screenshot_dir / "native-p4-rpc-failure.png")
            print(await options.locator("body").aria_snapshot())
        raise
    finally:
        observer.stop()


async def verify_alarm_history(hass, entry, shell, outputs, wire, replies, screenshot_dir):
    """Drive the mounted module over real HA WS and actual Recorder ingestion."""
    rec = get_recorder(hass)
    assert await asyncio.wait_for(asyncio.shield(rec.async_db_ready), 10)
    zone, = entry.options.get("zones", entry.data["zones"])
    target = next(tank for tank in zone["tanks"] if tank.get("alarm_switch") == PASSIVE_ALARM)
    original_options = entry.options
    store = Path(entry.runtime_data.settings.store.path)
    store_before = store.read_bytes() if store.exists() else None
    service_calls = []

    @callback
    def observe(event):
        service_calls.append((event.data["domain"], event.data["service"]))

    unsub = hass.bus.async_listen(EVENT_CALL_SERVICE, observe)
    try:
        samples = []
        for value in ("off", "on", "off"):
            hass.states.async_set(PASSIVE_ALARM, value)
            samples.append(hass.states.get(PASSIVE_ALARM).last_changed.isoformat(timespec="microseconds").replace("+00:00", "Z"))
            await asyncio.sleep(0.01)
        committed = hass.loop.create_future()
        rec.queue_task(SynchronizeTask(committed))
        await asyncio.wait_for(committed, 10)
        await shell.get_by_role("button", name="補水參數", exact=True).click()
        panel = shell.get_by_role("region", name="告警輸出紀錄")
        await expect(panel).to_be_visible()
        pick = panel.get_by_label("選擇水桶")
        await pick.select_option(label="育苗區 · 第 2 桶")
        assert not wire
        await panel.get_by_role("button", name="重新整理紀錄").click()
        await expect(panel.get_by_role("listitem")).to_have_count(2)
        assert len(wire) == 1
        request = wire[0]
        assert request == {"id": request["id"], "type": ALARM_HISTORY_GET, "version": 1,
                           "entry_id": entry.entry_id, "zone_id": zone["id"], "tank_id": target["id"]}
        reply = replies[request["id"]]
        assert reply["success"], reply
        result = reply["result"]
        assert result["status"] == "ok" and not result["limited"], result
        assert result["coverage"] == {"start_utc": samples[0], "end_utc": samples[2]}
        exact = [{"at_utc": samples[2], "state": "off"}, {"at_utc": samples[1], "state": "on"}]
        assert result["events"] == exact
        assert await panel.locator("li time").evaluate_all("nodes => nodes.map(el => el.dateTime)") == [row["at_utc"] for row in exact]
        await expect(panel.get_by_role("listitem").first).to_contain_text("關閉")
        await expect(panel.get_by_role("listitem").last).to_contain_text("開啟")
        await expect(panel.get_by_text("已驗證涵蓋：", exact=False)).to_contain_text("UTC")
        if screenshot_dir:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            await panel.screenshot(path=screenshot_dir / "native-alarm-history.png")
        await pick.select_option(label="育苗區 · 一號水桶")
        await expect(panel.locator("time")).to_have_count(0)
        await panel.get_by_role("button", name="重新整理紀錄").click()
        await expect(panel.get_by_text("未設定告警輸出", exact=True)).to_be_visible()
        assert len(wire) == 2 and replies[wire[1]["id"]]["result"]["status"] == "no_binding"
        assert entry.options is original_options and (store.read_bytes() if store.exists() else None) == store_before
        assert not service_calls and outputs.calls.empty()
        print("PASS: native alarm history WS, committed Recorder off/on/off ↔ two UTC rows, other tank no binding, zero service/config effects", flush=True)
    finally:
        unsub()
    await shell.get_by_role("button", name="操作總覽", exact=True).click()


async def run_browser(hass, entry, outputs, base_url: str, screenshot_dir: Path | None, login_only: bool, reference: Path | None,
                      history_only: bool = False) -> None:
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
            history_wire = []
            history_replies = {}

            def record_frame(payload):
                if isinstance(payload, str):
                    message = json.loads(payload)
                    if message.get("type") == "call_service":
                        calls.append(message)
                    if message.get("type") == ALARM_HISTORY_GET:
                        history_wire.append(message)

            def record_reply(payload):
                messages = json.loads(payload)
                ids = {request["id"] for request in history_wire}
                for message in messages if isinstance(messages, list) else [messages]:
                    if message.get("id") in ids and message.get("type") == "result":
                        history_replies[message["id"]] = message

            def watch_socket(socket):
                socket.on("framesent", record_frame)
                if history_only:
                    socket.on("framereceived", record_reply)

            page.on("websocket", watch_socket)
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
            async with page.expect_response(base_url + URL_PATH) as served_module:
                await page.goto(base_url + "/lovelace/water-test", wait_until="domcontentloaded")
            served = await served_module.value
            assert served.ok and await served.body() == (ROOT / "custom_components" / DOMAIN / "dashboard.js").read_bytes()
            shell = page.locator(CARD_TAG)
            tiles = shell.locator(".tank")
            readings = shell.locator(".level")
            await tiles.first.wait_for()
            assert await tiles.count() == 5
            assert await shell.locator(".zone-head h2").count() == 1
            assert await page.evaluate("document.querySelector('home-assistant').hass.locale.language") == "en"
            await expect(shell.get_by_role("heading", name="補水控制", exact=True)).to_be_visible()
            await expect(shell.get_by_text("1 區 · 5 桶 · 水位單位 m", exact=True)).to_be_visible()
            await expect(shell.get_by_role("meter")).to_have_count(0)
            await expect(readings.first).to_have_attribute("aria-label", "一號水桶 · 1.25 m")
            if history_only:
                await verify_alarm_history(hass, entry, shell, outputs, history_wire, history_replies, screenshot_dir)
                assert not errors and not await page.evaluate("window.waterTestUncaught"), errors
                return
            await readings.first.click(timeout=8000)
            details = page.locator("ha-more-info-dialog ha-dialog[open]")
            await details.wait_for(state="attached", timeout=10000)
            await page.keyboard.press("Escape")
            await details.wait_for(state="detached", timeout=10000)
            before = await readings.first.element_handle()
            hass.states.async_set("sensor.test_depth_1", "183", {"unit_of_measurement": "cm"})
            await expect(readings.first).to_have_attribute("aria-label", "一號水桶 · 1.83 m")
            assert await readings.first.evaluate("(node, old) => node === old", before)
            await native_add_tank(hass, entry, 6)
            await expect(tiles).to_have_count(6)
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
            options = await context.new_page()
            options.set_default_timeout(8000)
            options.on("websocket", lambda ws: ws.on("framesent", record_frame))
            options.on("pageerror", lambda error: errors.append(str(error)))
            await shell.get_by_role("button", name="補水參數", exact=True).click()
            await expect(shell.get_by_role("heading", name="各桶門檻與顯示量程", exact=True)).to_be_visible()
            await expect(shell.get_by_role("link")).to_have_count(0)
            await shell.get_by_role("button", name="區域與水桶", exact=True).click()
            await expect(shell.get_by_role("heading", name="區域與水桶清單", exact=True)).to_be_visible()
            settings = shell.get_by_role("link", name="前往整合設定：區域與水桶", exact=True)
            async with context.expect_page() as opened:
                await settings.click(modifiers=["Control"])
            target = await opened.value
            await expect(target.get_by_role("button", name="Configure", exact=True)).to_be_visible()
            await target.close()
            await shell.get_by_role("button", name="操作總覽").click()
            await verify_threshold_previews(page, options, shell, hass, entry, outputs, calls, base_url, screenshot_dir)
            scale_options = await context.new_page()
            scale_options.on("pageerror", lambda error: errors.append(str(error)))
            await verify_browser_scale(scale_options, base_url, shell, entry)
            assert not errors and not await page.evaluate("window.waterTestUncaught"), errors
            print(f"PASS: exact native resource, native More info and live inventory; en-US shell with Chinese card; grid {geometry}; external requests blocked={len(external_requests)}")
            if not login_only:
                await verify_reference(browser, base_url, hass, entry, outputs, screenshot_dir, reference, restrict)


async def open_card_editor(page, label):
    """Use only the on-screen Edit button in the current live panel."""
    modal = page.locator("hui-dialog-edit-card ha-dialog[open]")
    deck = page.locator("hui-panel-view hui-card-options:visible")
    await expect(modal).to_have_count(0)
    # HA reuses the dialog host. A late native `closed`/popstate during a quick
    # reopen can close the new editor; wait for that specific lifecycle to settle.
    await page.wait_for_function("""() => !history.state?.dialog &&
        performance.now() - (waterEditorDialogEvents.findLast(e => e[0] !== 'show')?.[2] ?? -1000) >= 300""")
    action = deck.get_by_role("button", name=label, exact=True)
    await expect(action).to_have_count(1)
    await expect(action).to_be_visible()
    # Activate the native control itself, not a text span (which can hit the
    # closing mobile scrim while the retained dialog transitions out).
    await action.press("Enter")
    await expect(modal).to_have_count(1)
    editor = modal.locator(f"{EDITOR_TAG}:visible")
    await expect(editor.get_by_role("heading", name="本卡顯示設定", exact=True)).to_be_visible()
    return modal, editor


async def open_quick_parameters(page, tank_id):
    """Trusted normal-card entry, never a Lovelace editor as a backend writer."""
    shell = page.locator(f"hui-panel-view {CARD_TAG}:visible")
    await shell.locator(f'[data-tank-id="{tank_id}"] .quick-parameters').click()
    modal = shell.get_by_role("dialog", name="快捷參數", exact=True)
    panel = modal.locator(f"{CARD_TAG}-quick-parameters")
    await expect(panel.get_by_role("heading", name="補水參數", exact=True)).to_be_visible()
    return modal, panel


async def verify_native_editor(hass, entry, outputs, url, screenshots):
    """Real Lovelace display config plus real per-tank Settings CAS/readback."""
    tank_id, _ = await native_commission(hass, entry)
    synthetic = hass.states.get("sensor.test_depth_1")
    store = Path(entry.runtime_data.settings.store.path)
    asserted_asset = (ROOT / "custom_components" / DOMAIN / "dashboard.js").read_bytes()
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)

    async def restrict(route):
        if route.request.url.startswith(url + "/"):
            await route.continue_()
        else:
            await route.abort()

    async with async_playwright() as playwright, await playwright.chromium.launch(
        executable_path=os.environ.get("WATER_CHROMIUM_EXECUTABLE"), headless=True
    ) as browser:
        widths = {}
        for language, width, height, card_edit, cancel, save in (
            ("en-US", 1180, 910, "Edit", "Cancel", "Save"),
            ("zh-Hant", 390, 844, "編輯", "取消", "儲存"),
        ):
            async with await browser.new_context(locale=language, viewport={"width": width, "height": height}) as ctx:
                await ctx.route("**/*", restrict)
                page = await ctx.new_page()
                page.set_default_timeout(8000)
                await page.add_init_script("""
                    window.waterEditorErrors = []; window.waterEditorEvents = 0;
                    window.waterEditorDialogEvents = [];
                    addEventListener('show-dialog', e => waterEditorDialogEvents.push(['show',e.detail?.dialogTag,performance.now()]),true);
                    addEventListener('dialog-closed', e => waterEditorDialogEvents.push(['close',e.detail?.dialog,performance.now()]),true);
                    addEventListener('popstate',e=>waterEditorDialogEvents.push(['popstate',e.state?.dialog??null,performance.now()]),true);
                    addEventListener('unhandledrejection',e=>waterEditorErrors.push(e.reason?.code??String(e.reason)));
                    addEventListener('config-changed',e=>{
                      if(e.composedPath().some(el=>el.localName==='uninus-water-dashboard-editor'))waterEditorEvents++;
                    },true);
                """)
                seen, errors = [], []

                def record(raw):
                    if isinstance(raw, str):
                        message = json.loads(raw)
                        if "id" in message:  # Never capture auth frames or credentials.
                            seen.append(message)

                def messages(kind):
                    return [message for message in seen if message["type"] == kind]

                page.on("websocket", lambda ws: ws.on("framesent", record))
                page.on("pageerror", lambda error: errors.append(str(error)))
                if language == "en-US":
                    await native_login(page, url)
                    await page.evaluate(SETTINGS_CALL, {"type": "lovelace/resources/create", "res_type": "module", "url": URL_PATH})
                    await page.evaluate(SETTINGS_CALL, {"type": "lovelace/config/save", "config": {"title": TITLE, "views": [
                        {"title": TITLE, "path": "water-editor", "type": "panel", "cards": [{"type": f"custom:{CARD_TAG}"}]},
                    ]}})
                dashboard = url + "/lovelace/water-editor"
                try:
                    async with page.expect_response(url + URL_PATH) as asset:
                        if language == "en-US":
                            await page.goto(dashboard, wait_until="domcontentloaded")
                        else:
                            await native_login(page, dashboard)
                    assert (await asset.value).ok and await (await asset.value).body() == asserted_asset
                    await page.wait_for_function("lang=>document.querySelector('home-assistant')?.hass?.locale?.language===lang",
                                                 arg="en" if language == "en-US" else language)
                    shell = page.locator(f"hui-panel-view {CARD_TAG}:visible")
                    start = shell.get_by_role("button", name="手動補水", exact=True).first
                    await expect(start).to_be_enabled()
                    assert await shell.evaluate("card=>[card.preview,card.editMode]") == [False, False]
                    baseline = await read_settings(page, entry)
                    disk_before = store.read_bytes() if store.exists() else None
                    prior_options, runtime = entry.options, entry.runtime_data
                    yaml_before = await page.evaluate(SETTINGS_CALL, {"type": "lovelace/config"})
                    seen.clear()

                    # Real Lovelace edit/Cancel/Save is now display-only.
                    label = await page.evaluate("document.querySelector('home-assistant').hass.localize('ui.panel.lovelace.menu.configure_ui')")
                    tooltip = page.locator("hui-root ha-tooltip").filter(has_text=label)
                    if label and await tooltip.count():
                        ident = await tooltip.first.evaluate("el=>el.for??el.getAttribute('for')")
                        await page.locator(f"hui-root ha-icon-button#{ident}").click()
                    else:
                        await page.locator("#dashboardmenu").click()
                        await page.get_by_text(label or "Edit dashboard", exact=True).click()
                    await expect(start).to_be_disabled()
                    modal, display = await open_card_editor(page, card_edit)
                    preview = modal.locator(CARD_TAG)
                    assert await preview.evaluate("card=>[card.preview,card.editMode]") == [True, True]
                    await expect(display.get_by_role("spinbutton")).to_have_count(0)
                    await expect(preview.locator(".quick-parameters:visible")).to_have_count(0)
                    preview_start = preview.get_by_role("button", name="手動補水", exact=True).first
                    await expect(preview_start).to_be_disabled()
                    await preview_start.dispatch_event("click")
                    await expect(preview.get_by_role("dialog")).to_have_count(0)
                    details = display.get_by_role("checkbox", name="顯示詳細狀態", exact=True)
                    original_details = await details.is_checked()
                    await details.set_checked(not original_details)
                    assert await page.evaluate("waterEditorEvents") == 1
                    await modal.get_by_role("button", name=cancel, exact=True).click()
                    await expect(page.locator("hui-dialog-edit-card ha-dialog[open]")).to_have_count(0)
                    assert not messages(f"{DOMAIN}/config_get") and not messages(f"{DOMAIN}/config_save")
                    assert not messages(f"{DOMAIN}/topology_get") and not messages(f"{DOMAIN}/topology_save")
                    assert not messages("lovelace/config/save") and (store.read_bytes() if store.exists() else None) == disk_before
                    assert await page.evaluate(SETTINGS_CALL, {"type": "lovelace/config"}) == yaml_before

                    modal, display = await open_card_editor(page, card_edit)
                    await expect(display.get_by_role("checkbox", name="顯示詳細狀態", exact=True)).to_be_checked(checked=original_details)
                    mode = modal.locator(".gui-mode-button")
                    await mode.click()
                    yaml_field = modal.locator(".cm-content[contenteditable=true]")
                    await yaml_field.click(); await page.keyboard.press("Control+a")
                    columns = 6 if language == "en-US" else 7
                    await page.keyboard.insert_text(f"type: custom:{CARD_TAG}\nshow_details: true\ngrid_options:\n  columns: {columns}\n")
                    await mode.click()
                    await expect(display.get_by_role("checkbox", name="顯示詳細狀態", exact=True)).to_be_checked()
                    selected_zone = next(z for z in baseline["zones"] if any(t["id"] == tank_id for t in z["tanks"]))
                    selected_tank = next(t for t in selected_zone["tanks"] if t["id"] == tank_id)
                    await display.get_by_role("checkbox", name=selected_zone["name"], exact=True).check()
                    await display.get_by_role("checkbox", name=f"{selected_zone['name']} · {selected_tank['name']}", exact=True).check()
                    await modal.get_by_role("button", name=save, exact=True).click()
                    await expect(page.locator("hui-dialog-edit-card ha-dialog[open]")).to_have_count(0)
                    assert len(messages("lovelace/config/save")) == 1
                    assert not messages(f"{DOMAIN}/config_get") and not messages(f"{DOMAIN}/config_save")
                    assert (store.read_bytes() if store.exists() else None) == disk_before
                    yaml_saved = await page.evaluate(SETTINGS_CALL, {"type": "lovelace/config"})
                    expected_yaml = deepcopy(yaml_before)
                    expected_yaml["views"][0]["cards"][0] = {"type": f"custom:{CARD_TAG}", "show_details": True,
                        "zone_ids": [selected_zone["id"]], "tank_ids": [tank_id], "grid_options": {"columns": columns},
                        "display_mode": "thin"}
                    assert yaml_saved == expected_yaml, (yaml_saved, expected_yaml)
                    modal, display = await open_card_editor(page, card_edit)
                    await expect(display.get_by_role("checkbox", name=selected_zone["name"], exact=True)).to_be_checked()
                    await expect(display.get_by_role("checkbox", name=f"{selected_zone['name']} · {selected_tank['name']}", exact=True)).to_be_checked()
                    await modal.get_by_role("button", name=cancel, exact=True).click()
                    await expect(page.locator("hui-dialog-edit-card ha-dialog[open]")).to_have_count(0)
                    assert not messages(f"{DOMAIN}/config_get") and not messages(f"{DOMAIN}/config_save")
                    await page.locator("hui-root .exit-edit-mode").click()
                    await expect(start).to_be_enabled()
                    print(f"PASS {language}: native display-only Cancel/GUI/YAML/Save, zero private API or equipment calls")

                    # Normal per-tank Quick dialog: draft continuity/cancel and one CAS Save.
                    second = await ctx.new_page()
                    await second.goto(dashboard, wait_until="domcontentloaded")
                    ordinary = second.locator(CARD_TAG)
                    await expect(ordinary.get_by_role("heading", name="補水控制", exact=True)).to_be_visible()
                    modal, quick = await open_quick_parameters(page, tank_id)
                    scale = quick.get_by_role("spinbutton", name="水位顯示量程上限（m）", exact=True)
                    old_value = await scale.input_value()
                    await scale.fill(""); await scale.press_sequentially("7.")
                    remembered = await scale.element_handle()
                    await scale.evaluate("el=>{el.dataset.blurs=0;el.addEventListener('blur',()=>el.dataset.blurs++);}")
                    sample = "166" if language == "en-US" else "167"
                    hass.states.async_set(synthetic.entity_id, sample, synthetic.attributes)
                    await expect(shell.get_by_role("button", name=f"一號水桶 · {int(sample)/100:.2f} m")).to_be_visible()
                    await page.keyboard.type("125")
                    await expect(scale).to_have_value("7.125")
                    assert await scale.evaluate("(el,old)=>el===old&&el.getRootNode().activeElement===el&&el.dataset.blurs==='0'", remembered)
                    assert not messages(f"{DOMAIN}/config_save")
                    widths[language] = await quick.evaluate("""el=>{
                      const root=el.shadowRoot,host=el.getBoundingClientRect();
                      const visible=[...root.querySelectorAll('input,select,button')].filter(x=>x.getClientRects().length);
                      return {width:host.width,trackCount:getComputedStyle(root.querySelector('.policy-grid')).gridTemplateColumns.split(' ').length,
                        noOverflow:visible.every(x=>{const r=x.getBoundingClientRect();return host.left<=r.left&&r.right<=host.right;})};
                    }""")
                    assert widths[language]["noOverflow"]
                    if screenshots:
                        await page.screenshot(path=screenshots / f"native-quick-{language}.png")
                    await modal.get_by_role("button", name="取消", exact=True).click()
                    assert not messages(f"{DOMAIN}/config_save")
                    assert (store.read_bytes() if store.exists() else None) == disk_before
                    assert await read_settings(page, entry) == baseline

                    modal, quick = await open_quick_parameters(page, tank_id)
                    scale = quick.get_by_role("spinbutton", name="水位顯示量程上限（m）", exact=True)
                    await expect(scale).to_have_value(old_value)
                    await expect(quick.get_by_role("group", name="共用補水門檻", exact=True)).to_have_count(0)
                    await expect(quick.get_by_role("button", name="編輯區域與水桶", exact=True)).to_have_count(0)
                    refill = quick.get_by_role("group", name="補水門檻", exact=True)
                    alarm = quick.get_by_role("group", name="低液位告警", exact=True)
                    await refill.get_by_role("combobox").select_option("local")
                    await alarm.get_by_role("combobox").select_option("local")
                    await refill.get_by_role("spinbutton", name="下限（m）", exact=True).fill("0.45")
                    await refill.get_by_role("spinbutton", name="上限（m）", exact=True).fill("1.55")
                    await alarm.get_by_role("spinbutton", name="低位門檻（m）", exact=True).fill("0.3")
                    await alarm.get_by_role("spinbutton", name="延遲（秒）", exact=True).fill("11")
                    maximum = 2.75 if language == "en-US" else 3.25
                    await scale.fill(str(maximum))
                    await quick.get_by_role("button", name="儲存此桶參數", exact=True).click()
                    await expect(quick.get_by_text("已儲存並重新讀回確認。", exact=True)).to_be_visible()
                    [write] = messages(f"{DOMAIN}/config_save")
                    assert len(messages("lovelace/config/save")) == 1
                    assert (write["version"],write["entry_id"],write["if_revision"],write["if_source_snapshot"]) == (
                        1,entry.entry_id,baseline["revision"],baseline["source_snapshot"])
                    assert set(write["changes"]) == {"tanks"} and [t["id"] for t in write["changes"]["tanks"]] == [tank_id]
                    assert any(msg["id"]>write["id"] and msg.get("entry_id")==entry.entry_id for msg in messages(f"{DOMAIN}/config_get"))
                    persisted = await read_settings(page, entry)
                    tank = next(t for z in persisted["zones"] for t in z["tanks"] if t["id"]==tank_id)
                    assert persisted["common"] == baseline["common"]
                    assert tank["scale_max_m"] == maximum and tank["refill"]["effective"] == [0.45,1.55]
                    assert tank["alarm"]["effective"] == [0.3,11] and not tank["refill"]["use_common"] and not tank["alarm"]["use_common"]
                    for before_zone, after_zone in zip(baseline["zones"],persisted["zones"],strict=True):
                        assert [{k:v for k,v in t.items()} for t in before_zone["tanks"] if t["id"]!=tank_id] == [t for t in after_zone["tanks"] if t["id"]!=tank_id]
                    assert entry.options is prior_options and entry.runtime_data is runtime
                    physical=store.read_bytes()
                    assert physical!=disk_before and json.loads(physical)["data"]["common"] == persisted["common"]
                    await modal.get_by_role("button",name="取消",exact=True).click()
                    assert await read_settings(page,entry)==persisted and store.read_bytes()==physical
                    assert await page.evaluate(SETTINGS_CALL,{"type":"lovelace/config"})==yaml_saved
                    for card in (ordinary,shell):
                        await expect(card.get_by_role("meter",name=f"水位量程：0–{maximum} m")).to_be_visible()
                    modal,quick=await open_quick_parameters(page,tank_id)
                    await expect(quick.get_by_role("spinbutton",name="水位顯示量程上限（m）",exact=True)).to_have_value(str(maximum))
                    await modal.get_by_role("button",name="取消",exact=True).click()
                    assert not messages("call_service") and outputs.calls.empty()
                    assert len(messages(f"{DOMAIN}/config_save"))==1 and not messages(f"{DOMAIN}/topology_save")
                    assert not errors and not await page.evaluate("waterEditorErrors")
                    print(f"PASS {language}: normal Quick trusted typing/Cancel, one selected-tank CAS Save, independent durable readback, two cards converge")
                except Exception:
                    print(f"C EDITOR DIAGNOSTIC {language}: {await page.locator('body').aria_snapshot()}")
                    if screenshots:
                        await page.screenshot(path=screenshots / f"native-c-diagnostic-{language}.png")
                    raise
        assert widths["en-US"]["trackCount"]==2 and widths["zh-Hant"]["trackCount"]==1,widths
        if screenshots:
            (screenshots / "native-quick-geometry.json").write_text(json.dumps(widths,indent=2),encoding="utf-8")


async def verify_reference(browser, base_url, hass, entry, outputs, screenshots, reference, restrict):
    """Matched proposal measurements using the exact shipped card on a zh-Hant native shell."""
    await seed_reference_layout(hass, entry)
    async with await browser.new_context(viewport={"width": 1316, "height": 1050}, locale="zh-Hant") as ctx:
        await ctx.route("**/*", restrict)
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script("window.waterTestUncaught = []; addEventListener('unhandledrejection', e => waterTestUncaught.push(e.reason?.message ?? String(e.reason)))")
        async with page.expect_response(base_url + URL_PATH) as served_module:
            await native_login(page, base_url + "/lovelace/water-test")
        assert await (await served_module.value).body() == (ROOT / "custom_components" / DOMAIN / "dashboard.js").read_bytes()
        await page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.locale?.language === 'zh-Hant'")
        shell = page.locator(CARD_TAG)
        await expect(shell.get_by_text("2 區 · 8 桶 · 水位單位 m", exact=True)).to_be_visible()
        await expect(shell.get_by_text("補水門檻　0.5／1.5 m", exact=True)).to_be_visible()
        await expect(shell.get_by_text("低液位告警　≤ 0.4 m · 1 秒", exact=True)).to_be_visible()
        await expect(shell.get_by_role("meter")).to_have_count(8)
        await expect(shell.locator("[data-low-level-active]")).to_have_count(1)
        for i, (reading, sample) in enumerate(zip(await shell.locator(".level").all(), REFERENCE_LEVELS, strict=True)):
            await expect(reading).to_have_attribute("aria-label", f"{'東' if i < 4 else '西'} {i % 4 + 1} 桶 · {sample} m")
        await expect(shell.locator(".relation").nth(1)).to_have_text("↓ 低於告警門檻")
        await expect(shell.locator(".relation").nth(2)).to_have_text("↑ 高於補水門檻")
        prior = shell.locator(".level").nth(2)
        await prior.click()
        details = page.locator("ha-more-info-dialog ha-dialog[open]")
        await details.wait_for(state="attached")
        if screenshots:
            await page.screenshot(path=screenshots / "native-more-info-zh-Hant.png")
        await page.keyboard.press("Escape")
        await details.wait_for(state="detached")
        for name, label in (("parameters", "補水參數"), ("devices", "區域與水桶")):
            await shell.get_by_role("button", name=label, exact=True).click()
            if screenshots:
                await shell.screenshot(path=screenshots / f"native-{name}-zh-Hant.png")
        await shell.get_by_role("button", name="操作總覽").click()
        await page.get_by_role("button", name="手動補水", exact=True).nth(2).click()
        await shell.get_by_role("button", name="確認開始", exact=True).click()
        await outputs.expect("refill", "on")
        await expect(shell.locator(".tank").nth(2).get_by_text("補水中", exact=True)).to_be_visible()
        await expect(shell.get_by_role("button", name="停止補水", exact=True)).to_be_enabled()
        offline = await ctx.new_page() if reference else None
        if offline:
            await offline.set_content(reference.read_text(encoding="utf-8"))
            await offline.locator(".host").evaluate("el => { el.hidden = true; }")
        responsive = {}
        for name, vw, width, rows in (("desktop", 1316, 1060, 3), ("desktop-1030", 1286, 1030, 3),
                                      ("desktop-984", 1240, 984, 3), ("mobile", 390, 390, 2)):
            await page.set_viewport_size({"width": vw, "height": 1050 if vw > 390 else 844})
            actual = await shell.locator(".shell").evaluate(MEASURE)
            assert actual["width"] == width, (name, actual)
            assert actual["zones_columns"] == (1 if vw == 390 else 2), (name, actual)
            assert actual["first_rows"] == [rows, rows] and actual["tank_counts"] == [4, 4], (name, actual)
            assert actual["contained"], (name, actual)
            assert actual["action_heights"] == [44] * 16, (name, actual)
            if screenshots:
                # An element capture in a short viewport can include HA's fixed
                # toolbar over the card. Preserve width; increase capture height
                # only and reset the composed scroll chain for a clean product crop.
                if actual["height"] + 160 > page.viewport_size["height"]:
                    await page.set_viewport_size({"width": vw, "height": ceil(actual["height"]) + 160})
                await shell.evaluate("""el => {
                    for (let n = el; n; n = n.assignedSlot ?? n.parentNode ?? n.host ?? null) {
                        if (n instanceof HTMLElement) n.scrollTop = 0;
                    }
                    window.scrollTo(0, 0);
                }""")
                await shell.screenshot(path=screenshots / f"water-console-{name}.png")
            pair = {"native": actual}
            if offline:
                await offline.set_viewport_size({"width": width, "height": 1050 if vw > 390 else 844})
                frozen = offline.locator(".shell")
                ref = await frozen.evaluate(MEASURE)
                assert [ref[k] for k in ("width", "zones_columns", "first_rows", "tank_counts")] == [
                    actual[k] for k in ("width", "zones_columns", "first_rows", "tank_counts")], (name, ref, actual)
                assert all(abs(a-b) <= 16 for az, rz in zip(actual["row_heights"], ref["row_heights"], strict=True)
                           for a,b in zip(az, rz, strict=True)), (name, ref, actual)
                assert actual["gauge_fills"] == ref["gauge_fills"], (name, ref, actual)
                pair["reference"] = ref
                if screenshots:
                    await frozen.screenshot(path=screenshots / f"reference-{name}.png")
            responsive[name] = pair
            print(f"PASS: {name}, width {width}, zone row heights {actual['row_heights']}"
                  + (f" vs reference {pair['reference']['row_heights']}" if offline else ""))
        await shell.get_by_role("button", name="停止補水", exact=True).click()
        await outputs.expect("refill", "off")
        assert not errors and not await page.evaluate("window.waterTestUncaught"), errors
        if screenshots:
            (screenshots / "native-geometry.json").write_text(json.dumps(responsive, indent=2), encoding="utf-8")


MEASURE = """root => {
    const card = root.getBoundingClientRect();
    const rect = node => node.getBoundingClientRect();
    const zones = [...root.querySelectorAll('.zone')];
    const tanks = zones.map(zone => [...zone.querySelectorAll('.tank')]);
    const rows = all => [...new Set(all.map(t => rect(t).top))].map(y => all.filter(t => rect(t).top === y));
    const bounds = root.querySelectorAll('.policy,.zone,.tank,.commands button:not([hidden]),.vessel:not([hidden]),.relation,.reading');
    return {
        width: card.width, height: card.height,
        header_height: rect(root.querySelector('.pagehead')).height, strip_height: rect(root.querySelector('.policy')).height,
        zones_columns: new Set(zones.map(z => rect(z).x)).size,
        zone_widths: zones.map(z => rect(z).width),
        tank_counts: tanks.map(list => list.length),
        first_rows: tanks.map(list => rows(list)[0].length),
        row_heights: tanks.map(list => rows(list).map(row => Math.max(...row.map(t => rect(t).height)))),
        action_heights: [...root.querySelectorAll('.tank .commands button:not([hidden])')].map(b => rect(b).height),
        gauge_fills: [...root.querySelectorAll('.tank .water')].map(fill => Math.round(rect(fill).height * 10) / 10),
        contained: [...bounds].every(n => rect(n).left >= card.left - 1 && rect(n).right <= card.right + 1 && n.scrollWidth <= n.clientWidth + 1),
    };
}"""


async def main(login_only: bool, screenshots: Path | None, reference: Path | None, editor_only: bool = False,
               history_only: bool = False) -> None:
    print(f"Core {importlib.metadata.version('homeassistant')}; frontend {importlib.metadata.version('home-assistant-frontend')}")
    with TemporaryDirectory(prefix="water-native-browser-") as temp:
        root = Path(temp)
        stage_fixture(root)
        hass = HomeAssistant(str(root))
        loader.async_setup(hass)
        hass.config.skip_pip = True
        port = loopback_port()
        # Keep the copied integration import first for the entire fixture lifecycle.
        # A namespace package can otherwise recompute its paths after Core's preload.
        sys.path.insert(0, str(root))
        auth_provider = {"type": "trusted_networks", "trusted_networks": ["127.0.0.1/32"], "allow_bypass_login": True}
        core_config = {"homeassistant": {"auth_providers": [auth_provider]},
                       "http": {"server_host": "127.0.0.1", "server_port": port},
                       "frontend": {}, "history": {}, "recorder": {}}
        outputs = None
        try:
            assert await bootstrap.async_from_config_dict(core_config, hass) is hass
            assert "frontend" in hass.config.components and "lovelace" in hass.config.components
            # Native Options is an operation on a running Core, not a startup
            # seed bypass. Keep the real Settings live-owner guard intact.
            await hass.async_start()
            owner = await hass.auth.async_create_user("Local Water Test", group_ids=[GROUP_ID_ADMIN], local_only=True)
            assert owner.is_admin
            if not login_only:
                outputs = SyntheticOutputs(hass)
                if history_only:
                    passive = er.async_get(hass).async_get_or_create(
                        "switch", "browser_probe", "passive_history", suggested_object_id="browser_passive_history",
                    )
                    assert passive.entity_id == PASSIVE_ALARM
                for i in range(1, 11):
                    hass.states.async_set(f"sensor.test_depth_{i}", str(110 + 15 * i), {"unit_of_measurement": "cm"})
                entry = await native_entry(hass)
                module = sys.modules[f"custom_components.{DOMAIN}"]
                assert Path(module.__file__).resolve() == root / "custom_components" / DOMAIN / "__init__.py"
                for pos in range(2, 6):
                    await native_add_tank(hass, entry, pos, PASSIVE_ALARM if pos == 2 and history_only else None)
            else:
                entry = None
            url = f"http://127.0.0.1:{port}"
            if editor_only:
                await verify_native_editor(hass, entry, outputs, url, screenshots)
            else:
                await run_browser(hass, entry, outputs, url, screenshots, login_only, reference, history_only)
        finally:
            if outputs:
                outputs.alarm_on.set()
            try:
                await hass.async_stop()
                assert not get_recorder(hass).is_alive()
            finally:
                sys.path.remove(str(root))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login-only", action="store_true", help="Check loopback HA frontend boot only")
    mode.add_argument("--editor-only", action="store_true", help="Run only the native Lovelace editor acceptance")
    mode.add_argument("--history-only", action="store_true", help="Run only the native Recorder alarm history acceptance")
    parser.add_argument("--screenshots", type=Path, help="Explicit destination for optional PNGs")
    parser.add_argument("--reference", type=Path, help="Frozen approved proposal HTML for matched measurements and captures")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("aiohttp.access").disabled = True
    asyncio.run(main(args.login_only, args.screenshots, args.reference, args.editor_only, args.history_only))
    if not args.login_only and not args.editor_only and not args.history_only:
        # Each branch gets virgin state and its own Core/runtime; Recorder
        # fixtures for history cannot affect the existing operation/editor tests.
        for mode in ("--editor-only", "--history-only"):
            command = [sys.executable, "-u", str(Path(__file__).resolve()), mode]
            if args.screenshots:
                command += ["--screenshots", str(args.screenshots)]
            subprocess.run(command, check=True, timeout=180)
