"""Typed contracts used before a generated systems route mutates Blender data.

These descriptors intentionally contain no ``bpy`` imports.  A recipe can be
validated during catalogue import and tests can prove that a field, event, or
sampled source is never quietly treated as a plain scalar driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


SIGNAL_SCHEMA_VERSION = 1


class _ContractEnum(str, Enum):
    def __str__(self):
        return self.value


class SignalType(_ContractEnum):
    SCALAR = "SCALAR"
    VECTOR = "VECTOR"
    ROTATION = "ROTATION"
    BOOLEAN = "BOOLEAN"
    EVENT = "EVENT"
    COLOUR = "COLOUR"
    FIELD = "FIELD"
    TIME = "TIME"
    SAMPLED_SERIES = "SAMPLED_SERIES"


class SignalRepresentation(_ContractEnum):
    AUTO = "AUTO"
    FLOAT = "FLOAT"
    VECTOR3 = "VECTOR3"
    EULER = "EULER"
    QUATERNION = "QUATERNION"
    BOOLEAN = "BOOLEAN"
    EVENT_PULSE = "EVENT_PULSE"
    RGBA = "RGBA"
    FIELD = "FIELD"
    TIME = "TIME"
    SERIES = "SERIES"


class MotionSpace(_ContractEnum):
    NONE = "NONE"
    LOCAL = "LOCAL"
    WORLD = "WORLD"
    PARENT = "PARENT"
    CONTROLLER = "CONTROLLER"
    CAMERA = "CAMERA"
    CURVE = "CURVE"
    SURFACE = "SURFACE"
    NODE_DOMAIN = "NODE_DOMAIN"


class SourceState(_ContractEnum):
    STATELESS = "STATELESS"
    PRECOMPUTED = "PRECOMPUTED"
    SAMPLED = "SAMPLED"


class Route(_ContractEnum):
    DRIVER = "DRIVER"
    NODES = "NODES"
    SHADER_NODES = "SHADER_NODES"
    GEOMETRY_NODES = "GEOMETRY_NODES"
    CONSTRAINT = "CONSTRAINT"
    ACTION = "ACTION"
    CACHE = "CACHE"


class Portability(_ContractEnum):
    LIVE_NATIVE = "LIVE_NATIVE"
    BAKE_TO_NATIVE = "BAKE_TO_NATIVE"


@dataclass(frozen=True)
class SourceDescriptor:
    kind: str
    signal: SignalType
    units: str
    space: MotionSpace
    state: SourceState
    routes: frozenset[Route]
    portability: Portability
    fallback: str = "ERROR"
    representation: SignalRepresentation = SignalRepresentation.AUTO
    role: str = "SOURCE"
    schema_version: int = SIGNAL_SCHEMA_VERSION


_REPRESENTATIONS = {
    SignalType.SCALAR: frozenset({SignalRepresentation.AUTO, SignalRepresentation.FLOAT}),
    SignalType.VECTOR: frozenset({SignalRepresentation.AUTO, SignalRepresentation.VECTOR3}),
    SignalType.ROTATION: frozenset({
        SignalRepresentation.AUTO, SignalRepresentation.EULER,
        SignalRepresentation.QUATERNION,
    }),
    SignalType.BOOLEAN: frozenset({SignalRepresentation.AUTO, SignalRepresentation.BOOLEAN}),
    SignalType.EVENT: frozenset({SignalRepresentation.AUTO, SignalRepresentation.EVENT_PULSE}),
    SignalType.COLOUR: frozenset({SignalRepresentation.AUTO, SignalRepresentation.RGBA}),
    SignalType.FIELD: frozenset({SignalRepresentation.AUTO, SignalRepresentation.FIELD}),
    SignalType.TIME: frozenset({SignalRepresentation.AUTO, SignalRepresentation.TIME}),
    SignalType.SAMPLED_SERIES: frozenset({SignalRepresentation.SERIES}),
}


@dataclass(frozen=True)
class SignalConversion:
    source: SignalType
    target: SignalType
    method: str = ""
    options: dict[str, object] = field(default_factory=dict)


def validate_source(source: SourceDescriptor) -> None:
    if source.schema_version != SIGNAL_SCHEMA_VERSION:
        raise ValueError("Unsupported signal contract version %s." % source.schema_version)
    if not source.kind.strip():
        raise ValueError("A source kind is required.")
    if not source.units.strip():
        raise ValueError("A source must declare its units.")
    if not source.routes:
        raise ValueError("A source must declare at least one delivery route.")
    if source.representation not in _REPRESENTATIONS[source.signal]:
        raise ValueError(
            "%s representation is incompatible with %s signals."
            % (source.representation.value, source.signal.value)
        )
    if not source.role.strip():
        raise ValueError("A source must declare its semantic role.")
    if source.signal is SignalType.FIELD and Route.DRIVER in source.routes:
        raise ValueError("FIELD sources require a node-domain route, not a plain DRIVER route.")
    if source.signal is SignalType.FIELD and source.space not in {
        MotionSpace.WORLD,
        MotionSpace.LOCAL,
        MotionSpace.CONTROLLER,
        MotionSpace.CAMERA,
        MotionSpace.CURVE,
        MotionSpace.SURFACE,
        MotionSpace.NODE_DOMAIN,
    }:
        raise ValueError("FIELD sources require an explicit spatial domain.")
    if source.state is SourceState.SAMPLED:
        if source.portability is not Portability.BAKE_TO_NATIVE:
            raise ValueError("Sampled sources must bake to native data before detachment.")
        if not source.routes <= {
            Route.ACTION, Route.CACHE, Route.NODES, Route.SHADER_NODES,
            Route.GEOMETRY_NODES,
        }:
            raise ValueError("Sampled sources cannot claim an unsampled live driver route.")


def validate_conversion(conversion: SignalConversion) -> None:
    if conversion.source is conversion.target:
        return
    method = conversion.method.strip().upper()
    options = conversion.options
    if conversion.target is SignalType.ROTATION:
        if method not in {"EULER", "QUATERNION"}:
            raise ValueError(
                "Rotation conversion must declare quaternion handling or an explicit Euler order."
            )
        if method == "EULER" and not options.get("euler_order"):
            raise ValueError("Euler order is required for a rotation conversion.")
        return
    if conversion.source is SignalType.EVENT and conversion.target is SignalType.SCALAR:
        if method != "ENVELOPE":
            raise ValueError("Event-to-scalar conversion requires an envelope.")
        return
    if conversion.source is SignalType.VECTOR and conversion.target is SignalType.SCALAR:
        if method not in {"LENGTH", "DOT", "COMPONENT"}:
            raise ValueError("Vector-to-scalar conversion requires length, dot, or component.")
        return
    if conversion.source is SignalType.SCALAR and conversion.target is SignalType.COLOUR:
        if method not in {"PALETTE", "RAMP", "CHANNELS"}:
            raise ValueError("Scalar-to-colour conversion requires a palette, ramp, or channel map.")
        return
    if conversion.source is SignalType.BOOLEAN and conversion.target is SignalType.SCALAR:
        if method != "GATE":
            raise ValueError("Boolean-to-scalar conversion requires an explicit gate.")
        return
    raise ValueError(
        "Unsupported implicit conversion: %s to %s."
        % (conversion.source.value, conversion.target.value)
    )
