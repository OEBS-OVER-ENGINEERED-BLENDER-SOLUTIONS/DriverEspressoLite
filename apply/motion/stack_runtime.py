"""Transactional Blender adapter for direct and native-helper Motion Stacks."""

from __future__ import annotations

import hashlib
import uuid

import bpy

from ...engine.motion_stack.compiler import CompilationRoute, compile_stack
from ...engine.motion_stack.stack import MotionStack, resolve_active_layers, validate_stack
from ...generated import animation as generated_animation
from ...generated import helpers as generated_helpers
from ...generated import properties as generated_properties
from . import applied_motion, stack_records
from ..core import target_memory


STACK_EFFECT_PREFIX = "STK"


def _slug(value):
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]


def _helper_key(stack_id, layer_id):
    return "__espresso_stack_%s_%s" % (_slug(stack_id), _slug(layer_id))


def _target_channel(data_path, index):
    return "%s[%d]" % (data_path, int(index))


def _copy_driver_state(curve):
    if curve is None:
        return None
    driver = curve.driver
    variables = []
    for variable in driver.variables:
        targets = []
        for target in variable.targets:
            targets.append({
                "id": target.id,
                "data_path": target.data_path,
                "transform_type": target.transform_type,
                "transform_space": target.transform_space,
                "bone_target": target.bone_target,
            })
        variables.append((variable.name, variable.type, targets))
    return driver.type, driver.expression, variables


def serialize_driver_variables(driver):
    """Persist native variable bindings without retaining Python RNA pointers."""
    result = []
    for variable in tuple(getattr(driver, "variables", ()) or ()):
        targets = []
        for target in variable.targets:
            id_block = target.id
            targets.append({
                "id_type": id_block.__class__.__name__ if id_block else "",
                "id_name": getattr(id_block, "name", "") if id_block else "",
                "data_path": str(target.data_path or ""),
                "transform_type": str(target.transform_type or ""),
                "transform_space": str(target.transform_space or ""),
                "bone_target": str(target.bone_target or ""),
            })
        result.append({
            "name": str(variable.name), "type": str(variable.type),
            "targets": targets,
        })
    return tuple(result)


def _restore_serialized_variables(driver, bindings):
    for binding in bindings or ():
        variable = driver.variables.new()
        variable.name = str(binding.get("name") or "var")
        variable.type = str(binding.get("type") or "SINGLE_PROP")
        for target, values in zip(variable.targets, binding.get("targets", ())):
            id_block = target_memory._find_id_block(
                str(values.get("id_type") or ""), str(values.get("id_name") or ""),
            )
            if id_block is None and values.get("id_name"):
                raise ValueError(
                    "Motion Stack variable source no longer resolves: %s" % values["id_name"]
                )
            target.id = id_block
            for key in ("data_path", "transform_type", "transform_space", "bone_target"):
                value = values.get(key)
                if value:
                    setattr(target, key, value)


def _restore_driver(owner, data_path, index, state):
    if state is None:
        return
    curve = owner.driver_add(data_path, index)
    driver = curve.driver
    driver.type, driver.expression = state[0], state[1]
    for name, variable_type, targets in state[2]:
        variable = driver.variables.new()
        variable.name = name
        variable.type = variable_type
        for target, values in zip(variable.targets, targets):
            target.id = values["id"]
            target.data_path = values["data_path"]
            target.transform_type = values["transform_type"]
            target.transform_space = values["transform_space"]
            target.bone_target = values["bone_target"]


def _stack_code(stack_id):
    return (STACK_EFFECT_PREFIX + _slug(stack_id).upper())[:11]


def _remove_owner_helper_keys(owner, keys):
    for key in keys or ():
        try:
            owner.driver_remove('["%s"]' % key)
        except (RuntimeError, TypeError):
            pass
        if key in owner:
            del owner[key]


def _remove_helper_object(name):
    helper = bpy.data.objects.get(str(name or ""))
    return int(generated_helpers.remove_empty(helper)) if helper is not None else 0


def _build_helper_object(scene, motion, layers, expression_limit):
    """Build a fully independent helper before touching the driven target."""
    setup_id = "motion-stack.%s" % _slug(motion.stack_id)
    helper = generated_helpers.create_empty(
        scene,
        "Espresso Motion Stack · %s" % _slug(motion.stack_id),
        resource_id="motion-stack.helper.%s" % uuid.uuid4().hex[:12],
        setup_id=setup_id,
        effect_id=_stack_code(motion.stack_id),
        role="MOTION_STACK_HELPER",
    )
    helper.hide_render = True
    helper.hide_select = True
    keys = []
    try:
        for layer in layers:
            # The caller's lower limit is a routing-test budget; each helper
            # still receives Blender's real hard limit independently.
            if len(layer.expression) > 255:
                raise ValueError(
                    "Motion Stack layer %r exceeds Blender's expression limit."
                    % layer.label
                )
            key = _helper_key(motion.stack_id, layer.layer_id)
            keys.append(key)
            generated_properties.define(
                helper, key, 0.0,
                description="Native intermediate for %s" % layer.label,
            )
            curve = helper.driver_add('["%s"]' % key)
            curve.driver.type = "SCRIPTED"
            curve.driver.expression = layer.expression
            _restore_serialized_variables(curve.driver, layer.variables)
        helper.update_tag(refresh={"OBJECT"})
        return helper, tuple(keys)
    except Exception:
        generated_helpers.remove_empty(helper)
        raise


def apply(owner, data_path, index, motion: MotionStack, *, expression_limit=255):
    """Apply one validated stack atomically to one numeric target channel."""
    try:
        validate_stack(motion)
        channel = _target_channel(data_path, index)
        if any(layer.channel != channel for layer in resolve_active_layers(motion)):
            raise ValueError("Every layer in this vertical slice must target %s." % channel)
        plan = compile_stack(motion, expression_limit=expression_limit)
        if len(plan.channels) != 1:
            raise ValueError("This stack adapter requires exactly one target channel.")
        compiled = plan.channels[0]
    except (TypeError, ValueError) as exc:
        return False, str(exc)

    animation = getattr(owner, "animation_data", None)
    previous_curve = animation.drivers.find(data_path, index=index) if animation else None
    previous_state = _copy_driver_state(previous_curve)
    helper_keys = ()
    helper_object = None
    previous_record = applied_motion.read(owner)
    prior_stack_record = next(
        (item for item in previous_record if item.get("code") == _stack_code(motion.stack_id)),
        None,
    )
    old_helper_keys = tuple(
        (prior_stack_record or {}).get("extras", {}).get("helper_keys", ()),
    )
    old_helper_object_name = str(
        (prior_stack_record or {}).get("extras", {}).get("helper_object_name", ""),
    )
    try:
        layers = resolve_active_layers(motion)
        if compiled.route is CompilationRoute.NATIVE_HELPER:
            helper_object, helper_keys = _build_helper_object(
                bpy.context.scene, motion, layers, expression_limit,
            )
        if previous_curve is not None:
            owner.driver_remove(data_path, index)
        curve = owner.driver_add(data_path, index)
        driver = curve.driver
        driver.type = "SCRIPTED"
        driver.expression = compiled.expression
        if compiled.route is CompilationRoute.NATIVE_HELPER:
            for alias, layer in enumerate(layers):
                variable = driver.variables.new()
                variable.name = "s%d" % alias
                variable.type = "SINGLE_PROP"
                variable.targets[0].id = helper_object
                variable.targets[0].data_path = '["%s"]' % helper_keys[alias]
        else:
            _restore_serialized_variables(driver, compiled.variables)
        if len(driver.expression) > int(expression_limit):
            raise ValueError("Compiled driver still exceeds Blender's expression limit.")
        extras = stack_records.with_stack(
            {
                "template_id": "motion_stack",
                "helper_keys": list(helper_keys),
                "helper_object_name": getattr(helper_object, "name", ""),
            },
            motion,
        )
        applied_motion.remember(
            owner, _stack_code(motion.stack_id), "Motion Stack",
            [(data_path, index)], extras=extras,
        )
        _remove_helper_object(old_helper_object_name)
        _remove_owner_helper_keys(owner, old_helper_keys)
        owner.update_tag(refresh={"OBJECT"})
        return True, "Applied %d Motion Stack layer(s)." % len(motion.layers)
    except Exception as exc:
        try:
            owner.driver_remove(data_path, index)
        except (RuntimeError, TypeError):
            pass
        if helper_object is not None:
            generated_helpers.remove_empty(helper_object)
        applied_motion.write(owner, previous_record)
        _restore_driver(owner, data_path, index, previous_state)
        return False, str(exc)


def clear(owner, stack_id):
    code = _stack_code(stack_id)
    record = next((item for item in applied_motion.read(owner) if item.get("code") == code), None)
    if record is None:
        return 0
    removed = 0
    for data_path, index in applied_motion.paths_of(record):
        removed += int(owner.driver_remove(data_path, index))
    old_keys = tuple(record.get("extras", {}).get("helper_keys", ()))
    removed += _remove_helper_object(
        record.get("extras", {}).get("helper_object_name", ""),
    )
    removed += sum(1 for key in old_keys if key in owner)
    _remove_owner_helper_keys(owner, old_keys)
    applied_motion.forget(owner, code)
    return removed


def _action_fcurves(action):
    """``(container, curve)`` pairs, via the shared Action compatibility.

    The container is a channelbag on 4.4+ and the Action itself on 4.2 --
    both answer `.fcurves`, which is all the rollback below needs. Walking
    `layers` directly here found nothing on 4.2 and rolled back nothing.
    """
    from ...generated.resources import animation

    return animation.action_containers(action)


def _curve_snapshot(curve):
    return tuple({
        "co": tuple(point.co),
        "interpolation": point.interpolation,
        "easing": point.easing,
        "handle_left_type": point.handle_left_type,
        "handle_right_type": point.handle_right_type,
        "handle_left": tuple(point.handle_left),
        "handle_right": tuple(point.handle_right),
    } for point in curve.keyframe_points)


def _restore_curve_snapshot(curve, snapshot):
    while curve.keyframe_points:
        curve.keyframe_points.remove(curve.keyframe_points[-1], fast=True)
    for values in snapshot:
        point = curve.keyframe_points.insert(*values["co"], options={"FAST"})
        point.interpolation = values["interpolation"]
        point.easing = values["easing"]
        point.handle_left_type = values["handle_left_type"]
        point.handle_right_type = values["handle_right_type"]
        point.handle_left = values["handle_left"]
        point.handle_right = values["handle_right"]
    curve.update()


def _insert_and_verify_samples(owner, samples):
    """Write native keys transactionally while the live drivers still exist."""
    animation_data = owner.animation_data_create()
    previous_action = animation_data.action
    created_action = None
    if previous_action is None:
        created_action = generated_animation.create_action(
            "%sAction" % getattr(owner, "name", "Espresso"),
            resource_id="motion_stack_bake",
            role="baked_motion_stack",
        )
        animation_data.action = created_action
    action = animation_data.action
    existing = {
        (curve.data_path, int(curve.array_index)): (bag, curve)
        for bag, curve in _action_fcurves(action)
    }
    snapshots = {}
    created_curves = []
    try:
        for (data_path, index), values in samples.items():
            curve_index = max(0, int(index))
            key = (data_path, curve_index)
            entry = existing.get(key)
            if entry is None:
                from ...generated.resources import animation

                # `fcurve_ensure_for_datablock` arrived with slotted Actions
                # in 4.4; on 4.2 the helper creates through `Action.fcurves`.
                curve = animation.ensure_fcurve(
                    action, owner, data_path, index=curve_index,
                    group_name="Driver Espresso",
                )
                created_curves.append(curve)
                existing[key] = next(
                    (item for item in _action_fcurves(action) if item[1] == curve),
                    (None, curve),
                )
            else:
                curve = entry[1]
                snapshots[curve] = _curve_snapshot(curve)
            for frame, value in values:
                curve.keyframe_points.insert(frame, value, options={"FAST"})
            curve.update()
            for frame, value in values:
                point = next(
                    (item for item in curve.keyframe_points
                     if abs(float(item.co[0]) - float(frame)) < 1e-5),
                    None,
                )
                if point is None or abs(float(point.co[1]) - float(value)) > 1e-5:
                    raise RuntimeError(
                        "Could not verify native key %s[%d] at frame %s."
                        % (data_path, index, frame)
                    )
    except Exception:
        for curve, snapshot in snapshots.items():
            _restore_curve_snapshot(curve, snapshot)
        for curve in created_curves:
            entry = next(
                (item for item in _action_fcurves(action) if item[1] == curve), None,
            )
            if entry is not None:
                entry[0].fcurves.remove(curve)
        if created_action is not None:
            animation_data.action = previous_action
            generated_animation.remove_action(created_action, require_unused=False)
        raise


def bake_resolved(scene, owner, stack_id, *, start=None, end=None, step=1):
    """Bake the evaluated stack result, then detach all live stack resources."""
    code = _stack_code(stack_id)
    record = next((item for item in applied_motion.read(owner)
                   if item.get("code") == code), None)
    if record is None:
        return False, "The Motion Stack no longer resolves on this target."
    paths = applied_motion.paths_of(record)
    if not paths:
        return False, "The Motion Stack has no driven target channels."
    first = int(scene.frame_start if start is None else start)
    last = int(scene.frame_end if end is None else end)
    stride = max(1, int(step))
    original = int(scene.frame_current)
    samples = {path: [] for path in paths}
    try:
        for frame in range(first, last + 1, stride):
            scene.frame_set(frame)
            for data_path, index in paths:
                value = owner.path_resolve(data_path)
                samples[(data_path, index)].append(
                    (frame, float(value if index < 0 else value[index])),
                )
    finally:
        scene.frame_set(original)
    try:
        _insert_and_verify_samples(owner, samples)
    except Exception as exc:
        return False, "Native keyframing failed; the live stack was preserved: %s" % exc
    clear(owner, stack_id)
    scene.frame_set(original)
    return True, "Baked the resolved Motion Stack to native keyframes."
