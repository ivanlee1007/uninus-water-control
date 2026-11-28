const TAG = "uninus-water-dashboard";
const DOMAIN = "uninus_water_control";
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
  .alarm-history { margin-top:12px; }
  .alarm-history-controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .alarm-history select { max-width:100%; color:var(--primary-text-color); background:var(--card-background-color); }
  .alarm-history ol { padding-inline-start:1.5em; }
  .alarm-history li { margin-block:4px; overflow-wrap:anywhere; }
  .zones { grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr)); }
  .zone-head { margin-bottom:8px; }
  .tanks { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,138px),1fr)); gap:7px; }
  .tank { background:var(--tint); border:1px solid var(--line); border-radius:10px; padding:8px; min-width:0; }
  .tank .flex { gap:4px; }
  h2,h3,.settings-row,.diagnostics,.manual-dialog { overflow-wrap:anywhere; }
  .readout { justify-content:flex-start; flex-wrap:nowrap; align-items:center; gap:9px; margin:9px 0; }
  .level {
    flex:1; display:block; min-width:0; text-align:start; padding:0;
    min-height:44px; border:0; border-radius:5px; background:transparent;
  }
  .level:hover .value { text-decoration:underline; }
  .vessel { position:relative; display:grid; align-items:end; overflow:hidden; width:40px; height:82px; flex:none;
    border:1px solid #6191ac; border-radius:7px; background:var(--accent-bg); }
  .vessel::after { content:''; position:absolute; inset:0;
    background:repeating-linear-gradient(0deg, transparent 0 12px, rgba(255,255,255,.4) 12px 13px);
    mix-blend-mode:overlay; pointer-events:none; }
  .water { width:100%; background:linear-gradient(180deg,#3ab5f7,#0484c4); transition:height .6s cubic-bezier(.2,.7,.3,1); }
  .reading { min-width:0; font-size:23px; font-weight:850; letter-spacing:-.045em; line-height:1.1; }
  .value { overflow-wrap:anywhere; }
  .reading small { font-size:12px; letter-spacing:0; font-weight:700; color:var(--muted); margin-left:2px; }
  .relation { display:block; font-weight:600; letter-spacing:0; margin-top:3px; overflow-wrap:anywhere; }
  .relation[data-tone="alarm"] { color:var(--high); font-weight:800; }
  .relation[data-tone="limit"] { color:var(--low); }
  .relation[data-tone="unknown"] { color:var(--wait); }
  .tank[data-low-level-active] { border-color:color-mix(in srgb, var(--high) 55%, transparent);
    background:color-mix(in srgb, var(--high-bg) 55%, var(--tint)); }
  .dropicon { width:30px; height:30px; border-radius:50%; display:grid; place-items:center; flex:none;
    background:var(--accent-bg); color:var(--accent); }
  .dropicon svg { width:16px; height:16px; }
  .tank .flex { align-items:center; }
  .thresholds { display:grid; gap:3px; padding:7px 9px; border-radius:8px; margin-bottom:6px;
    background:var(--bg); border:1px solid var(--line); font-size:11px; line-height:1.35; color:var(--muted); }
  .thresholds .line { display:flex; justify-content:space-between; gap:8px; }
  .thresholds .key { letter-spacing:.04em; text-transform:uppercase; font-size:.62rem; color:var(--muted); flex:none; }
  .thresholds .value { font-variant-numeric:tabular-nums; color:var(--ink); font-weight:600; overflow-wrap:anywhere; }
  .thresholds .line[hidden] { display:none; }
  .global-pill { font-size:.78rem; font-weight:750; padding:5px 12px; border-radius:999px;
    background:var(--good-bg); color:var(--good); margin-left:auto; }
  .global-pill[data-state="alarm"] { background:var(--high-bg); color:var(--high); }
  .global-pill[data-state="running"] { background:var(--accent-bg); color:var(--accent); }
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
  .quick-dialog { width:min(680px,calc(100% - 24px)); max-height:calc(100dvh - 32px); overflow:auto; margin:auto;
    border:1px solid var(--line); border-radius:12px; padding:12px; background:var(--bg); color:var(--ink); }
  .quick-dialog::backdrop { background:#0006; }
  .manual-dialog p { margin:4px 0; }
  .manual-dialog-actions { display:flex; flex-wrap:wrap; justify-content:end; gap:4px; }
  .formbox { display:grid; gap:10px; }
  .editor-zone,.settings-row { border-top:1px solid var(--line); padding-top:8px; }
  .editor-zone { display:grid; gap:8px; }
  .options-link { display:inline-flex; align-items:center; text-decoration:none; }
  /* Dense mode: compact, high-scannability layout (card-local, never touches config). */
  .shell[data-mode="dense"] { padding:10px 10px 12px; border-radius:12px; }
  .shell[data-mode="dense"] .zones { grid-template-columns:1fr; gap:10px; margin-top:10px; }
  .shell[data-mode="dense"] .zone { padding:9px 10px 10px; border-radius:11px; }
  .shell[data-mode="dense"] .zone-head { margin-bottom:6px; }
  .shell[data-mode="dense"] .tanks { grid-template-columns:1fr; gap:6px; }
  .shell[data-mode="dense"] .tank { display:flex; flex-wrap:wrap; align-items:center; gap:4px 10px; padding:6px 8px; }
  .shell[data-mode="dense"] .tank h3 { font-size:.84rem; }
  .shell[data-mode="dense"] .tank .flex { gap:4px; justify-content:flex-start; flex:1 1 140px; min-width:100px; }
  .shell[data-mode="dense"] .readout { margin:4px 0; }
  .shell[data-mode="dense"] .level { min-height:0; }
  .shell[data-mode="dense"] .vessel { display:none; }
  .shell[data-mode="dense"] .reading { font-size:16px; letter-spacing:-.03em; }
  .shell[data-mode="dense"] .reading small { font-size:11px; }
  .shell[data-mode="dense"] .relation { font-size:.7rem; margin-top:2px; }
  .shell[data-mode="dense"] .info { display:none; }
  .shell[data-mode="dense"] .commands { display:flex; flex-wrap:wrap; gap:5px; margin-left:auto; }
  .shell[data-mode="dense"] .commands button { width:auto; min-height:32px; font-size:.72rem; padding:5px 8px; }
  .shell[data-mode="dense"] .quick-parameters { margin-left:auto; }
  .shell[data-mode="dense"] .diagnostics { flex-basis:100%; }
  .shell[data-mode="dense"] .range-warning { flex-basis:100%; }
  .shell[data-mode="dense"] .pill { font-size:.66rem; padding:2px 8px; }
  .shell[data-mode="dense"] .dropicon { display:none; }
  .shell[data-mode="dense"] .thresholds { display:none; }
  .shell[data-mode="dense"] .global-pill { font-size:.66rem; padding:2px 8px; }
  .shell[data-mode="dense"] .readout { align-items:center; }

  @container water-console (max-width:480px) {
    button { font-size:13px; }
    .vessel { width:32px; height:66px; }
    .reading { font-size:19px; }
    .thresholds { font-size:10px; padding:6px 8px; }
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


// Home Assistant DistanceConverter units, expressed as exact decimal factors.
const METRE_FACTORS = new Map([
  ["m", [1n, 0]], ["cm", [1n, -2]], ["mm", [1n, -3]], ["km", [1n, 3]],
  ["in", [254n, -4]], ["ft", [3048n, -4]], ["yd", [9144n, -4]],
  ["mi", [1609344n, -3]], ["nmi", [1852n, 0]],
]);

function metreSample(raw, [factor, shift]) {
  const [mantissa, power = "0"] = raw.toLowerCase().split("e");
  const negative = mantissa.startsWith("-");
  const unsigned = mantissa.replace(/^[+-]/, "");
  const fraction = unsigned.split(".")[1]?.length ?? 0;
  let digits = (BigInt(unsigned.replace(".", "")) * factor).toString();
  if (digits === "0") return negative ? "-0" : "0";
  let exponent = Number(power) - fraction + shift;
  const trimmed = digits.replace(/0+$/, "");
  exponent += digits.length - trimmed.length;
  digits = trimmed;
  const point = digits.length + exponent;
  // Avoid expanding extreme exponents into unbounded DOM text.
  const sample = point > 21 || point <= -6 ? `${digits[0]}${digits.length > 1 ? `.${digits.slice(1)}` : ""}e${point - 1}` :
    point <= 0 ? `0.${"0".repeat(-point)}${digits}` : point >= digits.length ? `${digits}${"0".repeat(point - digits.length)}` :
      `${digits.slice(0, point)}.${digits.slice(point)}`;
  return negative ? `-${sample}` : sample;
}

function depthFrom(state) {
  const raw = state.state;
  const unit = state.attributes?.unit_of_measurement;
  if (!METRE_FACTORS.has(unit) || typeof raw !== "string" ||
      !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(raw) || !Number.isFinite(Number(raw))) return undefined;
  const sample = unit === "m" ? raw : metreSample(raw, METRE_FACTORS.get(unit));
  const value = Number(sample);
  if (!Number.isFinite(value)) return undefined;
  // Do not round away a converted decimal sample across a policy boundary.
  const shortDepth = /^[+-]?\d+(?:\.\d{1,2})?$/.test(sample) && Math.abs(value) < 1e21;
  return { shown: shortDepth ? value.toFixed(2) : sample, value };
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

const ALARM_HISTORY_COMMAND = "uninus_water_control/alarm_history_get";
const HISTORY_STATUS = {
  no_binding: "未設定告警輸出",
  unavailable: "沒有可驗證的告警輸出歷史",
  no_events: "已驗證的涵蓋期間內沒有開關變化",
  ok: "已驗證的告警輸出變化",
};

function validHistoryDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(value)) return false;
  const ms = Date.parse(value);
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 23) === value.slice(0, 23);
}

function validHistory(result) {
  if (result?.version !== 1 || typeof result.scope !== "string" || !/^[0-9a-f]{32}$/.test(result.scope) ||
      typeof result.status !== "string" || !Object.hasOwn(HISTORY_STATUS, result.status) || typeof result.limited !== "boolean" ||
      !Array.isArray(result.events) || result.events.length > 50) return false;
  const interval = (item) => validHistoryDate(item?.start_utc) && validHistoryDate(item?.end_utc) &&
    item.start_utc <= item.end_utc;
  const { window, coverage, events, status, limited } = result;
  if (!interval(window) || Date.parse(window.end_utc) - Date.parse(window.start_utc) !== 86400000) return false;
  if (status === "no_binding" || status === "unavailable") return coverage === null && !events.length && !limited;
  if (!interval(coverage) || coverage.start_utc < window.start_utc || coverage.end_utc > window.end_utc ||
      (status === "ok") !== (events.length > 0)) return false;
  return events.every((row, index) => validHistoryDate(row?.at_utc) && ["on", "off"].includes(row.state) &&
    row.at_utc > coverage.start_utc && row.at_utc <= coverage.end_utc &&
    (!index || events[index - 1].at_utc > row.at_utc));
}

class UninusWaterDashboard extends HTMLElement {
  #hass;
  #configured = false;
  #preview;
  #editMode;
  #config;
  #quick;
  #quickPanel;

  #layout;
  #notice;
  #overview;
  #global;
  #globalAlarmCount = 0;
  #globalRunningCount = 0;
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
  #history;
  #historyContext;
  #historyEpoch = 0;
  #historyFlight;
  #historyDenied;

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
    const globalPill = element("span", "global-pill");
    globalPill.dataset.state = "normal";
    globalPill.hidden = true;
    const nav = element("nav", "tabs");
    nav.setAttribute("aria-label", "補水頁面");
    head.append(heading, globalPill, nav);
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
    this.#global = globalPill;
    for (const [key, titleText, linkText, help] of [
      ["parameters", "各桶門檻與顯示量程", null,
        "此處僅顯示已套用的參數。管理員可在正常操作總覽開啟每桶的「快捷參數」調整；共用預設、設備與安全設定請使用原生整合設定。門檻預覽不操作設備。"],
      ["devices", "區域與水桶清單", "前往整合設定：區域與水桶",
        "此處列出可讀取的水桶與手動控制。若要新增、改名或變更綁定，在整合的「設定」選單選擇對應的區域或水桶操作。"],
    ]) {
      const view = this.#views.get(key);
      const frame = element("div", "formbox");
      const header = element("header", "flex");
      const title = element("h2");
      const description = element("p", "dim");
      const inventory = element("div", "editor-split");
      text(title, titleText);
      text(description, help);
      header.append(title);
      const link = linkText ? element("a", "options-link") : null;
      if (link) {
        text(link, linkText);
        link.href = OPTIONS_PATH;
        link.hidden = true;
        header.append(link);
      }
      frame.append(header, description, inventory);
      view.section.append(frame);
      Object.assign(view, { inventory, link, rows: new Map() });
    }
    this.#mountHistory(this.#views.get("parameters").section);
    this.#showView("overview");
    root.append(style, this.#shell);
  }

  #showView(key) {
    if (this.#view !== key) this.#closeConfirmation();
    if (this.#view === "parameters" && key !== this.#view) this.#clearHistory(true);
    this.#view = key;
    for (const [id, { section, tab }] of this.#views) {
      const active = id === key;
      section.hidden = !active;
      tab.setAttribute("aria-pressed", active);
    }
  }

  static getStubConfig() {
    return { type: `custom:${TAG}`, display_mode: "thin" };
  }

  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  set preview(value) {
    if (this.#preview === value) return;
    this.#preview = value;
    this.#hostModeChanged();
  }

  get preview() { return this.#preview; }

  set editMode(value) {
    if (this.#editMode === value) return;
    this.#editMode = value;
    this.#hostModeChanged();
  }

  get editMode() { return this.#editMode; }

  #operationView() {
    // hui-card supplies both flags, after hass on initial load and in sequence
    // on updates. Unknown/partially supplied flags never authorize equipment.
    return this.#preview === false && this.#editMode === false;
  }

  #hostModeChanged() {
    if (!this.#operationView()) {
      this.#closeQuick();
      this.#closeConfirmation();
      this.#clearPanelFeedback();
    }
    this.#render();
  }

  getGridOptions() {
    return { columns: "full", rows: "auto", min_columns: 3 };
  }

  getCardSize() {
    return Math.max(1, Math.ceil(this.offsetHeight / 50));
  }

  setConfig(config) {
    const next = displayConfig(config);
    const detailsChanged = this.#config?.show_details !== next.show_details;
    this.#config = next;
    this.dataset.mode = next.display_mode;
    this.#shell.dataset.mode = next.display_mode;
    this.#configured = true;
    if (detailsChanged) for (const zone of this.#zones.values()) for (const slot of zone.tiles.values()) {
      slot.host.querySelector('.diagnostics').hidden = !next.show_details;
      slot.pill.setAttribute("aria-expanded", String(next.show_details));
    }
    this.#render();
  }

  set hass(value) {
    const authority = [value?.user?.id, value?.user?.is_admin, value?.user?.is_active,
      value?.connection, value?.connected, value?.callService];
    if (!this.#authority || authority.some((part, index) => part !== this.#authority[index])) {
      this.#authority = authority;
      this.#closeQuick();
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
    this.#closeQuick();
    this.#clearPanelFeedback();
    this.#clearHistory(true);
  }

  #mountHistory(parent) {
    const host = element("section", "formbox alarm-history");
    host.setAttribute("aria-label", "告警輸出紀錄");
    const title = element("h2");
    text(title, "告警輸出紀錄");
    const controls = element("div", "alarm-history-controls");
    const label = element("label");
    text(label, "選擇水桶 ");
    const select = element("select");
    select.setAttribute("aria-label", "選擇水桶");
    label.append(select);
    const refresh = button("settings-action");
    text(refresh, "重新整理紀錄");
    controls.append(label, refresh);
    const range = element("p", "dim");
    const summary = element("p");
    summary.setAttribute("aria-live", "polite");
    const coverage = element("p", "dim");
    const list = element("ol");
    host.append(title, controls, range, summary, coverage, list);
    host.hidden = true;
    this.#history = { parent, host, select, refresh, range, summary, coverage, list, targets: new Map() };
    select.addEventListener("change", () => {
      const pane = this.#history;
      if (pane.host.hidden || !pane.targets.has(select.value) || pane.key === select.value) return;
      this.#clearHistory();
      pane.key = select.value;
      this.#paintHistory();
    });
    refresh.addEventListener("click", () => void this.#refreshHistory());
  }

  #clearHistory(reset = false) {
    this.#historyEpoch++;
    const pane = this.#history;
    pane.result = pane.message = undefined;
    if (reset) {
      pane.host.hidden = true;
      pane.host.remove();
      pane.key = undefined;
      pane.targets.clear();
      pane.select.replaceChildren();
    }
    this.#paintHistory();
  }

  #currentHistoryContext() {
    const hass = this.#hass;
    return [hass?.user?.id, hass?.user?.is_admin, hass?.user?.is_active,
      hass?.connection, hass?.connected, hass?.callWS];
  }

  #canReadHistory() {
    const hass = this.#hass;
    return this.isConnected && this.#view === "parameters" && this.#operationView() &&
      !!hass?.user?.id && hass.user.is_admin === true && hass.user.is_active !== false &&
      !!hass.connection && hass.connected !== false && typeof hass.callWS === "function";
  }

  #updateHistory(zones, states) {
    const context = this.#currentHistoryContext();
    const pane = this.#history;
    if (!this.#historyContext || context.some((value, index) => this.#historyContext[index] !== value)) {
      this.#historyContext = context;
      this.#historyDenied = undefined;
      this.#clearHistory(true);
    }
    if (!this.#canReadHistory()) {
      if (!pane.host.hidden) this.#clearHistory(true);
      return;
    }
    const targets = new Map();
    for (const zone of zones) for (const tank of zone.tanks) {
      const entry_id = states[tank.entity]?.attributes?.uninus_water_settings_entry;
      if (typeof entry_id !== "string" || !ENTRY_ID.test(entry_id)) continue;
      const key = `${entry_id}:${zone.id}:${tank.id}:${tank.entity}`;
      targets.set(key, { label: `${zone.name} · ${tank.name}`, request: { entry_id, zone_id: zone.id, tank_id: tank.id } });
    }
    const sameOptions = targets.size === pane.targets.size && [...targets].every(([key, row], index) =>
      pane.select.options[index]?.value === key && pane.targets.get(key)?.label === row.label);
    pane.targets = targets;
    if (!targets.has(pane.key)) {
      if (pane.key !== undefined) this.#clearHistory();
      pane.key = targets.keys().next().value;
    }
    if (!sameOptions) pane.select.replaceChildren(...[...targets].map(([key, row]) => {
      const option = element("option");
      option.value = key;
      text(option, row.label);
      return option;
    }));
    if (pane.key && pane.select.value !== pane.key) pane.select.value = pane.key;
    pane.host.hidden = false;
    if (!pane.host.parentNode) pane.parent.append(pane.host);
    this.#paintHistory();
  }

  #paintHistory() {
    const pane = this.#history;
    const { result, key } = pane;
    const denied = key && this.#historyDenied === key;
    pane.select.disabled = !key;
    pane.refresh.disabled = pane.host.hidden || !key || !!this.#historyFlight || !!denied;
    const utc = (value, precise = false) => `${value.replace("T", " ").slice(0, precise ? -1 : 19)} UTC`;
    text(pane.range, result ? `查詢：${utc(result.window.start_utc)} 至 ${utc(result.window.end_utc)}。僅表示輸出變化，不代表觸發原因。` :
      "查詢最近 24 小時，最多 50 筆。僅含已確認的告警輸出變化，不代表觸發原因。");
    text(pane.summary, result ? HISTORY_STATUS[result.status] : pane.message || (denied ? "沒有權限讀取所選紀錄。" :
      key ? "選擇水桶並重新整理。" : "沒有可查詢的水桶。"));
    text(pane.coverage, result?.coverage ?
      `已驗證涵蓋：${utc(result.coverage.start_utc, true)} 至 ${utc(result.coverage.end_utc, true)}${result.limited ? " · 僅顯示有限的最新紀錄" : ""}` : "");
    if (pane.rendered === result) return;
    pane.rendered = result;
    pane.list.replaceChildren(...(result?.events ?? []).map((row) => {
      const li = element("li");
      const time = element("time");
      time.dateTime = row.at_utc;
      text(time, utc(row.at_utc, true));
      li.append(time, row.state === "on" ? " · 開啟" : " · 關閉");
      return li;
    }));
  }

  async #refreshHistory() {
    const pane = this.#history;
    const key = pane.key;
    const target = pane.targets.get(key);
    if (this.#historyFlight || !this.#canReadHistory() || !target || this.#historyDenied === key) return;
    const flight = { epoch: this.#historyEpoch, context: this.#currentHistoryContext() };
    this.#historyFlight = flight;
    pane.result = undefined;
    pane.message = "正在查詢紀錄…";
    this.#paintHistory();
    const current = () => this.#historyFlight === flight && this.#historyEpoch === flight.epoch &&
      this.#canReadHistory() && pane.key === key &&
      this.#currentHistoryContext().every((value, index) => value === flight.context[index]);
    try {
      const result = await this.#hass.callWS({ type: ALARM_HISTORY_COMMAND, version: 1, ...target.request });
      if (current()) {
        if (validHistory(result)) pane.result = result;
        else pane.message = "無法確認紀錄格式。";
      }
    } catch (err) {
      if (current()) {
        if (err?.code === "unauthorized") this.#historyDenied = key;
        pane.message = err?.code === "unauthorized" ? "沒有權限讀取所選紀錄。" : "無法查詢紀錄，請重新整理。";
      }
    } finally {
      if (this.#historyFlight === flight) {
        this.#historyFlight = undefined;
        this.#paintHistory();
      }
    }
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
    const restoreFocus = pending && node?.contains(this.shadowRoot.activeElement);
    if (node?.open) node.close();
    if (this.#dialog) text(this.#dialog.summary, "");
    node?.remove();
    if (!pending) return;
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
    if (!this.#operationView() || typeof this.#hass.callService !== "function" || this.#hass.connected === false ||
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
    if (!this.#operationView()) return;
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
    const reason = !this.#operationView() ? "編輯預覽不能操作設備" :
      !binding ? "未提供受控按鈕（尚未啟用或無法讀取）" : !status ? "無法取得控制狀態" : "目前不可操作";
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
    const dropicon = element("span", "dropicon");
    dropicon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>';
    const title = element("h3");
    const pill = button("pill diagnostics-toggle");
    header.append(dropicon, title, pill);
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
    const thresholds = element("div", "thresholds");
    const thresholdRefill = element("div", "line");
    const thresholdAlarm = element("div", "line");
    function lineOf(node, keyText) {
      const key = element("span", "key");
      text(key, keyText);
      const value = element("span", "value");
      node.append(key, value);
      thresholds.append(node);
      return value;
    }
    const thresholdsValues = {
      refill: lineOf(thresholdRefill, "補水門檻"),
      alarm: lineOf(thresholdAlarm, "低告警"),
    };
    const info = element("div", "info");
    const commands = element("div", "commands");
    const quick = button("quick-parameters");
    text(quick, "快捷參數");
    quick.hidden = true;
    quick.addEventListener("click", event => { if (event.isTrusted) this.#openQuick(tank.id); });
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
    host.append(header, measurement, rangeMessage, thresholds, info, commands, quick, diagnostics);
    diagnostics.hidden = !this.#config.show_details;
    pill.setAttribute("aria-expanded", String(!diagnostics.hidden));
    const slot = { host, title, pill, dropicon, readout, vessel, water, value, unit, relation,
      range, rangeMessage, thresholdsValues, previewRows, quick };
    readout.addEventListener("click", () => moreInfo(readout, slot.entity));
    const roles = new Map();
    for (const role of ROLES) roles.set(role, this.#newPanel(tank.id, role, slot));
    slot.roles = roles;
    info.append(...[...roles.values()].map(({ output }) => output));
    commands.append(...[...roles.values()].map(({ host }) => host));
    diagnostics.append(range, ...previewRows.values(), ...[...roles.values()].map(({ details }) => details));
    return slot;
  }

  #displayZones(states) {
    const config = this.#config;
    return zonesFrom(states).filter(zone => !config.zone_ids.length || config.zone_ids.includes(zone.id)).map(zone => ({
      ...zone, tanks: zone.tanks.filter(tank => !config.tank_ids.length || config.tank_ids.includes(tank.id)),
    })).filter(zone => zone.tanks.length);
  }

  #quickTarget(tankId) {
    const hass = this.#hass;
    if (!this.#operationView() || !authorizedEditor(editorPrincipal(hass)) || !readyTransport(editorTransport(hass))) return;
    for (const zone of this.#displayZones(hass.states ?? {})) {
      const tank = zone.tanks.find(tank => tank.id === tankId);
      if (!tank) continue;
      const entry_id = hass.states[tank.entity]?.attributes?.uninus_water_settings_entry;
      if (!matchesId(entry_id, ENTRY_ID)) return;
      return { entry_id, zone_id: zone.id, tank_id: tank.id, entity_id: tank.entity };
    }
  }

  #openQuick(tankId) {
    const target = this.#quickTarget(tankId);
    if (!target) return;
    this.#closeQuick();
    this.#closeConfirmation();
    const node = element("dialog", "quick-dialog");
    node.setAttribute("aria-label", "快捷參數");
    const cancel = button(); text(cancel, "取消");
    cancel.addEventListener("click", () => this.#closeQuick());
    node.addEventListener("cancel", event => { event.preventDefault(); this.#closeQuick(); });
    this.#quickPanel ??= element(`${TAG}-quick-parameters`);
    this.#quickPanel.setConfig({ type: `custom:${TAG}`, target });
    this.#quickPanel.hass = this.#hass;
    node.append(cancel, this.#quickPanel);
    this.#quick = { node, target, invoker: this.shadowRoot.querySelector(`[data-tank-id="${tankId}"] .quick-parameters`) };
    this.#shell.append(node);
    node.showModal(); cancel.focus();
  }

  #closeQuick() {
    const quick = this.#quick;
    if (!quick) return;
    this.#quick = undefined;
    quick.node.close(); quick.node.remove();
    if (quick.invoker?.isConnected && !quick.invoker.hidden) quick.invoker.focus({ preventScroll: true });
  }

  #render() {
    if (!this.isConnected || !this.#configured) return;
    const states = this.#hass?.states;
    this.#shell.toggleAttribute("data-dark", this.#hass?.themes?.darkMode === true);
    for (const { link } of this.#views.values()) if (link) link.hidden = this.#hass?.user?.is_admin !== true;
    const zones = this.#displayZones(states ?? {});
    this.#globalAlarmCount = 0;
    this.#globalRunningCount = 0;
    if (this.#quick) {
      const target = this.#quickTarget(this.#quick.target.tank_id);
      if (!target || JSON.stringify(target) !== JSON.stringify(this.#quick.target)) this.#closeQuick();
      else this.#quickPanel.hass = this.#hass;
    }
    this.#notice.hidden = zones.length > 0;
    text(this.#notice, states ? "沒有可顯示的水桶。" : "正在載入總覽卡片…");
    this.#updateCards(zones, states ?? {});
    this.#updateHistory(zones, states ?? {});
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
        slot.quick.hidden = !this.#quickTarget(tank.id);
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
        const relationText_ = relationText(depth, display, band, low);
        slot.relation.textContent = relationText_;
        slot.relation.dataset.tone = /告警|↓ 達|↓ 低/.test(relationText_) ? "alarm"
          : /門檻/.test(relationText_) ? "limit"
          : /不可用|無法取得|未設定/.test(relationText_) ? "unknown" : "normal";
        text(slot.thresholdsValues.refill, display.refill ? `${display.refill[0]}／${display.refill[1]} m` : "補水門檻未設定");
        text(slot.thresholdsValues.alarm, display.alarm ? `≤ ${display.alarm[0]} m · ${display.alarm[1]} 秒` : "低液位告警未設定");
        const alarmOn = low === "on";
        slot.host.toggleAttribute("data-low-level-active", alarmOn);
        if (alarmOn) this.#globalAlarmCount += 1;
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
        if (pill === "補水中") this.#globalRunningCount += 1;
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
    const global = this.#global;
    if (global) {
      const alarmCount = this.#globalAlarmCount ?? 0;
      const runningCount = this.#globalRunningCount ?? 0;
      if (alarmCount > 0) {
        text(global, `告警中 ${alarmCount} 桶`);
        global.dataset.state = "alarm";
      } else if (runningCount > 0) {
        text(global, `運轉中 ${runningCount} 桶`);
        global.dataset.state = "running";
      } else {
        text(global, "全部正常");
        global.dataset.state = "normal";
      }
      global.hidden = !zones.length;
    }
    const all = [...displays.values()];
    const waterPolicy = uniformPair(all, "refill");
    const alarmPolicy = uniformPair(all, "alarm");
    text(refill, waterPolicy ? refillText(waterPolicy) : all.some((d) => d.refill) ? "各桶補水門檻不同" : "補水門檻未設定");
    text(alarm, alarmPolicy ? alarmText(alarmPolicy) : all.some((d) => d.alarm) ? "各桶低液位告警設定不同" : "低液位告警未設定");
    summary.hidden = !zones.length;
    const pending = this.#confirmation?.panel;
    if (pending && nextPanels.get(pending.tankId)?.get(pending.role) !== pending) this.#closeConfirmation();
    if (focusWasLost(this.shadowRoot, focused)) this.#views.get(this.#view).tab.focus({ preventScroll: true });
    // Hidden tabs retain DOM too: reconcile every inventory against the current
    // readable snapshot, not only the selected tab.
    for (const key of ["parameters", "devices"]) {
      if (key === this.#view || this.#views.get(key).rows.size) this.#updateSettings(key, zones, states, displays, manual);
    }
  }

  #updateSettings(key, zones, states, displays, manual) {
    const view = this.#views.get(key);
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
        if (key === "parameters") {
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

const EDITOR_TAG = `${TAG}-editor`;
const ENTRY_ID = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const SETTINGS_ID = /^[0-9a-f]{32}$/;
const REVISION = /^[0-9a-f]{64}$/;
const POLICY = {
  refill: { label: "補水門檻", fields: ["下限（m）", "上限（m）"], describe: refillText },
  alarm: { label: "低液位告警", fields: ["低位門檻（m）", "延遲（秒）"], describe: alarmText },
};
const policyKinds = Object.keys(POLICY);
const samePair = (a, b) => a === b || (a !== null && b !== null && a[0] === b[0] && a[1] === b[1]);
const matchesId = (value, pattern) => typeof value === "string" && pattern.test(value) && value.trim() === value;
const validScale = (value) => value === null || (Number.isFinite(value) && value > 0);
const settingsPairText = (kind, pair) => !pair ? "未設定" : kind === "refill" ?
  `${pair[0]}／${pair[1]} m` : `≤ ${pair[0]} m · ${pair[1]} 秒`;
const APPLIED = "已儲存並重新讀回確認。";

const MAX_ALARM_DELAY = 2147483647;

function validPair(kind, value, allowLegacy = false) {
  return value === null || (Array.isArray(value) && value.length === 2 && value.every(Number.isFinite) &&
    (kind === "refill" ? value[0] < value[1] : Number.isInteger(value[1]) && value[1] >= 1 &&
      (allowLegacy || value[1] <= MAX_ALARM_DELAY)));
}

// Core and the browser may round an unsafe integer before they can validate it.
const writableNumber = (value) => Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER;
const writablePair = (kind, value) => validPair(kind, value) && (value === null || value.every(writableNumber));

function settingsError(code) { return Object.assign(new Error(code), { code }); }

function readSettingsDto(value, entry) {
  const bad = () => { throw settingsError("invalid_readback"); };
  if (value?.version !== 1 || value.entry_id !== entry || !matchesId(value.revision, REVISION) ||
      !matchesId(value.source_snapshot, REVISION) || !Array.isArray(value.zones)) bad();
  const common = {};
  for (const kind of policyKinds) {
    const pair = value.common?.[kind];
    if (!validPair(kind, pair)) bad();
    common[kind] = pair;
  }
  const zonesSeen = new Set();
  const tanksSeen = new Set();
  const label = (name) => typeof name === "string" && name.trim();
  const zones = value.zones.map((zone) => {
    if (!matchesId(zone?.id, SETTINGS_ID) || zonesSeen.has(zone.id) || !label(zone.name) || !Array.isArray(zone.tanks)) bad();
    zonesSeen.add(zone.id);
    const tanks = zone.tanks.map((tank) => {
      if (!matchesId(tank?.id, SETTINGS_ID) || tanksSeen.has(tank.id) || !label(tank.name) || !validScale(tank.scale_max_m)) bad();
      tanksSeen.add(tank.id);
      const result = { id: tank.id, name: tank.name, scale_max_m: tank.scale_max_m };
      for (const kind of policyKinds) {
        const policy = tank[kind];
        if (typeof policy?.use_common !== "boolean" || !validPair(kind, policy.local, true) ||
            !validPair(kind, policy.effective, !policy.use_common) ||
            (policy.use_common && common[kind] === null) ||
            !samePair(policy.effective, policy.use_common ? common[kind] : policy.local)) bad();
        result[kind] = { use_common: policy.use_common, local: policy.local, effective: policy.effective };
      }
      return result;
    });
    return { id: zone.id, name: zone.name, tanks };
  });
  return { version: 1, entry_id: entry, revision: value.revision, source_snapshot: value.source_snapshot, common, zones };
}

const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function settingsConflict(before, current) {
  if (before.revision !== current.revision) return "stale_revision";
  if (before.source_snapshot !== current.source_snapshot) return "stale_source";
}

const EDITOR_CSS = `
  :host { display:block; min-width:0; width:100%; }
  .settings { container:water-settings / inline-size; color:var(--primary-text-color,#143047); font-size:14px; line-height:1.45; }
  *, *::after, *::before { box-sizing:border-box; }
  h2,h3,h4,p { margin:0; overflow-wrap:anywhere; }
  h2 { font-size:1.15rem; }
  h3 { font-size:1rem; }
  h4 { font-size:.95rem; }
  .section,.tank-settings,.shared,.apply-strip { min-width:0; border-radius:12px; padding:12px; display:grid; gap:10px; }
  .section { background:var(--secondary-background-color,#eef4f9); }
  .mode-row { display:grid; gap:4px; background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#cddce7); border-radius:12px; padding:10px; }
  .shared,.tank-settings,.apply-strip { background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#cddce7); }
  .apply-strip { position:sticky; top:0; z-index:1; }
  /* Stacked mobile actions must not cover fields reached by backward Tab. */
  @media (max-width:600px) { .apply-strip { position:static; } }
  .affected { margin:0; padding-inline-start:1.25em; max-height:6em; overflow:auto; overflow-wrap:anywhere; }
  .editor-content,.zone-settings { display:grid; gap:10px; min-width:0; }
  ha-entity-picker { display:block; min-width:0; width:100%; }
  .shared-grid,.policy-grid,.pair { display:grid; gap:8px; min-width:0; grid-template-columns:minmax(0,1fr); }
  @container water-settings (min-width:420px) {
    .shared-grid,.policy-grid,.pair { grid-template-columns:repeat(2,minmax(0,1fr)); }
  }
  fieldset { min-inline-size:0; margin:0; padding:8px; border:1px solid var(--divider-color,#cddce7); border-radius:8px; }
  fieldset > :not(legend) + * { margin-top:7px; }
  legend { max-inline-size:100%; overflow-wrap:anywhere; padding-inline:4px; font-weight:600; }
  label { display:grid; gap:4px; min-width:0; }
  input,select,button { font:inherit; color:inherit; }
  input,select { width:100%; min-width:0; border:1px solid var(--divider-color,#aabcc9); border-radius:6px;
    background:var(--primary-background-color,#fff); padding:8px; }
  .dim { color:var(--secondary-text-color,#4f6477); }
  .actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
  button { cursor:pointer; padding:8px 12px; color:var(--primary-text-color,#143047);
    background:var(--secondary-background-color,#eef4f9); border:1px solid var(--divider-color,#aabcc9); border-radius:8px; }
  .apply { background:var(--primary-color,#075f92); color:var(--text-primary-color,#fff); border-color:transparent; font-weight:650; }
  button:disabled { cursor:not-allowed; background:var(--secondary-background-color,#eef4f9); color:var(--disabled-text-color,#4f6477); }
  :focus-visible { outline:2px solid var(--primary-color,#075f92); outline-offset:2px; }
  a { color:var(--link-text-color,#075f92); overflow-wrap:anywhere; }
  [hidden] { display:none !important; }
`;

function labeledControl(labelText, node) {
  const label = element("label");
  const caption = element("span");
  text(caption, labelText);
  label.append(caption, node);
  return label;
}

function settingsNumber(value, options = {}) {
  const input = element("input");
  input.type = "number";
  input.step = options.integer ? "1" : "any";
  if (options.integer) {
    input.min = "1";
    input.max = String(MAX_ALARM_DELAY);
  }
  input.value = value ?? "";
  return input;
}

function readNumber(input) {
  return input.validity.badInput ? NaN : input.value === "" ? null : input.valueAsNumber;
}

function pairFields(kind, baseline, mode) {
  const { label, fields, describe } = POLICY[kind];
  const frame = element("fieldset");
  const legend = element("legend");
  text(legend, mode ? label : `共用${label}`);
  frame.append(legend);
  const saved = element("p", "dim");
  text(saved, `目前套用：${baseline ? describe(baseline) : "未設定"}`);
  frame.append(saved);
  let select;
  if (mode) {
    select = element("select");
    for (const [value, caption] of [["local", "個別設定"], ["common", "使用共用設定"]]) {
      const option = element("option");
      option.value = value;
      text(option, caption);
      select.append(option);
    }
    select.value = mode.use_common ? "common" : "local";
    frame.append(labeledControl(`${label}模式`, select));
  }
  const values = mode ? mode.local : baseline;
  const pair = element("div", "pair");
  const inputs = fields.map((name, index) => {
    const input = settingsNumber(values?.[index], { integer: kind === "alarm" && index === 1 });
    pair.append(labeledControl(name, input));
    return input;
  });
  frame.append(pair);
  const draft = element("p", "dim");
  frame.append(draft);
  const legacyText = !writablePair(kind, values) ? inputs.map((input) => input.value) : null;
  return { frame, select, pair, inputs, draft, legacyText };
}

function draftPair(kind, { inputs, legacyText }, baseline) {
  // An unchanged binary64 value from an existing source is not a new write.
  if (legacyText?.every((value, index) => value === inputs[index].value && !inputs[index].validity.badInput)) {
    return { pair: baseline, valid: true };
  }
  const values = inputs.map(readNumber);
  const pair = values.every((value) => value === null) ? null : values;
  return { pair, valid: writablePair(kind, pair) };
}

const SETTINGS_MESSAGES = {
  ambiguous_entry: "找到多筆補水整合，無法安全選定設定範圍。",
  entry_not_loaded: "找不到唯一且已載入的補水整合。",
  scope_changed: "補水整合範圍已變更，請重新讀取設定。",
  unauthorized: "只有整合管理員可以讀取及套用設定。",
  stale_revision: "設定已被變更；請重新讀取並核對後再修改。本次草稿未套用。",
  stale_source: "水桶來源已變更；請重新讀取並核對後再修改。本次草稿未套用。",
  readback_required: "無法確認是否已套用；請重新讀取並核對，不要重送草稿。",
  invalid_readback: "無法驗證整合回傳的設定；請重新讀取並核對。",
  settings_unavailable: "無法安全讀取整合設定；請由管理員檢查整合或備份。",
  topology_not_enrolled: "此整合尚未移轉區桶設定。請先完成原生整合設定的安全移轉。",
  topology_restart_required: "區桶移轉尚未完成。請依原生整合設定的指示保留獨立切斷並重新啟動。",
  topology_recovery_required: "區桶設定需要復原。輸出身分與實際狀態未確認；請依整合復原指引處理。",
  topology_picker_unavailable: "此 Home Assistant 編輯環境無法載入原生實體選擇器。",
  binding_conflict: "選取的感測器或開關與其他綁定衝突。本次草稿未套用。",
  protected_topology: "不能換綁或移除已啟用控制的水桶。請前往原生整合安全設定。",
  removal_confirmation_required: "移除範圍未獲確認。本次草稿未套用。",
};

function editorPrincipal(hass) {
  return [hass?.user?.id, hass?.user?.is_admin, hass?.user?.is_active];
}

function editorTransport(hass) {
  return [hass?.connection, hass?.connection?.connected, hass?.connected, hass?.callWS];
}

const sameContext = (a, b) => a.every((part, index) => part === b[index]);

function authorizedEditor([id, admin, active]) {
  // Native current_user need not expose is_active; Core rechecks the principal.
  return typeof id === "string" && !!id && admin === true && active !== false;
}

function readyTransport([connection, socketReady, connected, callWS]) {
  return !!connection && socketReady !== false && connected === true && typeof callWS === "function";
}

class UninusWaterQuickParameters extends HTMLElement {
  #hass;
  #target;
  #principal = [];
  #transport = [];
  #configured = false;
  #epoch = 0;
  #flight;
  #needsRead = true;
  #problem;
  #snapshot;
  #fields;
  #content;
  #status;
  #impact;
  #impactList;
  #reload;
  #review;
  #conflict;
  #currentValues;
  #apply;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const css = element("style");
    css.textContent = EDITOR_CSS;
    const shell = element("div", "settings editor-content");
    shell.lang = "zh-Hant";
    this.#content = element("div", "editor-content");
    const onDraft = () => {
      if (this.#status.textContent === APPLIED) text(this.#status, "");
      this.#updateDraft();
    };
    this.#content.addEventListener("input", onDraft);
    this.#content.addEventListener("change", onDraft);
    const footer = element("div", "apply-strip");
    const info = element("p");
    text(info, "儲存會影響所有顯示此桶的卡片，不會操作設備或啟用自動補水。取消只丟棄未儲存草稿；已送出的請求不能撤銷。");
    this.#impact = element("p", "dim");
    this.#impactList = element("ul", "affected dim");
    this.#impactList.setAttribute("aria-label", "受影響水桶與設定差異");
    this.#impactList.hidden = true;
    this.#status = element("p");
    this.#status.setAttribute("role", "status");
    const actions = element("div", "actions");
    this.#apply = button("apply");
    text(this.#apply, "儲存此桶參數");
    this.#apply.disabled = true;
    this.#apply.addEventListener("click", (event) => { if (event.isTrusted) void this.#save(); });
    this.#reload = button();
    text(this.#reload, "重新讀取（捨棄草稿）");
    this.#reload.hidden = true;
    this.#reload.addEventListener("click", (event) => { if (event.isTrusted) void this.#load(true); });
    this.#review = button();
    text(this.#review, "讀取目前設定（保留草稿）");
    this.#review.hidden = true;
    this.#review.addEventListener("click", (event) => { if (event.isTrusted) void this.#load(false, true); });
    this.#currentValues = element("section", "section");
    this.#currentValues.setAttribute("aria-label", "整合目前設定（唯讀）");
    this.#currentValues.hidden = true;
    actions.append(this.#apply, this.#review, this.#reload);
    footer.append(info, this.#impact, this.#impactList, this.#status, actions);
    shell.append(footer, this.#currentValues, this.#content);
    root.append(css, shell);
  }

  setConfig(config) {
    if (config?.type !== `custom:${TAG}`) throw new Error(`Expected custom:${TAG}`);
    const target = config.target;
    if (!target || !matchesId(target.entry_id, ENTRY_ID) || !validInventoryId(target.zone_id) ||
        !validInventoryId(target.tank_id) || !/^sensor\.[a-z0-9_]+$/.test(target.entity_id)) throw new Error("Invalid quick target");
    if (this.#configured && sameJson(this.#target, target)) return;
    this.#target = { ...target };
    ++this.#epoch;
    this.#needsRead = true;
    this.#purge();
    this.#configured = true;
    this.#start();
  }

  set hass(value) {
    this.#hass = value;
    const principal = editorPrincipal(value);
    const transport = editorTransport(value);
    const changedPrincipal = !sameContext(principal, this.#principal);
    if (!changedPrincipal && sameContext(transport, this.#transport)) return;
    this.#principal = principal;
    this.#transport = transport;
    ++this.#epoch;
    this.#needsRead = true;
    this.#clearCurrentValues();
    if (changedPrincipal) this.#purge();
    else if (this.#flight?.sent && this.#snapshot === this.#flight.baseline) {
      this.#problem = SETTINGS_MESSAGES.readback_required;
    }
    this.#updateDraft();
    this.#start();
  }

  connectedCallback() { this.#start(); }

  disconnectedCallback() {
    ++this.#epoch;
    this.#needsRead = true;
    this.#hass = undefined;
    this.#principal = [];
    this.#transport = [];
    this.#purge();
    text(this.#status, "");
  }

  #start() {
    if (!this.isConnected || !this.#configured) return;
    const authorized = authorizedEditor(this.#principal);
    this.#reload.hidden = !authorized || !readyTransport(this.#transport);
    if (this.#reload.hidden) {
      text(this.#status, !this.#hass?.user ? "正在等待 Home Assistant 連線…" : !authorized ? SETTINGS_MESSAGES.unauthorized :
        "連線中斷，無法讀取設定。");
      return;
    }
    if (this.#needsRead && !this.#flight) void this.#load();
  }

  #purge() {
    this.#problem = undefined;
    this.#conflict = undefined;
    this.#snapshot = undefined;
    this.#fields = undefined;
    this.#clearCurrentValues();
    this.#content.replaceChildren();
    this.#updateDraft();
  }

  #current(epoch) {
    return epoch === this.#epoch && this.isConnected && this.#configured &&
      authorizedEditor(this.#principal) && readyTransport(this.#transport) &&
      sameContext(editorPrincipal(this.#hass), this.#principal) && sameContext(editorTransport(this.#hass), this.#transport);
  }

  async #discover(callWS, epoch, requireLoaded = true) {
    const rows = await callWS({ type: "config_entries/get", domain: DOMAIN });
    if (!this.#current(epoch)) throw settingsError("scope_changed");
    if (!Array.isArray(rows) || rows.some((row) => row?.domain !== DOMAIN || !matchesId(row.entry_id, ENTRY_ID))) {
      throw settingsError("entry_not_loaded");
    }
    if (rows.length > 1) throw settingsError("ambiguous_entry");
    if (!rows.length || (requireLoaded && rows[0].state !== "loaded")) throw settingsError("entry_not_loaded");
    if (rows[0].entry_id !== this.#target.entry_id) throw settingsError("scope_changed");
    return rows[0].entry_id;
  }

  async #read(callWS, epoch, entry) {
    if (!this.#current(epoch)) throw settingsError("scope_changed");
    const value = await callWS({ type: `${DOMAIN}/config_get`, version: 1, entry_id: entry });
    if (!this.#current(epoch) || await this.#discover(callWS, epoch) !== entry) throw settingsError("scope_changed");
    const settings = readSettingsDto(value, entry);
    if (!settings.zones.some(zone => zone.id === this.#target.zone_id && zone.tanks.some(tank => tank.id === this.#target.tank_id))) {
      throw settingsError("scope_changed");
    }
    return settings;
  }

  #issue(error, epoch, write) {
    if (!this.#current(epoch)) return;
    const code = error?.code;
    if (["unauthorized", "entry_not_loaded", "ambiguous_entry", "scope_changed"].includes(code)) this.#purge();
    if (code === "unauthorized") this.#reload.hidden = true;
    const definitive = ["stale_revision", "stale_source", "unauthorized", "entry_not_loaded", "settings_unavailable",
      "protected_topology", "binding_conflict", "removal_confirmation_required", "topology_not_enrolled",
      "topology_restart_required", "topology_recovery_required"].includes(code);
    const message = write?.sent && !definitive ? SETTINGS_MESSAGES.readback_required :
      Object.hasOwn(SETTINGS_MESSAGES, code) ? SETTINGS_MESSAGES[code] : "無法讀取或套用整合設定，請重新讀取後核對。";
    const stale = code === "stale_revision" || code === "stale_source";
    if ((write || stale) && this.#snapshot) this.#problem = message;
    if (stale && this.#snapshot) this.#conflict = {};
    text(this.#status, message);
  }

  #finish(flight, epoch) {
    if (this.#flight !== flight) return;
    this.#flight = undefined;
    this.#updateDraft();
    if (epoch !== this.#epoch) this.#start();
  }

  async #load(discard = false, review = false) {
    const epoch = this.#epoch;
    if (!this.#current(epoch) || this.#flight || this.#reload.hidden || review && !this.#conflict) return;
    const flight = this.#flight = {};
    const callWS = this.#transport[3].bind(this.#hass);
    if (!review) this.#needsRead = true;
    const draft = this.#draft();
    if (discard || (!this.#problem && draft?.valid && !draft.dirty)) this.#purge();
    this.#clearCurrentValues();
    this.#updateDraft();
    text(this.#status, "正在讀取整合設定…");
    try {
      const entry = await this.#discover(callWS, epoch);
      if (!this.#current(epoch)) return;
      if (this.#snapshot && this.#snapshot.entry_id !== entry) {
        if (review) throw settingsError("scope_changed");
        this.#purge();
      }
      const snapshot = await this.#read(callWS, epoch, entry);
      if (!this.#current(epoch)) return;
      const baseline = this.#snapshot;
      this.#needsRead = false;
      if (!baseline) {
        this.#mountSettings(snapshot);
      }
      // A same-owner read may unlock the draft; it cannot replace either CAS token.
      else {
        const code = settingsConflict(baseline, snapshot);
        if (!this.#problem && code) {
          this.#conflict = {};
          this.#problem = SETTINGS_MESSAGES[code];
        }
        if (this.#conflict) this.#showCurrentValues(snapshot);
      }
      text(this.#status, this.#problem ?? "");
    } catch (error) {
      this.#issue(error, epoch);
      if (this.#current(epoch) && this.#conflict && this.#status.textContent !== this.#problem) {
        text(this.#status, `${this.#problem} ${this.#status.textContent}`);
      }
    } finally {
      this.#finish(flight, epoch);
    }
  }

  #clearCurrentValues() {
    this.#currentValues.hidden = true;
    this.#currentValues.replaceChildren();
  }

  #showCurrentValues(settings) {
    const line = (copy, tag = "p") => {
      const node = element(tag);
      text(node, copy);
      return node;
    };
    const current = this.#currentValues;
    current.replaceChildren(line("整合目前設定（唯讀）", "h2"),
      line("原草稿保留在下方，不能直接重送。核對後請捨棄草稿，再重新輸入此桶參數。"));
    const original = this.#snapshot;
    if (original.revision !== settings.revision && original.source_snapshot !== settings.source_snapshot) {
      current.append(line("設定版本與水桶來源都已變更。"));
    }
    for (const kind of policyKinds) current.append(line(`共用${POLICY[kind].label}（唯讀）：${settingsPairText(kind, settings.common[kind])}`));
    for (const [zi, zone] of settings.zones.entries()) {
      if (zone.id !== this.#target.zone_id) continue;
      current.append(line(`第 ${zi + 1} 區 · ${zone.name}`, "h3"));
      for (const [ti, tank] of zone.tanks.entries()) {
        if (tank.id !== this.#target.tank_id) continue;
        const summary = element("div", "tank-settings");
        summary.append(line(`第 ${ti + 1} 桶 · ${tank.name}`, "h4"),
          line(`水位顯示量程：${tank.scale_max_m === null ? "未設定" : `${tank.scale_max_m} m`}`));
        for (const kind of policyKinds) {
          const { use_common, effective, local } = tank[kind];
          summary.append(line(`${POLICY[kind].label}（${use_common ? "共用" : "個別"}）：${settingsPairText(kind, effective)}${
            use_common ? `；保留的個別值：${settingsPairText(kind, local)}` : ""}`));
        }
        current.append(summary);
      }
    }
    current.hidden = false;
  }

  #mountSettings(snapshot) {
    const section = element("section", "section");
    const title = element("h2");
    text(title, "補水參數");
    const inventory = element("section", "section");
    const heading = element("h2");
    text(heading, "區域與水桶");
    const help = element("p", "dim");
    const link = element("a");
    text(help, "只編輯此桶參數。區桶名稱、來源綁定與安全啟用請使用原生整合設定。");
    text(link, "前往原生整合安全設定");
    link.href = OPTIONS_PATH;
    inventory.append(heading, help, link);
    const tanks = new Map();
    for (const zone of snapshot.zones) {
      if (zone.id !== this.#target.zone_id) continue;
      const group = element("section", "zone-settings");
      const name = element("h3");
      text(name, `${zone.name} · ${zone.tanks.length} 桶`);
      group.append(name);
      for (const [index, tank] of zone.tanks.entries()) {
        if (tank.id !== this.#target.tank_id) continue;
        const row = element("section", "tank-settings");
        row.dataset.tankId = tank.id;
        const tankName = element("h4");
        const label = zone.tanks.filter(({ name }) => name === tank.name).length > 1 ? `${tank.name}（第 ${index + 1} 桶）` : tank.name;
        text(tankName, label);
        tankName.id = `tank-${tank.id}`;
        row.setAttribute("aria-labelledby", tankName.id);
        const grid = element("div", "policy-grid");
        const inputs = {};
        for (const kind of policyKinds) {
          inputs[kind] = pairFields(kind, tank[kind].effective, tank[kind]);
          grid.append(inputs[kind].frame);
        }
        const scale = settingsNumber(tank.scale_max_m);
        const savedScale = element("p", "dim");
        text(savedScale, tank.scale_max_m === null ? `目前套用：${NO_SCALE}` : `目前套用：${scaleText(tank.scale_max_m)}`);
        const scaleNote = element("p", "dim");
        text(scaleNote, "留空則不顯示填水比例；量程是水位高度，不是容量，也不改變門檻。");
        row.append(tankName, grid, labeledControl("水位顯示量程上限（m）", scale), savedScale, scaleNote);
        group.append(row);
        tanks.set(tank.id, { row, inputs, scale, savedScaleText: scale.value, target: `${zone.name} · ${label}` });
      }
      inventory.append(group);
    }
    section.append(title, inventory);
    this.#snapshot = snapshot;
    this.#fields = { tanks };
    this.#content.replaceChildren(section);
    this.#updateDraft();
  }

  #draft() {
    const snapshot = this.#snapshot;
    const fields = this.#fields;
    if (!snapshot || !fields) return;
    const expected = structuredClone(snapshot);
    let valid = true;
    const uses = { refill: 0, alarm: 0 };
    const affected = [];
    const rows = [];
    snapshot.zones.forEach((zone, z) => zone.tanks.forEach((tank, t) => {
      const field = fields.tanks.get(tank.id);
      if (!field) return;
      const { inputs, scale, savedScaleText, target } = field;
      const next = expected.zones[z].tanks[t];
      const row = { id: tank.id };
      const differences = [];
      for (const kind of policyKinds) {
        const input = inputs[kind];
        const policy = tank[kind];
        const use_common = input.select.value === "common";
        const { pair: local, valid: pairValid } = draftPair(kind, input, policy.local);
        valid &&= pairValid;
        input.pair.hidden = use_common;
        if (use_common) {
          ++uses[kind];
          valid &&= expected.common[kind] !== null;
        }
        const effective = use_common ? expected.common[kind] : local;
        const hint = !pairValid ? (use_common ? "個別草稿不完整；切回個別設定修正。" : "請輸入完整且有效的門檻。") :
          effective ? POLICY[kind].describe(effective) : use_common ? "共用設定未設定" : "未設定";
        text(input.draft, `草稿預覽：${hint}`);
        if (use_common !== policy.use_common) (row[kind] ??= {}).use_common = use_common;
        if (pairValid && !samePair(local, policy.local)) (row[kind] ??= {}).local = local;
        if (pairValid && !samePair(effective, policy.effective)) {
          differences.push(`${POLICY[kind].label}：${settingsPairText(kind, policy.effective)} → ${settingsPairText(kind, effective)}`);
        } else if (row[kind]) {
          differences.push(`${POLICY[kind].label}：${use_common !== policy.use_common ?
            `改用${use_common ? "共用" : "個別"}設定` : "個別值已調整"}（生效值不變）`);
        }
        next[kind] = { use_common, local, effective };
      }
      const unchangedScale = scale.value === savedScaleText && !scale.validity.badInput;
      const max = unchangedScale ? tank.scale_max_m : readNumber(scale);
      const scaleValid = unchangedScale || (validScale(max) && (max === null || writableNumber(max)));
      valid &&= scaleValid;
      if (scaleValid && max !== tank.scale_max_m) {
        row.scale_max_m = max;
        const depth = (value) => value === null ? "未設定" : `${value} m`;
        differences.push(`量程：${depth(tank.scale_max_m)} → ${depth(max)}`);
      }
      next.scale_max_m = max;
      if (Object.keys(row).length > 1) rows.push(row);
      if (differences.length) affected.push(`${target}：${differences.join("；")}`);
    }));
    const changes = {};
    if (rows.length) changes.tanks = rows;
    return { valid, changes, expected, uses, affected, dirty: Object.keys(changes).length !== 0 };
  }

  #updateDraft() {
    const draft = this.#draft();
    const waiting = !!this.#flight || this.#needsRead;
    this.#content.hidden = this.#needsRead;
    this.#apply.disabled = waiting || !!this.#problem || !draft?.valid || !draft.dirty;
    this.#reload.disabled = !!this.#flight;
    this.#review.hidden = !this.#conflict || this.#reload.hidden || !this.#current(this.#epoch);
    this.#review.disabled = !!this.#flight;
    for (const control of this.#content.querySelectorAll("input,select")) {
      const disabled = waiting || !!this.#conflict;
      if (control.disabled !== disabled) control.disabled = disabled;
    }
    text(this.#impact, draft && !this.#needsRead ? `參數影響 ${draft.affected.length} 桶 · 共用補水 ${draft.uses.refill} 桶 · 共用低液位告警 ${draft.uses.alarm} 桶${draft.valid ? "" : " · 請填寫有效欄位"}` : "");
    const affected = this.#needsRead ? [] : draft?.affected ?? [];
    this.#impactList.hidden = !affected.length;
    const items = [...this.#impactList.children];
    if (items.length !== affected.length || items.some((item, index) => item.textContent !== affected[index])) {
      this.#impactList.replaceChildren(...affected.map((description) => {
        const item = element("li");
        text(item, description);
        return item;
      }));
    }
  }

  async #save() {
    const epoch = this.#epoch;
    if (!this.#current(epoch) || this.#flight || this.#needsRead || this.#problem) return;
    const baseline = this.#snapshot;
    const draft = this.#draft();
    if (!draft?.valid || !draft.dirty) return;
    const flight = this.#flight = { baseline, sent: false };
    const callWS = this.#transport[3].bind(this.#hass);
    this.#updateDraft();
    text(this.#status, "正在套用並讀回整合設定…");
    try {
      const entry = await this.#discover(callWS, epoch);
      if (!this.#current(epoch)) return;
      if (entry !== baseline.entry_id) throw settingsError("scope_changed");
      flight.sent = true;
      const result = await callWS({ type: `${DOMAIN}/config_save`, version: 1, entry_id: entry,
        if_revision: baseline.revision, if_source_snapshot: baseline.source_snapshot, changes: draft.changes });
      if (!this.#current(epoch)) return;
      const saved = readSettingsDto(result, entry);
      const readback = await this.#read(callWS, epoch, entry);
      if (!this.#current(epoch)) return;
      draft.expected.revision = saved.revision;
      if (saved.revision === baseline.revision || !sameJson(saved, draft.expected) || !sameJson(readback, saved)) {
        throw settingsError("readback_required");
      }
      this.#mountSettings(readback);
      text(this.#status, APPLIED);
    } catch (error) {
      this.#issue(error, epoch, flight);
    } finally {
      this.#finish(flight, epoch);
    }
  }
}

function displayConfig(config) {
  if (config?.type !== `custom:${TAG}`) throw new Error(`Expected custom:${TAG}`);
  for (const key of ["zone_ids", "tank_ids"]) {
    if (config[key] !== undefined && (!Array.isArray(config[key]) || !config[key].every(validInventoryId))) {
      throw new Error(`Invalid ${key}`);
    }
  }
  if (config.show_details !== undefined && typeof config.show_details !== "boolean") throw new Error("Invalid show_details");
  return { ...config, zone_ids: [...new Set(config.zone_ids ?? [])], tank_ids: [...new Set(config.tank_ids ?? [])],
    show_details: config.show_details ?? false,
    display_mode: config.display_mode === "dense" ? "dense" : "thin" };
}

class UninusWaterDashboardEditor extends HTMLElement {
  #hass;
  #config;
  #content;
  #signature;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const css = element("style"); css.textContent = EDITOR_CSS;
    this.#content = element("section", "settings section");
    root.append(css, this.#content);
  }

  setConfig(config) { this.#config = displayConfig(config); this.#signature = undefined; this.#render(); }
  set hass(value) { this.#hass = value; this.#render(); }
  connectedCallback() { this.#render(); }

  #change(key, value) {
    this.#config = { ...this.#config, [key]: value };
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: structuredClone(this.#config) }, bubbles: true, composed: true }));
  }

  #render() {
    if (!this.#config) return;
    const zones = zonesFrom(this.#hass?.states ?? {});
    const signature = JSON.stringify([zones, this.#config]);
    if (signature === this.#signature) return;
    this.#signature = signature;
    const heading = element("h2"); text(heading, "本卡顯示設定");
    const help = element("p", "dim"); text(help, "未選區域或水桶代表顯示全部可讀項目。只修改本卡顯示；參數請在日常卡片開啟快捷參數，設備與安全設定請使用整合設定。");
    const checkbox = (label, checked, change) => {
      const input = element("input"); input.type = "checkbox"; input.checked = checked;
      input.addEventListener("change", () => change(input.checked));
      return labeledControl(label, input);
    };
    const details = checkbox("顯示詳細狀態", this.#config.show_details, value => this.#change("show_details", value));
    // Card-local display mode: thin (default, full card) or dense (compact table).
    // Pure card setting: it never writes integration settings or physical devices.
    const modeSelect = element("select");
    modeSelect.setAttribute("aria-label", "卡片顯示模式");
    modeSelect.dataset.modeField = "display_mode";
    for (const [value, caption, helpText] of [
      ["thin", "細型卡片（B 模式）", "水位表與詳細狀態，適合獨立檢視"],
      ["dense", "濃縮運行面板（C 模式）", "緊密多欄、高掃描率，適合密集監看"],
    ]) {
      const option = element("option");
      option.value = value;
      text(option, caption);
      option.title = helpText;
      modeSelect.append(option);
    }
    modeSelect.value = this.#config.display_mode;
    modeSelect.addEventListener("change", () => {
      const mode = modeSelect.value === "dense" ? "dense" : "thin";
      if (mode === this.#config.display_mode) return;
      this.#change("display_mode", mode);
    });
    const modeNote = element("p", "dim");
    text(modeNote, "只影響本卡片的呈現，不改整合設定或任何控制。");
    const modeControl = element("div", "mode-row");
    modeControl.append(labeledControl("顯示模式", modeSelect), modeNote);
    const fields = [heading, help, details, modeControl];
    for (const [key, title, items] of [
      ["zone_ids", "顯示區域", zones],
      ["tank_ids", "顯示水桶", zones.flatMap(zone => zone.tanks.map(tank => ({ ...tank, name: `${zone.name} · ${tank.name}` })))],
    ]) {
      const group = element("fieldset"); const legend = element("legend"); text(legend, title); group.append(legend);
      for (const item of items) group.append(checkbox(item.name, this.#config[key].includes(item.id), checked => {
        const ids = this.#config[key].filter(id => id !== item.id);
        if (checked) ids.push(item.id);
        this.#change(key, ids);
      }));
      // Retain offline selections without silently deleting saved card config.
      for (const id of this.#config[key].filter(id => !items.some(item => item.id === id))) {
        group.append(checkbox(`目前無法讀取 · ${id}`, true, checked => { if (!checked) this.#change(key, this.#config[key].filter(value => value !== id)); }));
      }
      fields.push(group);
    }
    this.#content.replaceChildren(...fields);
  }
}

if (!customElements.get(EDITOR_TAG)) customElements.define(EDITOR_TAG, UninusWaterDashboardEditor);
if (!customElements.get(`${TAG}-quick-parameters`)) customElements.define(`${TAG}-quick-parameters`, UninusWaterQuickParameters);
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
