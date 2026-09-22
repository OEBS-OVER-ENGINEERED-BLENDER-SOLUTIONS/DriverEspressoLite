"""Serializable contracts shared by all generated-resource services."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Tuple


class Ownership(str, Enum):
    """Who may remove a resource when an Espresso setup is cleared."""

    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"
    BORROWED = "BORROWED"
    USER_OWNED = "USER_OWNED"


class ResourceKind(str, Enum):
    NODE_GROUP = "NODE_GROUP"
    NODE_INSTANCE = "NODE_INSTANCE"
    OBJECT = "OBJECT"
    COLLECTION = "COLLECTION"
    PROPERTY = "PROPERTY"
    DRIVER = "DRIVER"
    CONSTRAINT = "CONSTRAINT"
    MODIFIER = "MODIFIER"
    ACTION = "ACTION"
    MATERIAL = "MATERIAL"
    DATABLOCK = "DATABLOCK"


class LifecycleClassification(str, Enum):
    """Adoption state for a persistent route's lifecycle boundary."""

    MIGRATED = "migrated"
    COMPATIBLE_ADAPTER = "compatible_adapter"
    DEFERRED = "deferred"


LIFECYCLE_OPERATIONS = frozenset({
    "apply", "update", "replace", "bake", "detach", "clear", "discovery",
})


@dataclass(frozen=True)
class RouteLifecycle:
    """Immutable lifecycle capabilities and adoption state for one route."""

    route: str
    operations: Tuple[str, ...]
    classification: LifecycleClassification
    deferral_reason: str = ""
    owner_task: str = ""

    def __post_init__(self):
        if not self.route:
            raise ValueError("Route lifecycle requires a route")
        operations = tuple(self.operations)
        if frozenset(operations) != LIFECYCLE_OPERATIONS:
            raise ValueError("Route lifecycle must report all seven operations")
        classification = LifecycleClassification(self.classification)
        if classification is LifecycleClassification.DEFERRED:
            if not str(self.deferral_reason).strip() or not str(self.owner_task).strip():
                raise ValueError("Deferred routes require a reason and owner task")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "classification", classification)


@dataclass(frozen=True)
class SetupRequest:
    setup_id: str
    effect_id: str
    recipe_id: str
    label: str
    route: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    portability: str = "NATIVE_PLAYBACK"

    def __post_init__(self):
        if not self.setup_id or not self.effect_id or not self.route:
            raise ValueError("Setup request requires setup_id, effect_id and route")
        object.__setattr__(self, "parameters", _plain_mapping(self.parameters))
        object.__setattr__(self, "metadata", _plain_mapping(self.metadata))
        if self.portability not in {"NATIVE_PLAYBACK", "LIVE_NATIVE", "BAKE_TO_NATIVE"}:
            raise ValueError("Setup request has an unsupported portability contract")


def _plain_mapping(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    kind: ResourceKind
    ownership: Ownership
    id_type: str
    name: str
    role: str = ""
    dependencies: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.resource_id:
            raise ValueError("Generated resource requires a stable resource_id")
        if not self.id_type:
            raise ValueError("Generated resource requires a Blender id_type")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "metadata", _plain_mapping(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind.value,
            "ownership": self.ownership.value,
            "id_type": self.id_type,
            "name": self.name,
            "role": self.role,
            "dependencies": list(self.dependencies),
            "metadata": _plain_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResourceRecord":
        return cls(
            resource_id=str(value["resource_id"]),
            kind=ResourceKind(value["kind"]),
            ownership=Ownership(value["ownership"]),
            id_type=str(value["id_type"]),
            name=str(value.get("name", "")),
            role=str(value.get("role", "")),
            dependencies=tuple(value.get("dependencies", ())),
            metadata=_plain_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class SetupManifest:
    setup_id: str
    effect_id: str
    recipe_id: str
    label: str
    resources: Tuple[ResourceRecord, ...] = ()
    portability: str = "NATIVE_PLAYBACK"
    schema_version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.setup_id:
            raise ValueError("Generated setup requires a stable setup_id")
        if not self.effect_id:
            raise ValueError("Generated setup requires an effect_id")
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "metadata", _plain_mapping(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "setup_id": self.setup_id,
            "effect_id": self.effect_id,
            "recipe_id": self.recipe_id,
            "label": self.label,
            "resources": [item.to_dict() for item in self.resources],
            "portability": self.portability,
            "metadata": _plain_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SetupManifest":
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            setup_id=str(value["setup_id"]),
            effect_id=str(value["effect_id"]),
            recipe_id=str(value.get("recipe_id", "")),
            label=str(value.get("label", "")),
            resources=tuple(
                ResourceRecord.from_dict(item)
                for item in value.get("resources", ())
                if isinstance(item, Mapping)
            ),
            portability=str(value.get("portability", "NATIVE_PLAYBACK")),
            metadata=_plain_mapping(value.get("metadata")),
        )

    def resource_ids(self) -> Tuple[str, ...]:
        return tuple(item.resource_id for item in self.resources)

    def with_resources(self, resources: Iterable[ResourceRecord]) -> "SetupManifest":
        return SetupManifest(
            setup_id=self.setup_id,
            effect_id=self.effect_id,
            recipe_id=self.recipe_id,
            label=self.label,
            resources=tuple(resources),
            portability=self.portability,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )
