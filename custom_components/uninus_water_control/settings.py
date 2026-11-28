"""Entry-owned durable settings, independent of the manual-control options."""

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import logging
from math import isfinite
from pathlib import Path
import re
from uuid import uuid4

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .inventory import DISPLAY_SCALES, DOMAIN, inventory
from .topology_lifecycle import (
    MARK, enrollment_fence, fence_enrollment, fenced, legacy_topology, mark_owned, validate_topology, valid_witness, witness,
)

KEY = f"{DOMAIN}.settings"
# Native Options stores provenance only; all numbers remain in this Store.
LEGACY_PROVENANCE = "parameter_provenance"
PAIRS = {"refill": ("refill_lower_m", "refill_upper_m"), "alarm": ("alarm_lower_m", "alarm_delay_seconds")}
BINDINGS = ("level_sensor", "refill_switch", "alarm_switch")
ENTRY = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
IDENT = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[0-9a-f]{64}")
MAX_SAFE_NUMBER = 2**53 - 1
_LOGGER = logging.getLogger(__name__)


class SettingsError(Exception):
    """A sanitized wire error."""


def matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _invalid():
    raise SettingsError("invalid_format")


def _fields(value, allowed, required=()):
    if type(value) is not dict or value.keys() - set(allowed) or set(required) - value.keys():
        _invalid()
    return value


def _number(value, *, stored=False):
    # New numbers must survive Core's JSON decoder and the browser's Number,
    # including integer tokens Core may already have rounded to a float.
    if type(value) not in (float, int):
        return False
    if not stored:
        return -MAX_SAFE_NUMBER <= value <= MAX_SAFE_NUMBER
    try:
        # v0.2.0 wrote binary64 floats. An existing integer outside the safe
        # range is usable only when it denotes exactly one binary64 value.
        return isfinite(value) and float(value) == value
    except OverflowError:
        return False


def _wire_number(value):
    # Older exact large integers use a binary64 JSON token on the wire and
    # when carried forward; HA's serializer has a bounded integer range.
    return float(value) if type(value) is int and abs(value) > MAX_SAFE_NUMBER else value


def _pair(kind, value, *, stored=False, local=False):
    if value is None:
        return None
    if (type(value) is not list or len(value) != 2
            or not all(_number(number, stored=stored) for number in value)):
        _invalid()
    lower, upper = value
    if kind == "refill" and lower < upper:
        return [_wire_number(lower), _wire_number(upper)]
    if kind == "alarm" and upper >= 1 and int(upper) == upper:
        if upper <= 2_147_483_647:
            return [_wire_number(lower), upper if stored else int(upper)]
        # v0.2.0 stored NumberSelector floats. A merge may carry forward that
        # local value, but explicit changes (and all common pairs) stay capped.
        # Keep its representation rather than turning a legacy float into a huge int.
        if stored and local:
            return [_wire_number(lower), _wire_number(upper)]
    _invalid()


def _validate(edit, *, stored=False):
    fields = ("common", "tanks")
    _fields(edit, fields, fields if stored else ())
    common = {k: _pair(k, v, stored=stored) for k, v in
              _fields(edit.get("common", {}), PAIRS, PAIRS if stored else ()).items()}
    tanks = edit.get("tanks", [])
    if type(tanks) is not list:
        _invalid()
    result = {}
    fields = ("id", "scale_max_m", *PAIRS, *(("anchor",) if stored else ()))
    for row in tanks:
        _fields(row, fields, fields if stored else ("id",))
        ident = row["id"]
        if not matches(IDENT, ident) or ident in result:
            _invalid()
        target = result[ident] = dict(row)
        if stored and not matches(TOKEN, row["anchor"]):
            _invalid()
        scale = row.get("scale_max_m")
        if scale is not None and not (_number(scale, stored=stored) and scale > 0):
            _invalid()
        if "scale_max_m" in row:
            target["scale_max_m"] = _wire_number(scale)
        for kind in row.keys() & PAIRS.keys():
            keys = ("use_common", "local")
            mode = target[kind] = dict(_fields(row[kind], keys, keys if stored else ()))
            if "use_common" in mode and type(mode["use_common"]) is not bool:
                _invalid()
            if "local" in mode:
                mode["local"] = _pair(kind, mode["local"], stored=stored, local=True)
            if stored and mode["use_common"] and common[kind] is None:
                _invalid()
    return common, result


def _read_disk(path, key, entry_id):
    """Bypass HA Store's cache: its awaited write can log a failure and return."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            envelope = json.load(stream)
    except FileNotFoundError:
        return None
    if (type(envelope) is not dict or type(envelope.get("version")) is not int
            or envelope["version"] != 1 or type(envelope.get("minor_version")) is not int
            or envelope["minor_version"] != 1 or envelope.get("key") != key):
        _invalid()
    data = envelope.get("data")
    fields = ("entry_id", "generation", "common", "tanks")
    image_fields = ("topology", "enrollment", "integrity")
    _fields(data, (*fields, *image_fields), fields)
    if data["entry_id"] != entry_id or not matches(IDENT, data["generation"]):
        _invalid()
    if data.keys() & set(image_fields):
        _fields(data, (*fields, *image_fields), (*fields, *image_fields))
        if (not matches(IDENT, data["enrollment"])
                or data["integrity"] != _digest({k: v for k, v in data.items() if k != "integrity"})):
            _invalid()
        validate_topology(data["topology"], _pair, _number)
    common, tanks = _validate({"common": data["common"], "tanks": data["tanks"]}, stored=True)
    return {**data, "common": common, "tanks": list(tanks.values())}


def _legacy(scales, tank):
    saved = tank.get("thresholds", {})
    return {"scale_max_m": scales.get(tank["id"]), **{
        kind: {"use_common": False, "local": [saved[k] for k in keys] if all(k in saved for k in keys) else None}
        for kind, keys in PAIRS.items()
    }}


def _binding(tank, field):
    if row := tank.get(f"{field}_registry_id"):
        return ("row", row)
    return ("name", tank.get(field))


class Settings:
    """One owner and write gate per loaded integration entry."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.store = Store(hass, 1, f"{KEY}.{entry.entry_id}", private=True, atomic_writes=True)
        self.lock = asyncio.Lock()
        self.platforms = {}
        self.data = None
        self.enrollment = witness(entry)
        self.topology = None
        self.topology_busy = False
        self.indeterminate_topology = None
        self._pending_projection = False

    @property
    def zones(self):
        return self.topology["zones"] if self.topology is not None else inventory(self.entry)

    @property
    def manual(self):
        return self.topology["manual"] if self.topology is not None else self.entry.options.get("manual") or {}

    async def _async_read_file(self):
        try:
            return await self.hass.async_add_executor_job(_read_disk, self.store.path, self.store.key, self.entry.entry_id)
        except (SettingsError, ValueError, OSError) as err:
            raise SettingsError("settings_unavailable") from err

    async def async_read(self):
        try:
            data = await self._async_read_file()
            if MARK not in self.entry.data:
                if data and "topology" in data:
                    raise SettingsError("settings_unavailable")
            elif (not valid_witness(self.enrollment) or self.enrollment != witness(self.entry)
                  or self.enrollment["phase"] != "owned" or fenced(self.hass, self.entry)
                  or not data or data.get("enrollment") != self.enrollment["token"]
                  or self.topology is not None and data["topology"] != self.topology):
                raise SettingsError("settings_unavailable")
        except SettingsError:
            if self.topology is not None and not self.topology_busy:
                self._freeze_topology()
                _LOGGER.error("Owned water topology unavailable. Use the independent cutoff and topology recovery instructions")
            raise
        return data

    async def async_confirm_owned(self) -> bool:
        """Verify ownership on disk and withdraw Start if the live image is lost."""
        async with self.lock:
            self.require_live()
            try:
                verified = await self._async_read_file()
            except SettingsError:
                verified = None
            # A retired owner or a pending writer must not be frozen by this read.
            self.require_live()
            if self.topology is None or fenced(self.hass, self.entry):
                raise SettingsError("settings_unavailable")
            if verified is None or verified != self.data:
                self._freeze_topology()
                return False
            return True

    async def async_initialize(self):
        if MARK in self.entry.data:
            if reason := enrollment_fence(self.hass, self.entry):
                raise SettingsError(reason)
            if not valid_witness(self.enrollment):
                raise SettingsError("topology_recovery_required")
            try:
                if self.enrollment["phase"] == "requested":
                    try:
                        await self._async_prepare_topology()
                    except SettingsError as err:
                        # Even a failed preparation is a cold attempt. Preserve
                        # its verdict on reload; only another process may retry.
                        fence_enrollment(self.hass, self.entry, "topology_restart_required"
                                         if str(err) == "topology_restart_required" else "topology_recovery_required")
                        raise
                    raise SettingsError("topology_restart_required")
                self.data = await self.async_read()
                self.topology = self.data["topology"]
            except SettingsError as err:
                if str(err) == "topology_restart_required":
                    raise
                raise SettingsError("topology_recovery_required") from err
            return
        try:
            self.data = await self.async_read()
        except SettingsError:
            # Legacy projections and guarded controls still load. Unreadable data
            # remains on disk; Get and Save both report it instead of replacing it.
            _LOGGER.error("Water card settings unavailable for entry %s", self.entry.entry_id)

    async def _async_prepare_topology(self):
        entry = self.entry
        core_data, core_options = entry.data, entry.options
        topology = validate_topology(legacy_topology(entry), _pair, _number)
        data = await self._async_read_file()
        token = self.enrollment["token"]
        if data and "topology" in data:
            # A previous process may have saved the image but not the mark.
            # Never erase a different image or silently accept a different seed.
            if data["enrollment"] != token or data["topology"] != topology:
                raise SettingsError("settings_unavailable")
        else:
            previous = data
            policies = {row["id"]: row for row in (data or {}).get("tanks", [])}
            rows = []
            for zone in topology["zones"]:
                for tank in zone["tanks"]:
                    anchor = self._anchor(tank)
                    row = policies.get(tank["id"])
                    rows.append({**row, "anchor": anchor} if row and self._row_matches(row, tank, data) else {
                        "id": tank["id"], "anchor": anchor, **self._legacy_policy(tank),
                    })
            common, tanks = _validate({"common": (data or {}).get("common", dict.fromkeys(PAIRS)),
                                       "tanks": rows}, stored=True)
            data = {"entry_id": entry.entry_id, "generation": uuid4().hex,
                    "common": common, "tanks": list(tanks.values()), "topology": topology, "enrollment": token}
            data["integrity"] = _digest(data)
            await self.store.async_save(data)
            verified = await self._async_read_file()
            if verified != data:
                # Core's Store may swallow a write failure. If independent
                # readback proves the old starting point is unchanged, a cold
                # retry is safe without writing the Core ownership witness.
                if verified == previous:
                    raise SettingsError("topology_restart_required")
                raise SettingsError("settings_unavailable")
        if (self.hass.config_entries.async_get_entry(entry.entry_id) is not entry
                or entry.data is not core_data or entry.options is not core_options):
            raise SettingsError("settings_unavailable")
        mark_owned(self.hass, entry, self.enrollment)
        if LEGACY_PROVENANCE in entry.options:
            # The verified owned image has canonical rows and no dependency on
            # this bridge. Retiring metadata shares Core's witness save; it is
            # not a second numeric write or an enrollment durability claim.
            self.hass.config_entries.async_update_entry(entry, options={
                k: v for k, v in entry.options.items() if k != LEGACY_PROVENANCE})

    def _legacy_policy(self, tank):
        owner = self.topology if self.topology is not None else self.entry.data
        return _legacy(owner.get(DISPLAY_SCALES, {}), tank)

    def _anchor(self, tank):
        return _digest([[_binding(tank, field) for field in BINDINGS], self._legacy_policy(tank)])

    @property
    def generation(self):
        return self.data["generation"] if self.data else None

    @property
    def common(self):
        return self.data["common"] if self.data else dict.fromkeys(PAIRS)

    def _tank_source(self, tank):
        # A tank-local scope avoids invalidating a bridge on unrelated zone edits.
        return self.source_snapshot([{"id": tank["id"], "tanks": [tank]}])

    def _row_matches(self, row, tank, data):
        if row["id"] != tank["id"]:
            return False
        anchor = self._anchor(tank)
        if row["anchor"] == anchor:
            return True
        if self.topology is not None:
            return False
        bridges = self.entry.options.get(LEGACY_PROVENANCE)
        bridge = bridges.get(tank["id"]) if type(bridges) is dict else None
        return (type(bridge) is dict and bridge.keys() == {"generation", "anchor", "target", "source"}
                and bridge["generation"] == data["generation"] and bridge["anchor"] == row["anchor"]
                and bridge["target"] == anchor and bridge["source"] == self._tank_source(tank))

    def legacy_provenance(self, zones, data):
        """Bridge only verified active rows, in the same Options image as bindings.

        No numeric Store write is needed: either old Options/old anchor or new
        Options/exact bridge resolves the same row, including after a crash.
        A later parameter Save consumes the bridge by writing a canonical anchor;
        the next native edit prunes its now-obsolete generation-bound metadata.
        """
        previous = {t["id"]: t for z in self.zones for t in z["tanks"]}
        rows = {r["id"]: r for r in (data or {}).get("tanks", [])}
        bridges = {}
        for zone in zones:
            for tank in zone["tanks"]:
                old, row = previous.get(tank["id"]), rows.get(tank["id"])
                if (old is not None and row is not None and self._row_matches(row, old, data)
                        and row["anchor"] != self._anchor(tank)):
                    bridges[tank["id"]] = {"generation": data["generation"], "anchor": row["anchor"],
                                          "target": self._anchor(tank), "source": self._tank_source(tank)}
        return bridges

    def _policy(self, tank):
        if self.data:
            for row in self.data["tanks"]:
                if self._row_matches(row, tank, self.data):
                    return row
        return self._legacy_policy(tank)

    def tank_view(self, tank):
        policy = self._policy(tank)
        return {"id": tank["id"], "name": tank["name"], "scale_max_m": policy["scale_max_m"], **{
            k: {**policy[k], "effective": self.common[k] if policy[k]["use_common"] else policy[k]["local"]}
            for k in PAIRS
        }}

    def with_thresholds(self, tank):
        policy = self.tank_view(tank)
        return dict(tank, thresholds={k: v for kind, keys in PAIRS.items() if policy[kind]["effective"] is not None
                                     for k, v in zip(keys, policy[kind]["effective"], strict=True)})

    def tokens(self):
        entry = self.entry
        revision = _digest([entry.entry_id, self.enrollment, self.generation] if self.topology is not None
                           else [entry.entry_id, dict(entry.data), dict(entry.options), self.generation])
        return revision, self.source_snapshot(self.zones)

    def source_snapshot(self, zones):
        entry = self.entry
        registry = er.async_get(self.hass)
        sources = []
        for zone in zones:
            for tank in zone["tanks"]:
                bindings = []
                for field in BINDINGS:
                    binding = _binding(tank, field)
                    row = registry.async_get(binding[1]) if binding[1] else None
                    bindings.append([binding, row.id if row else None, row.domain if row else None,
                                     row.platform if row else None, row.disabled_by if row else None,
                                     row.device_id if row else None])
                sources.append([zone["id"], tank["id"], bindings])
        return _digest([entry.entry_id, sources])

    def view(self):
        revision, source = self.tokens()
        return {"version": 1, "entry_id": self.entry.entry_id, "revision": revision, "source_snapshot": source,
                "common": self.common, "zones": [{"id": z["id"], "name": z["name"],
                                                 "tanks": [self.tank_view(t) for t in z["tanks"]]}
                                                for z in self.zones]}

    def candidate(self, changes, topology=None):
        common_edits, edits = _validate(changes)
        common = self.common | common_edits
        live = {t["id"]: t for z in (topology["zones"] if topology is not None else self.zones) for t in z["tanks"]}
        if edits.keys() - live.keys():
            _invalid()
        before = {t["id"]: t for z in self.zones for t in z["tanks"]} if topology is not None else live
        active = {row["id"]: {**row, "anchor": self._anchor(live[row["id"]])}
                  for row in (self.data or {}).get("tanks", []) if row["id"] in live and row["id"] in before
                  and self._row_matches(row, before[row["id"]], self.data)}
        for ident, edit in edits.items():
            row = active.get(ident) or {"id": ident, "anchor": self._anchor(live[ident]),
                                        **self._legacy_policy(live[ident])}
            active[ident] = {**row, **{k: row[k] | edit.get(k, {}) for k in PAIRS}}
            if "scale_max_m" in edit:
                active[ident]["scale_max_m"] = edit["scale_max_m"]
        common, tanks = _validate({"common": common, "tanks": list(active.values())}, stored=True)
        result = {"entry_id": self.entry.entry_id, "generation": uuid4().hex,
                  "common": common, "tanks": list(tanks.values())}
        if self.topology is not None:
            result.update(topology=self.topology if topology is None else topology, enrollment=self.enrollment["token"])
            result["integrity"] = _digest(result)
        return result

    def require_live(self, *, reconcile=False):
        owner = getattr(self.entry, "runtime_data", None)
        if ((self.topology_busy and not (reconcile and self.indeterminate_topology is not None))
                or witness(self.entry) != self.enrollment or (MARK in self.entry.data and self.topology is None)):
            raise SettingsError("settings_unavailable")
        if (self.hass.state is not CoreState.running or self.entry.state is not ConfigEntryState.LOADED
                or self.hass.config_entries.async_get_entry(self.entry.entry_id) is not self.entry
                or owner is None or owner.retiring or owner.settings is not self or owner.options is not self.entry.options):
            raise SettingsError("entry_not_loaded")

    def _freeze_topology(self):
        self.topology_busy = True
        self._publish_roles()

    def _publish_roles(self):
        owner = getattr(self.entry, "runtime_data", None)
        if owner is not None and owner.settings is self:
            for role in owner.roles.values():
                role._publish()

    async def async_start_manual(self, role, context, authorize):
        """Require the owned image at the actual Start admission boundary."""
        async with self.lock:
            self.require_live()
            await self.async_read()
            await authorize(context)
            self.require_live()
            role.start(context)

    async def async_edit_topology(self, zones, manual, generation, authorize, admitted):
        """Serialize a protected native edit with Card writes, then load the verified image."""
        if self.topology is None:
            raise SettingsError("settings_unavailable")
        async with self.lock:
            self.require_live()
            data = await self.async_read()
            await authorize()
            self.require_live()
            if self.generation != generation or data != self.data:
                raise SettingsError("configuration_changed")
            admitted()
            from .binding_validation import binding_errors

            if binding_errors(er.async_get(self.hass), self.zones, zones):
                raise SettingsError("binding_conflict")
            live = {tank["id"] for zone in zones for tank in zone["tanks"]}
            topology = validate_topology(deepcopy(self.topology | {"zones": zones, "manual": manual,
                DISPLAY_SCALES: {key: value for key, value in self.topology[DISPLAY_SCALES].items() if key in live}}),
                _pair, _number)
            by_id = {row["id"]: row for row in data["tanks"]}
            previous = {tank["id"]: tank for zone in self.zones for tank in zone["tanks"]}
            rows = []
            for zone in topology["zones"]:
                for tank in zone["tanks"]:
                    old = by_id.get(tank["id"])
                    anchor = self._anchor(tank)
                    rows.append({**old, "anchor": anchor} if old and tank["id"] in previous
                                and old["anchor"] == self._anchor(previous[tank["id"]]) else {
                        "id": tank["id"], "anchor": anchor, **self._legacy_policy(tank),
                    })
            common, tanks = _validate({"common": data["common"], "tanks": rows}, stored=True)
            candidate = {**data, "generation": uuid4().hex, "topology": topology,
                         "common": common, "tanks": list(tanks.values())}
            del candidate["integrity"]
            candidate["integrity"] = _digest(candidate)
            self._freeze_topology()
            await self._async_commit_topology(candidate)
        # Core's unload drains this same gate. Never await native reload inside it.
        if not await self.hass.config_entries.async_reload(self.entry.entry_id):
            raise SettingsError("readback_required")
        owner = getattr(self.entry, "runtime_data", None)
        if owner is None or owner.settings is self or owner.settings.data != candidate:
            raise SettingsError("readback_required")

    async def _async_commit_topology(self, candidate):
        """One Store write and verify path for native and ordinary topology edits."""
        try:
            await self.store.async_save(candidate)
            observed = await self._async_read_file()
        except Exception as err:
            self._freeze_topology()
            raise SettingsError("readback_required") from err
        if observed != candidate:
            if observed != self.data:
                self._freeze_topology()
            raise SettingsError("readback_required")

    async def _async_use_topology(self, candidate):
        from . import alarm_history

        owner = self.entry.runtime_data
        self.topology = candidate["topology"]
        owner.zones = self.zones
        for zone in self.zones:
            for tank in zone["tanks"]:
                if tank["id"] == owner.tank_id:
                    owner.tank = tank
                    owner.labels.update(zone=zone["name"], tank=tank["name"])
        alarm_history.reconcile(self.hass, owner)
        await self.async_use(candidate)

    async def _async_reconcile_topology(self, authorize):
        """A fresh Get may prove either prior or exact admitted bytes; never write."""
        previous, candidate = self.indeterminate_topology
        try:
            observed = await self._async_read_file()
        except SettingsError as err:
            raise SettingsError("topology_recovery_required") from err
        await authorize()
        self.require_live(reconcile=True)
        if observed == candidate:
            try:
                await self._async_use_topology(candidate)
            except Exception as err:
                raise SettingsError("topology_recovery_required") from err
        elif observed != previous or self.data != previous:
            raise SettingsError("topology_recovery_required")
        self.require_live(reconcile=True)
        self.indeterminate_topology = None
        self.topology_busy = False
        self._publish_roles()
        await authorize()
        self.require_live()

    async def async_use(self, data):
        if self.data != data:
            self.data = data
            self._pending_projection = True
        if self._pending_projection:
            from .sensor import async_reconcile_settings

            await async_reconcile_settings(self.hass, self.entry, self)
            self._pending_projection = False

    async def async_request(self, authorize, msg):
        """Complete admitted storage under the gate even if its client leaves."""
        async with self.lock:
            await authorize()
            topology_get = msg["type"].endswith("/topology_get")
            if topology_get and self.indeterminate_topology is not None:
                from . import card_topology

                self.require_live(reconcile=True)
                await self._async_reconcile_topology(authorize)
                return card_topology.view(self)
            self.require_live()
            data = await self.async_read()
            await authorize()
            self.require_live()
            await self.async_use(data)
            await authorize()
            self.require_live()
            topology = topology_get or msg["type"].endswith("/topology_save")
            if topology:
                from . import card_topology

                if self.topology is None:
                    raise SettingsError("topology_not_enrolled")
            if "changes" not in msg:
                return card_topology.view(self) if topology else self.view()
            revision, source = self.tokens()
            if revision != msg["if_revision"]:
                raise SettingsError("stale_revision")
            if source != msg["if_source_snapshot"]:
                raise SettingsError("stale_source")
            if topology:
                zones = validate_topology(card_topology.build(self, msg), _pair, _number)
                next_source = self.source_snapshot(zones["zones"])
                candidate = self.candidate(msg["changes"], zones)
                try:
                    await self._async_commit_topology(candidate)
                except SettingsError:
                    if self.topology_busy:
                        self.indeterminate_topology = (data, candidate)
                    raise
                try:
                    self.require_live()
                    await self._async_use_topology(candidate)
                except Exception as err:
                    self.indeterminate_topology = (data, candidate)
                    self._freeze_topology()
                    raise SettingsError("readback_required") from err
                await authorize()
                self.require_live()
                if self.tokens()[1] != next_source:
                    raise SettingsError("readback_required")
                return card_topology.view(self)
            candidate = self.candidate(msg["changes"])
            try:
                await self.store.async_save(candidate)
                observed = await self.async_read()
                if observed != candidate:
                    raise SettingsError("readback_required")
                self.require_live()
                await self.async_use(observed)
            except Exception as err:
                raise SettingsError("readback_required") from err
            await authorize()
            self.require_live()
            if self.tokens()[1] != source:
                raise SettingsError("readback_required")
            return self.view()
