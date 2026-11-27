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
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

# HA's bootstrap imports LiteLLM indirectly; skip its optional remote price map.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "uninus_water_control"
TITLE = "水量總覽驗證"
CARD_TAG = "uninus-water-dashboard"
URL_PATH = "/uninus_water_control/dashboard.js"
MANUAL = "uninus_water_manual"
PREVIEW = "uninus_water_thresholds"
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


async def verify_browser_scale(options, base_url, shell, screenshot_dir):
    await options.goto(f"{base_url}/config/integrations/integration/{DOMAIN}", wait_until="domcontentloaded")

    async def open_form():
        await options.get_by_role("button", name="Configure", exact=True).press("Enter")
        await options.get_by_text("Edit water-level display scale", exact=True).click()
        await options.locator("ha-selector-select").get_by_role("combobox").click()
        await options.get_by_role("option", name="1.1 · 育苗區 / 一號水桶 · sensor.test_depth_1", exact=True).click()
        await options.get_by_role("button", name="Submit", exact=True).click()
        await expect(options.get_by_role("alertdialog", name="Water-level display scale", exact=True)).to_be_visible()
        return options.get_by_role("spinbutton", name="Display scale maximum", exact=True)

    scale = await open_form()
    await expect(scale).to_have_value("")
    await scale.click()
    await scale.press_sequentially("2.5")
    await expect(scale).to_have_value("2.5")
    if screenshot_dir:
        await options.screenshot(path=screenshot_dir / "native-scale-options.png")
    await options.get_by_role("button", name="Submit", exact=True).click()
    await expect(options.get_by_text("Options successfully saved.", exact=True)).to_be_visible()
    await options.get_by_role("button", name="Finish", exact=True).click()
    await expect(shell.get_by_role("meter", name="水位量程：0–2.5 m")).to_have_attribute("aria-valuemax", "2.5")
    await options.reload(wait_until="domcontentloaded")
    await expect(await open_form()).to_have_value("2.5")
    assert not await options.evaluate("window.waterTestUncaught")
    await options.close()
    print("PASS: native display-scale Options select/type/save/reopen and live measured meter")


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
    """Only browser Options writes configuration; preview interactions only open native More info."""
    fields = {
        "Refill lower threshold": "1.25", "Stop upper threshold": "1.85",
        "Low-level alarm threshold": "1.2", "Continuous low-level alarm delay": "4",
    }
    expected_settings = dict(zip(
        ("refill_lower_m", "refill_upper_m", "alarm_lower_m", "alarm_delay_seconds"),
        (1.25, 1.85, 1.2, 4), strict=True,
    ))
    zone = entry.options["zones"][0]
    tank_id = zone["tanks"][0]["id"]
    manual_before = entry.options["manual"]
    tank = shell.locator(f'[data-tank-id="{tank_id}"]')
    refill = tank.locator('[data-preview-kind="refill_preview"]')
    alarm = tank.locator('[data-preview-kind="low_level_alarm"]')
    threshold_dialog = options.get_by_role("alertdialog", name="Read-only threshold previews", exact=True)
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

    async def open_form():
        # Activate the actual button; its slotted icon can intercept pointer hit-testing.
        await options.get_by_role("button", name="Configure", exact=True).press("Enter")
        await options.get_by_text("Edit threshold previews", exact=True).click()
        picker = options.locator("ha-selector-select").get_by_role("combobox")
        await picker.click()
        await options.get_by_role("option", name="1.1 · 育苗區 / 一號水桶 · sensor.test_depth_1", exact=True).click()
        await options.get_by_role("button", name="Submit", exact=True).click()
        await expect(threshold_dialog).to_be_visible()
        await expect(options.get_by_role("spinbutton")).to_have_count(len(fields))

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
        await options.goto(f"{base_url}/config/integrations/integration/{DOMAIN}", wait_until="domcontentloaded")
        await open_form()
        for label, value in fields.items():
            field = options.get_by_role("spinbutton", name=label, exact=True)
            await expect(field).to_have_value("")
            await field.click()
            await field.press_sequentially(value)
            await expect(field).to_have_value(value)
        if screenshot_dir is not None:
            await options.screenshot(path=screenshot_dir / "native-p4-options-filled.png")
        await options.get_by_role("button", name="Submit", exact=True).click()
        await expect(threshold_dialog).not_to_be_visible()
        await expect(options.get_by_text("Options successfully saved.", exact=True)).to_be_visible()
        await options.get_by_role("button", name="Finish", exact=True).click()
        await expect(options.get_by_role("alertdialog", name="Success", exact=True)).not_to_be_visible()

        # Establish producer truth independently of the live dashboard projection.
        await hass.async_block_till_done()
        assert entry.state == ConfigEntryState.LOADED
        assert entry.options["manual"] == manual_before
        assert entry.options["zones"][0]["tanks"][0]["thresholds"] == expected_settings
        assert all("thresholds" not in item for item in entry.options["zones"][0]["tanks"][1:])
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

        # Start a fresh Options frontend, rather than inheriting the closing dialog's state.
        await options.reload(wait_until="domcontentloaded")
        await open_form()
        for label, value in fields.items():
            await expect(options.get_by_role("spinbutton", name=label, exact=True)).to_have_value(value)
        if screenshot_dir is not None:
            await options.screenshot(path=screenshot_dir / "native-p4-options-reopened.png")
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
        print("PASS: browser Options select/type/save/reopen; live refill boundaries, delayed preview alarm, unavailable and recovery; retained preview node/focus; native More info; zero preview browser service, switch or notification calls")
    except Exception:
        if screenshot_dir is not None and not options.is_closed():
            await options.screenshot(path=screenshot_dir / "native-p4-options-failure.png")
            print(await options.locator("body").aria_snapshot())
        raise
    finally:
        observer.stop()


async def run_browser(hass, entry, outputs, base_url: str, screenshot_dir: Path | None, login_only: bool, reference: Path | None) -> None:
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
            for tab, heading, label in (
                ("補水參數", "各桶門檻與顯示量程", "前往整合設定：門檻與量程"),
                ("區域與水桶", "管理區域與水桶", "前往整合設定：區域與水桶"),
            ):
                await shell.get_by_role("button", name=tab, exact=True).click()
                await expect(shell.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                settings = shell.get_by_role("link", name=label, exact=True)
                async with context.expect_page() as opened:
                    await settings.click(modifiers=["Control"])
                target = await opened.value
                await expect(target.get_by_role("button", name="Configure", exact=True)).to_be_visible()
                await target.close()
            await shell.get_by_role("button", name="操作總覽").click()
            await verify_threshold_previews(page, options, shell, hass, entry, outputs, calls, base_url, screenshot_dir)
            scale_options = await context.new_page()
            scale_options.on("pageerror", lambda error: errors.append(str(error)))
            await verify_browser_scale(scale_options, base_url, shell, screenshot_dir)
            assert not errors and not await page.evaluate("window.waterTestUncaught"), errors
            print(f"PASS: exact native resource, native More info and live inventory; en-US shell with Chinese card; grid {geometry}; external requests blocked={len(external_requests)}")
            if not login_only:
                await verify_reference(browser, base_url, hass, entry, outputs, screenshot_dir, reference, restrict)


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


async def main(login_only: bool, screenshots: Path | None, reference: Path | None) -> None:
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
            owner = await hass.auth.async_create_user("Local Water Test", group_ids=[GROUP_ID_ADMIN], local_only=True)
            assert owner.is_admin
            if not login_only:
                outputs = SyntheticOutputs(hass)
                for i in range(1, 11):
                    hass.states.async_set(f"sensor.test_depth_{i}", str(110 + 15 * i), {"unit_of_measurement": "cm"})
                entry = await native_entry(hass)
                module = sys.modules[f"custom_components.{DOMAIN}"]
                assert Path(module.__file__).resolve() == root / "custom_components" / DOMAIN / "__init__.py"
                for pos in range(2, 6):
                    await native_add_tank(hass, entry, pos)
            else:
                entry = None
            await hass.async_start()
            await run_browser(hass, entry, outputs, f"http://127.0.0.1:{port}", screenshots, login_only, reference)
        finally:
            if outputs:
                outputs.alarm_on.set()
            try:
                await hass.async_stop()
            finally:
                sys.path.remove(str(root))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login-only", action="store_true", help="Check loopback HA frontend boot only")
    parser.add_argument("--screenshots", type=Path, help="Explicit destination for optional PNGs")
    parser.add_argument("--reference", type=Path, help="Frozen approved proposal HTML for matched measurements and captures")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("aiohttp.access").disabled = True
    asyncio.run(main(args.login_only, args.screenshots, args.reference))
