"""Uniform identity for generated constraints and modifiers.

Blender constraints and modifiers do not support custom ID properties.  Their
stable ownership metadata therefore lives as plain JSON on the owning ID
datablock, while the constraint/modifier keeps its normal artist-facing name.
"""

from __future__ import annotations

import json

from ..core.contracts import Ownership, ResourceKind


ATTACHMENTS_PROPERTY = "__espresso_generated_attachments"


def _read(owner):
    try:
        raw = owner.get(ATTACHMENTS_PROPERTY, "[]")
        values = json.loads(raw) if isinstance(raw, str) else []
    except (AttributeError, TypeError, ValueError):
        return []
    return [item for item in values if isinstance(item, dict)]


def _write(owner, values):
    if values:
        owner[ATTACHMENTS_PROPERTY] = json.dumps(values, separators=(",", ":"))
    elif ATTACHMENTS_PROPERTY in owner.keys():
        del owner[ATTACHMENTS_PROPERTY]


def _remember(owner, value, *, resource_id, setup_id, effect_id, kind,
              ownership, role):
    values = [item for item in _read(owner)
              if item.get("resource_id") != resource_id]
    values.append({
        "resource_id": str(resource_id),
        "setup_id": str(setup_id),
        "effect_id": str(effect_id),
        "kind": ResourceKind(kind).value,
        "ownership": Ownership(ownership).value,
        "role": str(role),
        "name": str(value.name),
        "type": str(value.type),
    })
    _write(owner, values)
    return value


def identity(owner, value):
    if owner is None or value is None:
        return {}
    kind = (ResourceKind.CONSTRAINT.value
            if hasattr(value, "target_space") else ResourceKind.MODIFIER.value)
    for item in _read(owner):
        if (item.get("kind") == kind and item.get("name") == value.name
                and item.get("type") == value.type):
            return dict(item)
    return {}


def restamp(owner, value, *, resource_id: str, setup_id: str = "",
            effect_id: str = "", ownership: Ownership = Ownership.EXCLUSIVE,
            role: str = ""):
    """Assign fresh identity to a copied native attachment on its new owner."""
    if owner is None or value is None:
        raise ValueError("Attachment restamp requires an owner and attachment")
    kind = (ResourceKind.CONSTRAINT
            if hasattr(value, "target_space") else ResourceKind.MODIFIER)
    _forget(owner, value, kind)
    return _remember(
        owner, value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=kind, ownership=ownership, role=role,
    )


def _forget(owner, value, kind):
    values = [
        item for item in _read(owner)
        if not (item.get("kind") == ResourceKind(kind).value
                and item.get("name") == value.name
                and item.get("type") == value.type)
    ]
    _write(owner, values)


def create_constraint(owner, constraint_type: str, name: str, *,
                      resource_id: str, setup_id: str = "",
                      effect_id: str = "",
                      ownership: Ownership = Ownership.EXCLUSIVE,
                      role: str = "", record_owner=None):
    value = owner.constraints.new(constraint_type)
    value.name = name
    return _remember(
        record_owner or owner, value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.CONSTRAINT,
        ownership=ownership, role=role,
    )


def create_modifier(owner, modifier_type: str, name: str, *,
                    resource_id: str, setup_id: str = "",
                    effect_id: str = "",
                    ownership: Ownership = Ownership.EXCLUSIVE,
                    role: str = ""):
    value = owner.modifiers.new(name, modifier_type)
    return _remember(
        owner, value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.MODIFIER,
        ownership=ownership, role=role,
    )


def remove_constraint(owner, value, *, record_owner=None) -> bool:
    if owner is None or value is None or value not in owner.constraints[:]:
        return False
    _forget(record_owner or owner, value, ResourceKind.CONSTRAINT)
    owner.constraints.remove(value)
    return True


def remove_modifier(owner, value) -> bool:
    if owner is None or value is None or value not in owner.modifiers[:]:
        return False
    _forget(owner, value, ResourceKind.MODIFIER)
    owner.modifiers.remove(value)
    return True
