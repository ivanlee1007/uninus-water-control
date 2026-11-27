const TAG = "uninus-water-dashboard";
const ATTRIBUTE = "uninus_water_dashboard";
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
  [hidden] { display: none; }
`;

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
    this.#hass = value;
    this.#render();
  }

  connectedCallback() {
    this.#render();
  }

  disconnectedCallback() {
    this.#helperFlight = undefined;
    this.#helperFailed = false;
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
    const language = this.#hass?.locale?.language ?? this.#hass?.language ?? "";
    const traditional = /^zh-(?:hant|tw|hk|mo)(?:-|$)/i.test(language);
    const message = NOTICES[traditional ? "zhHant" : "en"][state];
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
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, UninusWaterDashboard);
const picker = window.customCards ??= [];
if (!picker.some((card) => card.type === TAG)) {
  picker.push({
    type: TAG,
    name: "UNiNUS Water Dashboard",
    description: "Read-only overview of water levels by zone.",
    preview: true,
  });
}
