"""Named, reusable input controllers for existing Espresso drivers."""

from __future__ import annotations

import copy
import json
import uuid

import bpy

from ...apply import apply_behavior, driver_targets, source_binding, target_memory
from ...catalogue import catalogue_contracts, templates
from ...engine import live_control_core, utils
from ...engine.expression.driver_literals import format_driver_literal


CONTROLLER_VARIABLE = "espctl_"
BASE_VARIABLE = "espbase"
FRAME_VARIABLE = "espfrm_"


def _diagnostics_enabled(context):
    prefs = utils.addon_preferences(context)
    return bool(getattr(prefs, "show_diagnostics", False))

RESET_CAPTURED = "CAPTURED"
RESET_BASELINE = "BASELINE"
RESET_CUSTOM = "CUSTOM"

SCOPE_ACTIVE = "ACTIVE"
SCOPE_RELATED = "RELATED"
SCOPE_MOTION = "MOTION"
SCOPE_SCENE = "SCENE"


def _read_json(raw, fallback):
    try:
        value = json.loads(raw or "")
    except Exception:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# Every field a controller record is expected to carry. The panel and the
# attach path read these directly, so a record written by an older build - or
# left half-formed by an interrupted edit - would otherwise raise KeyError
# inside draw(), which Blender surfaces as the panel simply vanishing.
_CONTROLLER_DEFAULTS = {
    "name": "Unnamed Controller",
    "source": {},
    "interpretation": live_control_core.INTERPRET_INFLUENCE,
    "threshold": 0.5,
    "invert": False,
    "clamp": True,
    "response_curve": live_control_core.RESPONSE_SMOOTH,
    "transition_mode": live_control_core.TRANSITION_IMMEDIATE,
    "transition_duration": 0,
    "inputs": (),
}


def _normalized_controller(record):
    """Fill in any missing fields so callers can rely on the full schema."""
    if not isinstance(record, dict) or not record.get("uid"):
        return None
    filled = dict(_CONTROLLER_DEFAULTS)
    filled.update(record)
    return filled


# A controller is STORED ON the ID that carries its driving property - the object
# whose custom property drives it - not in one scene-wide blob. Co-locating them
# removes a whole failure mode: delete the object and its controller goes with
# it, instead of leaving a record pointing at a source that no longer exists.
# Append or link that object into another file and the controller travels too.
#
# Controllers driven by a SCENE property have no object to live on, so the scene
# remains a valid home for those.
CONTROLLER_KEY = "espresso_controllers"


def _controller_stores(props):
    """Every ID that may hold controller records: the scene and its objects."""
    scene = getattr(props, "id_data", None)
    if scene is None:
        return []
    stores = [scene]
    stores.extend(getattr(scene, "objects", []) or [])
    return stores


def _read_store(owner):
    raw = owner.get(CONTROLLER_KEY) if owner is not None else None
    records = _read_json(raw or "[]", [])
    return records if isinstance(records, list) else []


def _write_store(owner, records):
    if owner is None:
        return
    if records:
        owner[CONTROLLER_KEY] = json.dumps(records, sort_keys=True)
    elif CONTROLLER_KEY in owner:
        del owner[CONTROLLER_KEY]


def controller_home(record, scene=None):
    """The ID a controller belongs on: the datablock owning its input property.

    Resolved from the STORED target rather than a live property lookup, so a
    controller whose property was renamed still has a home to be written back to
    and repaired in place. Falls back to the scene when the source is a scene
    property or cannot be resolved at all.
    """
    targets = (record.get("source") or {}).get("targets") or []
    if targets:
        target = targets[0]
        id_type = str(target.get("id_type") or "").upper()
        name = target.get("id_name") or ""
        if id_type == "OBJECT":
            obj = bpy.data.objects.get(name)
            if obj is not None:
                return obj
        elif id_type == "SCENE":
            return bpy.data.scenes.get(name) or scene
    return scene


def read_controllers(props):
    """Gather controllers from every store, newest definition winning per uid.

    The legacy scene-wide list is read last so an older file keeps working; the
    next write relocates those records onto their objects and empties it.
    """
    seen = {}
    order = []
    for owner in _controller_stores(props):
        for record in _read_store(owner):
            normalized = _normalized_controller(record)
            if normalized is None:
                continue
            if normalized["uid"] not in seen:
                order.append(normalized["uid"])
            seen[normalized["uid"]] = normalized

    for record in _read_json(getattr(props, "live_controllers", "[]"), []) or []:
        normalized = _normalized_controller(record)
        if normalized is None or normalized["uid"] in seen:
            continue
        order.append(normalized["uid"])
        seen[normalized["uid"]] = normalized

    return [seen[uid] for uid in order]


def write_controllers(props, controllers):
    """Distribute records to their homes and clear stores that no longer hold any."""
    scene = getattr(props, "id_data", None)
    grouped = {}
    for record in controllers:
        home = controller_home(record, scene)
        if home is None:
            continue
        grouped.setdefault(home, []).append(record)

    for owner in grouped:
        _write_store(owner, grouped[owner])
    for owner in _controller_stores(props):
        if owner not in grouped:
            _write_store(owner, [])

    # Any records still sitting in the legacy scene-wide list have now been
    # written to their real homes, so empty it. This is what migrates an older
    # file - implicitly, on the first write - and it is also required for
    # correctness: left populated, read_controllers would keep resurrecting a
    # controller the user had just deleted.
    if getattr(props, "live_controllers", "[]") not in ("[]", ""):
        props.live_controllers = "[]"


def read_bindings(props):
    return _read_json(getattr(props, "live_control_bindings", "{}"), {})


def write_bindings(props, bindings):
    props.live_control_bindings = json.dumps(bindings, sort_keys=True)


def discard_bindings_for_targets(scene, props, targets):
    """Forget controller wrappers after their public drivers were baked/cleared.

    Unlike ``remove_fcurves`` this deliberately does not restore the original
    expression: the target driver no longer exists.  It only removes binding
    records and any private carrier properties they owned.
    """
    bindings = read_bindings(props)
    removed = 0
    for descriptor in targets or ():
        binding = bindings.pop(_target_key(descriptor), None)
        if not binding:
            continue
        for path in (binding.get("carrier_path"), *(binding.get("retired_carrier_paths") or ())):
            _remove_carrier(scene, path)
        removed += 1
    if removed:
        write_bindings(props, bindings)
    return removed


NO_CONTROLLER = "__NONE__"


def controller_items(props, _context):
    # Without a blank first entry Blender falls back to index 0, so every object
    # looks like it already has the first controller attached - selected-looking,
    # but not editable, because nothing is actually bound to it.
    controllers = read_controllers(props)
    if not controllers:
        return [
            (
                NO_CONTROLLER,
                "None",
                "Right-click a property, choose Use as Espresso Input, then create a controller",
            )
        ]
    return [
        (NO_CONTROLLER, "None", "No controller chosen - pick one below to attach it"),
        *(
            (item["uid"], item["name"], item.get("source", {}).get("display_label", ""))
            for item in controllers
        ),
    ]


def selected_controller(props):
    uid = getattr(props, "live_controller_uid", "")
    return controller_by_uid(props, uid)


def controller_by_uid(props, uid):
    """Resolve one stored controller without changing the UI selection."""
    return next((item for item in read_controllers(props) if item["uid"] == uid), None)


def create_controller(
    props,
    source_entry,
    name,
    interpretation,
    *,
    threshold=0.5,
    invert=False,
    clamp=True,
    response_curve=live_control_core.RESPONSE_SMOOTH,
    transition_mode=live_control_core.TRANSITION_IMMEDIATE,
    transition_duration=0,
):
    clean_name = " ".join((name or "").split())
    if not clean_name:
        return None, "Enter a controller name."
    status = source_binding.source_status(source_entry)
    if status["code"] != source_binding.SOURCE_ACTIVE:
        return None, status["reason"] or "Choose a valid Espresso input source."
    controllers = read_controllers(props)
    if any(item["name"].casefold() == clean_name.casefold() for item in controllers):
        return None, f'A controller named "{clean_name}" already exists.'
    controller = {
        "uid": uuid.uuid4().hex[:16],
        "name": clean_name,
        "source": source_entry,
        "interpretation": interpretation,
        "threshold": float(threshold),
        "invert": bool(invert),
        "clamp": bool(clamp),
        "response_curve": response_curve,
        "transition_mode": transition_mode,
        "transition_duration": max(0, int(transition_duration)),
        "inputs": (source_entry,),
    }
    controllers.append(controller)
    write_controllers(props, controllers)
    props.live_controller_uid = controller["uid"]
    return controller, ""


def update_controller(props, uid, values):
    controllers = read_controllers(props)
    current = next((item for item in controllers if item["uid"] == uid), None)
    if current is None:
        return None, "That controller no longer exists."
    name = " ".join((values.get("name") or "").split())
    if not name:
        return None, "Enter a controller name."
    if any(
        item["uid"] != uid and item["name"].casefold() == name.casefold()
        for item in controllers
    ):
        return None, f'A controller named "{name}" already exists.'
    source = values.get("source", current["source"])
    status = source_binding.source_status(source)
    if status["code"] != source_binding.SOURCE_ACTIVE:
        return None, status["reason"] or "Choose a valid Espresso input source."
    current.update({
        "name": name,
        "source": source,
        "interpretation": values.get("interpretation", current["interpretation"]),
        "threshold": float(values.get("threshold", current.get("threshold", 0.5))),
        "invert": bool(values.get("invert", current.get("invert", False))),
        "clamp": bool(values.get("clamp", current.get("clamp", True))),
        "response_curve": values.get(
            "response_curve",
            current.get("response_curve", live_control_core.RESPONSE_SMOOTH),
        ),
        "transition_mode": values.get(
            "transition_mode",
            current.get("transition_mode", live_control_core.TRANSITION_IMMEDIATE),
        ),
        "transition_duration": max(
            0, int(values.get("transition_duration", current.get("transition_duration", 0)))
        ),
        "inputs": tuple(values.get("inputs", current.get("inputs", (source,)))) or (source,),
    })
    write_controllers(props, controllers)
    return current, ""


def trusted_template_baseline(template, props):
    if (
        catalogue_contracts.explicit_baseline(template) is not None
        or catalogue_contracts.has_ordered_output_range(template)
        or template.get("data_path") == "scale"
    ):
        return float(getattr(props, "preview_output_baseline", 0.0))
    return None


def _id_record(id_block):
    if id_block is None:
        return {"type": "", "name": "", "library": ""}
    return {
        "type": id_block.__class__.__name__,
        "name": getattr(id_block, "name", ""),
        "library": target_memory.library_filepath(id_block),
    }


def _target_snapshot(target):
    result = {"id": _id_record(getattr(target, "id", None))}
    for name in (
        "id_type",
        "data_path",
        "bone_target",
        "transform_type",
        "transform_space",
        "rotation_mode",
        "context_property",
    ):
        try:
            result[name] = getattr(target, name)
        except Exception:
            pass
    return result


def snapshot_driver(driver):
    variables = []
    for variable in driver.variables:
        variables.append({
            "name": variable.name,
            "type": variable.type,
            "targets": [_target_snapshot(target) for target in variable.targets],
        })
    return {
        "type": driver.type,
        "expression": driver.expression,
        "use_self": bool(getattr(driver, "use_self", False)),
        "variables": variables,
    }


def _clear_variables(driver):
    while len(driver.variables):
        driver.variables.remove(driver.variables[0])


def _restore_target(target, record):
    for name in ("id_type",):
        if name in record:
            try:
                setattr(target, name, record[name])
            except Exception:
                pass
    id_record = record.get("id") or {}
    if id_record.get("name"):
        try:
            target.id = target_memory._find_id_block(
                id_record.get("type", ""),
                id_record["name"],
                library=id_record.get("library", ""),
            )
        except Exception:
            pass
    for name in (
        "data_path",
        "bone_target",
        "transform_type",
        "transform_space",
        "rotation_mode",
        "context_property",
    ):
        if name in record:
            try:
                setattr(target, name, record[name])
            except Exception:
                pass


def restore_driver(driver, snapshot):
    _clear_variables(driver)
    driver.type = snapshot.get("type", "SCRIPTED")
    if driver.type == "SCRIPTED":
        driver.use_self = bool(snapshot.get("use_self", False))
        driver.expression = snapshot.get("expression", "0")
    for variable_record in snapshot.get("variables", []):
        variable = driver.variables.new()
        variable.name = variable_record["name"]
        variable.type = variable_record["type"]
        for target, record in zip(variable.targets, variable_record.get("targets", [])):
            _restore_target(target, record)


def _bind_single_prop(driver, name, source):
    variable = driver.variables.get(name)
    if variable is None:
        variable = driver.variables.new()
        variable.name = name
    variable.type = "SINGLE_PROP"
    target = variable.targets[0]
    target.id_type = source["id_type"]
    target.id = source["id"]
    target.data_path = source["data_path"]


def _target_descriptor(fcurve):
    owner = getattr(fcurve, "id_data", None)
    return target_memory.serialize_target(owner, fcurve.data_path, fcurve.array_index)


def _target_key(descriptor):
    return json.dumps({
        "id_type": descriptor.get("id_type", ""),
        "id_name": descriptor.get("id_name", ""),
        "owner_path": descriptor.get("owner_path", ""),
        "data_path": descriptor.get("data_path", ""),
        "index": int(descriptor.get("index", -1)),
    }, sort_keys=True)


def _source_signature(entry):
    targets = (entry or {}).get("targets") or ()
    if not targets:
        return ""
    target = targets[0]
    return json.dumps({
        "id_type": target.get("id_type", ""),
        "id_name": target.get("id_name", ""),
        "owner_path": target.get("owner_path", ""),
        "data_path": target.get("data_path", ""),
        "index": int(target.get("index", -1)),
    }, sort_keys=True)


def _current_target_value(descriptor):
    resolved, reason = target_memory.resolve_target_record(descriptor)
    if resolved is None:
        raise ValueError(reason or "The selected target no longer exists.")
    return apply_behavior.read_target_current_value(
        resolved["owner"], resolved["data_path"], resolved["index"],
    )


def _allocate_carrier(scene, uid):
    key = f"__espresso_carrier_{uuid.uuid4().hex}"
    scene[key] = 0.0
    try:
        scene.id_properties_ui(key).update(
            description=f"Internal Driver Espresso carrier ({uid})",
        )
    except Exception:
        pass
    return f'["{key}"]'


def _remove_carrier(scene, path):
    if not path:
        return
    try:
        scene.driver_remove(path)
    except Exception:
        pass
    if path.startswith('["') and path.endswith('"]'):
        key = path[2:-2]
        try:
            del scene[key]
        except Exception:
            pass


def _synthetic_template(driver):
    return {
        "requires_driver_variables": [
            {"name": variable.name, "preview_default": 0.5}
            for variable in driver.variables
        ]
    }


def _install_target_expression(driver, expression, scene):
    valid, message = utils.validate_driver_expression(
        expression, _synthetic_template(driver), scene,
    )
    if not valid:
        raise ValueError(message)
    driver.type = "SCRIPTED"
    driver.expression = expression
    if driver.expression != expression:
        raise ValueError("Blender did not store the complete controller expression.")


def _carrier_fcurve(scene, path):
    result = scene.driver_add(path)
    return result[0] if isinstance(result, list) else result


def _unsupported_template_reason(template):
    if catalogue_contracts.resolve_additive_profile(template) == catalogue_contracts.ELAPSED_TIME:
        return (
            "This accumulated-time template needs phase-safe capture before it can "
            "be stopped and resumed honestly. That controller mode is reserved for "
            "the captured-controls update."
        )
    return ""


def draft_base_snapshot(owner, base_snapshot, configure):
    """Build a replacement driver on the same ID without touching its public channel."""
    key = f"__espresso_draft_{uuid.uuid4().hex}"
    path = f'["{key}"]'
    owner[key] = 0.0
    try:
        fcurve = owner.driver_add(path)
        if isinstance(fcurve, list):
            fcurve = fcurve[0]
        restore_driver(fcurve.driver, base_snapshot)
        configure(fcurve.driver)
        return snapshot_driver(fcurve.driver)
    finally:
        try:
            owner.driver_remove(path)
        finally:
            del owner[key]


def replace_base_snapshots(scene, props, replacements, template=None):
    """Commit validated base recipes while preserving attached controller layers."""
    from ...apply import driver_manager

    bindings_before = getattr(props, "live_control_bindings", "{}")
    bindings = read_bindings(props)
    public_before = []
    prepared = []
    for item in replacements:
        owner = item["owner"]
        path = item["data_path"]
        index = int(item["index"])
        if index < 0:
            return False, (
                "Controller-aware Update needs one exact driver component; "
                "reapply a specific component before updating."
            )
        if getattr(driver_manager, "foreign_live_motion_for_channel", lambda *_: None)(
            owner, path, index,
        ):
            return False, f"An unavailable edition owns {path}[{index}]."
        descriptor = target_memory.serialize_target(owner, path, index)
        key = _target_key(descriptor)
        binding = bindings.get(key)
        animation = getattr(owner, "animation_data", None)
        fcurve = animation.drivers.find(path, index=index) if animation else None
        if binding and fcurve is None:
            return False, f"The attached Controller driver at {path}[{index}] is missing."
        controller = None
        source = None
        if binding:
            if not isinstance(binding, dict) or not isinstance(binding.get("original"), dict):
                return False, f"The Controller binding at {path}[{index}] is damaged."
            controller = controller_by_uid(props, binding.get("controller_uid", ""))
            if controller is None:
                return False, "The attached Controller no longer exists."
            source, reason = source_binding.resolve_source(controller.get("source"))
            if source is None:
                return False, reason or "The Controller input is unavailable."
            signature = (binding.get("capture") or {}).get("source_signature")
            if signature and signature != _source_signature(controller.get("source")):
                return False, "The Controller input changed; reattach it before updating."
            if item["base"].get("type") != "SCRIPTED":
                return False, "Only scripted-expression drivers can use an Espresso Controller."
            try:
                compiled = live_control_core.compile_controlled_expression(
                    item["base"].get("expression", ""),
                    controller.get("interpretation", live_control_core.INTERPRET_SWITCH),
                    float(binding["rest_value"]),
                    threshold=controller.get("threshold", 0.5),
                    invert=controller.get("invert", False),
                    clamp=controller.get("clamp", True),
                    response_curve=controller.get(
                        "response_curve", live_control_core.RESPONSE_SMOOTH,
                    ),
                    transition_mode=controller.get(
                        "transition_mode", live_control_core.TRANSITION_IMMEDIATE,
                    ),
                    transition_duration=controller.get("transition_duration", 0),
                    capture_frame=int((binding.get("capture") or {}).get(
                        "frame", scene.frame_current,
                    )),
                    frame_variable_name=FRAME_VARIABLE,
                    max_length=utils.MAX_DRIVER_EXPRESSION_LENGTH,
                )
            except (KeyError, TypeError, ValueError) as exc:
                return False, f"Could not rebuild the Controller: {exc}"
            if compiled["strategy"] == "CARRIER" and item["base"].get("use_self"):
                return False, "This long driver uses Use Self and cannot move to a carrier."
        else:
            compiled = None
        public_before.append({
            "owner": owner, "path": path, "index": index,
            "existed": fcurve is not None,
            "snapshot": snapshot_driver(fcurve.driver) if fcurve else None,
        })
        prepared.append((item, key, binding, controller, source, compiled))

    new_carriers = []
    obsolete_carriers = []
    try:
        for item, key, old_binding, controller, source, compiled in prepared:
            owner, path, index = item["owner"], item["data_path"], int(item["index"])
            animation = getattr(owner, "animation_data", None)
            fcurve = animation.drivers.find(path, index=index) if animation else None
            if fcurve is None:
                result = owner.driver_add(path, index) if index >= 0 else owner.driver_add(path)
                fcurve = result[0] if isinstance(result, list) else result
            base = item["base"]
            if old_binding is None:
                restore_driver(fcurve.driver, base)
                continue
            binding = copy.deepcopy(old_binding)
            old_paths = {
                value for value in (
                    old_binding.get("carrier_path"),
                    *(old_binding.get("retired_carrier_paths") or ()),
                ) if value
            }
            carrier_path = ""
            if compiled["strategy"] == "CARRIER":
                carrier_path = _allocate_carrier(
                    scene, f'{controller["uid"]}:{uuid.uuid4().hex[:8]}',
                )
                new_carriers.append(carrier_path)
                carrier = _carrier_fcurve(scene, carrier_path)
                restore_driver(carrier.driver, base)
                restore_driver(fcurve.driver, {
                    "type": "SCRIPTED", "expression": "0",
                    "use_self": False, "variables": [],
                })
                _bind_single_prop(fcurve.driver, BASE_VARIABLE, {
                    "id_type": "SCENE", "id": scene, "data_path": carrier_path,
                })
            else:
                restore_driver(fcurve.driver, base)
            _bind_single_prop(fcurve.driver, CONTROLLER_VARIABLE, source)
            if controller.get("transition_mode") != live_control_core.TRANSITION_IMMEDIATE:
                _bind_single_prop(fcurve.driver, FRAME_VARIABLE, {
                    "id_type": "SCENE", "id": scene, "data_path": "frame_current",
                })
            _install_target_expression(fcurve.driver, compiled["expression"], scene)
            enabled_driver = snapshot_driver(fcurve.driver)
            binding["original"] = base
            if template is not None:
                binding["template_id"] = template.get("id", "")
            binding["strategy"] = compiled["strategy"]
            binding["carrier_path"] = carrier_path
            if binding.get("disabled"):
                old_public = next(
                    value["snapshot"] for value in public_before
                    if value["owner"] == owner
                    and value["path"] == path and value["index"] == index
                )
                if not isinstance(binding.get("enabled_driver"), dict):
                    raise ValueError("The disabled Controller has no resumable driver.")
                binding["enabled_driver"] = enabled_driver
                restore_driver(fcurve.driver, old_public)
                retained = {
                    target.get("data_path")
                    for variable in old_public.get("variables", ())
                    for target in variable.get("targets", ())
                }
                binding["retired_carrier_paths"] = sorted(old_paths & retained)
            else:
                binding.pop("retired_carrier_paths", None)
            obsolete_carriers.extend(
                old_paths - set(binding.get("retired_carrier_paths") or ()) - {carrier_path}
            )
            bindings[key] = binding
        write_bindings(props, bindings)
    except Exception as exc:
        unrestored = 0
        for item in public_before:
            owner, path, index = item["owner"], item["path"], item["index"]
            try:
                if not item["existed"]:
                    owner.driver_remove(path, index) if index >= 0 else owner.driver_remove(path)
                else:
                    animation = getattr(owner, "animation_data", None)
                    fcurve = animation.drivers.find(path, index=index) if animation else None
                    if fcurve is None:
                        result = owner.driver_add(path, index) if index >= 0 else owner.driver_add(path)
                        fcurve = result[0] if isinstance(result, list) else result
                    restore_driver(fcurve.driver, item["snapshot"])
            except Exception:
                unrestored += 1
        for path in new_carriers:
            _remove_carrier(scene, path)
        props.live_control_bindings = bindings_before
        suffix = f" {unrestored} public driver(s) could not be restored." if unrestored else ""
        return False, f"Controller-aware update was rolled back: {exc}.{suffix}"
    for path in set(obsolete_carriers):
        _remove_carrier(scene, path)
    return True, "Updated the driver and its Controller."


def attach_fcurves(
    scene,
    props,
    fcurves,
    controller,
    reset_mode,
    custom_value,
    baseline_value,
    template,
):
    reason = _unsupported_template_reason(template)
    if reason:
        return False, reason, 0
    source, source_reason = source_binding.resolve_source(controller.get("source"))
    if source is None:
        return False, source_reason or "The controller source is unavailable.", 0
    unique = []
    seen = set()
    for fcurve in fcurves:
        pointer = fcurve.as_pointer()
        if pointer not in seen:
            seen.add(pointer)
            unique.append(fcurve)
    if not unique:
        return False, "No compatible Espresso driver target was found.", 0

    bindings_before = getattr(props, "live_control_bindings", "{}")
    bindings = read_bindings(props)
    current_snapshots = [(fcurve, snapshot_driver(fcurve.driver)) for fcurve in unique]
    new_carriers = []
    stale_carriers = []
    try:
        for fcurve in unique:
            descriptor = _target_descriptor(fcurve)
            if not descriptor:
                raise ValueError("Could not serialize one selected driver target.")
            key = _target_key(descriptor)
            old_binding = bindings.get(key)
            original = (
                old_binding.get("original")
                if isinstance(old_binding, dict) and old_binding.get("original")
                else snapshot_driver(fcurve.driver)
            )
            if original.get("type") != "SCRIPTED":
                raise ValueError("Only scripted-expression drivers can use an Espresso Controller.")
            if not old_binding:
                original_names = {item.get("name") for item in original.get("variables", [])}
                collision = original_names & {CONTROLLER_VARIABLE, BASE_VARIABLE}
                if collision:
                    raise ValueError(
                        "The driver already uses reserved Espresso Controller variable(s): "
                        + ", ".join(sorted(collision))
                    )
            if old_binding and old_binding.get("carrier_path"):
                stale_carriers.append(old_binding["carrier_path"])
            if old_binding:
                stale_carriers.extend(old_binding.get("retired_carrier_paths") or ())

            if reset_mode == RESET_BASELINE:
                if baseline_value is None:
                    raise ValueError(
                        "Template Baseline is unavailable because this template has "
                        "no audited baseline contract. Use Captured Rest or Custom Value."
                    )
                rest_value = float(baseline_value)
            elif reset_mode == RESET_CUSTOM:
                rest_value = float(custom_value)
            elif old_binding and old_binding.get("reset_mode") == RESET_CAPTURED:
                rest_value = float(old_binding.get("rest_value", 0.0))
            else:
                rest_value = float(_current_target_value(descriptor))

            compiled = live_control_core.compile_controlled_expression(
                original.get("expression", ""),
                controller.get("interpretation", live_control_core.INTERPRET_SWITCH),
                rest_value,
                threshold=controller.get("threshold", 0.5),
                invert=controller.get("invert", False),
                clamp=controller.get("clamp", True),
                response_curve=controller.get(
                    "response_curve", live_control_core.RESPONSE_SMOOTH,
                ),
                transition_mode=controller.get(
                    "transition_mode", live_control_core.TRANSITION_IMMEDIATE,
                ),
                transition_duration=controller.get("transition_duration", 0),
                capture_frame=int(scene.frame_current),
                frame_variable_name=FRAME_VARIABLE,
                max_length=utils.MAX_DRIVER_EXPRESSION_LENGTH,
            )

            carrier_path = ""
            if compiled["strategy"] == "CARRIER":
                if original.get("use_self"):
                    raise ValueError(
                        "This long driver uses Use Self, so moving it to an internal "
                        "carrier would change its meaning. Shorten it before attaching."
                    )
                carrier_path = _allocate_carrier(
                    scene, f'{controller["uid"]}:{uuid.uuid4().hex[:8]}',
                )
                new_carriers.append(carrier_path)
                carrier = _carrier_fcurve(scene, carrier_path)
                restore_driver(carrier.driver, original)
                restore_driver(fcurve.driver, {
                    "type": "SCRIPTED", "expression": "0",
                    "use_self": False, "variables": [],
                })
                _bind_single_prop(fcurve.driver, BASE_VARIABLE, {
                    "id_type": "SCENE", "id": scene, "data_path": carrier_path,
                })
            else:
                restore_driver(fcurve.driver, original)

            _bind_single_prop(fcurve.driver, CONTROLLER_VARIABLE, source)
            if controller.get("transition_mode") != live_control_core.TRANSITION_IMMEDIATE:
                _bind_single_prop(fcurve.driver, FRAME_VARIABLE, {
                    "id_type": "SCENE", "id": scene, "data_path": "frame_current",
                })
            _install_target_expression(fcurve.driver, compiled["expression"], scene)
            try:
                source_value = source_binding.sample_source_value(controller.get("source"))
            except ValueError:
                source_value = 0.0
            bindings[key] = {
                "target": descriptor,
                "controller_uid": controller["uid"],
                "original": original,
                "strategy": compiled["strategy"],
                "carrier_path": carrier_path,
                "reset_mode": reset_mode,
                "rest_value": rest_value,
                "template_id": template.get("id", ""),
                "capture": {
                    "frame": int(scene.frame_current),
                    "value": rest_value,
                    "source_value": source_value,
                    "source_signature": _source_signature(controller.get("source")),
                },
                "disabled": False,
            }
    except Exception as exc:
        # A restore can itself fail if the driver was partially rewritten before
        # the outer error fired. Never claim a clean rollback in that case - the
        # user must know which drivers to inspect.
        unrestored = 0
        for fcurve, snapshot in current_snapshots:
            try:
                restore_driver(fcurve.driver, snapshot)
            except Exception:
                unrestored += 1
        for index in new_carriers:
            _remove_carrier(scene, index)
        props.live_control_bindings = bindings_before
        if unrestored:
            return False, (
                f"Controller attach failed and {unrestored} driver(s) could not be fully "
                f"restored - inspect them in the Drivers Editor: {exc}"
            ), 0
        return False, f"Controller attach was rolled back: {exc}", 0

    write_bindings(props, bindings)
    for path in set(stale_carriers) - set(new_carriers):
        _remove_carrier(scene, path)
    return True, f"Attached controller to {len(unique)} driver(s).", len(unique)


def remove_fcurves(scene, props, fcurves):
    bindings_before = getattr(props, "live_control_bindings", "{}")
    bindings = read_bindings(props)
    snapshots = [(fcurve, snapshot_driver(fcurve.driver)) for fcurve in fcurves]
    removed = []
    try:
        for fcurve in fcurves:
            descriptor = _target_descriptor(fcurve)
            binding = bindings.get(_target_key(descriptor)) if descriptor else None
            if not binding:
                continue
            restore_driver(fcurve.driver, binding["original"])
            removed.append(binding)
            bindings.pop(_target_key(descriptor), None)
    except Exception as exc:
        unrestored = 0
        for fcurve, snapshot in snapshots:
            try:
                restore_driver(fcurve.driver, snapshot)
            except Exception:
                unrestored += 1
        props.live_control_bindings = bindings_before
        if unrestored:
            return False, (
                f"Controller removal failed and {unrestored} driver(s) could not be fully "
                f"restored - inspect them in the Drivers Editor: {exc}"
            ), 0
        return False, f"Controller removal was rolled back: {exc}", 0
    write_bindings(props, bindings)
    for binding in removed:
        for path in (binding.get("carrier_path"), *(binding.get("retired_carrier_paths") or ())):
            _remove_carrier(scene, path)
    if not removed:
        return False, "The selected scope has no attached Espresso Controller.", 0
    return True, f"Removed controller from {len(removed)} driver(s).", len(removed)


def set_fcurves_enabled(scene, props, fcurves, enabled, template):
    """Toggle controller output without losing the live driver or its undo state.

    Disable captures the evaluated result and temporarily replaces the public
    driver with that constant. Enable restores the exact controlled-driver
    snapshot. The binding stays owned throughout, so this is reversible and
    independent of timeline playback direction.
    """
    if not live_control_core.supports_safe_disable(
        catalogue_contracts.resolve_additive_profile(template)
    ):
        return False, _unsupported_template_reason(template), 0
    bindings_before = getattr(props, "live_control_bindings", "{}")
    bindings = read_bindings(props)
    snapshots = [(fcurve, snapshot_driver(fcurve.driver)) for fcurve in fcurves]
    changed = 0
    retired_carriers = []
    try:
        for fcurve in fcurves:
            descriptor = _target_descriptor(fcurve)
            key = _target_key(descriptor) if descriptor else ""
            binding = bindings.get(key)
            if not binding:
                continue
            if enabled:
                if not binding.get("disabled"):
                    continue
                restore_driver(fcurve.driver, binding["enabled_driver"])
                binding.pop("enabled_driver", None)
                binding["disabled"] = False
                retired_carriers.extend(binding.pop("retired_carrier_paths", ()))
            else:
                if binding.get("disabled"):
                    continue
                controller = controller_by_uid(props, binding.get("controller_uid", ""))
                if controller is None:
                    raise ValueError("The attached Controller no longer exists.")
                signature = _source_signature(controller.get("source"))
                capture = binding.get("capture") or {}
                if capture and capture.get("source_signature") != signature:
                    raise ValueError("The Controller input changed; reattach it before capture.")
                hold_value = float(_current_target_value(descriptor))
                enabled_driver = snapshot_driver(fcurve.driver)
                binding["enabled_driver"] = enabled_driver
                transition_mode = controller.get(
                    "transition_mode", live_control_core.TRANSITION_IMMEDIATE,
                )
                transition_duration = int(controller.get("transition_duration", 0))
                hold_driver = copy.deepcopy(enabled_driver)
                if hold_driver.get("type") != "SCRIPTED":
                    raise ValueError("Safe easing requires a scripted Espresso driver.")
                hold_driver["expression"] = live_control_core.compile_hold_expression(
                    enabled_driver.get("expression", ""),
                    hold_value,
                    mode=transition_mode,
                    duration=transition_duration,
                    capture_frame=int(scene.frame_current),
                )
                restore_driver(fcurve.driver, hold_driver)
                binding["capture"] = {
                    "frame": int(scene.frame_current),
                    "value": hold_value,
                    "source_value": source_binding.sample_source_value(controller.get("source")),
                    "source_signature": signature,
                }
                binding["disabled"] = True
            changed += 1
    except Exception as exc:
        for fcurve, snapshot in snapshots:
            restore_driver(fcurve.driver, snapshot)
        props.live_control_bindings = bindings_before
        return False, f"Safe Controller toggle was rolled back: {exc}", 0
    if not changed:
        state = "disabled" if enabled else "enabled"
        return False, f"No {state} Controller was found in this scope.", 0
    write_bindings(props, bindings)
    for path in set(retired_carriers):
        _remove_carrier(scene, path)
    action = "Enabled" if enabled else "Captured and safely disabled"
    return True, f"{action} {changed} controlled driver(s).", changed


def _entry_fcurves(entry):
    result = []
    for target in (entry or {}).get("targets", []):
        fcurve = driver_targets.resolve_driver(target)
        if fcurve is not None:
            result.append(fcurve)
    return result


def scene_motion_effect(context, props):
    """The applied effect the Scene Motion scope points at, or None."""
    from ...apply.motion import applied_motion_manager

    token = str(getattr(props, "live_control_scene_token", "") or "")
    if not token:
        return None
    return applied_motion_manager.find_effect(context, token, "SCENE")


def scene_motion_label(effect):
    """How a scene motion is named wherever it is offered or reported:
    recipe, the channel it drives, and the host it lives on."""
    from ...apply.motion import applied_motion

    host, record = effect.get("host"), effect.get("record")
    label = applied_motion.describe_with_channel(host, record) if record else (
        effect.get("label") or "Applied motion")
    # The OBJECT, not the datablock. A motion on a group nested in a material
    # is hosted by that node tree, and "Fire_Controls" tells an artist nothing
    # about which thing in their scene it is; "Fire Plane" does. Falls back to
    # the host's own name for anything that is not a node tree.
    where = ""
    if getattr(host, "bl_idname", "") in {"ShaderNodeTree", "GeometryNodeTree",
                                          "CompositorNodeTree"}:
        users = [obj.name for obj in applied_motion.objects_using_node_tree(host)]
        if users:
            where = ", ".join(users[:2]) + (" +%d" % (len(users) - 2) if len(users) > 2 else "")
    if not where:
        where = getattr(host, "name", "")
    return "%s — %s" % (label, where) if where else label


def scene_motion_choices(context):
    """(record token, label) for every motion a controller could attach to.

    Only driver-backed effects: a setup made of objects and node groups has no
    F-curve for a controller layer to wrap, so offering it would only lead to
    the resolver's "has no drivers" reason a click later.
    """
    from ...apply.motion import applied_motion, applied_motion_manager

    found = []
    for effect in applied_motion_manager.collect_effects_for_draw(context, "SCENE"):
        record = effect.get("record")
        if not record or not applied_motion.paths_of(record):
            continue
        # LIVE drivers only. The collector keeps a stamp whose driver was
        # deleted by hand -- the driver is the authority on whether motion
        # exists, the stamp on what it is, and Clear needs to see leftovers.
        # A controller cannot attach to a leftover, so it is not offered.
        # Found in the wild: an Empty with one Scene Length Loop on Rotation Z
        # and a stale stamp for Rotation X, offered as two identical rows.
        live = _record_fcurves(effect.get("host"), record)
        if not live:
            continue
        found.append((effect, live))

    # Name the channel only where two offers would otherwise read the same.
    # A lone transform driver keeps its plain label; two applications of one
    # recipe on X and Z of the same object must say which is which.
    labels = [scene_motion_label(effect) for effect, _live in found]
    counts = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    choices = []
    for (effect, live), label in zip(found, labels):
        if counts[label] > 1:
            channels = ", ".join(dict.fromkeys(_channel_words(fcurve) for fcurve in live))
            if channels:
                label = "%s (%s)" % (label, channels)
        choices.append((effect.get("record_token", ""), label))
    return choices


_TRANSFORM_WORDS = {
    "location": "Location", "rotation_euler": "Rotation",
    "rotation_quaternion": "Rotation", "scale": "Scale",
    "delta_location": "Delta Location", "delta_rotation_euler": "Delta Rotation",
    "delta_scale": "Delta Scale",
}


def _channel_words(fcurve):
    """"Rotation Z" for a transform channel; the path's last name otherwise."""
    path = str(fcurve.data_path or "")
    axis = "XYZW"[fcurve.array_index] if 0 <= fcurve.array_index < 4 else ""
    if path in _TRANSFORM_WORDS:
        return ("%s %s" % (_TRANSFORM_WORDS[path], axis)).strip()
    tail = path.rsplit(".", 1)[-1].replace("_", " ")
    return ("%s %s" % (tail, axis)).strip()


def _record_fcurves(host, record):
    """The live F-curves a record's paths actually point at on its host."""
    from ...apply.motion import applied_motion

    animation = getattr(host, "animation_data", None)
    drivers = [fcurve for fcurve in (animation.drivers if animation else ())
               if getattr(fcurve, "driver", None)]
    out = []
    for path, index in applied_motion.paths_of(record or {}):
        for fcurve in drivers:
            if fcurve.data_path == path and (index < 0 or fcurve.array_index == index):
                out.append(fcurve)
    return out


def _scene_motion_fcurves(context, props):
    """The drivers the chosen scene motion actually owns, with the reason
    when there is nothing to hand back."""
    from ...apply.motion import applied_motion

    if not str(getattr(props, "live_control_scene_token", "") or ""):
        return [], "Choose a motion from the scene to attach to."
    effect = scene_motion_effect(context, props)
    if effect is None:
        return [], "The chosen motion no longer exists in this scene."
    # One matching rule, shared with the picker: what the picker offers is
    # exactly what this hands back, so a motion can never be offered and then
    # fail to resolve.
    fcurves = _record_fcurves(effect.get("host"), effect.get("record"))
    if not fcurves:
        return [], "%s has no drivers a controller can attach to." % scene_motion_label(effect)
    return fcurves, ""


def scope_template(context, template, scope):
    """The recipe the scoped drivers were APPLIED with.

    For every scope but Scene Motion that is the panel's current template,
    because those scopes are defined relative to it. A scene motion can be
    any recipe at all -- the panel may show Police Lights while the chosen
    motion is Campfire Flicker -- and its rest baseline belongs to the recipe
    that made it, not to whatever the panel happens to be showing.
    """
    if scope != SCOPE_SCENE:
        return template
    effect = scene_motion_effect(context, context.scene.espresso_props)
    if effect is None:
        return template
    found = effect.get("template") or templates.TEMPLATE_BY_ID.get(
        str(effect.get("template_id") or ""))
    return found or template


def resolve_scope_fcurves(context, template, scope):
    props = context.scene.espresso_props
    if scope == SCOPE_SCENE:
        # Deliberately before the active-driver lookup: this scope does not
        # care what is active, and must not fail because nothing is.
        return _scene_motion_fcurves(context, props)
    active, _driver, reason = utils.get_target_driver_fcurve(context)
    if scope == SCOPE_ACTIVE:
        return ([active] if active is not None else []), reason
    if scope == SCOPE_MOTION:
        entry = target_memory.latest_entry(props)
        if not entry or entry.get("apply_kind") != "motion":
            return [], "Apply or select an Espresso Motion Set first."
        if entry.get("template_id") != template.get("id"):
            return [], "Select the same Motion Set template that was applied most recently."
        fcurves = _entry_fcurves(entry)
        return fcurves, "" if fcurves else "The remembered Motion Set no longer exists."
    if active is None:
        return [], reason
    pair_id = template.get("pair_with")
    if not pair_id:
        return [], "This template has no declared Related Pair."
    pair_entry = target_memory.recent_template_entry(props, pair_id)
    if not pair_entry:
        return [], (
            f"Apply {templates.TEMPLATE_BY_ID.get(pair_id, {}).get('name', pair_id)} "
            "once so Espresso can identify its paired target."
        )
    fcurves = ([active] if active is not None else []) + _entry_fcurves(pair_entry)
    return fcurves, "" if fcurves else "The related pair targets no longer exist."


def _binding_recipe_driver(scene, fcurve, binding):
    carrier_path = (binding or {}).get("carrier_path", "")
    animation_data = getattr(scene, "animation_data", None)
    if carrier_path and animation_data is not None:
        carrier = animation_data.drivers.find(carrier_path)
        if carrier is not None:
            return carrier.driver
    return fcurve.driver


def _missing_recipe_variables(driver, template):
    """Delegate to the one shared implementation.

    This calls ``operators.missing_required_variables`` rather than keeping a
    near-copy of it with its own None-guard. Two copies of the same rule are
    exactly how the earlier float-formatter divergence happened, so this now
    forwards instead. Imported lazily because ``props`` reaches back into this
    module.
    """
    from ..actions.operators import missing_required_variables

    return missing_required_variables(driver, template)


def _active_object_fcurves(context):
    """Every driver owned by the active object, its data and its shape keys."""
    obj = getattr(context, "active_object", None)
    if obj is None:
        return []
    fcurves = []
    data = getattr(obj, "data", None)
    for owner in (obj, data, getattr(data, "shape_keys", None)):
        animation_data = getattr(owner, "animation_data", None)
        if animation_data is None:
            continue
        fcurves.extend(
            fcurve for fcurve in animation_data.drivers
            if getattr(fcurve, "driver", None)
        )
    return fcurves


def object_controller_uids(context, props):
    """Controller uids actually attached to a driver on the active object.

    Controllers live on the scene, so without this every object showed every
    controller. Ownership is derived from the binding records rather than a
    stored owner name, so it stays correct when objects are renamed or
    duplicated - a stored name would silently point at the wrong object.
    """
    bindings = read_bindings(props)
    uids = set()
    for fcurve in _active_object_fcurves(context):
        descriptor = _target_descriptor(fcurve)
        if not descriptor:
            continue
        binding = bindings.get(_target_key(descriptor))
        if binding and binding.get("controller_uid"):
            uids.add(binding["controller_uid"])
    return uids


def resolve_scope_control_status(context, template, scope, selected_uid=""):
    """Describe persisted Controller coverage for the currently selected scope."""
    fcurves, reason = resolve_scope_fcurves(context, template, scope)
    result = {
        "code": "UNAVAILABLE" if not fcurves else "NONE",
        "target_count": len(fcurves),
        "attached_count": 0,
        "controller_names": [],
        "strategies": [],
        "has_selected_controller": False,
        "missing_recipe_variables": [],
        "current_expression": (
            fcurves[0].driver.expression if fcurves else ""
        ),
        "broken_count": 0,
        "reason": reason,
    }
    if not fcurves:
        return result

    props = context.scene.espresso_props
    bindings = read_bindings(props)
    controllers = {
        item["uid"]: item.get("name", "Unnamed Controller")
        for item in read_controllers(props)
    }
    attached = []
    missing_recipe_variables = set()
    for fcurve in fcurves:
        descriptor = _target_descriptor(fcurve)
        binding = bindings.get(_target_key(descriptor)) if descriptor else None
        if not binding:
            continue
        attached.append(binding)
        driver = fcurve.driver
        variable = driver.variables.get(CONTROLLER_VARIABLE)
        if (
            variable is None
            or CONTROLLER_VARIABLE not in driver.expression
        ):
            result["broken_count"] += 1
        if binding.get("template_id") == (template or {}).get("id"):
            recipe_driver = _binding_recipe_driver(
                context.scene, fcurve, binding,
            )
            missing_recipe_variables.update(
                _missing_recipe_variables(recipe_driver, template)
            )

    result["attached_count"] = len(attached)
    if not attached:
        return result

    controller_uids = {
        binding.get("controller_uid", "")
        for binding in attached
        if binding.get("controller_uid")
    }
    result["controller_names"] = sorted({
        controllers.get(uid, "Missing Controller")
        for uid in controller_uids
    })
    result["strategies"] = sorted({
        binding.get("strategy", "INLINE")
        for binding in attached
    })
    result["has_selected_controller"] = bool(
        selected_uid and selected_uid in controller_uids
    )
    result["missing_recipe_variables"] = sorted(missing_recipe_variables)

    if len(attached) < len(fcurves):
        result["code"] = "PARTIAL"
    elif len(controller_uids) > 1:
        result["code"] = "MIXED"
    elif selected_uid and selected_uid not in controller_uids:
        result["code"] = "MIXED"
    else:
        result["code"] = "ATTACHED"
    return result


def relink_controller_bindings(scene, props, controller):
    """Re-apply a controller to every driver it is attached to.

    Rebinding the source variable alone is NOT enough. Interpretation,
    threshold, invert, clamp and response curve are baked into each driver's
    expression *string* when it is attached, so editing them must recompile and
    reinstall every attached expression — otherwise the edit updates only the
    controller's stored metadata and the drivers keep running the old maths
    while the UI reports success.

    Re-attaching reuses ``attach_fcurves`` so carrier/inline transitions, the
    length guard and reserved-name checks all stay in one place. Each driver
    keeps its own recorded reset mode and rest value, so re-applying never
    moves a driver's anchor.
    """
    source, reason = source_binding.resolve_source(controller.get("source"))
    if source is None:
        return False, reason or "The controller source is unavailable.", 0

    targets = []
    for binding in read_bindings(props).values():
        if binding.get("controller_uid") != controller["uid"]:
            continue
        fcurve = driver_targets.resolve_driver(binding.get("target") or {})
        if fcurve is not None:
            targets.append((fcurve, dict(binding)))
    if not targets:
        return True, "Updated 0 attached driver(s).", 0

    bindings_before = getattr(props, "live_control_bindings", "{}")
    snapshots = [(fcurve, snapshot_driver(fcurve.driver)) for fcurve, _ in targets]
    count = 0
    for fcurve, binding in targets:
        template = templates.TEMPLATE_BY_ID.get(binding.get("template_id", "")) or {}
        reset_mode = binding.get("reset_mode", RESET_CAPTURED)
        # The stored rest value is passed as both the custom and baseline
        # candidate so whichever mode this driver was attached with resolves
        # back to the exact same anchor it already had.
        stored_rest = float(binding.get("rest_value", 0.0))
        ok, message, _applied = attach_fcurves(
            scene, props, [fcurve], controller,
            reset_mode, stored_rest, stored_rest, template,
        )
        if not ok:
            for target_fcurve, snapshot in snapshots:
                try:
                    restore_driver(target_fcurve.driver, snapshot)
                except Exception:
                    pass
            props.live_control_bindings = bindings_before
            return False, f"Controller update was rolled back: {message}", 0
        count += 1
    return True, f"Updated {count} attached driver(s).", count


def delete_controller(scene, props, uid):
    bindings = read_bindings(props)
    matching = {
        key: item for key, item in bindings.items()
        if item.get("controller_uid") == uid
    }
    fcurves = [
        driver_targets.resolve_driver(item.get("target") or {})
        for item in matching.values()
    ]
    fcurves = [item for item in fcurves if item is not None]
    if fcurves:
        ok, message, _count = remove_fcurves(scene, props, fcurves)
        if not ok:
            return False, message
    bindings = read_bindings(props)
    for key, item in matching.items():
        if key in bindings:
            for path in (item.get("carrier_path"), *(item.get("retired_carrier_paths") or ())):
                _remove_carrier(scene, path)
            bindings.pop(key, None)
    write_bindings(props, bindings)
    controllers = [item for item in read_controllers(props) if item["uid"] != uid]
    write_controllers(props, controllers)
    props.live_controller_uid = controllers[0]["uid"] if controllers else NO_CONTROLLER
    return True, "Controller deleted and its original drivers restored."


def _source_summary(controller):
    source = controller.get("source") or {}
    status = source_binding.source_status(source)
    return source.get("display_label", "Unknown source"), status


def input_source_ui_state(props):
    """Describe the pending Espresso Input without scanning any drivers."""
    source = source_binding.latest_source(props)
    status = source_binding.source_status(source)
    code = status["code"]
    if code == source_binding.SOURCE_ACTIVE:
        return {
            "code": code,
            "headline": "Input detected",
            "label": source.get("display_label", "Espresso Input"),
            "detail": "",
            "can_create": True,
            "can_clear": True,
        }
    if code == source_binding.SOURCE_EMPTY:
        return {
            "code": code,
            "headline": "No Espresso Input detected",
            "label": "",
            "detail": "Right-click a property and choose Use as Espresso Input.",
            "can_create": False,
            "can_clear": False,
        }
    return {
        "code": code,
        "headline": "Input detected, but unavailable",
        "label": source.get("display_label", "Remembered Espresso Input") if source else "",
        "detail": status["reason"] or "The remembered input source is missing.",
        "can_create": False,
        "can_clear": True,
    }


class ESPRESSO_OT_input_source_help(bpy.types.Operator):
    bl_idname = "espresso.input_source_help"
    bl_label = "Espresso Input"
    bl_description = "Right-click a property and choose Use as Espresso Input"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        self.report({"INFO"}, self.bl_description)
        return {"FINISHED"}


class ESPRESSO_OT_create_controller(bpy.types.Operator):
    bl_idname = "espresso.create_controller"
    bl_label = "Create Espresso Controller"
    bl_description = "Create a reusable controller from the current Espresso Input"
    bl_options = {"REGISTER", "UNDO"}

    controller_name: bpy.props.StringProperty(name="Controller Name", default="Controller")
    interpretation: bpy.props.EnumProperty(
        name="Input Interpretation",
        items=[
            (live_control_core.INTERPRET_SWITCH, "On / Off Switch", "Values above the threshold enable the driver"),
            (live_control_core.INTERPRET_INFLUENCE, "Float Influence", "Blend continuously between rest and the driven result"),
        ],
        default=live_control_core.INTERPRET_SWITCH,
    )
    threshold: bpy.props.FloatProperty(name="On Threshold", default=0.5)
    invert: bpy.props.BoolProperty(name="Invert", default=False)
    clamp: bpy.props.BoolProperty(name="Clamp to 0–1", default=True)
    response_curve: bpy.props.EnumProperty(
        name="Response",
        items=[
            (live_control_core.RESPONSE_LINEAR, "Linear", "Direct influence"),
            (live_control_core.RESPONSE_SMOOTH, "Smooth", "Smoothstep response"),
            (live_control_core.RESPONSE_EASE_IN_OUT, "Ease In-Out", "Smootherstep response"),
        ],
        default=live_control_core.RESPONSE_SMOOTH,
    )
    transition_mode: bpy.props.EnumProperty(
        name="Transition",
        items=[
            (live_control_core.TRANSITION_IMMEDIATE, "Immediate", "Apply without a ramp"),
            (live_control_core.TRANSITION_LINEAR, "Linear", "Blend in at a constant rate"),
            (live_control_core.TRANSITION_SMOOTH, "Smooth", "Smoothstep blend in"),
            (live_control_core.TRANSITION_EASE_IN_OUT, "Ease In-Out", "Smoother eased blend in"),
        ],
        default=live_control_core.TRANSITION_IMMEDIATE,
    )
    transition_duration: bpy.props.IntProperty(
        name="Transition Duration", default=8, min=0, soft_max=48, subtype="TIME",
    )

    def invoke(self, context, _event):
        source = source_binding.latest_source(context.scene.espresso_props)
        label = (source or {}).get("display_label", "Controller")
        self.controller_name = label.split(">")[-1].strip() or "Controller"
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        source = source_binding.latest_source(context.scene.espresso_props)
        box = layout.box()
        box.label(text="Source", icon="LINKED")
        box.label(text=(source or {}).get("display_label", "No Espresso Input selected"))
        layout.prop(self, "controller_name")
        layout.prop(self, "interpretation")
        if self.interpretation == live_control_core.INTERPRET_SWITCH:
            layout.prop(self, "threshold")
        else:
            layout.prop(self, "clamp")
            layout.prop(self, "response_curve")
        layout.prop(self, "invert")
        layout.prop(self, "transition_mode")
        if self.transition_mode != live_control_core.TRANSITION_IMMEDIATE:
            layout.prop(self, "transition_duration")

    def execute(self, context):
        props = context.scene.espresso_props
        controller, message = create_controller(
            props,
            source_binding.latest_source(props),
            self.controller_name,
            self.interpretation,
            threshold=self.threshold,
            invert=self.invert,
            clamp=self.clamp,
            response_curve=self.response_curve,
            transition_mode=self.transition_mode,
            transition_duration=self.transition_duration,
        )
        if controller is None:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, f'Created controller "{controller["name"]}".')
        return {"FINISHED"}


class ESPRESSO_OT_edit_controller(bpy.types.Operator):
    bl_idname = "espresso.edit_controller"
    bl_label = "Configure Espresso Controller"
    bl_description = "Rename, reconfigure, or relink the selected controller"
    bl_options = {"REGISTER", "UNDO"}

    controller_name: bpy.props.StringProperty(name="Controller Name")
    interpretation: bpy.props.EnumProperty(
        name="Input Interpretation",
        items=[
            (live_control_core.INTERPRET_SWITCH, "On / Off Switch", ""),
            (live_control_core.INTERPRET_INFLUENCE, "Float Influence", ""),
        ],
    )
    threshold: bpy.props.FloatProperty(name="On Threshold", default=0.5)
    invert: bpy.props.BoolProperty(name="Invert", default=False)
    clamp: bpy.props.BoolProperty(name="Clamp to 0–1", default=True)
    response_curve: bpy.props.EnumProperty(
        name="Response",
        items=[
            (live_control_core.RESPONSE_LINEAR, "Linear", ""),
            (live_control_core.RESPONSE_SMOOTH, "Smooth", ""),
            (live_control_core.RESPONSE_EASE_IN_OUT, "Ease In-Out", ""),
        ],
    )
    transition_mode: bpy.props.EnumProperty(
        name="Transition",
        items=[
            (live_control_core.TRANSITION_IMMEDIATE, "Immediate", ""),
            (live_control_core.TRANSITION_LINEAR, "Linear", ""),
            (live_control_core.TRANSITION_SMOOTH, "Smooth", ""),
            (live_control_core.TRANSITION_EASE_IN_OUT, "Ease In-Out", ""),
        ],
    )
    transition_duration: bpy.props.IntProperty(
        name="Transition Duration", default=8, min=0, soft_max=48, subtype="TIME",
    )
    relink_from_input: bpy.props.BoolProperty(
        name="Relink from Current Espresso Input",
        description="Replace this controller's source with the property currently marked Use as Espresso Input",
        default=False,
    )

    def invoke(self, context, _event):
        controller = selected_controller(context.scene.espresso_props)
        if controller is None:
            self.report({"WARNING"}, "No controller selected.")
            return {"CANCELLED"}
        self.controller_name = controller["name"]
        self.interpretation = controller["interpretation"]
        self.threshold = controller.get("threshold", 0.5)
        self.invert = controller.get("invert", False)
        self.clamp = controller.get("clamp", True)
        self.response_curve = controller.get("response_curve", live_control_core.RESPONSE_SMOOTH)
        self.transition_mode = controller.get(
            "transition_mode", live_control_core.TRANSITION_IMMEDIATE,
        )
        self.transition_duration = int(controller.get("transition_duration", 0))
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        controller = selected_controller(context.scene.espresso_props)
        layout = self.layout
        if controller:
            label, status = _source_summary(controller)
            box = layout.box()
            box.label(text="Current Source", icon="LINKED")
            box.label(text=label)
            if status["code"] != source_binding.SOURCE_ACTIVE:
                box.alert = True
                box.label(text=status["reason"], icon="ERROR")
        layout.prop(self, "controller_name")
        layout.prop(self, "interpretation")
        if self.interpretation == live_control_core.INTERPRET_SWITCH:
            layout.prop(self, "threshold")
        else:
            layout.prop(self, "clamp")
            layout.prop(self, "response_curve")
        layout.prop(self, "invert")
        layout.prop(self, "transition_mode")
        if self.transition_mode != live_control_core.TRANSITION_IMMEDIATE:
            layout.prop(self, "transition_duration")
        pending = source_binding.latest_source(context.scene.espresso_props)
        row = layout.row()
        row.enabled = bool(pending)
        row.prop(self, "relink_from_input")

    def execute(self, context):
        props = context.scene.espresso_props
        current = selected_controller(props)
        if current is None:
            self.report(
                {"WARNING"},
                "That controller no longer exists — it may have been removed or undone.",
            )
            return {"CANCELLED"}
        values = {
            "name": self.controller_name,
            "interpretation": self.interpretation,
            "threshold": self.threshold,
            "invert": self.invert,
            "clamp": self.clamp,
            "response_curve": self.response_curve,
            "transition_mode": self.transition_mode,
            "transition_duration": self.transition_duration,
        }
        if self.relink_from_input:
            values["source"] = source_binding.latest_source(props)
        controller, message = update_controller(props, current["uid"], values)
        if controller is None:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        ok, message, _count = relink_controller_bindings(context.scene, props, controller)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, f'Updated controller "{controller["name"]}".')
        return {"FINISHED"}


class ESPRESSO_MT_controller_scene_motions(bpy.types.Menu):
    """Every driver-backed motion in the scene a controller could attach to."""

    bl_label = "Scene Motions"
    bl_idname = "ESPRESSO_MT_controller_scene_motions"

    def draw(self, context):
        layout = self.layout
        choices = scene_motion_choices(context)
        if not choices:
            layout.label(text="No driver-backed motions in this scene", icon="INFO")
            return
        current = str(context.scene.espresso_props.live_control_scene_token or "")
        for token, label in choices:
            op = layout.operator(
                "espresso.select_controller_scene_motion", text=label,
                icon="RADIOBUT_ON" if token == current else "RADIOBUT_OFF",
            )
            op.record_token = token


class ESPRESSO_OT_select_controller_scene_motion(bpy.types.Operator):
    bl_idname = "espresso.select_controller_scene_motion"
    bl_label = "Choose Scene Motion"
    bl_description = "Attach the controller to this motion, wherever it lives in the scene"
    bl_options = {"INTERNAL", "UNDO"}

    record_token: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.espresso_props
        props.live_control_scene_token = self.record_token
        effect = scene_motion_effect(context, props)
        if effect is None:
            self.report({"WARNING"}, "That motion no longer resolves in this scene.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Controller scope: %s." % scene_motion_label(effect))
        return {"FINISHED"}


class ESPRESSO_OT_attach_controller(bpy.types.Operator):
    bl_idname = "espresso.attach_controller"
    bl_label = "Attach / Update Controller"
    bl_description = "Attach the selected controller to the chosen driver scope"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        controller = selected_controller(props)
        if controller is None:
            self.report({"WARNING"}, "Create or select a controller first.")
            return {"CANCELLED"}
        template = scope_template(
            context, templates.TEMPLATE_BY_ID.get(props.template), props.live_control_scope)
        fcurves, reason = resolve_scope_fcurves(context, template, props.live_control_scope)
        if not fcurves:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        ok, message, _count = attach_fcurves(
            context.scene,
            props,
            fcurves,
            controller,
            props.live_reset_mode,
            props.live_custom_value,
            trusted_template_baseline(template, props),
            template,
        )
        self.report({"INFO"} if ok else {"WARNING"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class ESPRESSO_OT_remove_controller(bpy.types.Operator):
    bl_idname = "espresso.remove_controller"
    bl_label = "Remove Control"
    bl_description = "Remove the controller layer and restore the original driver exactly"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        template = scope_template(
            context, templates.TEMPLATE_BY_ID.get(props.template), props.live_control_scope)
        fcurves, reason = resolve_scope_fcurves(context, template, props.live_control_scope)
        if not fcurves:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        ok, message, _count = remove_fcurves(context.scene, props, fcurves)
        self.report({"INFO"} if ok else {"WARNING"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class ESPRESSO_OT_safe_disable_controller(bpy.types.Operator):
    bl_idname = "espresso.safe_disable_controller"
    bl_label = "Disable & Hold"
    bl_description = "Capture the current result and pause this Controller without deleting its driver setup"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        template = scope_template(
            context, templates.TEMPLATE_BY_ID.get(props.template), props.live_control_scope)
        fcurves, reason = resolve_scope_fcurves(context, template, props.live_control_scope)
        if not fcurves:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        ok, message, _count = set_fcurves_enabled(
            context.scene, props, fcurves, False, template,
        )
        self.report({"INFO"} if ok else {"WARNING"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class ESPRESSO_OT_enable_controller(bpy.types.Operator):
    bl_idname = "espresso.enable_controller"
    bl_label = "Resume Controller"
    bl_description = "Restore the captured Controller driver exactly"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        template = scope_template(
            context, templates.TEMPLATE_BY_ID.get(props.template), props.live_control_scope)
        fcurves, reason = resolve_scope_fcurves(context, template, props.live_control_scope)
        if not fcurves:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        ok, message, _count = set_fcurves_enabled(
            context.scene, props, fcurves, True, template,
        )
        self.report({"INFO"} if ok else {"WARNING"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class ESPRESSO_OT_relink_controller_source(bpy.types.Operator):
    bl_idname = "espresso.relink_controller_source"
    bl_label = "Update Input Source"
    bl_description = (
        "Replace this controller's stored source with the current Espresso Input "
        "and relink every attached driver"
    )
    bl_options = {"REGISTER", "UNDO"}

    controller_uid: bpy.props.StringProperty(default="", options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.espresso_props
        controller = (
            controller_by_uid(props, self.controller_uid)
            if self.controller_uid
            else selected_controller(props)
        )
        if controller is None:
            self.report({"WARNING"}, "Select a controller first.")
            return {"CANCELLED"}
        pending = source_binding.latest_source(props)
        status = source_binding.source_status(pending)
        if status["code"] != source_binding.SOURCE_ACTIVE:
            self.report(
                {"WARNING"},
                status["reason"] or "Mark a property as Use as Espresso Input first.",
            )
            return {"CANCELLED"}
        controllers_before = copy.deepcopy(read_controllers(props))
        updated, message = update_controller(
            props,
            controller["uid"],
            {"name": controller["name"], "source": pending},
        )
        if updated is None:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        ok, message, count = relink_controller_bindings(context.scene, props, updated)
        if not ok:
            # Updating the stored controller and recompiling attached drivers
            # form one transaction. Keeping the new source after driver
            # rollback would make the UI claim a source the drivers do not use.
            write_controllers(props, controllers_before)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f'Updated input source for "{updated["name"]}" ({count} driver(s) relinked).',
        )
        return {"FINISHED"}


class ESPRESSO_OT_delete_controller(bpy.types.Operator):
    bl_idname = "espresso.delete_controller"
    bl_label = "Delete Espresso Controller"
    bl_description = "Delete this controller and restore every driver it controls"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        props = context.scene.espresso_props
        controller = selected_controller(props)
        if controller is None:
            self.report({"WARNING"}, "No controller selected — it may have already been removed.")
            return {"CANCELLED"}
        ok, message = delete_controller(context.scene, props, controller["uid"])
        self.report({"INFO"} if ok else {"WARNING"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


def draw_panel(layout, props, template, context):
    input_state = input_source_ui_state(props)
    all_controllers = read_controllers(props)
    owned_uids = object_controller_uids(context, props)
    # Show only the controllers this object actually uses. The full list stays
    # reachable through the dropdown, so attaching one to another object is a
    # deliberate act rather than the default everywhere.
    controllers = [item for item in all_controllers if item["uid"] in owned_uids]

    picked = selected_controller(props)

    # The Espresso Input readout only matters while you are sourcing a new
    # controller. Once one is loaded it is redundant - the controller already
    # names its own source on the row below - so it just eats panel height.
    if picked is None and not controllers:
        from ..views import source_display

        source_display.draw_pending_source_card(layout, context, title="ESPRESSO INPUT")

    # Only bail out when there is genuinely nothing to act on. Picking a
    # scene controller that is not on this object yet has to fall through to
    # the normal body, otherwise the dropdown is a dead end: the Attach
    # Controller button lives further down and never gets drawn.
    if not controllers and picked is None:
        row = layout.row(align=True)
        row.enabled = bool(all_controllers)
        row.prop(props, "live_controller_uid", text="Controller")
        create_row = layout.row()
        create_row.enabled = input_state["can_create"]
        create_row.operator(
            "espresso.create_controller",
            text="Create New Controller from Input",
            icon="ADD",
        )
        return

    controller = picked
    # True when the dropdown is blank but this object is driven anyway, so the
    # rows below describe a controller the enum is not naming.
    resolved_fallback = controller is None
    if resolved_fallback:
        controller = controllers[0]
    row = layout.row(align=True)
    row.prop(props, "live_controller_uid", text="Controller")
    if resolved_fallback:
        # The enum is blank (or points elsewhere) but this object really is
        # driven by a controller, so name the one the rows below describe.
        attached = layout.row()
        attached.enabled = False
        attached.label(text=f"Attached: {controller['name']}", icon="LINKED")
    add_row = row.row(align=True)
    add_row.enabled = input_state["can_create"]
    add_row.operator("espresso.create_controller", text="", icon="ADD")
    # Edit and Delete act on the dropdown selection, so they are dead while it
    # is blank - grey them out instead of letting them report a warning.
    edit_row = row.row(align=True)
    edit_row.enabled = not resolved_fallback
    edit_row.operator("espresso.edit_controller", text="", icon="PREFERENCES")
    edit_row.operator("espresso.delete_controller", text="", icon="X")

    # Compact row for a loaded controller's stored source. The pending-input
    # card above already shows the path when a source is present.
    from ..views import source_display

    source_display.draw_source_row(
        layout,
        bpy.context,
        source_display.SOURCE_CONTROLLER,
        controller=controller,
    )
    mode_label = (
        "On / Off Switch"
        if controller["interpretation"] == live_control_core.INTERPRET_SWITCH
        else "Float Influence"
    )
    layout.label(text=f"Mode: {mode_label}", icon="DRIVER")
    layout.prop(props, "live_reset_mode", text="Reset")
    if props.live_reset_mode == RESET_CUSTOM:
        layout.prop(props, "live_custom_value", text="Custom Value")
    elif (
        props.live_reset_mode == RESET_BASELINE
        and trusted_template_baseline(template, props) is None
    ):
        warning = layout.row()
        warning.alert = True
        warning.label(text="No audited Template Baseline for this template.", icon="ERROR")
    layout.prop(props, "live_control_scope", text="Scope")
    if props.live_control_scope == SCOPE_SCENE:
        # The picker, right under the scope that needs it. The closed control
        # names what is chosen, so the status box below never has to be read
        # to know which motion this controller is about.
        chosen = scene_motion_effect(context, props)
        picker = layout.row(align=True)
        picker.label(text="Motion:")
        picker.menu(
            "ESPRESSO_MT_controller_scene_motions",
            text=scene_motion_label(chosen) if chosen else "Choose a motion in the scene",
            icon="DOWNARROW_HLT",
        )

    control_status = resolve_scope_control_status(
        context,
        template,
        props.live_control_scope,
        controller["uid"],
    )
    status_box = layout.box()
    status_box.label(text="TARGET CONTROL STATUS", icon="DRIVER")
    status_row = status_box.row()
    status_row.alert = (
        control_status["code"] in {"PARTIAL", "MIXED"}
        or control_status["broken_count"] > 0
    )
    if control_status["code"] == "UNAVAILABLE":
        status_row.label(text="No compatible target selected", icon="INFO")
        if control_status["reason"]:
            for line in utils.wrap_text(control_status["reason"], width=54):
                status_box.label(text=line, icon="BLANK1")
    elif control_status["code"] == "NONE":
        status_row.label(
            text="No Espresso Controller attached",
            icon="RADIOBUT_OFF",
        )
        status_box.label(
            text=(
                "The target has no recorded espctl_ controller layer."
                if _diagnostics_enabled(context)
                else "The target has no recorded controller layer."
            ),
            icon="BLANK1",
        )
    else:
        target_count = control_status["target_count"]
        attached_count = control_status["attached_count"]
        if control_status["code"] == "ATTACHED":
            chosen = (scene_motion_effect(context, props)
                      if props.live_control_scope == SCOPE_SCENE else None)
            headline = (
                f"Attached to {scene_motion_label(chosen)}"
                if chosen is not None
                else "Attached to Active Driver"
                if target_count == 1
                else f"Attached to all {target_count} scoped drivers"
            )
            icon = "CHECKMARK"
        elif control_status["code"] == "PARTIAL":
            headline = (
                f"Attached to {attached_count} of {target_count} scoped drivers"
            )
            icon = "ERROR"
        else:
            headline = "Scoped drivers use a different or mixed Controller"
            icon = "ERROR"
        status_row.label(text=headline, icon=icon)
        names = ", ".join(control_status["controller_names"])
        strategies = " + ".join(
            strategy.title() for strategy in control_status["strategies"]
        )
        if names:
            status_box.label(
                text=f"{names} · {strategies or 'Controller'}",
                icon="BLANK1",
            )
        if control_status["broken_count"]:
            broken_row = status_box.row()
            broken_row.alert = True
            broken_row.label(
                text=(
                    "Recorded binding is damaged: espctl_ is missing."
                    if _diagnostics_enabled(context)
                    else "Recorded binding is damaged."
                ),
                icon="ERROR",
            )
        else:
            if _diagnostics_enabled(context):
                marker = (
                    "Target uses espbase + espctl_."
                    if "CARRIER" in control_status["strategies"]
                    else "Target expression contains espctl_."
                )
            else:
                marker = (
                    "Target uses a controller layer."
                    if "CARRIER" in control_status["strategies"]
                    else "Target expression includes the controller."
                )
            status_box.label(text=marker, icon="BLANK1")

    if control_status["missing_recipe_variables"]:
        recipe_row = status_box.row()
        recipe_row.alert = True
        recipe_row.label(
            text=(
                "Controller attached, but recipe input is missing: "
                + ", ".join(control_status["missing_recipe_variables"])
            ),
            icon="ERROR",
        )

    actions = layout.row(align=True)
    attach_action = actions.row(align=True)
    attach_action.enabled = (
        input_state["code"] == source_binding.SOURCE_ACTIVE
        and control_status["target_count"] > 0
    )
    if control_status["has_selected_controller"]:
        attach_label = "Update Controller"
    elif control_status["attached_count"]:
        attach_label = "Replace Controller"
    else:
        attach_label = "Attach Controller"
    attach_action.operator(
        "espresso.attach_controller",
        text=attach_label,
        icon="LINKED",
    )
    remove_action = actions.row(align=True)
    remove_action.enabled = control_status["attached_count"] > 0
    remove_action.operator(
        "espresso.remove_controller",
        text="Remove Control",
        icon="UNLINKED",
    )
    capture_actions = layout.row(align=True)
    capture_actions.enabled = control_status["attached_count"] > 0
    capture_actions.operator(
        "espresso.safe_disable_controller",
        text="Disable & Hold",
        icon="PAUSE",
    )
    capture_actions.operator(
        "espresso.enable_controller",
        text="Resume",
        icon="PLAY",
    )
    if _unsupported_template_reason(template):
        notice = layout.row()
        notice.alert = True
        notice.label(text="Reserved for captured controls", icon="ERROR")


CLASSES = (
    ESPRESSO_OT_input_source_help,
    ESPRESSO_OT_create_controller,
    ESPRESSO_OT_edit_controller,
    ESPRESSO_MT_controller_scene_motions,
    ESPRESSO_OT_select_controller_scene_motion,
    ESPRESSO_OT_attach_controller,
    ESPRESSO_OT_remove_controller,
    ESPRESSO_OT_safe_disable_controller,
    ESPRESSO_OT_enable_controller,
    ESPRESSO_OT_relink_controller_source,
    ESPRESSO_OT_delete_controller,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
