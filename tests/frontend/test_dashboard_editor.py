"""Trusted Chromium coverage of per-tank Quick parameters with a controlled WS peer."""

import pytest

from test_dashboard_browser import browser_page  # noqa: F401 - pytest fixture


SETUP = """() => {
    const cls = customElements.get('uninus-water-dashboard');
    const type = 'custom:uninus-water-dashboard';
    window.entry = '01K00A00000000000000000000';
    const zone = id(1), tank = id(2);
    window.server = {version:1, entry_id:entry, revision:'a'.repeat(64), source_snapshot:'b'.repeat(64),
        common:{refill:[0.5,1.5], alarm:null}, zones:[{id:zone,name:'東區',tanks:[{id:tank,name:'試驗桶',
           refill:{use_common:false,local:[0.4,1.6],effective:[0.4,1.6]},
           alarm:{use_common:false,local:[0.2,5],effective:[0.2,5]},scale_max_m:null}]}]};
    window.wire = [];
    window.entries = [{domain:'uninus_water_control', state:'loaded',entry_id:entry}];
    window.ws = async msg => {
        wire.push(structuredClone(msg));
        if (msg.type === 'config_entries/get') return structuredClone(entries);
        if (msg.type.endsWith('/config_get')) return structuredClone(server);
        if (msg.type.endsWith('/config_save')) {
            Object.assign(server.common, msg.changes.common);
            const saved = server.zones[0].tanks[0];
            for (const row of msg.changes.tanks ?? []) {
                if ('scale_max_m' in row) saved.scale_max_m = row.scale_max_m;
                for (const kind of ['refill','alarm']) if (row[kind]) Object.assign(saved[kind], row[kind]);
            }
            for (const kind of ['refill','alarm']) saved[kind].effective =
                saved[kind].use_common ? server.common[kind] : saved[kind].local;
            server.revision = 'c'.repeat(64);
            return structuredClone(server);
        }
        throw new Error('unexpected route');
    };
    window.hass = {user:{id:'admin',is_admin:true}, connected:true, connection:{connected:true},
        callWS:ws, states:{'sensor.public':{attributes:{uninus_water_settings_entry:'not a scope source'}}}};
    window.mount = () => {
        const editor = document.createElement('uninus-water-dashboard-quick-parameters');
        if (!editor) return;
        editor.hass = hass;
        editor.setConfig({type,target:{entry_id:entry,zone_id:id(1),tank_id:id(2),entity_id:'sensor.p2'}});
        document.querySelector('#area').append(editor);
        return editor;
    };
    window.editor = mount();
}"""


def test_native_editor_drafts_require_explicit_apply_and_readback(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）", exact=True)
    common = page.get_by_role("group", name="補水門檻", exact=True)
    page.get_by_role("heading", name="補水參數", exact=True).wait_for()
    assert apply.is_disabled()
    scale.press_sequentially("2.75")
    page.evaluate("""() => {
        window.input = editor.shadowRoot.activeElement;
        editor.hass = {...hass, states:{'sensor.Elsewhere':{state:'ready'}}};
        editor.setConfig({type:'custom:uninus-water-dashboard',target:{entry_id:entry,zone_id:id(1),tank_id:id(2),entity_id:'sensor.p2'}});
    }""")
    assert page.evaluate("editor.shadowRoot.activeElement === input")
    assert scale.input_value() == "2.75"
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
    page.evaluate("() => { editor.remove(); window.editor = mount(); }")
    scale.wait_for()
    assert scale.input_value() == ""
    page.get_by_role("combobox", name="補水門檻模式").select_option("common")
    assert page.get_by_role("combobox", name="低液位告警模式").input_value() == "local"
    scale.fill("2.75")
    impact = page.get_by_role("list", name="受影響水桶與設定差異")
    assert impact.is_visible()
    assert "東區 · 試驗桶" in impact.inner_text()
    assert "0.4／1.6 m → 0.5／1.5 m" in impact.inner_text()
    assert "未設定 → 2.75 m" in impact.inner_text()
    apply.click()
    page.get_by_text("已儲存並重新讀回確認。", exact=True).wait_for()
    writes = page.evaluate("wire.filter(x => x.type.endsWith('/config_save'))")
    assert len(writes) == 1
    assert writes[0] == {
        "type": "uninus_water_control/config_save", "version": 1,
        "entry_id": page.evaluate("entry"), "if_revision": "a" * 64, "if_source_snapshot": "b" * 64,
        "changes": {"tanks": [{"id": "0" * 31 + "2",
                     "refill": {"use_common": True}, "scale_max_m": 2.75}]},
    }
    assert page.evaluate("wire.at(-1).type") == "config_entries/get"
    assert page.evaluate("wire.slice(wire.findIndex(x=>x.type.endsWith('/config_save'))+1).some(x=>x.type.endsWith('/config_get'))")
    assert apply.is_disabled()
    assert page.evaluate("activity.events") == []


def test_native_editor_rejects_ambiguous_or_replaced_authority(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）", exact=True)
    scale.fill("2")
    page.evaluate("entries.push({...entries[0],entry_id:'01K00A00000000000000000001'})")
    page.get_by_role("button", name="儲存此桶參數", exact=True).click()
    page.get_by_text("找到多筆補水整合，無法安全選定設定範圍。", exact=True).wait_for()
    assert not scale.count()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
    page.evaluate("""() => {
        entries.pop();
        editor.remove();
        window.editor = mount();
    }""")
    scale.fill("3")
    page.evaluate("() => { hass.user.is_admin = false; editor.hass = {...hass}; }")
    assert not scale.count()
    page.get_by_text("只有整合管理員可以讀取及套用設定。", exact=True).wait_for()


def test_invalid_policy_or_unset_common_cannot_apply(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    alarm = page.get_by_role("group", name="低液位告警", exact=True)
    mode = page.get_by_role("combobox", name="低液位告警模式")
    mode.select_option("common")
    assert apply.is_disabled()  # A missing inherited default must never be fabricated.
    mode.select_option("local")
    alarm.get_by_role("spinbutton", name="低位門檻（m）").fill("0.25")
    alarm.get_by_role("spinbutton", name="延遲（秒）").fill("1.5")
    assert apply.is_disabled()
    alarm.get_by_role("spinbutton", name="延遲（秒）").fill("1")
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    scale.fill("0")
    assert apply.is_disabled()
    scale.fill("")
    assert apply.is_enabled()
    apply.click()
    page.get_by_text("已儲存並重新讀回確認。", exact=True).wait_for()
    changes = page.evaluate("wire.find(x=>x.type.endsWith('/config_save')).changes")
    assert changes == {"tanks": [{"id": "0" * 31 + "2", "alarm": {"local": [0.25, 1]}}]}
    assert page.get_by_role("combobox", name="補水門檻模式").input_value() == "local"
    mode.select_option("common")
    assert apply.is_disabled()
    mode.select_option("local")
    assert alarm.get_by_role("spinbutton", name="延遲（秒）", exact=True).input_value() == "1"


def test_editor_rejects_lossy_numeric_draft_without_dispatch(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    refill = page.get_by_role("group", name="補水門檻", exact=True)
    lower = refill.get_by_role("spinbutton", name="下限（m）")
    upper = refill.get_by_role("spinbutton", name="上限（m）")
    lower.fill("0.1")
    upper.fill("9007199254740993")
    assert upper.input_value() == "9007199254740993"
    assert apply.is_disabled()
    upper.fill("1.125")
    scale.fill("18446744073709551617")
    assert apply.is_disabled()
    scale.fill("2.75")
    assert apply.is_enabled()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
    apply.click()
    page.get_by_text("已儲存並重新讀回確認。", exact=True).wait_for()
    writes = page.evaluate("wire.filter(x => x.type.endsWith('/config_save'))")
    assert [row["changes"] for row in writes] == [{"tanks": [{"id": "0" * 31 + "2", "refill": {"local": [0.1, 1.125]}, "scale_max_m": 2.75}]}]


def test_stale_save_and_wrong_readback_do_not_rebase_or_retry(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""() => {
        window.behavior = 'stale_revision';
        hass.callWS = async msg => {
            if (msg.type.endsWith('/config_save') && behavior === 'stale_revision') {
                wire.push(structuredClone(msg));
                throw {code:behavior, message:'private message'};
            }
            const result = await ws(msg);
            if (msg.type.endsWith('/config_get') && behavior === 'wrong' && server.revision.startsWith('c')) {
                result.zones[0].tanks[0].scale_max_m = 9;
            }
            return result;
        };
        editor.hass = hass;
    }""")
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale.fill("2.5")
    apply.click()
    page.get_by_text("設定已被變更；請重新讀取並核對後再修改。本次草稿未套用。", exact=True).wait_for()
    assert scale.input_value() == "2.5"
    assert apply.is_disabled()
    assert not page.get_by_text("private message").count()
    assert page.evaluate("wire.filter(x=>x.type.endsWith('/config_save')).length") == 1
    page.evaluate("behavior = 'wrong'")
    page.get_by_role("button", name="重新讀取（捨棄草稿）", exact=True).click()
    scale.fill("2.75")
    apply.click()
    page.get_by_text("無法確認是否已套用；請重新讀取並核對，不要重送草稿。", exact=True).wait_for()
    assert apply.is_disabled() and scale.input_value() == "2.75"
    assert not page.get_by_text("已儲存並重新讀回確認。", exact=True).count()
    assert page.evaluate("wire.filter(x=>x.type.endsWith('/config_save')).length") == 2


def test_same_principal_reconnect_keeps_draft_and_original_cas(browser_page):
    page = browser_page
    page.evaluate(SETUP)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale.press_sequentially("2.75")
    draft = scale.element_handle()
    page.evaluate("""() => {
        window.beforeReconnect = wire.length;
        editor.hass = {...hass, connected:false};
    }""")
    assert apply.is_disabled()
    assert page.evaluate("wire.length === beforeReconnect")
    page.evaluate("""() => {
        editor.hass = {...hass, connection:{connected:true}, callWS:async (msg) => {
            const dto = await ws(msg);
            return msg.type.endsWith('/config_get') ? new Promise(resolve => { window.resumeRead = () => resolve(dto); }) : dto;
        }};
    }""")
    page.wait_for_function("typeof window.resumeRead === 'function'")
    assert apply.is_disabled()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
    # Only gate the reconnect read, not the subsequent save verification.
    page.evaluate("() => { const read = resumeRead; window.resumeRead = undefined; read(); }")
    scale.wait_for()
    assert scale.input_value() == "2.75"
    assert scale.evaluate("(el, old) => el === old", draft)
    assert apply.is_enabled()
    assert page.evaluate("wire.slice(beforeReconnect).map(x=>x.type)") == [
        "config_entries/get", "uninus_water_control/config_get", "config_entries/get",
    ]
    apply.click()
    page.wait_for_function("typeof window.resumeRead === 'function'")
    page.evaluate("resumeRead()")
    page.get_by_text("已儲存並重新讀回確認。", exact=True).wait_for()
    [write] = page.evaluate("wire.filter(x => x.type.endsWith('/config_save'))")
    assert write["if_revision"] == "a" * 64 and write["if_source_snapshot"] == "b" * 64
    assert write["changes"]["tanks"][0]["scale_max_m"] == 2.75


@pytest.mark.parametrize(("key", "notice"), [
    ("revision", "設定已被變更；請重新讀取並核對後再修改。本次草稿未套用。"),
    ("source_snapshot", "水桶來源已變更；請重新讀取並核對後再修改。本次草稿未套用。"),
])
def test_same_principal_reconnect_reports_original_snapshot_conflict(browser_page, key, notice):
    page = browser_page
    page.evaluate(SETUP)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale.fill("2.75")
    page.evaluate("""key => {
        editor.hass = {...hass, connected:false};
        server[key] = 'd'.repeat(64);
        server.zones[0].tanks[0].scale_max_m = 4;
        editor.hass = hass;
    }""", key)
    page.get_by_text(notice, exact=True).wait_for()
    assert scale.input_value() == "2.75"
    assert apply.is_disabled()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
    page.get_by_role("button", name="重新讀取（捨棄草稿）", exact=True).click()
    scale.wait_for()
    assert scale.input_value() == "4"
    assert apply.is_disabled()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")


@pytest.mark.parametrize("accepted", [True, False])
def test_late_previous_transport_reply_cannot_emit_success(browser_page, accepted):
    page = browser_page
    page.evaluate(SETUP)
    page.evaluate("""accepted => {
        hass.callWS = async msg => {
            if (!msg.type.endsWith('/config_save')) return ws(msg);
            if (!accepted) {
                wire.push(structuredClone(msg));
                return new Promise((resolve, reject) => { window.settle = () => reject(new Error('obsolete error')); });
            }
            const response = await ws(msg);
            return new Promise(resolve => { window.settle = () => resolve(response); });
        };
        editor.hass = hass;
    }""", accepted)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale.fill("2.5")
    apply.click()
    page.wait_for_function("typeof window.settle === 'function'")
    page.evaluate("""() => {
        window.beforeReconnect = wire.length;
        editor.hass = {...hass, connection:{connected:true}, callWS:ws};
    }""")
    assert not scale.is_visible() and apply.is_disabled()
    assert page.evaluate("wire.length === beforeReconnect")
    page.evaluate("settle()")
    page.get_by_text("無法確認是否已套用；請重新讀取並核對，不要重送草稿。", exact=True).wait_for()
    assert scale.input_value() == "2.5" and apply.is_disabled()
    assert not page.get_by_text("已儲存並重新讀回確認。", exact=True).count()
    assert page.evaluate("wire.slice(beforeReconnect).map(x=>x.type)") == [
        "config_entries/get", "uninus_water_control/config_get", "config_entries/get",
    ]
    page.get_by_role("button", name="重新讀取（捨棄草稿）", exact=True).click()
    scale.wait_for()
    assert scale.input_value() == ("2.5" if accepted else "")  # A fresh authoritative GET, not a late acknowledgment.
    assert apply.is_disabled()
    assert page.evaluate("wire.filter(x=>x.type.endsWith('/config_save')).length") == 1


@pytest.mark.parametrize("transition", ["demotion", "principal", "entry", "close"])
def test_editor_drops_draft_on_real_owner_loss_during_held_get(browser_page, transition):
    page = browser_page
    page.evaluate(SETUP)
    scale = page.get_by_role("spinbutton", name="水位顯示量程上限（m）")
    apply = page.get_by_role("button", name="儲存此桶參數", exact=True)
    scale.fill("2.75")
    page.evaluate("""() => {
        window.reconnected = {...hass, connection:{connected:true}, callWS:async (msg) => {
            const dto = await ws(msg);
            if (!msg.type.endsWith('/config_get') || window.releaseRead) return dto;
            dto.zones[0].tanks[0].scale_max_m = 8;
            return new Promise(resolve => { window.releaseRead = () => resolve(dto); });
        }};
        editor.hass = reconnected;
    }""")
    page.wait_for_function("typeof window.releaseRead === 'function'")
    page.evaluate("""transition => {
        server.zones[0].tanks[0].scale_max_m = 4;
        if (transition === 'demotion') {
            // Real role transition on the same user object/ID and same transport.
            reconnected.user.is_admin = false;
            editor.hass = {...reconnected};
        } else if (transition === 'principal') {
            editor.hass = {...reconnected, user:{id:'different',is_admin:true}};
        } else if (transition === 'entry') {
            entries[0].entry_id = server.entry_id = '01K00A00000000000000000001';
        } else {
            editor.remove();
        }
    }""", transition)
    if transition in ("demotion", "principal", "close"):
        assert not scale.count()
    if transition == "demotion":
        page.get_by_text("只有整合管理員可以讀取及套用設定。", exact=True).wait_for()
        page.evaluate("() => { reconnected.user.is_admin = true; editor.hass = reconnected; }")
    elif transition == "close":
        page.evaluate("() => { editor.hass = hass; document.querySelector('#area').append(editor); }")
    page.evaluate("releaseRead()")
    if transition == "entry":
        page.get_by_text("補水整合範圍已變更，請重新讀取設定。", exact=True).wait_for()
        assert not scale.count()
        assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")
        return
    scale.wait_for()
    assert scale.input_value() == "4" and apply.is_disabled()
    assert not page.evaluate("wire.some(x => x.type.endsWith('/config_save'))")


def test_preview_and_incomplete_host_flags_block_action_methods(browser_page):
    page = browser_page
    page.evaluate("""() => {
        const cls = customElements.get('uninus-water-dashboard');
        window.card = document.createElement('uninus-water-dashboard');
        const s = window.previewStates = {'sensor.p1':projection(1)};
        const manual = (action) => ({version:1, tank_id:id(1), role:'refill', action});
        for (const action of ['start','stop','reset','status']) {
            const entity_id = (action === 'status' ? 'sensor' : 'button')+'.test_'+action;
            s[entity_id] = {entity_id,state:action === 'status' ? 'idle' : 'unknown',
                attributes:{uninus_water_manual:{...manual(action), ...(action === 'status' ? {
                    requested:'off',actual:'off',phase:'idle',pending_on:false,pending_off:false,
                    fault:null,can_start:true,max_runtime_seconds:60} : {})}}};
        }
        window.calls = [];
        card.setConfig(cls.getStubConfig());
        card.hass = {...snapshot(s), callService:(...args) => calls.push(args)};
        document.querySelector('#area').append(card);
    }""")
    start = page.get_by_role("button", name="手動補水", exact=True)
    assert start.is_disabled()
    page.evaluate("card.preview = false")
    assert start.is_disabled()
    page.evaluate("card.editMode = false")
    assert start.is_enabled()
    start.click()
    confirm = page.get_by_role("button", name="確認開始", exact=True)
    old = confirm.element_handle()
    page.evaluate("card.preview = true")
    assert start.is_disabled()
    old.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
    assert page.evaluate("calls") == []
    page.evaluate("() => { card.editMode = true; card.preview = false; }")
    assert start.is_disabled()
    page.evaluate("""() => {
        const state = previewStates['sensor.test_status'];
        state.state = 'fault';
        Object.assign(state.attributes.uninus_water_manual, {phase:'fault',fault:'command_failed',can_start:false});
        card.hass = {...snapshot(previewStates), callService:(...args) => calls.push(args)};
    }""")
    refill = page.locator('[data-manual-role="refill"]')
    for label in ("停止補水", "清除故障"):
        action = refill.get_by_role("button", name=label, exact=True)
        assert action.is_visible() and action.is_disabled()
        action.dispatch_event("click")
    assert page.evaluate("calls") == []
