"""Managed helper collections and Empty objects."""

from __future__ import annotations

import bpy

from ..core.contracts import Ownership, ResourceKind
from ..core.registry import stamp_resource


COLLECTION_NAME = "__Driver Espresso Helpers"


def collection(scene, name: str = COLLECTION_NAME):
    value = bpy.data.collections.get(name)
    if value is None:
        value = bpy.data.collections.new(name)
        scene.collection.children.link(value)
    elif not any(child is value for child in scene.collection.children):
        scene.collection.children.link(value)
    return value


def create_empty(scene, name: str, *, resource_id: str, setup_id: str = "",
                 effect_id: str = "",
                 ownership: Ownership = Ownership.EXCLUSIVE,
                 role: str = "", parent=None, preserve_world: bool = True,
                 target_collection=None):
    helper = bpy.data.objects.new(name, None)
    (target_collection or collection(scene)).objects.link(helper)
    stamp_resource(
        helper, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.OBJECT,
        ownership=ownership, role=role,
    )
    if parent is not None:
        world = helper.matrix_world.copy()
        helper.parent = parent
        if preserve_world:
            helper.matrix_parent_inverse = parent.matrix_world.inverted()
            helper.matrix_world = world
    return helper


def remove_empty(helper, *, require_unused: bool = False) -> bool:
    if helper is None or helper.name not in bpy.data.objects:
        return False
    if require_unused and helper.users > 1:
        return False
    bpy.data.objects.remove(helper, do_unlink=True)
    return True


def remove_collection(value, *, require_empty: bool = True) -> bool:
    if value is None or value.name not in bpy.data.collections:
        return False
    if require_empty and (value.objects or value.children):
        return False
    bpy.data.collections.remove(value)
    return True
