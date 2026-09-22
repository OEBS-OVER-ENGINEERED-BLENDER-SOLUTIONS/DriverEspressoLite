"""Lifecycle adapters for generated materials and future Blender datablocks."""

from __future__ import annotations

import bpy

from ..core.contracts import Ownership, ResourceKind
from ..core.registry import stamp_resource


def create_mesh(name: str, *, resource_id: str, setup_id: str = "",
                effect_id: str = "",
                ownership: Ownership = Ownership.EXCLUSIVE,
                role: str = ""):
    value = bpy.data.meshes.new(name)
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.DATABLOCK,
        ownership=ownership, role=role,
    )


def remove_mesh(value, *, require_unused: bool = True) -> bool:
    if value is None or value.name not in bpy.data.meshes:
        return False
    if require_unused and value.users:
        return False
    bpy.data.meshes.remove(value)
    return True


def create_curve(name: str, curve_type: str = "CURVE", *, resource_id: str,
                 setup_id: str = "", effect_id: str = "",
                 ownership: Ownership = Ownership.EXCLUSIVE,
                 role: str = ""):
    """Create an owned Curve-family datablock through the generated boundary."""
    value = bpy.data.curves.new(name, str(curve_type))
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.DATABLOCK,
        ownership=ownership, role=role,
    )


def remove_curve(value, *, require_unused: bool = True) -> bool:
    if value is None or value.name not in bpy.data.curves:
        return False
    if require_unused and value.users:
        return False
    bpy.data.curves.remove(value)
    return True


def create_object(name: str, data, *, resource_id: str, setup_id: str = "",
                  effect_id: str = "",
                  ownership: Ownership = Ownership.EXCLUSIVE,
                  role: str = "", target_collection=None):
    value = bpy.data.objects.new(name, data)
    if target_collection is not None:
        target_collection.objects.link(value)
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.OBJECT,
        ownership=ownership, role=role,
    )


def create_detached_object(name: str, data, *, target_collection=None):
    """Create an ordinary artist-owned object for a realized bake result."""
    value = bpy.data.objects.new(name, data)
    if target_collection is not None:
        target_collection.objects.link(value)
    return value


def create_detached_mesh_from_object(value):
    """Snapshot evaluated geometry without assigning Espresso ownership."""
    return bpy.data.meshes.new_from_object(value)


def remove_object(value, *, require_unused: bool = False) -> bool:
    if value is None or value.name not in bpy.data.objects:
        return False
    if require_unused and value.users > 1:
        return False
    bpy.data.objects.remove(value, do_unlink=True)
    return True


def create_collection(name: str, *, resource_id: str, setup_id: str = "",
                      effect_id: str = "",
                      ownership: Ownership = Ownership.EXCLUSIVE,
                      role: str = "", target_collection=None):
    """Create an owned collection without forcing it into the scene tree.

    Source-palette collections are deliberately left unlinked: Geometry Nodes
    can reference them directly, while the artist's Outliner remains clean.
    """
    value = bpy.data.collections.new(name)
    if target_collection is not None:
        target_collection.children.link(value)
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.DATABLOCK,
        ownership=ownership, role=role,
    )


def create_detached_collection(name: str, *, target_collection=None):
    """Create an ordinary output collection that Espresso will not own.

    Bake results deliberately cross the generated/live boundary. They still use
    this lifecycle service, but receive no ownership stamp so they survive
    cleanup and add-on removal as normal artist data.
    """
    value = bpy.data.collections.new(name)
    if target_collection is not None:
        target_collection.children.link(value)
    return value


def remove_collection(value, *, require_unused: bool = False) -> bool:
    if value is None or value.name not in bpy.data.collections:
        return False
    if require_unused and value.users:
        return False
    bpy.data.collections.remove(value)
    return True


def create_material(name: str, *, resource_id: str, setup_id: str = "",
                    effect_id: str = "",
                    ownership: Ownership = Ownership.EXCLUSIVE,
                    role: str = "", use_nodes: bool = False):
    value = bpy.data.materials.new(name)
    value.use_nodes = bool(use_nodes)
    return stamp_resource(
        value, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.MATERIAL,
        ownership=ownership, role=role,
    )


def remove_material(value, *, require_unused: bool = True) -> bool:
    if value is None or value.name not in bpy.data.materials:
        return False
    if require_unused and value.users:
        return False
    bpy.data.materials.remove(value)
    return True
