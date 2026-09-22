"""Transactional access to named Geometry Nodes modifier inputs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InputWrite:
    owner: object
    modifier: object
    socket_name: str
    value: object


def input_identifier(modifier, socket_name):
    tree = getattr(modifier, "node_group", None)
    if tree is None:
        return ""
    for item in tree.interface.items_tree:
        if (getattr(item, "item_type", "") == "SOCKET"
                and item.in_out == "INPUT" and item.name == socket_name):
            return item.identifier
    return ""


def _plain_value(value):
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return value
    try:
        return tuple(value)
    except TypeError:
        return value


def read_input(modifier, socket_name):
    identifier = input_identifier(modifier, socket_name)
    if not identifier:
        raise KeyError("Geometry Nodes input %r is unavailable" % socket_name)
    return _plain_value(modifier[identifier])


def read_optional_input(modifier, socket_name, default=None):
    """Read presentation-only state without making stale interfaces fatal.

    Saved files can briefly retain an ownership record while a migrated or
    replaced node group no longer exposes an optional socket.  Read-only UI
    paths treat that state as unavailable; mutation continues to use the
    strict ``read_input``/``write_inputs`` contract.
    """
    identifier = input_identifier(modifier, socket_name)
    if not identifier:
        return default
    try:
        return _plain_value(modifier[identifier])
    except (KeyError, ReferenceError, TypeError):
        return default


def _action_curves(action):
    if action is None:
        return ()
    if hasattr(action, "fcurves"):
        return tuple((action.fcurves, curve) for curve in action.fcurves)
    return tuple(
        (bag.fcurves, curve)
        for layer in getattr(action, "layers", ()) or ()
        for strip in getattr(layer, "strips", ()) or ()
        for bag in getattr(strip, "channelbags", ()) or ()
        for curve in getattr(bag, "fcurves", ()) or ()
    )


def migrate_legacy_owner_input(owner, modifier, property_key, socket_name,
                               component=-1):
    """Move one legacy owner property onto its native modifier socket.

    Geometry Nodes inputs are already keyframeable.  Older Driver Espresso
    files briefly mirrored them onto object properties and drove the modifier,
    which exposed each socket driver as a separate Applied Effect.  Retargeting
    the existing Action F-curve in place preserves every keyframe and handle.
    """
    if owner is None or modifier is None or property_key not in owner.keys():
        return False
    identifier = input_identifier(modifier, socket_name)
    if not identifier:
        raise KeyError("Geometry Nodes input %r is unavailable" % socket_name)
    component = int(component)
    legacy_path = '["%s"]' % property_key
    socket_path = '["%s"]' % identifier
    native_path = modifier.path_from_id(socket_path)
    native_index = component if component >= 0 else 0
    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    curves = _action_curves(action)
    destination_exists = any(
        curve.data_path == native_path and int(curve.array_index) == native_index
        for _collection, curve in curves
    )
    for collection, curve in curves:
        if curve.data_path != legacy_path:
            continue
        if destination_exists:
            collection.remove(curve)
        else:
            curve.data_path = native_path
            curve.array_index = native_index
            destination_exists = True

    value = owner[property_key]
    try:
        if component >= 0:
            current = list(modifier[identifier])
            current[component] = value
            modifier[identifier] = current
            modifier.driver_remove(socket_path, component)
        else:
            modifier[identifier] = value
            modifier.driver_remove(socket_path)
    except (AttributeError, KeyError, RuntimeError, TypeError):
        # Some migrated files have already lost the driver.  The native value
        # and Action path are still valid, so absence is not a migration error.
        if component >= 0:
            current = list(modifier[identifier])
            current[component] = value
            modifier[identifier] = current
        else:
            modifier[identifier] = value
    del owner[property_key]
    if hasattr(owner, "update_tag"):
        owner.update_tag(refresh={"DATA"})
    return True


def _input_names_by_identifier(modifier):
    tree = getattr(modifier, "node_group", None)
    if tree is None:
        return {}
    return {
        item.identifier: item.name
        for item in tree.interface.items_tree
        if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
    }


def retarget_modifier_input_animation(owner, source_modifier, target_modifier):
    """Preserve native socket keys when an owned modifier is rebuilt."""
    if owner is None or source_modifier is None or target_modifier is None:
        return 0
    source_prefix = source_modifier.path_from_id()
    source_names = _input_names_by_identifier(source_modifier)
    target_identifiers = {
        name: identifier
        for identifier, name in _input_names_by_identifier(target_modifier).items()
    }
    action = getattr(getattr(owner, "animation_data", None), "action", None)
    curves = _action_curves(action)
    moved = 0
    for collection, curve in curves:
        path = str(curve.data_path)
        if not path.startswith(source_prefix + '["'):
            continue
        identifier = path[len(source_prefix) + 2:].split('"]', 1)[0]
        socket_name = source_names.get(identifier)
        target_identifier = target_identifiers.get(socket_name)
        if not target_identifier:
            continue
        target_path = target_modifier.path_from_id('["%s"]' % target_identifier)
        collision = next((
            other for _owner, other in curves
            if other is not curve and other.data_path == target_path
            and int(other.array_index) == int(curve.array_index)
        ), None)
        if collision is not None:
            collection.remove(curve)
        else:
            curve.data_path = target_path
        moved += 1
    return moved


def remove_modifier_input_animation(owner, modifier):
    """Remove Action channels whose endpoint disappears with ``modifier``."""
    if owner is None or modifier is None:
        return 0
    prefix = modifier.path_from_id() + '["'
    action = getattr(getattr(owner, "animation_data", None), "action", None)
    removed = 0
    for collection, curve in _action_curves(action):
        if str(curve.data_path).startswith(prefix):
            collection.remove(curve)
            removed += 1
    return removed


def write_inputs(writes):
    """Apply a validated cross-modifier batch or restore every prior value."""
    prepared = []
    for write in tuple(writes):
        identifier = input_identifier(write.modifier, write.socket_name)
        if not identifier:
            return False, "Geometry Nodes input %r is unavailable." % write.socket_name
        prepared.append((write, identifier, _plain_value(write.modifier[identifier])))

    try:
        for write, identifier, _previous in prepared:
            write.modifier[identifier] = write.value
        for owner in {write.owner for write, _identifier, _previous in prepared}:
            if hasattr(owner, "update_tag"):
                owner.update_tag(refresh={"DATA"})
    except Exception as exc:
        for write, identifier, previous in reversed(prepared):
            try:
                write.modifier[identifier] = previous
            except Exception:
                pass
        return False, "Could not update the live Geometry Nodes setup: %s" % exc
    return True, "Updated the live Geometry Nodes setup."
