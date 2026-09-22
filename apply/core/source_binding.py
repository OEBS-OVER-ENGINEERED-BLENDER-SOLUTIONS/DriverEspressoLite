"""Remembered input-source binding for templates that need driver variables."""

from __future__ import annotations

import json
import re

from ...engine.targeting.button_targeting import ButtonDriverTarget
from . import target_memory
from ...engine import utils


_CLASS_TO_DRIVER_ID_TYPE = {
    "Object": "OBJECT",
    "Scene": "SCENE",
    "Material": "MATERIAL",
    "Camera": "CAMERA",
    "Light": "LIGHT",
    "World": "WORLD",
    "Collection": "COLLECTION",
    "Mesh": "MESH",
    "Curve": "CURVE",
    "Armature": "ARMATURE",
    "Lattice": "LATTICE",
    "Speaker": "SPEAKER",
    "Key": "KEY",
    "Texture": "TEXTURE",
    "ParticleSettings": "PARTICLE_SETTINGS",
}

SOURCE_ACTIVE = "ACTIVE"
SOURCE_EMPTY = "EMPTY"
SOURCE_MISSING_OWNER = "MISSING_OWNER"
SOURCE_MISSING_PROPERTY = "MISSING_PROPERTY"
SOURCE_INVALID_INDEX = "INVALID_INDEX"

ESPRESSO_INPUT_PREFIX = "espinp_"
LEGACY_INPUT_VARIABLE_NAMES = frozenset({"var", "distance", "steer", "var_idx"})


def required_variable_names(template):
    return [item["name"] for item in template.get("requires_driver_variables", [])]


def template_needs_source(template):
    return bool(required_variable_names(template))


def build_source_entry(targets, display_label):
    entry = target_memory.build_entry(targets[:1], display_label, "input_source")
    if entry:
        entry["source_kind"] = "espresso_input"
    return entry


def _read_entry(raw):
    try:
        entry = json.loads(raw or "{}")
    except Exception:
        return None
    return entry if isinstance(entry, dict) and entry.get("targets") else None


def latest_source(props):
    return _read_entry(getattr(props, "espresso_input_source", "{}"))


def remember_source(props, entry):
    props.espresso_input_source = json.dumps(entry or {}, sort_keys=True)


def clear_source(props):
    props.espresso_input_source = "{}"


def source_label(props):
    entry = latest_source(props)
    return entry.get("display_label", "") if entry else ""


def _driver_id_type(root):
    return _CLASS_TO_DRIVER_ID_TYPE.get(root.__class__.__name__, root.__class__.__name__.upper())


def _joined_data_path(owner, data_path, index):
    owner_path = target_memory._owner_path(owner)
    if owner_path:
        prop_path = owner_path + (data_path if data_path.startswith("[") else "." + data_path)
    else:
        prop_path = data_path
    if int(index) >= 0 and not prop_path.endswith(f"[{int(index)}]"):
        prop_path += f"[{int(index)}]"
    return prop_path


def _source_descriptor(entry, *, require_property):
    if not entry or not entry.get("targets"):
        return None, SOURCE_EMPTY, "No Espresso input source is selected."
    target, reason = target_memory.resolve_target_record(entry["targets"][0])
    if target is None:
        return None, SOURCE_MISSING_OWNER, (
            reason or "The Espresso input owner no longer exists."
        )
    owner = target["owner"]
    root = target_memory._target_root(owner)
    if root is None:
        return None, SOURCE_MISSING_OWNER, "The Espresso input owner no longer exists."

    data_path = target["data_path"]
    index = int(target.get("index", -1))
    if require_property:
        try:
            value = owner.path_resolve(data_path)
        except Exception:
            return None, SOURCE_MISSING_PROPERTY, (
                "The Espresso input property is missing or renamed. Right-click its "
                "replacement and choose Relink Espresso Input & Applied Drivers."
            )
        if index >= 0:
            try:
                value[index]
            except Exception:
                return None, SOURCE_INVALID_INDEX, (
                    "The remembered Espresso input channel no longer exists."
                )

    return {
        "id_type": _driver_id_type(root),
        "id": root,
        "data_path": _joined_data_path(owner, data_path, index),
        "owner": owner,
        "index": index,
    }, SOURCE_ACTIVE, ""


def source_status(entry):
    """Validate one remembered source without scanning any drivers."""
    descriptor, code, reason = _source_descriptor(entry, require_property=True)
    return {"code": code, "reason": reason, "descriptor": descriptor}


def resolve_source(entry):
    descriptor, _code, reason = _source_descriptor(entry, require_property=True)
    return descriptor, reason


def sample_source_value(entry):
    """Read the remembered property through its original RNA owner."""
    if not entry or not entry.get("targets"):
        raise ValueError("No Espresso input source is selected.")
    target, reason = target_memory.resolve_target_record(entry["targets"][0])
    if target is None:
        raise ValueError(reason or "The Espresso input source no longer exists.")
    try:
        value = target["owner"].path_resolve(target["data_path"])
        index = int(target.get("index", -1))
        return float(value[index] if index >= 0 else value)
    except (AttributeError, IndexError, KeyError, ReferenceError, TypeError, ValueError) as exc:
        raise ValueError("The Espresso input property cannot be sampled.") from exc


# Variable kinds that describe WHERE the driven thing is, rather than reading a
# value from somewhere else. A template asking for one of these is asking to be
# told its own position, which is what lets an identical expression behave
# differently on every lamp in a row.
_SELF_POSITION_TYPES = {"TRANSFORMS"}
# ...and the kinds that additionally need the artist to pick something to
# measure against.
_NEEDS_SOURCE_TYPES = {"SINGLE_PROP", "LOC_DIFF"}


def _has_centre_variable(template):
    return any(
        var_def.get("reads") == "CENTRE"
        for var_def in template.get("requires_driver_variables", []) or ()
    )


def _has_source_transform_variable(template):
    return any(
        var_def.get("type", "SINGLE_PROP") == "TRANSFORMS"
        and var_def.get("reads") == "SOURCE"
        for var_def in template.get("requires_driver_variables", []) or ()
    )


def needs_input_source(template):
    """Whether this template can't be applied until an input source is chosen.

    A template that only wants each object's OWN position is self-contained -
    it reads the object the driver lands on. Demanding an input source for one
    is a made-up requirement, and it blocked Position Wave and Radial Sweep
    from the apply flow entirely. LOC_DIFF is different and still counts: it
    measures TO something, so it genuinely needs a centre object.
    """
    kinds = {
        var_def.get("type", "SINGLE_PROP")
        for var_def in template.get("requires_driver_variables", []) or ()
    }
    return (
        bool(kinds & _NEEDS_SOURCE_TYPES)
        or _has_centre_variable(template)
        or _has_source_transform_variable(template)
    )


def owner_object_for(driver, fallback=None):
    """The Object a driver belongs to, for variables that must target it.

    ``driver.id_data`` is the datablock carrying the animation data, which is
    only sometimes the Object: a light's energy driver reports the Light, and a
    colour driver reports the node tree. Position variables must target the
    OBJECT, so the light case has to be resolved back through the objects that
    use that data.

    Note what this implies for a strip: lamps SHARING one light datablock also
    share its drivers, so no per-lamp variation is possible however the
    variable is wired. Each lamp needs its own data for a spatial effect to
    mean anything - that is a property of Blender, not of this code.
    """
    import bpy

    if fallback is not None:
        return fallback
    owner = getattr(driver, "id_data", None)
    if owner is None:
        return None
    if isinstance(owner, bpy.types.Object):
        return owner
    for candidate in bpy.data.objects:
        if getattr(candidate, "data", None) is owner:
            return candidate

    # A driver on a material's node tree - an Emission Strength, say - reports
    # the SHADER TREE, which is two steps from an object. Walk back through the
    # material to the objects using it.
    #
    # Only an unambiguous answer counts. A material on ten objects has ten
    # equally valid owners, and picking one would silently bind every object's
    # driver to whichever happened to be first. Callers that DO know the object
    # pass it as ``fallback``, which is handled above.
    users = _objects_using_node_tree(owner)
    return users[0] if len(users) == 1 else None


def _objects_using_node_tree(node_tree):
    """Every object that carries this embedded shader tree."""
    from ..motion import applied_motion

    return applied_motion.objects_using_node_tree(node_tree)


def bind_required_variables(driver, template, source_entry, owner_object=None):
    required = template.get("requires_driver_variables", [])
    if not required:
        return True, ""

    kinds = {var_def.get("type", "SINGLE_PROP") for var_def in required}
    unknown = kinds - _SELF_POSITION_TYPES - _NEEDS_SOURCE_TYPES
    if unknown:
        return False, "Driver variable type %s needs manual setup." % ", ".join(sorted(unknown))

    # Only demand an input source if something actually needs one. A template
    # that just wants its own position is self-contained, and asking the artist
    # to pick an input for it would be a made-up requirement.
    source = None
    if (
        kinds & _NEEDS_SOURCE_TYPES
        or _has_centre_variable(template)
        or _has_source_transform_variable(template)
    ):
        if not source_entry:
            return False, "Choose an Espresso input source first."
        # LOC_DIFF measures to an OBJECT and never reads a property off it, so
        # insisting the source resolve to a live property would reject the
        # obvious choice - an Empty used purely as a centre marker.
        need_property = "SINGLE_PROP" in kinds
        source, _code, reason = _source_descriptor(
            source_entry, require_property=need_property)
        if not source:
            return False, reason or "Input source no longer exists or is not usable."

    owner = None
    if kinds & (_SELF_POSITION_TYPES | {"LOC_DIFF"}):
        owner = owner_object_for(driver, owner_object)
        if owner is None:
            return False, "Could not work out which object this driver belongs to."

    for var_def in required:
        kind = var_def.get("type", "SINGLE_PROP")
        var = driver.variables.get(var_def["name"])
        if var is None:
            var = driver.variables.new()
            var.name = var_def["name"]
        var.type = kind
        if kind == "TRANSFORMS":
            target = var.targets[0]
            # Most position variables read the object the driver lands on. A
            # variable marked CENTRE reads the chosen centre object instead,
            # which is what lets Radial Sweep turn about a centre the artist
            # can see and move rather than about the world origin.
            target.id = (
                source["id"]
                if var_def.get("reads") in {"CENTRE", "SOURCE"}
                else owner
            )
            target.transform_type = var_def.get("transform_type", "LOC_X")
            target.transform_space = var_def.get("transform_space", "WORLD_SPACE")
        elif kind == "LOC_DIFF":
            # Distance is measured FROM the driven object TO whatever the
            # artist chose, so the pair is (self, source) in that order.
            var.targets[0].id = owner
            var.targets[1].id = source["id"]
        else:
            target = var.targets[0]
            target.id_type = source["id_type"]
            target.id = source["id"]
            target.data_path = source["data_path"]
    return True, ""


def _active_object_driver_owners(active_object):
    if active_object is None:
        return []
    owners = [active_object]
    data = getattr(active_object, "data", None)
    if data is not None:
        owners.append(data)
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None:
            owners.append(shape_keys)
    result = []
    seen = set()
    for owner in owners:
        pointer = owner.as_pointer() if hasattr(owner, "as_pointer") else id(owner)
        if pointer not in seen:
            seen.add(pointer)
            result.append(owner)
    return result


def _canonical_input_name(name):
    return name if name.startswith(ESPRESSO_INPUT_PREFIX) else ESPRESSO_INPUT_PREFIX + name


def _is_espresso_input_name(name):
    return name.startswith(ESPRESSO_INPUT_PREFIX) or name in LEGACY_INPUT_VARIABLE_NAMES


def plan_input_relink(active_object, old_entry, new_entry):
    """Find exact Espresso bindings on one object without changing Blender data."""
    old_source, _old_code, old_reason = _source_descriptor(
        old_entry, require_property=False,
    )
    if old_source is None:
        return {"ok": False, "message": old_reason, "count": 0, "matches": []}
    new_source, _new_code, new_reason = _source_descriptor(
        new_entry, require_property=True,
    )
    if new_source is None:
        return {"ok": False, "message": new_reason, "count": 0, "matches": []}
    if active_object is None:
        return {
            "ok": False,
            "message": "Select the object whose Espresso drivers should be relinked.",
            "count": 0,
            "matches": [],
        }

    matches = []
    for owner in _active_object_driver_owners(active_object):
        animation_data = getattr(owner, "animation_data", None)
        for fcurve in getattr(animation_data, "drivers", ()) if animation_data else ():
            driver = fcurve.driver
            variables = list(driver.variables)
            for variable in variables:
                if variable.type != "SINGLE_PROP" or not _is_espresso_input_name(variable.name):
                    continue
                target = variable.targets[0]
                if target.id is not old_source["id"] or target.data_path != old_source["data_path"]:
                    continue
                desired_name = _canonical_input_name(variable.name)
                if desired_name != variable.name and any(
                    other is not variable and other.name == desired_name
                    for other in variables
                ):
                    return {
                        "ok": False,
                        "message": (
                            f"Driver variable '{desired_name}' already exists on "
                            f"{fcurve.data_path}; nothing was changed."
                        ),
                        "count": 0,
                        "matches": [],
                    }
                matches.append({
                    "owner": owner,
                    "fcurve": fcurve,
                    "driver": driver,
                    "variable": variable,
                    "target": target,
                    "old_name": variable.name,
                    "new_name": desired_name,
                })
    return {
        "ok": True,
        "message": "",
        "count": len(matches),
        "matches": matches,
        "new_source": new_source,
    }


def apply_input_relink(plan, props, new_entry):
    """Apply a preflighted relink atomically, including legacy name migration."""
    if not plan.get("ok"):
        return False, plan.get("message", "Input relink preflight failed."), 0
    snapshots = []
    previous_source = getattr(props, "espresso_input_source", "{}")
    new_source = plan["new_source"]
    try:
        for match in plan["matches"]:
            variable = match["variable"]
            target = match["target"]
            driver = match["driver"]
            snapshots.append({
                "variable": variable,
                "name": variable.name,
                "target": target,
                "id_type": target.id_type,
                "id": target.id,
                "data_path": target.data_path,
                "driver": driver,
                "expression": driver.expression,
            })
            if match["new_name"] != match["old_name"]:
                # Renaming to a longer variable name can push a near-limit
                # expression past Blender's 255-character ceiling. Blender
                # truncates silently and raises nothing, so an unguarded write
                # would commit a broken driver and never reach the rollback
                # below. Validate the substituted string first, then confirm
                # Blender stored it byte-for-byte.
                renamed_expression = re.sub(
                    rf"\b{re.escape(match['old_name'])}\b",
                    match["new_name"],
                    driver.expression,
                )
                # The validator only accepts names it knows about, so describe
                # this driver's own variables to it — using the post-rename name
                # for the one being migrated, since the rename happens below.
                known_variables = [
                    {
                        "name": match["new_name"] if item.name == match["old_name"] else item.name,
                        "preview_default": 0.5,
                    }
                    for item in driver.variables
                ]
                valid, message = utils.validate_driver_expression(
                    renamed_expression, {"requires_driver_variables": known_variables},
                )
                if not valid:
                    raise RuntimeError(message)
                driver.expression = renamed_expression
                if driver.expression != renamed_expression:
                    raise RuntimeError(
                        "Blender did not store the complete relinked expression "
                        f"({len(driver.expression)} of {len(renamed_expression)} characters)."
                    )
                variable.name = match["new_name"]
                if variable.name != match["new_name"]:
                    raise RuntimeError(
                        f"Could not rename driver variable to '{match['new_name']}'."
                    )
            target.id_type = new_source["id_type"]
            target.id = new_source["id"]
            target.data_path = new_source["data_path"]
        remember_source(props, new_entry)
    except Exception as exc:
        for snapshot in reversed(snapshots):
            snapshot["variable"].name = snapshot["name"]
            snapshot["target"].id_type = snapshot["id_type"]
            snapshot["target"].id = snapshot["id"]
            snapshot["target"].data_path = snapshot["data_path"]
            snapshot["driver"].expression = snapshot["expression"]
        props.espresso_input_source = previous_source
        return False, f"Input relink was rolled back: {exc}", 0
    count = int(plan.get("count", 0))
    return True, f"Relinked {count} Espresso input variable(s).", count


def target_from_entry(entry):
    resolved, _reason = target_memory.resolve_entry(entry)
    if not resolved:
        return None
    item = resolved[0]
    return ButtonDriverTarget(item["owner"], item["data_path"], int(item.get("index", -1)))
