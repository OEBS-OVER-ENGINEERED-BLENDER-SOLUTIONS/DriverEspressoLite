"""Which property "apply to my selection" writes to.

Applying one template across many objects needs to know what to drive. The artist
right-clicks the property they mean and chooses "Use as Espresso Target", the same way
they nominate an input; the choice is remembered on the scene. Only the route to the
property is stored (owner kind, data path and index), never a pointer to one object's
property, so the same route resolved against each selected object gives every object its
own driver. When nothing is nominated, lights drive their Power.
"""

from __future__ import annotations

import json


# Owner kinds a target route can name. A route names a kind rather than the datablock
# that was clicked, so it can be resolved against a different object.
OWNER_OBJECT = "OBJECT"
OWNER_DATA = "DATA"
OWNER_MATERIAL = "MATERIAL"
OWNER_NODE_TREE = "NODE_TREE"

# What a light gets when nothing has been nominated.
LIGHT_DEFAULT = {
    "owner": OWNER_DATA,
    "data_path": "energy",
    "index": -1,
    "label": "Light > Power",
    "material_index": 0,
}


def describe(owner, owner_object, data_path, index):
    """A route to ``owner``'s property that can be resolved on another object.

    Returns None when the owner is not something with an equivalent elsewhere,
    rather than inventing a route that would resolve to nothing.

    A button inside a datablock (a node socket, a modifier, a constraint) hands
    over that inner struct as its owner; the route is then the struct's path
    from its datablock, so it can be resolved against another object's own.
    """
    root = getattr(owner, "id_data", None)
    if root is not None and root != owner:
        try:
            data_path = owner.path_from_id(data_path)
        except (AttributeError, TypeError, ValueError):
            return None
        owner = root
    if owner == owner_object:
        kind, material_index = OWNER_OBJECT, 0
    elif owner == getattr(owner_object, "data", None):
        kind, material_index = OWNER_DATA, 0
    else:
        kind, material_index = None, 0
        materials = list(
            getattr(getattr(owner_object, "data", None), "materials", None) or ())
        for slot, material in enumerate(materials):
            if owner == material:
                kind, material_index = OWNER_MATERIAL, slot
                break
            if owner == getattr(material, "node_tree", None):
                kind, material_index = OWNER_NODE_TREE, slot
                break
        if kind is None:
            return None

    return {
        "owner": kind,
        "data_path": data_path,
        "index": int(index),
        "material_index": material_index,
        "label": _label_for(kind, owner, data_path, index),
    }


def _label_for(kind, owner, data_path, index):
    """Something readable for the panel: the route, not the value. A node socket's data
    path is unreadable as-is (``nodes["Principled BSDF"].inputs[28].default_value``),
    so the socket is resolved and asked its name rather than having one parsed out of
    the path.
    """
    readable = data_path
    if data_path.startswith("nodes[") and data_path.endswith(".default_value"):
        try:
            socket = owner.path_resolve(data_path.rsplit(".", 1)[0])
            node_name = data_path.split('"')[1]
            readable = "%s > %s" % (node_name, socket.name)
        except Exception:
            pass                    # fall back to the raw path, still usable
    suffix = "" if index < 0 else "[%d]" % index
    return "%s > %s%s" % (kind.replace("_", " ").title(), readable, suffix)


def resolve(entry, obj):
    """The datablock on ``obj`` this route names, or None with a reason."""
    if not entry:
        return None, "no target chosen"

    kind = entry.get("owner")
    if kind == OWNER_OBJECT:
        return obj, ""
    if kind == OWNER_DATA:
        data = getattr(obj, "data", None)
        return (data, "") if data is not None else (None, "has no object data")

    slot = int(entry.get("material_index", 0))
    materials = list(getattr(getattr(obj, "data", None), "materials", None) or ())
    if slot >= len(materials) or materials[slot] is None:
        return None, "has no material in slot %d" % slot
    material = materials[slot]
    if kind == OWNER_MATERIAL:
        return material, ""
    tree = getattr(material, "node_tree", None)
    return (tree, "") if tree is not None else (None, "material has no nodes")


def targets_for(entry, obj):
    """``ButtonDriverTarget``s for this route on one object, and why not."""
    from ...engine.targeting.button_targeting import ButtonDriverTarget

    owner, reason = resolve(entry, obj)
    if owner is None:
        return [], reason
    try:
        owner.path_resolve(entry["data_path"])
    except Exception:
        return [], "has no %s" % entry["data_path"]
    # The object is named here rather than inferred. For a material target the owner is
    # a node tree, and which object it belongs to is ambiguous when the material is
    # shared, which is exactly when applying across a selection happens.
    return [ButtonDriverTarget(
        owner, entry["data_path"], entry["index"], owner_object=obj)], ""


def is_light_selection(objects):
    """Whether every selected object is a light."""
    objects = list(objects)
    return bool(objects) and all(
        getattr(obj, "type", "") == "LIGHT" for obj in objects)


def effective_entry(props, objects):
    """The route the PANEL button uses for this selection.

    Lights get Power, and they get it even when a target has been nominated.
    That is deliberate rather than a fallback: a light has essentially one
    thing worth driving, so asking which is a step that never changes the
    answer - and a target nominated for a set of meshes should not silently
    redirect what happens when lamps are selected next.

    Anything else needs a nominated target, because there is no property that
    can be assumed of an arbitrary object. Without one the panel button is
    disabled and the right-click route is the way in.
    """
    if is_light_selection(objects):
        return dict(LIGHT_DEFAULT), True
    chosen = read(props)
    if chosen:
        return chosen, False
    return None, False


def missing_for(entry, objects):
    """Which of ``objects`` this route does not resolve on, and why.

    Applying to some of a selection and not others leaves a half-built rig
    that looks finished, so the caller refuses outright rather than skipping.
    """
    missing = []
    for obj in objects:
        targets, reason = targets_for(entry, obj)
        if not targets:
            missing.append((obj.name, reason))
    return missing


# ------------------------------------------------------------------- storage


def read(props):
    try:
        return json.loads(getattr(props, "espresso_apply_target", "") or "{}") or None
    except ValueError:
        return None


def write(props, entry):
    props.espresso_apply_target = json.dumps(entry or {}, sort_keys=True)


def clear(props):
    props.espresso_apply_target = ""


def label(props, objects=()):
    """What the panel shows this selection will be driven on.

    No "(default)" suffix for lights: Power is not a default they could
    override from here, it is simply what the light route drives.
    """
    entry, _is_light = effective_entry(props, list(objects))
    return entry.get("label", "") if entry else ""
