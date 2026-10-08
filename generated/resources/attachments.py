"""Uniform identity for generated constraints and modifiers.

Blender constraints and modifiers do not support custom ID properties.  Their
stable ownership metadata therefore lives as plain JSON on the owning ID
datablock, while the constraint/modifier keeps its normal artist-facing name.
"""

from __future__ import annotations

import json

from ..core.contracts import ResourceKind


ATTACHMENTS_PROPERTY = "__espresso_generated_attachments"


def _read(owner):
    try:
        raw = owner.get(ATTACHMENTS_PROPERTY, "[]")
        values = json.loads(raw) if isinstance(raw, str) else []
    except (AttributeError, TypeError, ValueError):
        return []
    return [item for item in values if isinstance(item, dict)]


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

