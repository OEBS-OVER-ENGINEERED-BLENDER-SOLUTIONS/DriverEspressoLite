"""Blender-free Motion Stack data model and semantic validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Tuple


class _ValueEnum(str, Enum):
    def __str__(self):
        return self.value


class BlendMode(_ValueEnum):
    REPLACE = "REPLACE"
    ADD = "ADD"
    MULTIPLY = "MULTIPLY"
    MAX = "MAX"
    MIN = "MIN"


class BaselineContract(_ValueEnum):
    ZERO = "ZERO"
    ONE = "ONE"
    CAPTURED = "CAPTURED"
    TEMPLATE = "TEMPLATE"


@dataclass(frozen=True)
class StackLayer:
    layer_id: str
    effect_id: str
    label: str
    channel: str
    expression: str
    blend: BlendMode = BlendMode.ADD
    baseline: BaselineContract = BaselineContract.ZERO
    weight: float = 1.0
    muted: bool = False
    solo: bool = False
    mask: Tuple[str, ...] = ()
    route_hint: str = "AUTO"
    variables: Tuple[Mapping[str, object], ...] = ()

    def __post_init__(self):
        if not self.layer_id.strip() or not self.effect_id.strip():
            raise ValueError("Stack layers require stable layer and effect IDs.")
        if not self.channel.strip() or not self.expression.strip():
            raise ValueError("Stack layers require a channel and expression.")
        weight = float(self.weight)
        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError("Stack layer weight must be finite and non-negative.")
        object.__setattr__(self, "blend", BlendMode(self.blend))
        object.__setattr__(self, "baseline", BaselineContract(self.baseline))
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "mask", tuple(str(value) for value in self.mask))
        object.__setattr__(self, "variables", tuple(dict(value) for value in self.variables))

    def to_dict(self):
        return {
            "layer_id": self.layer_id, "effect_id": self.effect_id,
            "label": self.label, "channel": self.channel,
            "expression": self.expression, "blend": self.blend.value,
            "baseline": self.baseline.value, "weight": self.weight,
            "muted": self.muted, "solo": self.solo,
            "mask": list(self.mask), "route_hint": self.route_hint,
            "variables": [dict(value) for value in self.variables],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]):
        return cls(**dict(
            value,
            mask=tuple(value.get("mask", ())),
            variables=tuple(value.get("variables", ())),
        ))


@dataclass(frozen=True)
class MotionStack:
    stack_id: str
    layers: Tuple[StackLayer, ...] = ()
    schema_version: int = 1

    def __post_init__(self):
        if not self.stack_id.strip():
            raise ValueError("A Motion Stack requires a stable stack ID.")
        object.__setattr__(self, "layers", tuple(self.layers))

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "stack_id": self.stack_id,
            "layers": [layer.to_dict() for layer in self.layers],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]):
        return cls(
            stack_id=str(value["stack_id"]),
            layers=tuple(StackLayer.from_dict(item) for item in value.get("layers", ())),
            schema_version=int(value.get("schema_version", 1)),
        )


def validate_stack(motion: MotionStack) -> None:
    seen = set()
    for layer in motion.layers:
        if layer.layer_id in seen:
            raise ValueError("Motion Stack layer IDs must be unique.")
        seen.add(layer.layer_id)
        if layer.channel.startswith("rotation") and layer.blend in {BlendMode.MAX, BlendMode.MIN}:
            raise ValueError("Rotation channels do not support MAX or MIN blending.")
        if layer.channel.startswith("scale") and layer.blend is BlendMode.ADD:
            raise ValueError("Scale layers must use MULTIPLY or REPLACE composition.")


def resolve_active_layers(motion: MotionStack) -> Tuple[StackLayer, ...]:
    validate_stack(motion)
    candidates = tuple(layer for layer in motion.layers if not layer.muted and layer.weight > 0.0)
    solo = tuple(layer for layer in candidates if layer.solo)
    return solo or candidates
