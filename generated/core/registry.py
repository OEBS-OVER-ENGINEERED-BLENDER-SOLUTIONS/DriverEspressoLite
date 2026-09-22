"""Small registries used by generated-resource builders and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Tuple

from .contracts import Ownership, ResourceKind, ResourceRecord


RESOURCE_ID = "__espresso_resource_id"
SETUP_ID = "__espresso_setup_id"
EFFECT_ID = "__espresso_effect_id"
RESOURCE_KIND = "__espresso_resource_kind"
OWNERSHIP = "__espresso_ownership"
RESOURCE_ROLE = "__espresso_resource_role"


class RegistrationError(ValueError):
    pass


class DuplicateRegistrationError(RegistrationError):
    pass


@dataclass(frozen=True)
class BuilderEntry:
    builder_id: str
    callback: Callable[..., Any]
    version: int = 1
    domain: str = ""


class BuilderRegistry:
    """Deterministic registry with explicit duplicate protection."""

    def __init__(self, family: str):
        self.family = str(family)
        self._entries: Dict[str, BuilderEntry] = {}

    def register(self, builder_id: str, callback: Callable[..., Any], *,
                 version: int = 1, domain: str = "") -> BuilderEntry:
        builder_id = str(builder_id).strip()
        if not builder_id:
            raise RegistrationError("Builder ID cannot be empty")
        if builder_id in self._entries:
            raise DuplicateRegistrationError(
                "%s builder %r is already registered" % (self.family, builder_id))
        if not callable(callback):
            raise RegistrationError("Builder %r is not callable" % builder_id)
        entry = BuilderEntry(builder_id, callback, int(version), str(domain))
        self._entries[builder_id] = entry
        return entry

    def decorator(self, builder_id: str, *, version: int = 1, domain: str = ""):
        def decorate(callback):
            self.register(builder_id, callback, version=version, domain=domain)
            return callback
        return decorate

    def replace(self, builder_id: str, callback: Callable[..., Any], *,
                version: int = 1, domain: str = "") -> BuilderEntry:
        """Replace an existing implementation without changing its public ID.

        Registration remains duplicate-safe; hot reloads and deliberate builder
        upgrades must opt into replacement explicitly.
        """
        builder_id = str(builder_id).strip()
        if builder_id not in self._entries:
            raise RegistrationError(
                "No %s builder registered for %r" % (self.family, builder_id))
        if not callable(callback):
            raise RegistrationError("Builder %r is not callable" % builder_id)
        entry = BuilderEntry(builder_id, callback, int(version), str(domain))
        self._entries[builder_id] = entry
        return entry

    def get(self, builder_id: str):
        return self._entries.get(builder_id)

    def require(self, builder_id: str) -> BuilderEntry:
        try:
            return self._entries[builder_id]
        except KeyError as exc:
            raise RegistrationError(
                "No %s builder registered for %r" % (self.family, builder_id)) from exc

    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._entries))


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


def clear_resource_identity(value) -> bool:
    """Remove Espresso ownership metadata from a surviving native resource."""
    changed = False
    for key in (RESOURCE_ID, SETUP_ID, EFFECT_ID, RESOURCE_KIND, OWNERSHIP, RESOURCE_ROLE):
        try:
            if key in value:
                del value[key]
                changed = True
        except (AttributeError, TypeError):
            return changed
    return changed


def resource_record(value, *, identity: Mapping[str, str] | None = None,
                    dependencies=(), metadata=None) -> ResourceRecord:
    """Describe a stamped ID or owner-recorded attachment as plain data."""
    identity = dict(identity or resource_identity(value))
    if not identity.get("resource_id"):
        raise ValueError("Blender resource has no Espresso identity")
    bl_rna = getattr(value, "bl_rna", None)
    id_type = getattr(bl_rna, "identifier", "") or type(value).__name__
    return ResourceRecord(
        resource_id=str(identity["resource_id"]),
        kind=ResourceKind(identity["kind"]),
        ownership=Ownership(identity.get("ownership", Ownership.EXCLUSIVE.value)),
        id_type=str(id_type),
        name=str(getattr(value, "name", "")),
        role=str(identity.get("role", "")),
        dependencies=tuple(dependencies),
        metadata=dict(metadata or {}),
    )
