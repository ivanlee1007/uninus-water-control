"""Join current-epoch native output observations to bounded HA Recorder rows."""

import asyncio
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from hashlib import sha256
import hmac
from itertools import islice
from uuid import uuid4

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api as ws
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED, STATE_OFF, STATE_ON
from homeassistant.core import EventOrigin, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.util.dt import utcnow

from .config_api import _authorize, _BASE, _pattern
from .inventory import DOMAIN, inventory
from .settings import IDENT, SettingsError

KEY = f"{DOMAIN}.alarm_history"
WINDOW = timedelta(hours=24)
MAX_TARGETS = 128
MAX_POINTS = 256
MAX_EVENTS = 50
MAX_FLIGHTS = 4
QUERY_TIMEOUT = 5
_VALUES = (STATE_OFF, STATE_ON)


def _utc(stamp):
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _point(state):
    if state is not None and state.state in _VALUES:
        return (state.last_changed, state.state)
    return None


def _source(hass, entity):
    """Do not import Recorder's SQL dependencies on installations without it."""
    if ((rec := hass.data.get(DATA_INSTANCE)) is None or not rec.recording or not rec.enabled
            or not rec.async_db_ready.done() or rec.async_db_ready.cancelled() or not rec.async_db_ready.result()
            or rec.engine is None or (rec.entity_filter is not None and not rec.entity_filter(entity))):
        return None
    return (rec, rec.engine, rec.entity_filter)


def _read_rows(hass, entity, first, end):
    from homeassistant.components.recorder.history import state_changes_during_period

    # Core's bounded one-entity API, not History's unbounded/all-entity search.
    # One extra row detects overflow. No initial-state synthesis or attributes.
    result = state_changes_during_period(
        hass, first - timedelta(microseconds=1), end, entity_id=entity,
        no_attributes=True, include_start_time_state=False, limit=MAX_POINTS + 1,
    )
    return tuple((row.last_updated, row.state) for row in result.get(entity, ()))


@dataclass(eq=False)
class _Track:
    owner: object
    tank: dict
    row: object = None
    scope: str = field(default_factory=lambda: uuid4().hex)
    source: tuple | None = None
    points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    limited: bool = False

    def cut(self):
        self.scope = uuid4().hex
        self.points.clear()
        self.source = None
        self.limited = False

    def observe(self, old, new, source):
        if new is None or source is None:
            self.cut()
            return
        if self.points and (source != self.source or old != self.points[-1] or new[0] <= old[0]):
            self.cut()
        self.source = source
        if len(self.points) == MAX_POINTS:
            self.limited = True
        self.points.append(new)


def _configured_row(owner, tank):
    """Resolve only the persisted registry ID; a passive switch never binds by name."""
    if not (ident := tank.get("alarm_switch_registry_id")):
        return None
    if (row := owner.registry.async_get(ident)) and row.id == ident and row.domain == "switch" and not row.disabled_by:
        return row
    return None


def _alarm_row(owner, tank):
    row = _configured_row(owner, tank)
    if row is None:
        return None
    manual = owner.settings.manual
    if not isinstance(manual, Mapping):
        return None
    if manual.get("tank_id") == tank["id"] and "alarm" in manual:
        role = owner.roles.get("alarm")
        settings = manual["alarm"]
        if (not isinstance(settings, Mapping) or settings.get("output") != row.id or role is None
                or owner.tank is not tank or owner.tank_id != tank["id"] or role.owner is not owner
                or role.binding.output != row.id or role.output() is not row):
            return None
    return row


class _History:
    def __init__(self, hass):
        self.hass = hass
        self.secret = uuid4().bytes
        self.tracks = {}
        self.by_entity = defaultdict(set)
        self.flights = set()
        hass.bus.async_listen(EVENT_STATE_CHANGED, self.changed, event_filter=self.tracked_entity)
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self.registry_changed)
        hass.bus.async_listen(EVENT_CALL_SERVICE, self.recorder_changed, event_filter=self.recorder_service)

    @callback
    def tracked_entity(self, data):
        return data.get("entity_id") in self.by_entity

    @staticmethod
    @callback
    def recorder_service(data):
        return data.get("domain") == "recorder"

    def attach(self, owner):
        wanted = {(owner.entry.entry_id, zone["id"], tank["id"]): tank
                  for zone in owner.zones for tank in zone["tanks"] if tank.get("alarm_switch_registry_id")}
        for key, track in tuple(self.tracks.items()):
            if track.owner is owner and key not in wanted:
                self.bind(track, None)
                del self.tracks[key]
        for key, tank in wanted.items():
            if (track := self.tracks.get(key)) is None:
                if len(self.tracks) >= MAX_TARGETS:
                    continue
                track = self.tracks[key] = _Track(owner, tank)
            track.tank = tank
            self.bind(track, _alarm_row(owner, tank))

    def detach(self, owner):
        for key, track in tuple(self.tracks.items()):
            if track.owner is owner:
                self.bind(track, None)
                del self.tracks[key]

    def bind(self, track, row):
        if track.row is row:
            return
        if track.row is not None:
            entity = track.row.entity_id
            self.by_entity[entity].discard(track)
            if not self.by_entity[entity]:
                del self.by_entity[entity]
        track.cut()
        track.row = row
        if row is not None:
            self.by_entity[row.entity_id].add(track)

    @callback
    def registry_changed(self, event):
        for track in self.tracks.values():
            self.bind(track, _alarm_row(track.owner, track.tank))

    @callback
    def recorder_changed(self, event):
        if event.data["service"] in {"enable", "disable", "purge", "purge_entities"}:
            for track in self.tracks.values():
                track.cut()

    @callback
    def changed(self, event):
        entity = event.data["entity_id"]
        old = _point(event.data["old_state"])
        new = _point(event.data["new_state"])
        source = _source(self.hass, entity)
        is_native = (event.origin is EventOrigin.local and event.data["new_state"] is not None
                     and self.hass.states.get(entity) is event.data["new_state"])
        for track in self.by_entity.get(entity, ()):
            try:
                track.owner.settings.require_live()
            except SettingsError:
                track.cut()
                continue
            if not is_native or source is None or new is None or _alarm_row(track.owner, track.tank) is not track.row:
                track.cut()
            elif new != old:
                track.observe(old, new, source)

    def select(self, user, key):
        entry_id, zone_id, tank_id = key
        entry = self.hass.config_entries.async_get_entry(entry_id)
        owner = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
        if owner is None:
            raise SettingsError("entry_not_loaded")
        owner.settings.require_live()
        matched = tuple(islice((t for z in inventory(entry) if z["id"] == zone_id
                                for t in z["tanks"] if t["id"] == tank_id), 2))
        if len(matched) != 1:
            raise SettingsError("unknown_tank")
        tank, = matched
        registry = owner.registry
        name = registry.async_get_entity_id("sensor", DOMAIN, tank_id)
        projection = registry.async_get(name) if name else None
        if projection is None or projection.config_entry_id != entry_id or projection.disabled_by:
            raise SettingsError("unknown_tank")
        if not user.permissions.check_entity(projection.entity_id, POLICY_READ):
            raise Unauthorized
        state = self.hass.states.get(projection.entity_id)
        metadata = state.attributes.get("uninus_water_dashboard", {}) if state else {}
        if (state is None or state.attributes.get("uninus_water_settings_entry") != entry_id
                or not isinstance(metadata, Mapping) or metadata.get("version") != 1
                or any(not isinstance(item := metadata.get(field), Mapping) or item.get("id") != ident
                       for field, ident in (("zone", zone_id), ("tank", tank_id)))):
            raise SettingsError("unknown_tank")
        configured = _configured_row(owner, tank)
        if configured is not None and not user.permissions.check_entity(configured.entity_id, POLICY_READ):
            raise Unauthorized
        return owner, tank, projection, configured, _alarm_row(owner, tank)

    def _scope(self, key):
        return hmac.new(self.secret, ":".join(key).encode(), sha256).hexdigest()[:32]

    async def request(self, connection, msg):
        user = await _authorize(self.hass, connection)
        if connection in self.flights or len(self.flights) >= MAX_FLIGHTS:
            raise SettingsError("history_busy")
        self.flights.add(connection)
        job = None
        try:
            key = (msg["entry_id"], msg["zone_id"], msg["tank_id"])
            selected = self.select(user, key)
            owner, tank, _, _, row = selected
            track = self.tracks.get(key)
            end = utcnow()
            start = end - WINDOW
            dto = {"version": 1, "scope": self._scope(key), "status": "unavailable",
                   "window": {"start_utc": _utc(start), "end_utc": _utc(end)},
                   "coverage": None, "events": [], "limited": False}
            if not tank.get("alarm_switch_registry_id") and not tank.get("alarm_switch"):
                dto["status"] = "no_binding"
            if row is None or track is None or track.owner is not owner or track.tank is not tank or track.row is not row:
                return dto
            dto["scope"] = track.scope
            source = _source(self.hass, row.entity_id)
            if source is None or track.source != source:
                return dto
            points = tuple(point for point in track.points if start < point[0] < end)
            if not points:
                return dto
            scope = track.scope
            limited = track.limited
            try:
                job = asyncio.ensure_future(source[0].async_add_executor_job(
                    partial(_read_rows, self.hass, row.entity_id, points[0][0], end),
                ))
                async with asyncio.timeout(QUERY_TIMEOUT):
                    rows = await asyncio.shield(job)
            except Exception:
                rows = ()  # DB/timeout details may contain private entity information.
            user = await _authorize(self.hass, connection)
            fresh = self.select(user, key)
            if (any(a is not b for a, b in zip(selected, fresh, strict=True))
                    or self.tracks.get(key) is not track or track.scope != scope
                    or _source(self.hass, row.entity_id) != source or track.source != source):
                raise SettingsError("stale_history")
            if rows != points:
                return dto  # Missing, excess or ambiguous native endpoints: do not backfill.
            edges = [current for previous, current in zip(points, points[1:]) if previous[1] != current[1]]
            dto.update(scope=scope, status="ok" if edges else "no_events",
                       coverage={"start_utc": _utc(points[0][0]), "end_utc": _utc(points[-1][0])},
                       events=[{"at_utc": _utc(stamp), "state": value} for stamp, value in reversed(edges[-MAX_EVENTS:])],
                       limited=limited or len(edges) > MAX_EVENTS)
            return dto
        finally:
            if job is not None and not job.done():
                # Cancellation/timeout of the WS caller does not end the database work.
                job.add_done_callback(partial(self.release, connection))
            else:
                self.flights.discard(connection)

    def release(self, connection, job):
        self.flights.discard(connection)
        if not job.cancelled():
            job.exception()


@ws.websocket_command({vol.Required("type"): f"{DOMAIN}/alarm_history_get", **_BASE,
                       vol.Required("zone_id"): _pattern(IDENT), vol.Required("tank_id"): _pattern(IDENT)})
@ws.require_admin
@ws.async_response
async def get_history(hass, connection, msg):
    try:
        result = await hass.data[KEY].request(connection, msg)
    except Unauthorized:
        connection.send_error(msg["id"], "unauthorized", "Unauthorized")
    except SettingsError as err:
        connection.send_error(msg["id"], str(err), str(err))
    else:
        connection.send_result(msg["id"], result)


@callback
def register(hass):
    if KEY not in hass.data:
        hass.data[KEY] = _History(hass)
        ws.async_register_command(hass, get_history)


def attach(hass, entry, owner):
    if history := hass.data.get(KEY):
        history.attach(owner)
        entry.async_on_unload(partial(history.detach, owner))


def detach(hass, owner):
    if history := hass.data.get(KEY):
        history.detach(owner)


def reconcile(hass, owner):
    if history := hass.data.get(KEY):
        history.attach(owner)
