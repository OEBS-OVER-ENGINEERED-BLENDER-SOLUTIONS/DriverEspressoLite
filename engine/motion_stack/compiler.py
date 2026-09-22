"""Plan Motion Stack delivery without mutating Blender data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import Mapping, Tuple

try:
    from .stack import BlendMode, MotionStack, StackLayer, resolve_active_layers
except ImportError:  # Direct-file loading keeps schema tests independent of bpy.
    from espresso_mgx_stack import BlendMode, MotionStack, StackLayer, resolve_active_layers


class CompilationRoute(str, Enum):
    DIRECT_DRIVER = "DIRECT_DRIVER"
    NATIVE_HELPER = "NATIVE_HELPER"


@dataclass(frozen=True)
class CompiledChannel:
    channel: str
    expression: str
    route: CompilationRoute
    layer_ids: Tuple[str, ...]
    helper_layers: Tuple[str, ...] = ()
    variables: Tuple[Mapping[str, object], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class CompilationPlan:
    stack_id: str
    channels: Tuple[CompiledChannel, ...]


def _weighted(layer: StackLayer) -> str:
    expression = layer.expression
    if layer.weight == 1.0:
        return expression
    if layer.blend is BlendMode.MULTIPLY:
        return "(1+(%s-1)*%g)" % (expression, layer.weight)
    return "((%s)*%g)" % (expression, layer.weight)


def _compose(layers: Tuple[StackLayer, ...]) -> str:
    result = _weighted(layers[0])
    for layer in layers[1:]:
        value = _weighted(layer)
        if layer.blend is BlendMode.REPLACE:
            result = value
        elif layer.blend is BlendMode.ADD:
            result = "((%s)+(%s))" % (result, value)
        elif layer.blend is BlendMode.MULTIPLY:
            result = "((%s)*(%s))" % (result, value)
        elif layer.blend is BlendMode.MAX:
            result = "max((%s),(%s))" % (result, value)
        elif layer.blend is BlendMode.MIN:
            result = "min((%s),(%s))" % (result, value)
    return result


def _compose_aliases(layers: Tuple[StackLayer, ...]) -> str:
    aliased = tuple(
        StackLayer(
            layer_id=layer.layer_id,
            effect_id=layer.effect_id,
            label=layer.label,
            channel=layer.channel,
            expression="s%d" % index,
            blend=layer.blend,
            baseline=layer.baseline,
            weight=layer.weight,
            muted=layer.muted,
            solo=layer.solo,
            mask=layer.mask,
            route_hint=layer.route_hint,
            variables=layer.variables,
        )
        for index, layer in enumerate(layers)
    )
    return _compose(aliased)


def _alias_variables(layers: Tuple[StackLayer, ...]):
    """Give every layer variable a short, collision-free target-driver name."""
    aliased_layers = []
    variables = []
    for layer_index, layer in enumerate(layers):
        expression = layer.expression
        layer_variables = []
        for variable_index, binding in enumerate(layer.variables):
            old_name = str(binding.get("name") or "var")
            new_name = "e%dv%d" % (layer_index, variable_index)
            expression = re.sub(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(old_name),
                new_name,
                expression,
            )
            renamed = dict(binding)
            renamed["name"] = new_name
            layer_variables.append(renamed)
            variables.append(renamed)
        aliased_layers.append(replace(
            layer,
            expression=expression,
            variables=tuple(layer_variables),
        ))
    return tuple(aliased_layers), tuple(variables)


def compile_stack(motion: MotionStack, *, expression_limit: int = 255) -> CompilationPlan:
    active = resolve_active_layers(motion)
    grouped = {}
    order = []
    for layer in active:
        if layer.channel not in grouped:
            grouped[layer.channel] = []
            order.append(layer.channel)
        grouped[layer.channel].append(layer)
    channels = []
    for channel in order:
        layers = tuple(grouped[channel])
        aliased_layers, variables = _alias_variables(layers)
        expression = _compose(aliased_layers)
        route = CompilationRoute.DIRECT_DRIVER
        helpers = ()
        reason = (
            "Composed expression and aliased variables fit Blender's driver budget."
            if variables else "Composed expression fits Blender's driver budget."
        )
        if len(expression) > int(expression_limit):
            route = CompilationRoute.NATIVE_HELPER
            helpers = tuple(layer.layer_id for layer in layers)
            expression = _compose_aliases(layers)
            variables = ()
            reason = "Composed expression exceeds the direct-driver budget."
        channels.append(CompiledChannel(
            channel=channel,
            expression=expression,
            route=route,
            layer_ids=tuple(layer.layer_id for layer in layers),
            helper_layers=helpers,
            variables=variables,
            reason=reason,
        ))
    return CompilationPlan(motion.stack_id, tuple(channels))
