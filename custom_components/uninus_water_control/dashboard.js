const TAG = "uninus-water-dashboard";
const ATTRIBUTE = "uninus_water_dashboard";
const MANUAL = "uninus_water_manual";
const PREVIEW = "uninus_water_thresholds";
const PREVIEW_KEYS = ["version", "zone_id", "tank_id", "kind"];

const PREVIEW_TYPES = new Map([
  ["refill_preview", { domain: /^sensor\.[a-z0-9_]+$/,
    states: ["refill_threshold", "between_thresholds", "stop_threshold", "unavailable"] }],
  ["low_level_alarm", { domain: /^binary_sensor\.[a-z0-9_]+$/,
    states: ["on", "off", "unavailable"] }],
]);
const ROLES = ["refill", "alarm"];
const ACTIONS = ["start", "stop", "reset"];
const PHASES = ["idle", "starting", "running", "stopping", "fault", "unknown"];
const FEEDBACK = ["on", "off", "unknown"];
const OPTIONS_PATH = "/config/integrations/integration/uninus_water_control";
const VIEWS = { overview: "操作總覽", parameters: "補水參數", devices: "區域與水桶" };
const OUTPUT = { refill: "補水輸出", alarm: "告警輸出" };
const ACTION_LABEL = {
  refill: { start: "手動補水", stop: "停止補水", reset: "清除故障" },
  alarm: { start: "開啟告警", stop: "關閉告警", reset: "清除故障" },
};
const PHASE_LABEL = { idle: "待命", starting: "啟動中", running: "運轉中", stopping: "停止中", fault: "故障", unknown: "未知" };
const REPORT = { on: "開", off: "關", unknown: "未知" };
const FAULT_TEXT = {
  restart_required: "請重新啟動 Home Assistant 以恢復控制",
  output_binding_changed: "輸出綁定已變更", source_invalid: "水位來源無效",
  output_unknown: "輸出回報未知", unexpected_off: "非預期關閉", unexpected_on: "非預期開啟",
  on_unconfirmed: "未確認開啟", off_unconfirmed: "未確認關閉", runtime_limit: "已達運轉上限",
  command_failed: "輸出請求失敗", command_interrupted: "輸出請求中斷", start_guard_changed: "啟動條件已變更",
};
const REFILL_LABEL = {
  refill_threshold: "↓ 達補水低門檻", between_thresholds: "水位在補水門檻內", stop_threshold: "↑ 達補水高門檻",
  unavailable: "補水預覽無法取得",
};
const refillText = ([low, high]) => `補水門檻　${low}／${high} m`;
const alarmText = ([low, seconds]) => `低液位告警　≤ ${low} m · ${seconds} 秒`;
const scaleText = (max) => `水位量程：0–${max} m`;
const NO_SCALE = "未設定水位量程";
const UNAVAILABLE = "液位讀值不可用";

// Layout and palette from the approved proposal's product area (not its demo host).
const STYLE = `
  :host { display:block; min-width:0; }
  .shell {
    container: water-console / inline-size; padding:14px; border-radius:13px;
    --bg:#eef4f9; --card:#fff; --tint:#f7fafc; --ink:#143047; --muted:#4f6477; --line:#cddce7;
    --accent:#075f92; --accent-bg:#dceffc; --good:#086352; --good-bg:#d9f4ed;
    --high:#8e2827; --high-bg:#fde5e1; --low:#174e8b; --low-bg:#e1edff;
    --neutral:#425568; --neutral-bg:#e8eef3; --wait:#70420f; --wait-bg:#f9ecd8;
    background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,'Microsoft JhengHei',sans-serif;
  }
  .shell[data-dark] {
    --bg:#17232e; --card:#243442; --tint:#1d2b38; --ink:#e3eff9; --muted:#b5c7d8; --line:#496173;
    --accent:#15638d; --accent-bg:#25435c; --good:#b3efdc; --good-bg:#274c47;
    --high:#ffd1ca; --high-bg:#623635; --low:#c3deff; --low-bg:#294563;
    --neutral:#d2e3f0; --neutral-bg:#354859; --wait:#ffe0aa; --wait-bg:#574127;
  }
  *, *::before, *::after { box-sizing:border-box; }
  [hidden] { display:none !important; }
  h1,h2,h3,p { margin:0; }
  h1 { font-size:25px; line-height:1.17; }
  h2 { font-size:17px; }
  h3 { font-size:15px; }
  button,.options-link {
    font:inherit; color:var(--ink); min-width:0; min-height:44px; padding:5px 11px;
    background:var(--card); border:1px solid var(--line); border-radius:9px;
    font-weight:700; cursor:pointer;
  }
  button:not(:disabled):hover,.options-link:hover { border-color:var(--accent); }
  button:disabled { cursor:not-allowed; color:var(--muted); background:var(--tint); border-style:dashed; }
  :focus-visible { outline:2px solid var(--low); outline-offset:2px; }
  .flex,.policy,.readout { display:flex; align-items:center; gap:8px; justify-content:space-between; flex-wrap:wrap; }
  .pagehead { margin-bottom:10px; }
  .tabs { display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin:8px 0; }
  .tabs button[aria-pressed=true],.options-link { background:var(--accent); border-color:var(--accent); color:white; }
  .dim { color:var(--muted); font-size:12px; }
  .policy,.zone,.formbox { background:var(--card); border:1px solid var(--line); border-radius:13px; padding:10px; }
  .policy { justify-content:flex-start; gap:8px 16px; margin-bottom:10px; }
  .policy b { overflow-wrap:anywhere; }
  .pill { font-size:12px; line-height:1.3; font-weight:750; padding:4px 7px; border-radius:999px;
    background:var(--neutral-bg); color:var(--neutral); overflow-wrap:anywhere; }
  .pill.good { background:var(--good-bg); color:var(--good); }
  .pill.high,.commands .stop:not(:disabled) { background:var(--high-bg); color:var(--high); }
  .pill.wait { background:var(--wait-bg); color:var(--wait); }
  .notice { background:var(--card); border:1px solid var(--line); border-radius:9px; padding:8px 10px; }
  .zones,.editor-split { display:grid; gap:10px; align-items:start; }
  .zones { grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr)); }
  .zone-head { margin-bottom:8px; }
  .tanks { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,138px),1fr)); gap:7px; }
  .tank { background:var(--tint); border:1px solid var(--line); border-radius:10px; padding:8px; min-width:0; }
  .tank .flex { gap:4px; }
  h2,h3,.settings-row,.diagnostics,.manual-dialog { overflow-wrap:anywhere; }
  .readout { justify-content:flex-start; flex-wrap:nowrap; margin:7px 0; }
  .level {
    flex:1; display:block; min-width:0; text-align:start; padding:0;
    min-height:44px; border:0; border-radius:5px; background:transparent;
  }
  .level:hover .value { text-decoration:underline; }
  .vessel { display:grid; align-items:end; overflow:hidden; width:25px; height:34px; flex:none;
    border:1px solid #6191ac; border-radius:5px; background:var(--accent-bg); }
  .water { width:100%; background:#328fc6; }
  .reading { min-width:0; font-size:23px; font-weight:850; letter-spacing:-.045em; line-height:1.1; }
  .value { overflow-wrap:anywhere; }
  .reading small { font-size:12px; letter-spacing:0; font-weight:700; color:var(--muted); margin-left:2px; }
  .relation { display:block; font-weight:600; letter-spacing:0; margin-top:3px; overflow-wrap:anywhere; }
  .info { display:grid; align-content:start; min-height:34px; line-height:1.25; font-size:12px; margin-bottom:6px; gap:2px; }
  .commands { display:grid; gap:4px; }
  .commands button { width:100%; padding-inline:4px; }
  .diagnostics-toggle { min-height:0; border:0; }
  .diagnostics-toggle::after { content:'▾' / ''; margin-inline-start:3px; }
  .diagnostics-toggle[aria-expanded=true]::after { content:'▴' / ''; }
  .diagnostics-toggle:hover { text-decoration:underline; }
  .diagnostics { display:grid; gap:4px; margin-top:6px; font-size:12px; }
  .diagnostics button { width:100%; text-align:start; padding-inline:5px; }
  .manual-fault,.range-warning { color:var(--high); font-weight:700; }
  .manual-message:empty,.manual-fault:empty,.manual-pending:empty { display:none; }
  .manual-dialog {
    position:static; width:100%; max-width:none; margin:6px 0 0; padding:8px;
    border:1px solid var(--line); border-radius:9px; background:var(--card); color:var(--ink);
  }
  .manual-dialog p { margin:4px 0; }
  .manual-dialog-actions { display:flex; flex-wrap:wrap; justify-content:end; gap:4px; }
  .formbox { display:grid; gap:10px; }
  .editor-zone,.settings-row { border-top:1px solid var(--line); padding-top:8px; }
  .editor-zone { display:grid; gap:8px; }
  .options-link { display:inline-flex; align-items:center; text-decoration:none; }
  @container water-console (max-width:480px) {
    button { font-size:13px; }
    .policy { gap:5px 10px; }
    .tank .pill { font-size:11px; }
    .tank h3 { font-size:14px; }
    .tank .flex { justify-content:flex-start; }
  }
  @container water-console (min-width:750px) {
    .editor-split { grid-template-columns:repeat(2,minmax(0,1fr)); }
  }
`;

function text(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function element(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function displayFrom(entity, state) {
  const wire = state.attributes?.uninus_water_display;
  if (state.entity_id !== entity || !wire || typeof wire !== "object" || Array.isArray(wire) || wire.version !== 1 ||
      Object.keys(wire).some((key) => !["version", "scale_max_m", "refill", "alarm"].includes(key))) return {};
  const validPair = (pair) => Array.isArray(pair) && pair.length === 2 &&
    Number.isFinite(pair[0]) && Number.isFinite(pair[1]);
  return {
    scale: Number.isFinite(wire.scale_max_m) && wire.scale_max_m > 0 ? wire.scale_max_m : undefined,
    refill: validPair(wire.refill) && wire.refill[0] < wire.refill[1] ? wire.refill : undefined,
    alarm: validPair(wire.alarm) && Number.isInteger(wire.alarm[1]) && wire.alarm[1] >= 1 ? wire.alarm : undefined,
  };
}

function uniformPair(displays, kind) {
  const first = displays[0]?.[kind];
  return first && displays.every((display) => display[kind]?.[0] === first[0] && display[kind][1] === first[1]) ? first : undefined;
}


function depthFrom(state) {
  const raw = state.state;
  if (state.attributes?.unit_of_measurement !== "m" || typeof raw !== "string" ||
      !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(raw)) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) return undefined;
  // Match the proposal's two-place readout without rounding away a distinct
  // numeric sample on the other side of a threshold.
  const shortDepth = /^[+-]?\d+(?:\.\d{1,2})?$/.test(raw) && Math.abs(value) < 1e21;
  return { shown: shortDepth ? value.toFixed(2) : raw, value };
}

function updateGauge(slot, depth, max) {
  const measurable = max !== undefined && !!depth;
  slot.vessel.hidden = !measurable;
  text(slot.range, max === undefined ? NO_SCALE : scaleText(max));
  const outside = measurable && (depth.value < 0 || depth.value > max);
  slot.rangeMessage.hidden = !outside;
  text(slot.rangeMessage, outside ? "超出顯示量程" : "");
  if (measurable) {
    const value = Math.min(max, Math.max(0, depth.value));
    slot.vessel.setAttribute("aria-valuemax", max);
    slot.vessel.setAttribute("aria-valuenow", value);
    slot.vessel.setAttribute("aria-label", scaleText(max));
    slot.water.style.height = `${value / max * 100}%`;
  }
}

function relationText(depth, display, refill, alarm) {
  if (!depth) return UNAVAILABLE;
  // The comparison and delayed preview are separate: a low reading does not
  // imply time-qualified alarm activity or that any physical output is ON.
  if (display.alarm && depth.value <= display.alarm[0]) {
    return depth.value < display.alarm[0] ? "↓ 低於告警門檻" : "↓ 達告警低門檻";
  }
  if (display.refill && refill === "stop_threshold" && depth.value > display.refill[1]) return "↑ 高於補水門檻";
  if (display.refill && refill === "refill_threshold" && depth.value < display.refill[0]) return "↓ 低於補水門檻";
  if (refill && refill !== "unavailable") return REFILL_LABEL[refill];
  if (alarm === "on") return "低液位告警中";
  return display.refill || refill ? REFILL_LABEL.unavailable : "補水門檻未設定";
}

function validStatus(state, status) {
  return state === status.phase && PHASES.includes(status.phase) &&
    ["on", "off"].includes(status.requested) && FEEDBACK.includes(status.actual) &&
    typeof status.pending_on === "boolean" && typeof status.pending_off === "boolean" &&
    (status.fault === null || (typeof status.fault === "string" && /^[a-z_]{1,48}$/.test(status.fault))) &&
    Number.isInteger(status.max_runtime_seconds) && status.max_runtime_seconds >= 1 &&
    status.max_runtime_seconds <= 300 && typeof status.can_start === "boolean";
}

function manualFrom(states) {
  const byTank = new Map();
  for (const [id, state] of Object.entries(states)) {
    const m = state?.attributes?.[MANUAL];
    if (typeof m?.tank_id !== "string" || !/^[0-9a-f]{32}$/.test(m.tank_id) ||
        !ROLES.includes(m.role) || !(m.action === "status" || ACTIONS.includes(m.action))) continue;
    if (!byTank.has(m.tank_id)) byTank.set(m.tank_id, new Map());
    const roles = byTank.get(m.tank_id);
    if (!roles.has(m.role)) roles.set(m.role, new Map());
    const actions = roles.get(m.role);
    // A malformed claimant of the same slot is still a collision, not authority.
    if (actions.has(m.action)) {
      actions.set(m.action, null);
      continue;
    }
    const domain = m.action === "status" ? /^sensor\.[a-z0-9_]+$/ : /^button\.[a-z0-9_]+$/;
    const identity = m.version === 1 && id === state.entity_id && domain.test(id);
    if (m.action === "status") {
      const status = validStatus(state.state, m) ? m : null;
      actions.set(m.action, identity && (status || state.state === "unavailable") ? { id, status } : null);
    } else {
      const available = state.state === "unknown" || (typeof state.state === "string" &&
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(state.state) &&
        Number.isFinite(Date.parse(state.state)));
      actions.set(m.action, identity && available ? id : null);
    }
  }
  return byTank;
}

function validInventoryId(id) {
  return typeof id === "string" && /^[0-9a-f]{32}$/.test(id);
}

const previewKey = (zone, tank) => `${zone}:${tank}`;

function previewsFrom(states) {
  const bySlot = new Map();
  for (const [id, state] of Object.entries(states)) {
    const wire = state?.attributes?.[PREVIEW];
    if (!wire || typeof wire !== "object" || Array.isArray(wire)) continue;
    const { zone_id, tank_id, kind } = wire;
    const type = PREVIEW_TYPES.get(kind);
    if (!type || !validInventoryId(zone_id) || !validInventoryId(tank_id)) continue;
    const key = previewKey(zone_id, tank_id);
    if (!bySlot.has(key)) bySlot.set(key, new Map());
    const kinds = bySlot.get(key);
    // Once a row claims the same slot, reject ambiguity even if one row is malformed.
    if (kinds.has(kind)) {
      kinds.set(kind, null);
      continue;
    }
    const valid = Object.keys(wire).length === PREVIEW_KEYS.length &&
      PREVIEW_KEYS.every((key) => Object.hasOwn(wire, key)) && wire.version === 1 &&
      id === state.entity_id && type.domain.test(id) && type.states.includes(state.state);
    kinds.set(kind, valid ? id : null);
  }
  return bySlot;
}

const byIndex = (a, b) => a.index - b.index;

function validSlot(slot) {
  return validInventoryId(slot?.id) &&
    typeof slot.name === "string" && Number.isSafeInteger(slot.index) && slot.index >= 0;
}

function duplicates(items, key) {
  const seen = new Set();
  const repeated = new Set();
  for (const item of items) {
    if (seen.has(item[key])) repeated.add(item[key]);
    else seen.add(item[key]);
  }
  return repeated;
}

function zonesFrom(states) {
  const zones = new Map();
  const tanks = [];
  for (const [entity, state] of Object.entries(states)) {
    const metadata = state?.attributes?.[ATTRIBUTE];
    if (metadata?.version !== 1 || !/^sensor\.[a-z0-9_]+$/.test(entity)) continue;
    const { zone, tank } = metadata;
    if (!validSlot(zone) || !validSlot(tank)) continue;
    if (!zones.has(zone.id)) {
      zones.set(zone.id, { id: zone.id, name: zone.name, index: zone.index, tanks: [] });
    }
    const group = zones.get(zone.id);
    if (group.name !== zone.name || group.index !== zone.index) group.conflicted = true;
    const item = { id: tank.id, name: tank.name, index: tank.index, entity };
    group.tanks.push(item);
    tanks.push(item);
  }
  // Missing indices are normal for a permission-filtered state map; collisions are not.
  const groups = [...zones.values()].filter((zone) => !zone.conflicted);
  const duplicateZoneSlots = duplicates(groups, "index");
  const duplicateTankIds = duplicates(tanks, "id");
  return groups.filter((zone) => !duplicateZoneSlots.has(zone.index)).filter((zone) => {
    const duplicateTankSlots = duplicates(zone.tanks, "index");
    zone.tanks = zone.tanks.filter((tank) =>
      !duplicateTankIds.has(tank.id) && !duplicateTankSlots.has(tank.index)).sort(byIndex);
    return zone.tanks.length > 0;
  }).sort(byIndex);
}

// Keep the focused subtree connected even when its list index changes.
function arrange(parent, children, focused) {
  const pivot = focused ? children.findIndex((child) => child.contains(focused)) : -1;
  let next = null;
  if (pivot !== -1) {
    next = children[pivot];
    let previous = next;
    for (const child of children.slice(pivot + 1)) {
      if (previous.nextSibling !== child) parent.insertBefore(child, previous.nextSibling);
      previous = child;
    }
  }
  for (let index = (pivot === -1 ? children.length : pivot) - 1; index >= 0; index--) {
    const child = children[index];
    if (child.parentNode !== parent || child.nextSibling !== next) parent.insertBefore(child, next);
    next = child;
  }
}


function button(className) {
  const node = element("button", className);
  node.type = "button";
  return node;
}

function focusWasLost(root, focused) {
  return focused && !root.activeElement && !focused.isConnected;
}

function moreInfo(node, entityId) {
  if (!entityId) return;
  node.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
}

function zoneReport(reports) {
  const on = reports.filter((report) => report === "on").length;
  const incomplete = reports.filter((report) => report !== "on" && report !== "off").length;
  if (!incomplete) return on ? `${on} 桶告警輸出開啟` : "告警輸出皆關";
  if (reports.every((report) => report === undefined)) return "無告警輸出回報";
  return on ? `${on} 桶告警輸出開啟 · ${incomplete} 桶未確認` : `告警輸出 ${incomplete} 桶未確認`;
}

const manualSlot = ({ tankId, role }) => `${tankId}:${role}`;
const stopObservation = (binding) => binding?.status && binding.buttons.stop
  ? `${binding.statusId}:${binding.buttons.stop}` : undefined;
const settledOff = (status) => status && (status.phase === "idle" || status.phase === "fault") &&
  status.requested === "off" && status.actual === "off" && !status.pending_on && !status.pending_off;

class UninusWaterDashboard extends HTMLElement {
  #hass;
  #configured = false;

  #layout;
  #notice;
  #overview;
  #shell;
  #views = new Map();
  #view = "overview";
  #zones = new Map();
  #panels = new Map();
  #flights = new Map();
  #startInterlocks = new Map();
  #authority;
  #sessionEpoch = 0;
  #confirmation;
  #dialog;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const style = element("style");
    style.textContent = STYLE;
    this.#shell = element("div", "shell");
    this.#shell.lang = "zh-Hant";
    const head = element("header", "pagehead flex");
    const heading = element("div");
    const title = element("h1");
    const count = element("p", "dim");
    text(title, "補水控制");
    heading.append(title, count);
    const nav = element("nav", "tabs");
    nav.setAttribute("aria-label", "補水頁面");
    head.append(heading, nav);
    this.#shell.append(head);
    for (const [key, name] of Object.entries(VIEWS)) {
      const tab = button();
      const section = element("section");
      section.id = key;
      section.setAttribute("aria-label", name);
      tab.setAttribute("aria-controls", key);
      tab.addEventListener("click", () => { this.#showView(key); this.#render(); });
      text(tab, name);
      nav.append(tab);
      this.#shell.append(section);
      this.#views.set(key, { section, tab });
    }
    this.#layout = element("div", "zones");
    this.#notice = element("div", "notice");
    this.#notice.setAttribute("role", "status");
    const summary = element("div", "policy");
    const refill = element("b");
    const separator = element("span", "dim");
    text(separator, "·");
    const alarm = element("b");
    summary.append(refill, separator, alarm);
    this.#views.get("overview").section.append(summary, this.#notice, this.#layout);
    this.#overview = { count, summary, refill, alarm };
    for (const [key, titleText, linkText, help] of [
      ["parameters", "各桶門檻與顯示量程", "前往整合設定：門檻與量程",
        "此處僅顯示各桶已儲存的參數，不會自動補水或操作告警。若要修改，在整合的「設定」選單選擇門檻預覽或水位顯示量程。"],
      ["devices", "管理區域與水桶", "前往整合設定：區域與水桶",
        "此處列出可讀取的水桶與手動控制。若要新增、改名或變更綁定，在整合的「設定」選單選擇對應的區域或水桶操作。"],
    ]) {
      const view = this.#views.get(key);
      const frame = element("div", "formbox");
      const header = element("header", "flex");
      const title = element("h2");
      const link = element("a", "options-link");
      const description = element("p", "dim");
      const inventory = element("div", "editor-split");
      text(title, titleText);
      text(link, linkText);
      text(description, help);
      link.href = OPTIONS_PATH;
      link.hidden = true;
      header.append(title, link);
      frame.append(header, description, inventory);
      view.section.append(frame);
      Object.assign(view, { inventory, link, rows: new Map() });
    }
    this.#showView("overview");
    root.append(style, this.#shell);
  }

  #showView(key) {
    if (this.#view !== key) this.#closeConfirmation();
    this.#view = key;
    for (const [id, { section, tab }] of this.#views) {
      const active = id === key;
      section.hidden = !active;
      tab.setAttribute("aria-pressed", active);
    }
  }

  static getStubConfig() {
    return { type: `custom:${TAG}` };
  }

  getGridOptions() {
    return { columns: "full", rows: "auto", min_columns: 3 };
  }

  getCardSize() {
    return Math.max(1, Math.ceil(this.offsetHeight / 50));
  }

  setConfig(config) {
    if (config?.type !== `custom:${TAG}`) throw new Error(`Expected custom:${TAG}`);
    if (this.#configured) return;
    this.#configured = true;
    this.#render();
  }

  set hass(value) {
    const authority = [value?.user?.id, value?.user?.is_admin, value?.user?.is_active,
      value?.connection, value?.connected, value?.callService];
    if (!this.#authority || authority.some((part, index) => part !== this.#authority[index])) {
      this.#authority = authority;
      this.#closeConfirmation();
      this.#clearPanelFeedback();
    }
    this.#hass = value;
    this.#render();
  }

  connectedCallback() {
    this.#render();
  }

  disconnectedCallback() {
    this.#closeConfirmation();
    this.#clearPanelFeedback();
  }

  #clearPanelFeedback() {
    this.#sessionEpoch++;
    for (const roles of this.#panels.values()) {
      for (const panel of roles.values()) {
        panel.feedback = undefined;
        panel.rejected = false;
        panel.startWait = undefined;
      }
    }
  }

  #closeConfirmation(after = "start") {
    const pending = this.#confirmation;
    const node = this.#dialog?.node;
    this.#confirmation = undefined;
    if (!pending || !node?.open) return;
    const restoreFocus = node.contains(this.shadowRoot.activeElement);
    node.close();
    if (restoreFocus && pending.panel.host.isConnected) {
      const preferred = pending.panel.buttons[after];
      const fallback = pending.panel.buttons.stop;
      const focus = [preferred, fallback, pending.panel.buttons.start].find((button) => !button.disabled && !button.hidden);
      (focus ?? pending.panel.readout).focus();
    }
  }

  #openConfirmation(panel) {
    const binding = this.#binding(panel);
    if (!binding || binding.key !== panel.key || !this.#allowed(panel, binding, "start")) return;
    this.#closeConfirmation();
    if (!this.#dialog) {
      const node = document.createElement("dialog");
      const title = document.createElement("strong");
      const summary = document.createElement("p");
      const warning = document.createElement("p");
      const actions = document.createElement("div");
      const cancel = document.createElement("button");
      const confirm = document.createElement("button");
      cancel.type = confirm.type = "button";
      node.className = "manual-dialog";
      title.id = "manual-confirm-title";
      node.setAttribute("aria-labelledby", title.id);
      actions.className = "manual-dialog-actions";
      cancel.autofocus = true;
      actions.append(cancel, confirm);
      node.append(title, summary, warning, actions);
      cancel.addEventListener("click", () => this.#closeConfirmation());
      node.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        this.#closeConfirmation();
      });
      confirm.addEventListener("click", () => {
        const confirmation = this.#confirmation;
        if (!confirmation || !node.open) return;
        this.#closeConfirmation("stop");
        void this.#press(confirmation.panel, "start", confirmation.key);
      });
      this.#dialog = { node, title, summary, warning, cancel, confirm };
    }
    this.#confirmation = { panel, key: binding.key };
    this.#refreshConfirmation(binding);
    // Modeless: this confirmation must never block either role's Stop button.
    panel.tankHost.append(this.#dialog.node);
    this.#dialog.node.show();
    this.#dialog.cancel.focus();
  }

  #refreshConfirmation(binding) {
    const pending = this.#confirmation;
    if (!pending) return;
    if (!binding || binding.key !== pending.key || !this.#allowed(pending.panel, binding, "start")) {
      this.#closeConfirmation();
      return;
    }

    const { title, summary, warning, cancel, confirm } = this.#dialog;
    text(title, ACTION_LABEL[pending.panel.role].start);
    text(summary, `${binding.tank.name} · 運轉上限：${binding.status.max_runtime_seconds} 秒`);
    text(warning, "確認安全後才開始。系統回報不代表實體裝置已動作。");
    text(cancel, "取消");
    text(confirm, "確認開始");
  }

  #binding(panel, tank, actions) {
    if (!this.isConnected || !this.#hass?.states) return;
    if (!tank) {
      if (this.#panels.get(panel.tankId)?.get(panel.role) !== panel) return;
      const states = this.#hass.states;
      tank = zonesFrom(states).flatMap((zone) => zone.tanks).find(({ id }) => id === panel.tankId);
      if (!tank) return;
      actions = manualFrom(states).get(tank.id)?.get(panel.role);
    }
    const identity = actions?.get("status");
    if (!identity) return;
    const buttons = Object.fromEntries(ACTIONS.map((action) => [action, actions.get(action) ?? null]));
    const { status, id } = identity;
    const key = JSON.stringify([tank.entity, tank.name, id, status?.max_runtime_seconds, ...Object.values(buttons)]);
    const statusKey = status && JSON.stringify([status.phase, status.requested, status.actual,
      status.pending_on, status.pending_off, status.fault, status.can_start]);
    return { buttons, status, statusId: id, statusKey, tank, key };
  }

  #busy(panel, action, target) {
    // Entity IDs can change while a native request is unresolved. Keep the
    // logical operation locked as well, without sharing the Start lock with Stop.
    return this.#flights.has(target) || [...this.#flights.values()].some((flight) =>
      flight.tankId === panel.tankId && flight.role === panel.role && flight.action === action);
  }

  #allowed(panel, binding, action) {
    const { buttons, status } = binding;
    if (typeof this.#hass.callService !== "function" || this.#hass.connected === false ||
        this.#hass.user?.is_active === false ||
        !buttons[action] || this.#busy(panel, action, buttons[action])) return false;
    // Stop is never gated on the status of ON or on a Start transport flight.
    if (action === "stop") return true;
    if (!status) return false;
    const off = status.requested === "off" && status.actual === "off" && !status.pending_on &&
      !status.pending_off && !this.#busy(panel, "stop", buttons.stop) && !this.#busy(panel, "start", buttons.start);
    if (!off) return false;
    // The v1 projection does not expose binding/source health for reset;
    // the native button checks those remaining preconditions in Core.
    if (action === "reset") return status.phase === "fault" && status.fault !== null && status.fault !== "restart_required";
    return status.phase === "idle" && status.fault === null && status.can_start && !!buttons.stop &&
      !this.#busy(panel, "reset", buttons.reset) && !this.#startInterlocks.has(manualSlot(panel));
  }

  #observeStart(panel, binding) {
    const slot = manualSlot(panel);
    const interlock = this.#startInterlocks.get(slot);
    if (!interlock) return;
    const { status, statusId } = binding ?? {};
    // Neither a stale/absent report nor a policy-only eligibility update proves
    // that a Start was observed or that the controller has since closed it.
    if (!status || statusId !== interlock.statusId || interlock.epoch !== this.#sessionEpoch ||
        this.#hass.connected === false || this.#hass.user?.is_active === false) {
      interlock.sawActivity = false;
      return;
    }
    if (status.requested === "on" || status.actual === "on" || status.pending_on || status.pending_off ||
        status.phase === "stopping") interlock.sawActivity = true;
    if (interlock.settled && interlock.sawActivity && settledOff(status)) this.#startInterlocks.delete(slot);
  }

  async #press(panel, action, confirmKey) {
    const binding = this.#binding(panel);
    if (!binding || binding.key !== panel.key || (action === "start" && binding.key !== confirmKey) ||
        !this.#allowed(panel, binding, action)) return;
    if (action === "stop") this.#closeConfirmation();
    const target = binding.buttons[action];
    const flight = { tankId: panel.tankId, role: panel.role, action };
    const slot = manualSlot(panel);
    const epoch = this.#sessionEpoch;
    if (action === "start") {
      // Admission may already have happened even if the response is rejected.
      // Own the safety interlock by tank/role, not by the rendered panel or entity name.
      this.#startInterlocks.set(slot, { flight, statusId: binding.statusId, epoch, settled: false, sawActivity: false });
      panel.startWait = { status: binding.statusKey, flight };
    }
    const interlock = this.#startInterlocks.get(slot);
    const stopOwner = panel.stopOwner;
    // A Stop sent before the Start reply settles must not clear uncertainty on
    // an unchanged OFF report: it may have reached Core before that Start.
    const canResolveWithStop = action === "stop" && interlock?.settled && stopOwner;
    this.#flights.set(target, flight);
    panel.feedback = flight;
    panel.rejected = false;
    try {
      const call = this.#hass.callService("button", "press", {}, { entity_id: target });
      this.#render();
      await call;
      if (canResolveWithStop && epoch === this.#sessionEpoch && this.#hass.connected !== false &&
          this.#hass.user?.is_active !== false && panel.stopOwner === stopOwner &&
          this.#startInterlocks.get(slot) === interlock) {
        const current = this.#binding(panel);
        if (current?.buttons.stop === target && stopObservation(current) === stopOwner.key && settledOff(current.status)) {
          this.#startInterlocks.delete(slot);
        }
      }
    } catch {
      if (panel.startWait?.flight === flight) panel.startWait = undefined;
      if (panel.feedback === flight) panel.rejected = true;
    } finally {
      if (action === "start" && this.#startInterlocks.get(slot) === interlock) interlock.settled = true;
      this.#flights.delete(target);
      if (panel.feedback === flight) panel.feedback = undefined;
      this.#render();
    }
  }


  #newPanel(tankId, role, slot) {
    const host = element("div", "action-slot");
    host.dataset.manualRole = role;
    const output = element("span");
    const details = element("section");
    details.dataset.controlDetails = role;
    const title = element("strong");
    text(title, OUTPUT[role]);
    const phase = element("div");
    const requested = element("div");
    const pending = element("div", "manual-pending");
    const fault = element("div", "manual-fault");
    const timing = element("div");
    const message = element("div", "manual-message");
    message.setAttribute("role", "status");
    const panel = { tankId, role, host, output, details, phase, requested, pending, fault, message, timing,
      tankHost: slot.host, readout: slot.readout };
    panel.buttons = Object.fromEntries(ACTIONS.map((action) => {
      const control = button(action === "stop" ? "stop" : "");
      control.dataset.action = action;
      control.addEventListener("click", () => {
        if (action === "start") this.#openConfirmation(panel);
        else void this.#press(panel, action);
      });
      text(control, ACTION_LABEL[role][action]);
      host.append(control);
      return [action, control];
    }));
    // Faults, feedback and Stop cannot be hidden behind the disclosure.
    details.append(title, phase, requested, timing);
    host.append(pending, fault, message);
    return panel;
  }

  #updatePanel(panel, binding) {
    const { status } = binding ?? {};
    if (panel.key !== binding?.key) {
      panel.key = binding?.key;
      panel.feedback = undefined;
      panel.rejected = false;
      panel.startWait = undefined;
    }
    const stopKey = stopObservation(binding);
    if (panel.stopOwner?.key !== stopKey) panel.stopOwner = stopKey ? { key: stopKey } : undefined;
    if (panel.startWait && status && panel.startWait.status !== binding.statusKey) panel.startWait = undefined;
    this.#observeStart(panel, binding);
    const { role } = panel;
    const { start, stop, reset } = panel.buttons;
    const focused = this.shadowRoot.activeElement;
    const active = status?.actual === "on" || status?.actual === "unknown" || status?.requested === "on" ||
      status?.pending_on || status?.pending_off || ["starting", "running", "stopping", "fault", "unknown"].includes(status?.phase);
    const startBusy = this.#busy(panel, "start", binding?.buttons.start);
    const stopBusy = this.#busy(panel, "stop", binding?.buttons.stop);
    const stopping = !!binding && (active || !status || startBusy || stopBusy || this.#startInterlocks.has(manualSlot(panel)));
    start.hidden = stopping;
    stop.hidden = !stopping;
    reset.hidden = !status?.fault;
    const actionAllowed = (action) => !!binding && this.#allowed(panel, binding, action);
    const reason = !binding ? "未提供受控按鈕（尚未啟用或無法讀取）" : !status ? "無法取得控制狀態" : "目前不可操作";
    for (const action of ACTIONS) {
      const control = panel.buttons[action];
      const disabled = !actionAllowed(action);
      if (control.disabled !== disabled) control.disabled = disabled;
      control.title = disabled ? reason : "";
    }
    if (Object.values(panel.buttons).includes(focused) && (focused.hidden || focused.disabled)) {
      // Never land on Start as a side effect of a focused Stop or Reset retiring.
      (stopping && !stop.disabled ? stop : panel.readout).focus({ preventScroll: true });
    }
    const actual = binding ? REPORT[status?.actual ?? "unknown"] : "未提供";
    text(panel.output, `${OUTPUT[role]}：${actual}`);
    text(panel.phase, `狀態：${status ? PHASE_LABEL[status.phase] : reason}`);
    text(panel.requested, `要求：${REPORT[status?.requested ?? "unknown"]}`);
    text(panel.timing, status ? `運轉上限：${status.max_runtime_seconds} 秒` : "運轉上限：未知");
    const pending = [status?.pending_on && "開", status?.pending_off && "關"].filter(Boolean);
    text(panel.pending, pending.length ? `待處理：${pending.join("／")}` : "");
    text(panel.fault, status?.fault ? `故障：${Object.hasOwn(FAULT_TEXT, status.fault) ? FAULT_TEXT[status.fault] : "未知"}` : "");
    text(panel.message, panel.feedback ? panel.feedback.action === "stop" ? "正在送出停止請求…" : "正在送出按鈕請求…" :
      panel.rejected ? "未確認按鈕請求。" : panel.startWait ? "等待狀態更新…" : "");
    if (this.#confirmation?.panel === panel) this.#refreshConfirmation(binding);
    return status;
  }

  #newTank(tank) {
    const host = element("article", "tank");
    host.dataset.tankId = tank.id;
    const header = element("header", "flex");
    const title = element("h3");
    const pill = button("pill diagnostics-toggle");
    header.append(title, pill);
    const measurement = element("div", "readout");
    const readout = button("level");
    const vessel = element("span", "vessel");
    vessel.setAttribute("role", "meter");
    vessel.setAttribute("aria-valuemin", "0");
    const water = element("span", "water");
    vessel.append(water);
    const reading = element("span", "reading");
    const value = element("span", "value");
    const unit = element("small");
    text(unit, " m");
    const relation = element("span", "dim relation");
    reading.append(value, unit, relation);
    readout.append(reading);
    measurement.append(vessel, readout);
    const rangeMessage = element("p", "dim range-warning");
    const info = element("div", "info");
    const commands = element("div", "commands");
    const diagnostics = element("div", "diagnostics");
    diagnostics.id = `details-${tank.id}`;
    diagnostics.hidden = true;
    pill.title = "詳細狀態";
    pill.setAttribute("aria-controls", diagnostics.id);
    pill.setAttribute("aria-expanded", "false");
    pill.addEventListener("click", () => {
      diagnostics.hidden = !diagnostics.hidden;
      pill.setAttribute("aria-expanded", String(!diagnostics.hidden));
    });
    const range = element("p", "dim");
    const previewRows = new Map([...PREVIEW_TYPES.keys()].map((kind) => {
      const node = button();
      node.addEventListener("click", () => moreInfo(node, node.dataset.entity));
      return [kind, node];
    }));
    host.append(header, measurement, rangeMessage, info, commands, diagnostics);
    const slot = { host, title, pill, readout, vessel, water, value, unit, relation, range, rangeMessage, previewRows };
    readout.addEventListener("click", () => moreInfo(readout, slot.entity));
    const roles = new Map();
    for (const role of ROLES) roles.set(role, this.#newPanel(tank.id, role, slot));
    slot.roles = roles;
    info.append(...[...roles.values()].map(({ output }) => output));
    commands.append(...[...roles.values()].map(({ host }) => host));
    diagnostics.append(range, ...previewRows.values(), ...[...roles.values()].map(({ details }) => details));
    return slot;
  }

  #render() {
    if (!this.isConnected || !this.#configured) return;
    const states = this.#hass?.states;
    this.#shell.toggleAttribute("data-dark", this.#hass?.themes?.darkMode === true);
    for (const { link } of this.#views.values()) if (link) link.hidden = this.#hass?.user?.is_admin !== true;
    const zones = states ? zonesFrom(states) : [];
    this.#notice.hidden = zones.length > 0;
    text(this.#notice, states ? "沒有可顯示的水桶。" : "正在載入總覽卡片…");
    this.#updateCards(zones, states ?? {});
  }

  #updateCards(zones, states) {
    const focused = this.shadowRoot.activeElement;
    const manual = manualFrom(states);
    const previews = previewsFrom(states);
    const displays = new Map(zones.flatMap((zone) => zone.tanks.map(({ id, entity }) => [id, displayFrom(entity, states[entity])])));
    const nextPanels = new Map();
    const nextZones = new Map();
    for (const zone of zones) {
      let current = this.#zones.get(zone.id);
      if (!current) {
        const section = element("section", "zone");
        section.dataset.zoneId = zone.id;
        const head = element("header", "zone-head flex");
        const group = element("div");
        const heading = element("h2");
        const count = element("p", "dim");
        const pill = element("span", "pill");
        const grid = element("div", "tanks");
        group.append(heading, count);
        head.append(group, pill);
        section.append(head, grid);
        current = { section, heading, count, pill, grid, tiles: new Map() };
      }
      text(current.heading, zone.name);
      text(current.count, `${zone.tanks.length} 桶`);
      const nextTiles = new Map();
      const reports = [];
      for (const tank of zone.tanks) {
        const slot = current.tiles.get(tank.id) ?? this.#newTank(tank);
        slot.entity = tank.entity;
        nextTiles.set(tank.id, slot);
        nextPanels.set(tank.id, slot.roles);
        const state = states[tank.entity];
        const display = displays.get(tank.id);
        const depth = depthFrom(state);
        const available = previews.get(previewKey(zone.id, tank.id));
        const band = states[available?.get("refill_preview")]?.state;
        const low = states[available?.get("low_level_alarm")]?.state;
        text(slot.title, tank.name);
        text(slot.value, depth?.shown ?? "—");
        slot.unit.hidden = !depth;
        slot.readout.setAttribute("aria-label", `${tank.name} · ${depth ? `${depth.shown} m` : UNAVAILABLE}`);
        slot.readout.title = "開啟水位詳細資訊";
        updateGauge(slot, depth, display.scale);
        text(slot.relation, relationText(depth, display, band, low));
        slot.host.toggleAttribute("data-low-level-active", low === "on");
        for (const [kind, control] of slot.previewRows) {
          const entity = available?.get(kind);
          const isRefill = kind === "refill_preview";
          const label = isRefill ? REFILL_LABEL[band] ?? (display.refill ? REFILL_LABEL.unavailable : "補水門檻未設定") :
            low === "on" ? "低液位告警中" : low === "off" ? "低液位告警未觸發" :
              low === "unavailable" || display.alarm ? "低液位告警預覽無法取得" : "低液位告警未設定";
          text(control, label);
          if (entity) {
            control.dataset.entity = entity;
            control.dataset.previewKind = kind;
          } else {
            delete control.dataset.entity;
            delete control.dataset.previewKind;
            if (this.shadowRoot.activeElement === control) slot.readout.focus({ preventScroll: true });
          }
          control.disabled = !entity;
        }
        const byRole = manual.get(tank.id);
        const refill = this.#updatePanel(slot.roles.get("refill"), this.#binding(slot.roles.get("refill"), tank, byRole?.get("refill")));
        const alarmBinding = this.#binding(slot.roles.get("alarm"), tank, byRole?.get("alarm"));
        const alarm = this.#updatePanel(slot.roles.get("alarm"), alarmBinding);
        reports.push(alarmBinding ? alarm?.actual ?? "unknown" : undefined);
        const refillPanel = slot.roles.get("refill");
        const commissioned = refillPanel.key !== undefined;
        const pill = refill ? refill.phase === "running" && refill.actual === "on" ? "補水中" :
          refill.phase === "idle" && refill.actual === "unknown" ? "輸出未知" :
            refill.phase === "idle" && refillPanel.startWait ? "等候回報" : PHASE_LABEL[refill.phase] :
          commissioned ? "回報未知" : "未提供";
        text(slot.pill, pill);
        slot.pill.setAttribute("aria-label", `${pill}：詳細狀態`);
        slot.pill.classList.toggle("good", pill === "補水中");
        slot.pill.classList.toggle("high", refill?.phase === "fault");
        slot.pill.classList.toggle("wait", !!(refillPanel.startWait || refill?.pending_on || refill?.pending_off));
      }
      text(current.pill, zoneReport(reports));
      current.pill.classList.toggle("high", reports.includes("on"));
      for (const [id, slot] of current.tiles) if (!nextTiles.has(id)) slot.host.remove();
      arrange(current.grid, [...nextTiles.values()].map((slot) => slot.host), this.shadowRoot.activeElement ?? focused);
      current.tiles = nextTiles;
      nextZones.set(zone.id, current);
    }
    for (const [id, zone] of this.#zones) if (!nextZones.has(id)) zone.section.remove();
    arrange(this.#layout, [...nextZones.values()].map((zone) => zone.section), this.shadowRoot.activeElement ?? focused);
    this.#zones = nextZones;
    this.#panels = nextPanels;
    for (const interlock of this.#startInterlocks.values()) {
      const { tankId, role } = interlock.flight;
      if (!nextPanels.get(tankId)?.has(role)) interlock.sawActivity = false;
    }
    const { count, summary, refill, alarm } = this.#overview;
    text(count, `${zones.length} 區 · ${displays.size} 桶 · 水位單位 m`);
    const all = [...displays.values()];
    const waterPolicy = uniformPair(all, "refill");
    const alarmPolicy = uniformPair(all, "alarm");
    text(refill, waterPolicy ? refillText(waterPolicy) : all.some((d) => d.refill) ? "各桶補水門檻不同" : "補水門檻未設定");
    text(alarm, alarmPolicy ? alarmText(alarmPolicy) : all.some((d) => d.alarm) ? "各桶低液位告警設定不同" : "低液位告警未設定");
    summary.hidden = !zones.length;
    const pending = this.#confirmation?.panel;
    if (pending && nextPanels.get(pending.tankId)?.get(pending.role) !== pending) this.#closeConfirmation();
    if (focusWasLost(this.shadowRoot, focused)) this.#views.get(this.#view).tab.focus({ preventScroll: true });
    if (this.#view !== "overview") this.#updateSettings(zones, states, displays, manual);
  }

  #updateSettings(zones, states, displays, manual) {
    const view = this.#views.get(this.#view);
    const next = new Map();
    for (const zone of zones) {
      let group = view.rows.get(zone.id);
      if (!group) {
        const section = element("section", "editor-zone");
        const title = element("h3");
        section.append(title);
        group = { section, title, rows: new Map() };
      }
      text(group.title, `${zone.name} · ${zone.tanks.length} 桶`);
      const rows = new Map();
      for (const tank of zone.tanks) {
        let row = group.rows.get(tank.id);
        if (!row) {
          const wrapper = element("div", "settings-row");
          const name = element("strong");
          const notes = ROLES.concat("level").map(() => element("div", "dim"));
          wrapper.append(name, ...notes);
          row = { wrapper, name, notes };
        }
        text(row.name, tank.name);
        if (this.#view === "parameters") {
          const d = displays.get(tank.id);
          text(row.notes[0], d.refill ? refillText(d.refill) : "補水門檻未設定");
          text(row.notes[1], d.alarm ? alarmText(d.alarm) : "低液位告警未設定");
          text(row.notes[2], d.scale === undefined ? NO_SCALE : scaleText(d.scale));
        } else {
          const roles = manual.get(tank.id);
          text(row.notes[0], `水位感測：${depthFrom(states[tank.entity]) ? "有讀值" : "無可用讀值"}`);
          ROLES.forEach((role, index) => {
            text(row.notes[index + 1], `${OUTPUT[role]}：${roles?.get(role)?.get("status") ? "有受控按鈕狀態" : "未提供受控按鈕"}`);
          });
        }
        rows.set(tank.id, row);
      }
      for (const [id, { wrapper }] of group.rows) if (!rows.has(id)) wrapper.remove();
      arrange(group.section, [group.title, ...[...rows.values()].map(({ wrapper }) => wrapper)]);
      group.rows = rows;
      next.set(zone.id, group);
    }
    for (const [id, { section }] of view.rows) if (!next.has(id)) section.remove();
    arrange(view.inventory, [...next.values()].map(({ section }) => section));
    view.rows = next;
  }

}

if (!customElements.get(TAG)) customElements.define(TAG, UninusWaterDashboard);
const picker = window.customCards ??= [];
if (!picker.some((card) => card.type === TAG)) {
  picker.push({
    type: TAG,
    name: "補水控制",
    description: "按區顯示水位、門檻預覽與已啟用的手動控制。",
    preview: true,
  });
}
