"""Strict ordinary-editor topology projection and validation (no I/O)."""
from copy import deepcopy
from uuid import uuid4

from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er

from .inventory import DISPLAY_SCALES, DOMAIN
from .manual import output_conflicts, source_conflicts
from .settings import BINDINGS, IDENT, SettingsError, _fields, _invalid, matches

_DOMAINS = dict(zip(BINDINGS, ('sensor', 'switch', 'switch'), strict=True))
_ZONE = ('id', 'name', 'tanks')
_TANK = ('id', 'name', *BINDINGS)


def _candidate(row):
    return row is not None and row.platform != DOMAIN and not row.disabled_by and row.domain in ('sensor', 'switch')


def _binding(registry, tank, field):
    entity_id = tank[field]
    if entity_id is None:
        return None
    ident = tank.get(f'{field}_registry_id')
    row = registry.async_get(ident) if ident else None
    # Preserve the persisted identity for unchanged drafts, but do not present
    # a name which now may denote an entirely different registry row.
    return {'entity_id': (row.entity_id if row else None) if ident else entity_id, 'registry_id': ident}


def view(settings):
    registry = er.async_get(settings.hass)
    revision, source = settings.tokens()
    protected = settings.manual.get('tank_id')
    return {'version': 1, 'entry_id': settings.entry.entry_id, 'revision': revision, 'source_snapshot': source,
            'zones': [{'id': z['id'], 'name': z['name'], 'tanks': [
                {'id': t['id'], 'name': t['name'], 'protected': t['id'] == protected,
                 **{field: _binding(registry, t, field) for field in BINDINGS}}
                for t in z['tanks']]} for z in settings.zones],
            'candidates': [{'entity_id': entity, 'registry_id': row.id}
                           for entity, row in sorted(registry.entities.items()) if _candidate(row)]}


def _name(value, previous):
    if value == previous and previous is not None:
        return previous
    if (type(value) is not str or not 1 <= len(value) <= 128 or value != value.strip()
            or any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value)):
        _invalid()
    return value


def build(settings, msg):
    registry = er.async_get(settings.hass)
    old_zones = {z['id']: z for z in settings.zones}
    old_tanks = {t['id']: t for z in old_zones.values() for t in z['tanks']}
    seen_zones, seen_tanks, zones, changed = set(), set(), [], []
    protected = settings.manual.get('tank_id')
    if not msg['zones']:
        _invalid()
    for z in msg['zones']:
        _fields(z, _ZONE, _ZONE)
        zone_id = z['id']
        if zone_id is not None and (not matches(IDENT, zone_id) or zone_id in seen_zones or zone_id not in old_zones):
            _invalid()
        if type(z['tanks']) is not list or not z['tanks']:
            _invalid()
        old_zone = old_zones.get(zone_id, {})
        previous = {t['id']: t for t in old_zone.get('tanks', [])}
        new_zone = {'id': zone_id or uuid4().hex, 'name': _name(z['name'], old_zone.get('name')), 'tanks': []}
        seen_zones.add(new_zone['id'])
        zones.append(new_zone)
        for t in z['tanks']:
            _fields(t, _TANK, _TANK)
            ident = t['id']
            if ident is not None and (not matches(IDENT, ident) or ident in seen_tanks or ident not in previous):
                _invalid()
            old = previous.get(ident)
            tank = deepcopy(old) if old else {'id': uuid4().hex}
            tank['name'] = _name(t['name'], old['name'] if old else None)
            seen_tanks.add(tank['id'])
            new_zone['tanks'].append(tank)
            for field, domain in _DOMAINS.items():
                choice = t[field]
                if old is not None and choice == _binding(registry, old, field):
                    continue
                if old is not None and ident == protected:
                    raise SettingsError('protected_topology')
                key = f'{field}_registry_id'
                if choice is None and domain == 'switch':
                    tank[field] = None
                    tank.pop(key, None)
                    continue
                _fields(choice, ('entity_id', 'registry_id'), ('entity_id', 'registry_id'))
                entity, row_id = choice['entity_id'], choice['registry_id']
                if (type(entity) is not str or not valid_entity_id(entity) or entity.partition('.')[0] != domain
                        or not matches(IDENT, row_id)):
                    _invalid()
                row = registry.async_get(entity)
                if not _candidate(row) or row.id != row_id:
                    raise SettingsError('stale_source')
                tank[field], tank[key] = entity, row.id
                changed.append((tank['id'], field, row))
    removed = {'zones': sorted(old_zones.keys() - seen_zones), 'tanks': sorted(old_tanks.keys() - seen_tanks)}
    if protected in removed['tanks']:
        raise SettingsError('protected_topology')
    if msg['remove'] != removed:
        raise SettingsError('removal_confirmation_required')
    for ident, field, row in changed:
        if (source_conflicts(zones, ident, row) if field == 'level_sensor'
                else output_conflicts(registry, zones, ident, field.removesuffix('_switch'), row)):
            raise SettingsError('binding_conflict')
    if zones == settings.zones:
        _invalid()
    return settings.topology | {'zones': zones, DISPLAY_SCALES: {
        k: v for k, v in settings.topology[DISPLAY_SCALES].items() if k in seen_tanks}}
