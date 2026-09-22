"""Generated Action ownership helpers."""

from __future__ import annotations

import bpy

from ..core.contracts import Ownership, ResourceKind
from ..core.registry import stamp_resource


def create_action(name: str, *, resource_id: str, setup_id: str = "",
                  effect_id: str = "",
                  ownership: Ownership = Ownership.EXCLUSIVE,
                  role: str = ""):
    value = bpy.data.actions.new(name)
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.ACTION,
        ownership=ownership, role=role,
    )


def create_detached_channelbag(action, owner, *, layer_name="Animation"):
    """Create the Blender 4.4+ layered-Action container for ``owner``.

    Driver Espresso supports Blender 5.x, where F-curves live in a channel bag
    rather than directly on ``Action.fcurves``.  Keeping the compatibility
    detail here prevents generated systems from each inventing their own Action
    setup.
    """
    slot = action.slots.new(owner.id_type, owner.name)
    layer = action.layers.new(layer_name)
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    return bag, slot


def assign_action(owner, action, slot):
    """Attach a fully-built layered Action to its owner.

    Builders can populate a detached Action first, then make this single swap.
    That keeps replacement transactional: a failed build cannot destroy the
    owner's previous Action.
    """
    animation_data = owner.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = slot


def create_channelbag(action, owner, *, layer_name="Animation"):
    """Create and immediately attach a Blender 4.4+ channel bag."""
    bag, slot = create_detached_channelbag(action, owner, layer_name=layer_name)
    assign_action(owner, action, slot)
    return bag


def action_containers(action):
    """``(container, curve)`` for every F-curve, whichever storage is in use.

    The container is the thing that OWNS the curve and can remove it: a
    channelbag on 4.4+, the Action itself on 4.2. Both expose `.fcurves`, so a
    caller can write `container.fcurves.remove(curve)` on either.

    Three Blenders, three shapes:

        4.2        Action.fcurves, no layers at all
        4.4 / 4.5  both, and they report the same curves
        5.0 / 5.1  layers only -- Action.fcurves is gone

    Layers are preferred where they exist, because on 5.x they are the only
    truth. `Action.fcurves` is the fallback, which is what makes 4.2 work:
    reading it through `getattr(action, "layers", ())` returns an empty tuple
    there, so every caller that did that silently found no curves instead of
    failing -- a bake that quietly did nothing rather than one that reported
    it could not run.
    """
    if action is None:
        return []
    pairs = []
    for layer in getattr(action, "layers", ()) or ():
        for strip in getattr(layer, "strips", ()) or ():
            for bag in getattr(strip, "channelbags", ()) or ():
                pairs.extend((bag, curve) for curve in bag.fcurves)
    if pairs:
        return pairs
    return [(action, curve) for curve in (getattr(action, "fcurves", ()) or ())]


def action_fcurves(action):
    """Every F-curve in an Action, whichever storage this Blender uses."""
    return [curve for _container, curve in action_containers(action)]


def ensure_fcurve(action, owner, data_path, *, index=0, group_name=""):
    """Find or create one F-curve on ``action`` for ``owner``.

    `Action.fcurve_ensure_for_datablock` arrived with slotted Actions in 4.4.
    On 4.2 the equivalent is `Action.fcurves.new`, which needs the curve to
    not already exist -- so it is searched for first.
    """
    index = max(0, int(index))
    if hasattr(action, "fcurve_ensure_for_datablock"):
        return action.fcurve_ensure_for_datablock(
            owner, data_path, index=index, group_name=group_name or "Driver Espresso",
        )
    for curve in getattr(action, "fcurves", ()) or ():
        if curve.data_path == data_path and int(curve.array_index) == index:
            return curve
    return action.fcurves.new(
        data_path, index=index, action_group=group_name or "Driver Espresso")


def remove_action(value, *, require_unused: bool = True) -> bool:
    if value is None or value.name not in bpy.data.actions:
        return False
    if require_unused and value.users:
        return False
    bpy.data.actions.remove(value)
    return True
