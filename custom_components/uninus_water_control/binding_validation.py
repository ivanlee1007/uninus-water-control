"""Admission checks for changed bindings shared by native and Card editors."""
from .manual import output_conflicts, source_conflicts

BINDING_FIELDS = ('level_sensor', 'refill_switch', 'alarm_switch')


def _identity(tank, field):
    if ident := tank.get(f'{field}_registry_id'):
        return ('registry', ident)
    return ('entity', tank.get(field))


def binding_errors(registry, previous, proposed):
    """Reject new sharing, preserving unchanged legacy bindings and row renames.

    This is draft-only validation: callers must not publish the proposed inventory
    until it succeeds. Names without a registry identity are conservative aliases.
    """
    old = {t['id']: t for z in previous for t in z['tanks']}
    errors = {}
    for zone in proposed:
        for tank in zone['tanks']:
            for field in BINDING_FIELDS:
                if not tank.get(field):
                    continue
                prior = old.get(tank['id'])
                if prior is not None and _identity(prior, field) == _identity(tank, field):
                    continue
                row = registry.async_get(tank.get(f'{field}_registry_id') or tank[field])
                if row is not None:
                    conflict = (source_conflicts(proposed, tank['id'], row) if field == 'level_sensor'
                                else output_conflicts(registry, proposed, tank['id'], field.removesuffix('_switch'), row))
                else:
                    # No registered row: a different string is not proof of a
                    # different physical output, but the same visible name is a conflict.
                    fields = ('level_sensor',) if field == 'level_sensor' else BINDING_FIELDS[1:]
                    conflict = any(
                        (other['id'], other_field) != (tank['id'], field)
                        and other.get(other_field)
                        and ((resolved.entity_id if (resolved := registry.async_get(
                            other.get(f'{other_field}_registry_id') or other[other_field])) else other[other_field])
                             == tank[field])
                        for z in proposed for other in z['tanks'] for other_field in fields
                    )
                if conflict:
                    errors[tank['id'], field] = 'shared_source' if field == 'level_sensor' else 'shared_output'
    return errors
