"""Bounded, categorized discovery for active Driver Espresso targets."""

from __future__ import annotations

import re

import bpy

from ..motion import applied_motion
from ...generated import attachments as generated_attachments
from . import target_memory


#: Where a driver this add-on applied files itself when the bounded node
#: walks did not already claim it -- typically one nested inside a material.
APPLIED_CATEGORY = "Applied Effects"

CATEGORY_ORDER = {
    "Transforms": 0, "Custom Properties": 1, "Object Properties": 2,
    "Data Properties": 3, "Shape Keys": 4, "Shader Nodes (Selected)": 5,
    "Geometry Nodes (Selected)": 6, "Compositor Nodes (Selected)": 7,
    APPLIED_CATEGORY: 8,
    "Other": 9,
}
CATEGORY_ICONS = {
    "Transforms": "ORIENTATION_GLOBAL", "Custom Properties": "PROPERTIES",
    "Object Properties": "OBJECT_DATA", "Data Properties": "OUTLINER_DATA_MESH",
    "Shape Keys": "SHAPEKEY_DATA", "Shader Nodes (Selected)": "NODE_MATERIAL",
    "Geometry Nodes (Selected)": "GEOMETRY_NODES",
    "Compositor Nodes (Selected)": "NODE_COMPOSITING",
    APPLIED_CATEGORY: "CHECKMARK", "Other": "DRIVER",
}
NODE_TREE_CATEGORIES = {
    "ShaderNodeTree": "Shader Nodes (Selected)",
    "GeometryNodeTree": "Geometry Nodes (Selected)",
    "CompositorNodeTree": "Compositor Nodes (Selected)",
}
_AXES = "XYZW"
_CUSTOM_PROPERTY_RE = re.compile(r'\["([^\"]+)"\]')


# Delta channels belong under Transforms too: an artist looking for what is
# driving their camera should find the operator's shake next to the dolly, not
# filed away under "Object Properties".
_TRANSFORM_PATHS = frozenset({
    "location", "rotation_euler", "rotation_quaternion", "scale",
    "delta_location", "delta_rotation_euler", "delta_rotation_quaternion",
    "delta_scale",
})


def _drivers(owner):
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        return []
    return [fcurve for fcurve in animation_data.drivers if getattr(fcurve, "driver", None)]


def _object_category(fcurve):
    path = fcurve.data_path
    if path in _TRANSFORM_PATHS:
        return "Transforms"
    if path.startswith('["') or ("pose.bones[" in path and '["' in path):
        return "Custom Properties"
    if path.startswith("modifiers["):
        return "Geometry Nodes (Selected)"
    return "Object Properties"


def _owned_modifier_socket(owner, fcurve):
    """True when a generated effect owns the modifier behind this socket."""
    path = str(getattr(fcurve, "data_path", "") or "")
    if not path.startswith("modifiers["):
        return False
    for modifier in getattr(owner, "modifiers", ()) or ():
        try:
            if (generated_attachments.identity(owner, modifier).get("resource_id")
                    and path.startswith(modifier.path_from_id() + '["')):
                return True
        except (AttributeError, ReferenceError, TypeError):
            continue
    return False


def _nice_label(fcurve, node_name=""):
    path = fcurve.data_path
    match = _CUSTOM_PROPERTY_RE.findall(path)
    if match:
        text = match[-1]
    elif path in _TRANSFORM_PATHS:
        text = path.replace("_", " ").title()
    else:
        text = path
    if fcurve.array_index >= 0:
        axis = _AXES[fcurve.array_index] if fcurve.array_index < len(_AXES) else str(fcurve.array_index)
        text += f" {axis}"
    return f"{node_name} · {text}" if node_name else text


def _descriptor(owner, fcurve, category, node_name="", owner_record=None):
    item = target_memory.serialize_target(
        owner, fcurve.data_path, fcurve.array_index, owner_record=owner_record,
    )
    if not item:
        return None
    item.update({
        "category": category, "icon": CATEGORY_ICONS.get(category, "DRIVER"),
        "label": f"{category}  |  {_nice_label(fcurve, node_name)}",
    })
    return item


def _selected_node_sources(context, active_object):
    sources, seen = [], set()

    def add(tree, category):
        if tree is None:
            return
        pointer = tree.as_pointer() if hasattr(tree, "as_pointer") else id(tree)
        key = (pointer, category)
        if key in seen:
            return
        seen.add(key)
        selected = [node for node in getattr(tree, "nodes", []) if getattr(node, "select", False)]
        if selected:
            sources.append((tree, category, selected))

    for slot in getattr(active_object, "material_slots", []) if active_object is not None else []:
        material = getattr(slot, "material", None)
        add(getattr(material, "node_tree", None), "Shader Nodes (Selected)")
    for modifier in getattr(active_object, "modifiers", []) if active_object is not None else []:
        if getattr(modifier, "type", "") == "NODES":
            add(getattr(modifier, "node_group", None), "Geometry Nodes (Selected)")
    screen = getattr(context, "screen", None)
    for area in getattr(screen, "areas", []) if screen is not None else []:
        if getattr(area, "type", "") != "NODE_EDITOR":
            continue
        space = getattr(getattr(area, "spaces", None), "active", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
        category = NODE_TREE_CATEGORIES.get(getattr(space, "tree_type", ""))
        if category is None:
            continue
        add(tree, category)
    return sources


def _node_fcurves(tree, selected_nodes):
    prefixes = []
    for node in selected_nodes:
        try:
            prefixes.append((node.path_from_id(), node.name))
        except Exception:
            continue
    for fcurve in _drivers(tree):
        for prefix, node_name in prefixes:
            if fcurve.data_path == prefix or fcurve.data_path.startswith(prefix + ".") or fcurve.data_path.startswith(prefix + "["):
                yield fcurve, node_name
                break



def _selected_bone_filter(context, active_object):
    """Bone names to keep, or None to keep everything.

    An armature owns every one of its bones' drivers, so a rig with drivers on
    a hundred bones puts a hundred times its channel count into this list -
    measured on a production rig, 387 rows, none of which the artist was looking
    for. In Pose Mode the selection already says which bones they mean, so
    honour it.

    Returns None outside Pose Mode, or when nothing is selected: an empty list
    there would blank the panel, which reads as broken rather than as filtered.
    """
    if getattr(context, "mode", "") != "POSE":
        return None
    if active_object is None or getattr(active_object, "type", "") != "ARMATURE":
        return None
    selected = getattr(context, "selected_pose_bones", None)
    if not selected:
        return None
    return {bone.name for bone in selected}


def _bone_name_in_path(data_path):
    """The bone a driver belongs to, or None for an object-level driver."""
    marker = 'pose.bones["'
    start = data_path.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = data_path.find('"]', start)
    return data_path[start:end] if end > start else None


def collect_driver_targets(context):
    """Collect active-object drivers plus drivers on selected shader/GN nodes."""
    active_object = getattr(context, "active_object", None)
    items, seen = [], set()
    owner_records = {}

    def serialized_owner(owner):
        pointer = owner.as_pointer() if hasattr(owner, "as_pointer") else id(owner)
        if pointer not in owner_records:
            owner_records[pointer] = target_memory.serialize_owner(owner)
        return owner_records[pointer]

    def append(owner, fcurve, category, node_name=""):
        item = _descriptor(
            owner, fcurve, category, node_name,
            owner_record=serialized_owner(owner),
        )
        if item is None:
            return
        key = (item["id_type"], item["id_name"], item["owner_path"], item["data_path"], item["index"])
        if key not in seen:
            seen.add(key)
            items.append(item)

    keep_bones = _selected_bone_filter(context, active_object)
    if active_object is not None:
        for fcurve in _drivers(active_object):
            if _owned_modifier_socket(active_object, fcurve):
                continue
            if keep_bones is not None:
                bone = _bone_name_in_path(getattr(fcurve, "data_path", ""))
                # Object-level drivers stay visible either way - there are few of
                # them and they are not what makes the list unusable.
                if bone is not None and bone not in keep_bones:
                    continue
            append(active_object, fcurve, _object_category(fcurve))
        data = getattr(active_object, "data", None)
        for fcurve in _drivers(data):
            append(data, fcurve, "Data Properties")
        tree = getattr(data, "node_tree", None)
        if tree is not None:
            category = NODE_TREE_CATEGORIES.get(
                getattr(tree, "bl_idname", ""), "Other",
            )
            for fcurve in _drivers(tree):
                append(tree, fcurve, category)
        shape_keys = getattr(data, "shape_keys", None)
        for fcurve in _drivers(shape_keys):
            append(shape_keys, fcurve, "Shape Keys")
    for tree, category, selected_nodes in _selected_node_sources(context, active_object):
        for fcurve, node_name in _node_fcurves(tree, selected_nodes):
            append(tree, fcurve, category, node_name)
    _append_applied_effect_targets(active_object, append)
    return sorted(items, key=lambda item: (
        CATEGORY_ORDER.get(item["category"], 99), item["label"].lower(), item["index"],
    ))


def _append_applied_effect_targets(active_object, append):
    """Drivers this add-on applied, wherever on the object they live.

    The node walks above are deliberately bounded to SELECTED nodes, because a
    material can carry hundreds of drivers and a list of all of them is not a
    list. That bound is right, and it also hid effects the add-on applied
    itself: a recipe whose controls sit in a group NESTED inside the material
    never appeared unless the artist happened to have a node selected in it.
    The heading count and these rows must come from the same walk, or the panel
    can show "Active Effects (1):" over an empty list.

    A SINGLE_PROPERTY effect earns no row of its own in `rows_for_targets`;
    it is meant to appear through its targets. So the targets have to be
    findable, and a recorded effect names its own driver paths exactly. This
    adds those and nothing else: the bound stays, and the thing the add-on put
    there stops hiding behind it.
    """
    if active_object is None:
        return
    # Hosts the ordinary walk above already visits are LEFT ALONE. Their rows
    # are filtered on purpose -- Pose Mode shows only the selected bones,
    # because an armature with drivers on a hundred bones is not a list -- and
    # adding recorded drivers back here would override that and put all of
    # them on screen again. Measured: it did, and the bone-selection suite
    # caught it. Only the hosts the bounded walk never reaches are added.
    walked = {id(active_object)}
    data = getattr(active_object, "data", None)
    for covered in (data, getattr(data, "node_tree", None),
                    getattr(data, "shape_keys", None)):
        if covered is not None:
            walked.add(id(covered))
    for host, _label, records in applied_motion.collect(
            applied_motion.hosts_for_object(active_object), validate=True):
        if id(host) in walked:
            continue
        by_path = {}
        for fcurve in _drivers(host):
            by_path.setdefault(fcurve.data_path, []).append(fcurve)
        if not by_path:
            continue
        for record in records:
            for data_path, index in applied_motion.paths_of(record):
                for fcurve in by_path.get(data_path, ()):
                    if index >= 0 and fcurve.array_index != index:
                        continue
                    # Not one of the "(Selected)" node categories: those say
                    # the artist chose to look at that node, and this row
                    # exists precisely because they did not have to. `append`
                    # de-duplicates, so a driver the walks above already
                    # collected keeps the category it was given there.
                    append(host, fcurve, APPLIED_CATEGORY)


LAST_CATEGORY = "Last Applied"
SCENE_CATEGORY = "Scene Applied"


def compact_host_label(label):
    """Short datablock-kind prefixes for the width-constrained manager UI."""
    text = str(label or "")
    return "OBJ:" + text[len("Object:"):] if text.startswith("Object:") else text


def _scene_channel_targets(host, record):
    """Resolve a stamped effect's real channels for the scene-wide tree."""
    rows = []
    drivers = _drivers(host)
    for data_path, index in applied_motion.paths_of(record):
        fcurve = next((curve for curve in drivers
                       if curve.data_path == data_path
                       and (index < 0 or curve.array_index == index)), None)
        if fcurve is None:
            continue
        if isinstance(host, bpy.types.Object):
            category = _object_category(fcurve)
        elif isinstance(host, bpy.types.NodeTree):
            category = NODE_TREE_CATEGORIES.get(getattr(host, "bl_idname", ""), "Other")
        elif isinstance(host, bpy.types.Key):
            category = "Shape Keys"
        else:
            category = "Data Properties"
        row = _descriptor(host, fcurve, category)
        if row is not None:
            rows.append(row)
    return rows


def targets_from_entry(entry):
    """Build picker rows from a remembered apply entry.

    The memory record and the picker row share a shape already - both come from
    ``target_memory.serialize_target`` - so this only adds the display fields
    and marks any target whose object no longer exists.
    """
    rows = []
    for target in (entry or {}).get("targets") or []:
        if not isinstance(target, dict) or not target.get("data_path"):
            continue
        resolved, _reason = target_memory.resolve_target_record(target)
        index = int(target.get("index", -1))
        suffix = f"[{index}]" if index >= 0 else ""
        owner = target.get("id_name") or "?"
        row = dict(target)
        row.update({
            "category": LAST_CATEGORY,
            "icon": "FILE_REFRESH" if resolved else "ERROR",
            "label": (
                f"{LAST_CATEGORY}  |  {owner} > {target['data_path']}{suffix}"
                + ("" if resolved else "  (missing)")
            ),
        })
        rows.append(row)
    return rows


def scene_targets(scene):
    """Picker rows for every applied Espresso effect in the current scene.

    Scene scope is intentionally stamp-driven rather than a raw "every driver in
    the file" crawl: Active already covers honest unstamped discovery for the
    thing the artist is looking at, while Scene is the wide management view for
    named applied effects.
    """
    rows = []
    collected = []
    seen_hosts = set()
    for host, host_label, records in applied_motion.collect_for_objects(
            tuple(getattr(scene, "objects", ()) or ())):
        key = id(host)
        if key in seen_hosts:
            continue
        seen_hosts.add(key)
        collected.append((host, host_label, records))
    for host, host_label, records in applied_motion.collect_for_scene(scene):
        key = id(host)
        if key in seen_hosts:
            continue
        seen_hosts.add(key)
        collected.append((host, host_label, records))

    for host, host_label, records in collected:
        for record in records:
            entry = (
                (record.get("extras") if isinstance(record.get("extras"), dict) else {}).get("target_entry")
                or {}
            )
            # The effect record's paths are authoritative and let Scene expose
            # every child of a motion set even when its legacy target-memory
            # entry is absent.  Use that entry only for old records whose paths
            # cannot be resolved directly.
            targets = _scene_channel_targets(host, record) or targets_from_entry(entry)
            for target in targets:
                row = dict(target)
                row.update({
                    "category": SCENE_CATEGORY,
                    "icon": "SCENE_DATA",
                    # Scene scope and the effect name are already visible in
                    # the selected tab and parent group.  Keep only the real
                    # destination identity here so repeated context does not
                    # crowd out the property an artist is trying to manage.
                    "label": "%s > %s" % (
                        compact_host_label(host_label), target.get("label", "Channel"),
                    ),
                })
                rows.append(row)
    return rows


def panel_targets(context, props):
    """Targets the Driver Target list should show, honouring the Active/Last toggle.

    Both the panel draw and the deferred re-sync timer call this. If only the
    draw honoured the toggle, the timer would repopulate the list from the
    active object a frame later and silently undo the switch.
    """
    if getattr(props, "driver_target_source", "ACTIVE") == "LAST":
        return targets_from_entry(target_memory.latest_entry(props))
    if getattr(props, "driver_target_source", "ACTIVE") == "SCENE":
        return scene_targets(getattr(context, "scene", None))
    return collect_driver_targets(context)


def target_signature(items):
    return "|".join(
        f"{item.get('id_type','')}:{item.get('id_name','')}:{item.get('owner_path','')}:{item.get('data_path','')}:{item.get('index',-1)}:{item.get('category','')}"
        for item in items
    )


def resolve_driver(descriptor):
    resolved, _reason = target_memory.resolve_target_record(descriptor)
    if resolved is None:
        return None
    animation_data = getattr(resolved["owner"], "animation_data", None)
    if animation_data is None:
        return None
    return animation_data.drivers.find(resolved["data_path"], index=resolved["index"])


def descriptor_from_item(item):
    return {
        "id_type": item.owner_id_type, "id_name": item.owner_id_name,
        "owner_path": item.owner_path, "data_path": item.data_path,
        "index": item.array_index, "category": item.category,
        "label": item.label, "icon": item.icon,
    }
