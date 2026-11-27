const TAG = "uninus-water-dashboard";
const ATTRIBUTE = "uninus_water_dashboard";
const MANUAL = "uninus_water_manual";
const ROLES = ["refill", "alarm"];
const ACTIONS = ["start", "stop", "reset"];
const PHASES = ["idle", "starting", "running", "stopping", "fault", "unknown"];
const FEEDBACK = ["on", "off", "unknown"];
const MANUAL_TEXT = {
  en: {
    refill: "Manual refill", alarm: "Manual alarm", start: "Start", stop: "Stop", reset: "Clear fault",
    cancel: "Cancel", confirm: "Confirm start", limit: "HA limit", seconds: "s",
    idle: "Idle", starting: "Starting", running: "Running", stopping: "Stopping", fault: "Fault",
    unknown: "Unknown", on: "On", off: "Off", requested: "Requested", actual: "HA feedback",
    pending: "Pending", none: "None", unavailable: "Status unavailable",
    sending: "Sending button request…", waiting: "Waiting for HA status…", rejected: "HA did not confirm the button request.",
    warning: "Start only when it is safe. HA feedback does not verify physical operation.",
  },
  zhHant: {
    refill: "手動補水", alarm: "手動警報", start: "開始", stop: "停止", reset: "清除故障",
    cancel: "取消", confirm: "確認開始", limit: "HA 運轉上限", seconds: "秒",
    idle: "待機", starting: "啟動中", running: "運轉中", stopping: "停止中", fault: "故障",
    unknown: "未知", on: "開啟", off: "關閉", requested: "要求", actual: "HA 回報",
    pending: "待處理", none: "無", unavailable: "無法取得狀態",
    sending: "正在送出按鈕請求…", waiting: "等待 HA 狀態更新…", rejected: "HA 未確認按鈕請求。",
    warning: "確認安全後才開始。HA 回報不代表實體裝置已動作。",
  },
};
const FAULT_TEXT = {
  en: {
    restart_required: "Restart Home Assistant to recover control",
    output_binding_changed: "Output binding changed", source_invalid: "Invalid level source",
    output_unknown: "Output feedback unknown", unexpected_off: "Unexpected OFF", unexpected_on: "Unexpected ON",
    on_unconfirmed: "ON not confirmed", off_unconfirmed: "OFF not confirmed", runtime_limit: "Runtime limit reached",
    command_failed: "Output request failed", command_interrupted: "Output request interrupted", start_guard_changed: "Start conditions changed",
  },
  zhHant: {
    restart_required: "請重新啟動 Home Assistant 以恢復控制",
    output_binding_changed: "輸出綁定已變更", source_invalid: "水位來源無效",
    output_unknown: "輸出回報未知", unexpected_off: "非預期關閉", unexpected_on: "非預期開啟",
    on_unconfirmed: "未確認開啟", off_unconfirmed: "未確認關閉", runtime_limit: "已達運轉上限",
    command_failed: "輸出請求失敗", command_interrupted: "輸出請求中斷", start_guard_changed: "啟動條件已變更",
  },
};
const NOTICES = {
  en: {
    loading: "Loading dashboard cards…",
    empty: "No tanks to display.",
    unavailable: "Dashboard cards could not be loaded.",
  },
  zhHant: {
    loading: "正在載入總覽卡片…",
    empty: "沒有可顯示的水桶。",
    unavailable: "無法載入總覽卡片。",
  },
};
const STYLE = `
  :host { display: block; min-width: 0; }
  .zones, .tiles { display: grid; gap: var(--grid-card-gap, 8px); }
  .zones {
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 348px), 1fr));
    align-items: start;
  }
  .tiles { grid-template-columns: repeat(auto-fit, minmax(min(100%, 164px), 1fr)); }
  .zone, [data-tank-id], .zone > *, [data-tank-id] > * { min-width: 0; }
  .zone { display: grid; gap: var(--grid-card-gap, 8px); }
  [data-tank-id] > *, .zone > :first-child { display: block; }
  .notice { padding: 16px; color: var(--primary-text-color); }
  .manual {
    margin-block-start: 4px; padding: 6px 8px;
    border: 1px solid var(--divider-color, #888); border-radius: var(--ha-card-border-radius, 12px);
    background: var(--ha-card-background, var(--card-background-color));
    color: var(--primary-text-color); font-size: 12px; line-height: 1.4;
    overflow-wrap: anywhere;
  }
  .manual-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; }
  .manual-actions > :last-child { grid-column: 1 / -1; justify-self: start; }
  .manual-actions ha-button { min-width: 0; }
  .manual-title { font-weight: 600; }
  .manual-message:empty { display: none; }
  .manual-dialog {
    box-sizing: border-box; position: static; width: 100%; max-width: none; margin: 4px 0 0;
    border: 1px solid var(--divider-color, #888); border-radius: 8px; padding: 8px;
    background: var(--card-background-color, white); color: var(--primary-text-color, #222);
    overflow-wrap: anywhere;
  }
  .manual-dialog p { margin: 4px 0; }
  .manual-dialog-actions { display: flex; flex-wrap: wrap; justify-content: end; gap: 4px; }
  .manual-dialog-actions button {
    background: transparent; color: inherit; border: 1px solid var(--divider-color, #888);
    border-radius: 6px; font: inherit; padding: 4px 8px; min-height: 32px; cursor: pointer;
  }
  [hidden] { display: none; }
`;

function languageOf(hass) {
  const language = hass?.locale?.language ?? hass?.language ?? "";
  return /^zh-(?:hant|tw|hk|mo)(?:-|$)/i.test(language) ? "zhHant" : "en";
}

function text(node, value) {
  if (node.textContent !== value) node.textContent = value;
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

const byIndex = (a, b) => a.index - b.index;

function validSlot(slot) {
  return typeof slot?.id === "string" && /^[0-9a-f]{32}$/.test(slot.id) &&
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

function tileConfig(tank) {
  return {
    type: "tile", entity: tank.entity, name: tank.name,
    tap_action: { action: "more-info" }, icon_tap_action: { action: "more-info" },
  };
}

class UninusWaterDashboard extends HTMLElement {
  #hass;
  #configured = false;
  #layout;
  #notice;
  #helpers;
  #helperFlight;
  #helperFailed = false;
  #zones = new Map();
  #panels = new Map();
  #flights = new Map();
  #authority;
  #confirmation;
  #dialog;

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    this.#layout = document.createElement("div");
    this.#layout.className = "zones";
    this.#notice = document.createElement("div");
    this.#notice.className = "notice";
    this.#notice.setAttribute("role", "status");
    this.#notice.hidden = true;
    root.append(style, this.#notice, this.#layout);
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
    const authority = [value?.user?.id, value?.connection, value?.connected, value?.callService];
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
    this.#helperFlight = undefined;
    this.#helperFailed = false;
    this.#closeConfirmation();
    this.#clearPanelFeedback();
  }

  #clearPanelFeedback() {
    for (const roles of this.#panels.values()) {
      for (const panel of roles.values()) {
        panel.feedback = undefined;
        panel.rejected = false;
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
      const focus = !preferred.disabled ? preferred : !fallback.disabled ? fallback : null;
      focus?.focus();
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
    panel.host.append(this.#dialog.node);
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
    const label = MANUAL_TEXT[languageOf(this.#hass)];
    const { title, summary, warning, cancel, confirm } = this.#dialog;
    text(title, `${label.start} · ${label[pending.panel.role]}`);
    text(summary, `${binding.tank.name} · ${label.limit}: ${binding.status.max_runtime_seconds} ${label.seconds}`);
    text(warning, label.warning);
    text(cancel, label.cancel);
    text(confirm, label.confirm);
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
    return { buttons, status, statusKey, tank, key };
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
      !this.#busy(panel, "reset", buttons.reset) && !panel.startWait;
  }

  async #press(panel, action, confirmKey) {
    const binding = this.#binding(panel);
    if (!binding || binding.key !== panel.key || (action === "start" && binding.key !== confirmKey) ||
        !this.#allowed(panel, binding, action)) return;
    if (action === "stop") this.#closeConfirmation();
    const target = binding.buttons[action];
    const flight = { tankId: panel.tankId, role: panel.role, action };
    this.#flights.set(target, flight);
    panel.feedback = flight;
    panel.rejected = false;
    if (action === "start") panel.startWait = { status: binding.statusKey, flight };
    try {
      const call = this.#hass.callService("button", "press", {}, { entity_id: target });
      this.#render();
      await call;
    } catch {
      if (panel.startWait?.flight === flight) panel.startWait = undefined;
      if (panel.feedback === flight) panel.rejected = true;
    } finally {
      this.#flights.delete(target);
      if (panel.feedback === flight) panel.feedback = undefined;
      this.#render();
    }
  }

  #newPanel(tankId, role) {
    const host = document.createElement("section");
    host.className = "manual";
    host.dataset.manualRole = role;
    const title = document.createElement("strong");
    title.className = "manual-title";
    const actions = document.createElement("div");
    actions.className = "manual-actions";
    const status = document.createElement("div");
    status.setAttribute("aria-live", "polite");
    const phase = document.createElement("div");
    const observed = document.createElement("div");
    const requested = document.createElement("div");
    const pending = document.createElement("div");
    const fault = document.createElement("div");
    const message = document.createElement("div");
    message.className = "manual-message";
    status.append(phase, observed, requested, pending, fault, message);
    const panel = { tankId, role, host, title, phase, observed, requested, pending, fault, message };
    panel.buttons = Object.fromEntries(ACTIONS.map((action) => {
      const button = document.createElement("ha-button");
      button.setAttribute("size", "small");
      button.setAttribute("appearance", action === "stop" ? "accent" : "plain");
      button.addEventListener("click", () => {
        if (action === "start") this.#openConfirmation(panel);
        else void this.#press(panel, action);
      });
      actions.append(button);
      return [action, button];
    }));
    host.append(title, actions, status);
    return panel;
  }

  #updatePanel(panel, binding) {
    const { status } = binding;
    if (panel.key !== binding.key) {
      panel.key = binding.key;
      panel.feedback = undefined;
      panel.rejected = false;
    }
    if (panel.startWait && status && panel.startWait.status !== binding.statusKey) panel.startWait = undefined;
    const language = languageOf(this.#hass);
    const label = MANUAL_TEXT[language];
    const line = (key, value) => `${label[key]}${language === "zhHant" ? "：" : ": "}${value}`;
    text(panel.title, label[panel.role]);
    if (panel.host.getAttribute("aria-label") !== label[panel.role]) panel.host.setAttribute("aria-label", label[panel.role]);
    text(panel.phase, status ? label[status.phase] : label.unavailable);
    text(panel.observed, line("actual", label[status?.actual ?? "unknown"]));
    text(panel.requested, line("requested", label[status?.requested ?? "unknown"]));
    const pending = [status?.pending_on && label.on, status?.pending_off && label.off].filter(Boolean);
    text(panel.pending, line("pending", pending.join(" / ") || (status ? label.none : label.unknown)));
    const fault = FAULT_TEXT[language];
    text(panel.fault, status?.fault ? line("fault", Object.hasOwn(fault, status.fault) ? fault[status.fault] : label.unknown) : "");
    text(panel.message, panel.feedback ? label.sending : panel.rejected ? label.rejected : panel.startWait ? label.waiting : "");
    for (const action of ACTIONS) {
      const button = panel.buttons[action];
      const disabled = !this.#allowed(panel, binding, action);
      if (button.disabled !== disabled) button.disabled = disabled;
      text(button, label[action]);
    }
    if (this.#confirmation?.panel === panel) this.#refreshConfirmation(binding);
  }

  async #loadHelpers() {
    const flight = {};
    this.#helperFlight = flight;
    try {
      const helpers = await window.loadCardHelpers();
      if (this.#helperFlight !== flight) return;
      if (typeof helpers?.createCardElement !== "function") throw new Error("Invalid card helpers");
      this.#helpers = helpers;
    } catch {
      if (this.#helperFlight !== flight) return;
      this.#helperFailed = true;
    }
    this.#helperFlight = undefined;
    this.#render();
  }

  #showNotice(state) {
    if (this.#layout.hasChildNodes()) {
      this.#layout.replaceChildren();
      this.#zones.clear();
    }
    this.#panels.clear();
    this.#closeConfirmation();
    const message = NOTICES[languageOf(this.#hass)][state];
    if (this.#notice.textContent !== message) this.#notice.textContent = message;
    this.#notice.hidden = false;
  }

  #render() {
    if (!this.isConnected || !this.#configured) return;
    if (!this.#hass?.states) return this.#showNotice("loading");
    const zones = zonesFrom(this.#hass.states);
    if (!zones.length) return this.#showNotice("empty");
    if (this.#helperFailed) return this.#showNotice("unavailable");
    if (!this.#helpers) {
      this.#showNotice("loading");
      if (!this.#helperFlight) void this.#loadHelpers();
      return;
    }
    try {
      this.#updateCards(zones, this.#helpers);
      this.#notice.hidden = true;
    } catch {
      this.#helpers = undefined;
      this.#helperFailed = true;
      this.#showNotice("unavailable");
    }
  }

  #updateCards(zones, helpers) {
    const focused = this.shadowRoot.activeElement;
    const manual = manualFrom(this.#hass.states);
    const nextPanels = new Map();
    const nextZones = new Map();
    for (const zone of zones) {
      let current = this.#zones.get(zone.id);
      if (!current) {
        const section = document.createElement("section");
        section.className = "zone";
        section.dataset.zoneId = zone.id;
        const heading = helpers.createCardElement({ type: "heading", heading: zone.name });
        const grid = document.createElement("div");
        grid.className = "tiles";
        section.append(heading, grid);
        current = { section, heading, grid, name: zone.name, tiles: new Map() };
      } else if (current.name !== zone.name) {
        current.heading.setConfig({ type: "heading", heading: zone.name });
        current.name = zone.name;
      }
      current.heading.hass = this.#hass;
      const nextTiles = new Map();
      for (const tank of zone.tanks) {
        let slot = current.tiles.get(tank.id);
        if (!slot) {
          const host = document.createElement("div");
          host.dataset.tankId = tank.id;
          const tile = helpers.createCardElement(tileConfig(tank));
          host.append(tile);
          slot = { host, tile, name: tank.name, entity: tank.entity };
        } else if (slot.name !== tank.name || slot.entity !== tank.entity) {
          slot.tile.setConfig(tileConfig(tank));
          slot.name = tank.name;
          slot.entity = tank.entity;
        }
        slot.tile.hass = this.#hass;
        const oldRoles = this.#panels.get(tank.id);
        const newRoles = new Map();
        for (const role of ROLES) {
          const actions = manual.get(tank.id)?.get(role);
          if (!actions?.get("status")) continue;
          const panel = oldRoles?.get(role) ?? this.#newPanel(tank.id, role);
          const binding = this.#binding(panel, tank, actions);
          this.#updatePanel(panel, binding);
          newRoles.set(role, panel);
        }
        if (newRoles.size) nextPanels.set(tank.id, newRoles);
        for (const [role, panel] of oldRoles ?? []) {
          if (!newRoles.has(role)) panel.host.remove();
        }
        arrange(slot.host, [slot.tile, ...[...newRoles.values()].map(({ host }) => host)], focused);
        nextTiles.set(tank.id, slot);
      }
      for (const [id, slot] of current.tiles) {
        if (!nextTiles.has(id)) slot.host.remove();
      }
      arrange(current.grid, [...nextTiles.values()].map((slot) => slot.host), focused);
      current.tiles = nextTiles;
      nextZones.set(zone.id, current);
    }
    for (const [id, zone] of this.#zones) {
      if (!nextZones.has(id)) zone.section.remove();
    }
    arrange(this.#layout, [...nextZones.values()].map((zone) => zone.section), focused);
    this.#zones = nextZones;
    this.#panels = nextPanels;
    const pending = this.#confirmation?.panel;
    if (pending && nextPanels.get(pending.tankId)?.get(pending.role) !== pending) this.#closeConfirmation();
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, UninusWaterDashboard);
const picker = window.customCards ??= [];
if (!picker.some((card) => card.type === TAG)) {
  picker.push({
    type: TAG,
    name: "UNiNUS Water Dashboard",
    description: "Water levels by zone, with native manual controls for commissioned tanks.",
    preview: true,
  });
}
