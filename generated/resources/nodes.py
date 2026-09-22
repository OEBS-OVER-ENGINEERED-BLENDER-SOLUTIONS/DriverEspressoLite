"""Creation and conservative removal of Espresso node groups."""

from __future__ import annotations

import bpy

from ..core.contracts import Ownership, ResourceKind
from ..core.registry import BuilderRegistry, stamp_resource


builders = BuilderRegistry("node")


def create_group(name: str, tree_type: str, *, resource_id: str,
                 setup_id: str = "", effect_id: str = "",
                 ownership: Ownership = Ownership.EXCLUSIVE,
                 role: str = ""):
    group = bpy.data.node_groups.new(name, tree_type)
    return stamp_resource(
        group, resource_id=resource_id, setup_id=setup_id,
        effect_id=effect_id, kind=ResourceKind.NODE_GROUP,
        ownership=ownership, role=role,
    )


def remove_group(group, *, require_unused: bool = True) -> bool:
    if group is None or group.name not in bpy.data.node_groups:
        return False
    if require_unused and group.users:
        return False
    bpy.data.node_groups.remove(group)
    return True


def separate_instances(tree, geometry, *, name="Espresso Input Components",
                       location=(0, 0)):
    """Feed instance-only nodes without discarding realized input components."""
    node = tree.nodes.new("GeometryNodeSeparateComponents")
    node.name = node.label = name
    node.location = location
    tree.links.new(geometry, node.inputs["Geometry"])
    return node


def join_transformed_instances(tree, components, instances, *,
                               name="Espresso Restored Components",
                               location=(0, 0)):
    """Recombine transformed instances with untouched realized components."""
    node = tree.nodes.new("GeometryNodeJoinGeometry")
    node.name = node.label = name
    node.location = location
    for output in components.outputs:
        if output.name != "Instances" and output.type == "GEOMETRY":
            tree.links.new(output, node.inputs["Geometry"])
    tree.links.new(instances, node.inputs["Geometry"])
    return node.outputs["Geometry"]
