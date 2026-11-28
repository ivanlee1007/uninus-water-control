"""Enrollment witness and private topology validation; no Core Store access."""
from copy import deepcopy
from uuid import uuid4

from homeassistant.core import callback, valid_entity_id
from homeassistant.util.hass_dict import HassKey

from .inventory import DISPLAY_SCALES, DOMAIN, legacy_inventory

MARK = 'topology_enrollment'
VERSION = 2
_BOOT_FENCE: HassKey[dict[str, str]] = HassKey(f'{DOMAIN}.topology_boot_fence')


def _uuid(value):
    return type(value) is str and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def witness(entry):
    return entry.data.get(MARK)


def valid_witness(value):
    return (type(value) is dict and value.keys() == {'phase', 'token'}
            and value['phase'] in ('requested', 'owned') and _uuid(value['token']))


def fenced(hass, entry):
    return enrollment_fence(hass, entry) is not None


def enrollment_fence(hass, entry):
    return hass.data.get(_BOOT_FENCE, {}).get(entry.entry_id)


@callback
def fence_enrollment(hass, entry, reason='topology_restart_required'):
    hass.data.setdefault(_BOOT_FENCE, {})[entry.entry_id] = reason


@callback
def _advance(hass, entry, value):
    # Latch before the public in-memory update, even if Core later fails to
    # persist it. No reload in this process is a durability acknowledgment.
    fence_enrollment(hass, entry)
    hass.config_entries.async_update_entry(entry, data=entry.data | {MARK: value})


@callback
def request_enrollment(hass, entry):
    _advance(hass, entry, {'phase': 'requested', 'token': uuid4().hex})


@callback
def mark_owned(hass, entry, requested):
    _advance(hass, entry, requested | {'phase': 'owned'})


def legacy_topology(entry):
    return deepcopy({'zones': legacy_inventory(entry), 'manual': entry.options.get('manual') or {},
                     DISPLAY_SCALES: entry.data.get(DISPLAY_SCALES, {})})


def validate_topology(topology, pair, number):
    """Validate every runtime-consumed field before projecting owned data."""
    from .settings import BINDINGS, PAIRS, _fields, _invalid

    fields = ('zones', 'manual', DISPLAY_SCALES)
    _fields(topology, fields, fields)
    zones = topology['zones']
    if type(zones) is not list or not zones:
        _invalid()
    zone_ids, tanks = set(), {}
    threshold_keys = {key for keys in PAIRS.values() for key in keys}
    binding_ids = tuple(f'{key}_registry_id' for key in BINDINGS)
    for zone in zones:
        _fields(zone, ('id', 'name', 'tanks'), ('id', 'name', 'tanks'))
        if (not _uuid(zone['id']) or zone['id'] in zone_ids or type(zone['name']) is not str
                or not zone['name'].strip() or type(zone['tanks']) is not list or not zone['tanks']):
            _invalid()
        zone_ids.add(zone['id'])
        for tank in zone['tanks']:
            required = ('id', 'name', *BINDINGS)
            _fields(tank, (*required, *binding_ids, 'thresholds'), required)
            if (not _uuid(tank['id']) or tank['id'] in tanks or type(tank['name']) is not str
                    or not tank['name'].strip()):
                _invalid()
            tanks[tank['id']] = tank
            for field, domain in zip(BINDINGS, ('sensor', 'switch', 'switch'), strict=True):
                name = tank[field]
                if name is None and domain == 'switch':
                    if f'{field}_registry_id' in tank:
                        _invalid()
                    continue
                if type(name) is not str or not valid_entity_id(name) or name.partition('.')[0] != domain:
                    _invalid()
                if f'{field}_registry_id' in tank and not _uuid(tank[f'{field}_registry_id']):
                    _invalid()
            thresholds = _fields(tank.get('thresholds', {}), threshold_keys)
            for kind, keys in PAIRS.items():
                if any(key in thresholds for key in keys):
                    if not all(key in thresholds for key in keys):
                        _invalid()
                    pair(kind, [thresholds[key] for key in keys], stored=True, local=True)
    scales = topology[DISPLAY_SCALES]
    if type(scales) is not dict or any(not _uuid(key) or not number(scale, stored=True) or scale <= 0
                                      for key, scale in scales.items()):
        _invalid()
    manual = _fields(topology['manual'], ('tank_id', 'refill', 'alarm'))
    if manual:
        if not _uuid(manual.get('tank_id')) or (tank := tanks.get(manual['tank_id'])) is None:
            _invalid()
        for role in ('refill', 'alarm'):
            if role not in manual:
                continue
            fields = ('output', 'source', 'cutoff_confirmed', 'max_runtime_seconds')
            binding = _fields(manual[role], fields, fields)
            if (not _uuid(binding['output']) or binding['output'] != tank.get(f'{role}_switch_registry_id')
                    or binding['cutoff_confirmed'] is not True or type(binding['max_runtime_seconds']) is not int
                    or not 1 <= binding['max_runtime_seconds'] <= 300):
                _invalid()
            source = binding['source']
            if role == 'refill':
                if not _uuid(source) or source != tank.get('level_sensor_registry_id'):
                    _invalid()
            elif source is not None:
                _invalid()
    return topology
