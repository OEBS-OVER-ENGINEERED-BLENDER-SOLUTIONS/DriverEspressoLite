"""Small registries used by generated-resource builders and adapters."""

from __future__ import annotations

from typing import Mapping

from .contracts import Ownership, ResourceKind


RESOURCE_ID = "__espresso_resource_id"
SETUP_ID = "__espresso_setup_id"
EFFECT_ID = "__espresso_effect_id"
RESOURCE_KIND = "__espresso_resource_kind"
OWNERSHIP = "__espresso_ownership"
RESOURCE_ROLE = "__espresso_resource_role"


def stamp_resource(value, *, resource_id: str, setup_id: str = "",
                   effect_id: str = "", kind: ResourceKind,
                   ownership: Ownership = Ownership.EXCLUSIVE,
                   role: str = ""):
    """Stamp stable identity on any Blender struct supporting ID properties."""
    if not resource_id:
        raise ValueError("Generated resource requires a resource_id")
    value[RESOURCE_ID] = str(resource_id)
    value[SETUP_ID] = str(setup_id)
    value[EFFECT_ID] = str(effect_id)
    value[RESOURCE_KIND] = ResourceKind(kind).value
    value[OWNERSHIP] = Ownership(ownership).value
    value[RESOURCE_ROLE] = str(role)
    return value


def resource_identity(value) -> Mapping[str, str]:
    """Return an empty mapping for ordinary Blender data."""
    try:
        resource_id = value.get(RESOURCE_ID, "")
    except (AttributeError, TypeError):
        return {}
    if not resource_id:
        return {}
    return {
        "resource_id": str(resource_id),
        "setup_id": str(value.get(SETUP_ID, "")),
        "effect_id": str(value.get(EFFECT_ID, "")),
        "kind": str(value.get(RESOURCE_KIND, "")),
        "ownership": str(value.get(OWNERSHIP, "")),
        "role": str(value.get(RESOURCE_ROLE, "")),
    }

