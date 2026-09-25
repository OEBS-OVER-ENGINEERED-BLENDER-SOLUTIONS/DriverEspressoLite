"""Right-click UI property integration for Driver Espresso."""

from __future__ import annotations

import dataclasses
import json
import re
import uuid

import bpy

from ...product.identity import NAME as _PRODUCT_NAME

from ...apply import apply_behavior
from ...apply import internal_helpers, motion_channels
from ...engine.targeting.button_targeting import ButtonDriverTarget, expand_multi_targets, multi_target_count
from ..actions import operators
from ..state import props as espresso_props
from ...apply import source_binding
from ...apply import apply_target
from ...apply import shared_material_sweep
from ...apply import light_layout
from ...apply import target_memory
from ...apply import application_plan
from ...apply.core import driver_manager as core_driver_manager
from ...apply.setups import audio_reactivity
from ...engine import utils
from ...generated import helpers as generated_helpers
from ...catalogue.core.templates import TEMPLATE_BY_ID
from ...catalogue import templates as template_catalogue
from . import button_menu_map


@dataclasses.dataclass(frozen=True)
class TargetInspectorInfo:
    display_label: str
    technical_path: str
    target_kind: str
    apply_scope: str
    suggestion_ids: tuple[str, ...] = ()


_POPUP_TARGET_CACHE = {}


def _required_variable_names(template):
    return [var["name"] for var in template.get("requires_driver_variables", [])]


def _button_flow_block_message(template, scene_props=None, source_entry=None):
    scene = getattr(scene_props, "id_data", None) if scene_props is not None else None
    if scene is not None:
        audio_message = audio_reactivity.application_block_message(template, scene)
        if audio_message:
            return audio_message
    required = _required_variable_names(template)
    if not required:
        return ""
    # Self-position templates read the object the driver lands on, so there is
    # nothing to choose. The gate lets them through.
    if not source_binding.needs_input_source(template):
        return ""
    source_entry = source_entry if source_entry is not None else (
        source_binding.latest_source(scene_props) if scene_props is not None else None
    )
    if source_entry:
        status = source_binding.source_status(source_entry)
        if status["code"] == source_binding.SOURCE_ACTIVE:
            return ""
        return status["reason"]
    names = ", ".join(required)
    return "Choose an Espresso input source before applying this template: " + names


def _button_array_index(context):
    # Try the standard Blender 3.3+ index for UI context
    index = getattr(context, "index", -1)
    if isinstance(index, int) and index >= 0:
        return index
    
    # Fallback to button_index
    index = getattr(context, "button_index", -1)
    if isinstance(index, int) and index >= 0:
        return index

    return None


def _targets_from_context_property(context):
    active_property = getattr(context, "property", None)
    if not active_property:
        return []
    try:
        owner, data_path, index = active_property
    except Exception:
        return []
    if owner is None or not data_path:
        return []
    index = int(index) if isinstance(index, int) else -1
    if index < 0:
        rgb = _whole_colour_rgb_indices(owner, data_path)
        if rgb:
            return [ButtonDriverTarget(owner, data_path, i) for i in rgb]
    return [ButtonDriverTarget(owner, data_path, index)]


def _whole_colour_rgb_indices(owner, data_path):
    """R/G/B indices for a colour SWATCH click, alpha deliberately excluded.

    ``context.property`` reports index -1 when the swatch itself was clicked
    rather than one of its component sliders, and ``driver_add(path, -1)`` then
    drives every component - including alpha. Alpha is opacity, not colour, so
    a colour expression landing on it silently fades the object, which is never
    what "apply this colour" means.

    Only COLOUR arrays are expanded here. Every other array (location, scale,
    quaternion) keeps index -1 meaning "the whole property", because for those
    every component genuinely is part of the value.
    """
    try:
        prop = owner.bl_rna.properties.get(data_path)
    except Exception:
        return []
    if prop is None or not getattr(prop, "is_array", False):
        return []
    if getattr(prop, "subtype", "") not in {"COLOR", "COLOR_GAMMA"}:
        return []
    length = int(getattr(prop, "array_length", 0) or 0)
    return list(range(min(3, length))) if length >= 3 else []


def _custom_property_name(data_path):
    match = re.fullmatch(r'\["(.+)"\]', data_path or "")
    return match.group(1) if match else ""


def _channel_name(index, data_path, prop=None):
    if index < 0:
        return ""
    if data_path in {"location", "rotation_euler", "rotation_quaternion", "scale", "delta_location", "delta_rotation_euler", "delta_scale"}:
        labels = ("X", "Y", "Z", "W")
        if index < len(labels):
            return labels[index]
    if getattr(prop, "subtype", "") == "COLOR":
        labels = ("R", "G", "B", "A")
        if index < len(labels):
            return labels[index]
    return str(index)


def _label_from_transform(data_path, channel):
    label_map = {
        "location": "Location",
        "rotation_euler": "Rotation",
        "rotation_quaternion": "Rotation",
        "scale": "Scale",
        "delta_location": "Delta Location",
        "delta_rotation_euler": "Delta Rotation",
        "delta_scale": "Delta Scale",
    }
    base = label_map.get(data_path, data_path.replace("_", " ").title())
    return f"{base} {channel}".strip()


def _id_root_label(owner):
    id_data = getattr(owner, "id_data", None)
    if id_data is None:
        return owner.__class__.__name__
    owner_type = id_data.__class__.__name__.replace("Object", "Object")
    owner_name = getattr(id_data, "name", owner_type)
    return f"{owner_type} > {owner_name}"


def _technical_target_path(owner, data_path, index):
    owner_path = ""
    try:
        owner_path = owner.path_from_id()
    except Exception:
        owner_path = ""
    if owner_path:
        joined = owner_path + (data_path if data_path.startswith("[") else "." + data_path)
    else:
        joined = data_path
    if index >= 0 and not joined.endswith(f"[{index}]"):
        joined += f"[{index}]"
    id_data = getattr(owner, "id_data", None)
    if id_data is not None and getattr(id_data, "name", ""):
        return f'{id_data.__class__.__name__}["{id_data.name}"].{joined}'
    return joined


def _classify_target(owner, data_path, index, prop=None):
    subtype = getattr(prop, "subtype", "")
    owner_type = owner.__class__.__name__
    custom_prop = _custom_property_name(data_path)
    if data_path in {"rotation_euler", "rotation_quaternion", "delta_rotation_euler"}:
        return "Rotation Channel" if index >= 0 else "Rotation"
    if data_path in {"scale", "delta_scale"}:
        return "Scale Channel" if index >= 0 else "Scale"
    if data_path in {"location", "delta_location"}:
        return "Location Channel" if index >= 0 else "Location"
    if subtype == "COLOR" or "Color" in owner_type or data_path == "color":
        return "Color Channel" if index >= 0 else "Color"
    if custom_prop:
        return "Custom Property"
    if owner_type.startswith("NodeSocket"):
        return "Node Input"
    if owner_type.endswith("Modifier"):
        return "Modifier Property"
    if owner_type.endswith("Constraint"):
        return "Constraint Property"
    if owner_type == "KeyBlock":
        return "Shape Key Value"
    return "Numeric Property" if index < 0 else "Numeric Channel"


def _suggestion_ids_for_kind(target_kind):
    mapping = {
        "Rotation": ("constant_speed", "sine_osc"),
        "Rotation Channel": ("constant_speed", "sine_osc"),
        "Scale": ("transition_smoothstep",),
        "Scale Channel": ("transition_smoothstep",),
        "Location": ("transition_linear",),
        "Location Channel": ("transition_linear",),
        "Color": ("simple_blink", "candle_flicker"),
        "Color Channel": ("simple_blink", "candle_flicker"),
        "Node Input": ("transition_smoothstep", "pulse_repeat"),
        "Custom Property": (),
        "Modifier Property": ("transition_smoothstep", "pulse_repeat"),
        "Constraint Property": ("transition_smoothstep",),
        "Shape Key Value": ("transition_smoothstep",),
        "Numeric Property": ("transition_linear", "pulse_repeat"),
        "Numeric Channel": ("transition_linear", "pulse_repeat"),
    }
    return tuple(template_id for template_id in mapping.get(target_kind, ("transition_linear", "pulse_repeat")) if template_id in TEMPLATE_BY_ID)


def inspect_button_target(context):
    targets = resolve_button_driver_targets(context)
    if not targets:
        return None

    target = targets[0]
    prop = getattr(context, "button_prop", None)
    custom_prop = _custom_property_name(target.data_path)
    channel = _channel_name(target.index, target.data_path, prop)
    target_kind = _classify_target(target.owner, target.data_path, target.index, prop)

    if target_kind.startswith("Rotation") or target_kind.startswith("Scale") or target_kind.startswith("Location"):
        property_label = _label_from_transform(target.data_path, channel)
    elif custom_prop:
        property_label = f"Custom Property > {custom_prop}"
    elif target_kind == "Node Input":
        socket_name = getattr(target.owner, "name", target.data_path)
        node_name = getattr(getattr(target.owner, "node", None), "name", "Node")
        property_label = f"Node Input > {node_name} > {socket_name}"
        if channel:
            property_label += f" > {channel}"
    elif target_kind == "Color Channel":
        base_name = getattr(prop, "name", getattr(target.owner, "name", "Color"))
        property_label = f"{base_name} > {channel}"
    elif target_kind == "Color":
        property_label = getattr(prop, "name", getattr(target.owner, "name", "Color"))
    elif target_kind == "Modifier Property":
        property_label = f"Modifier > {getattr(target.owner, 'name', 'Modifier')} > {getattr(prop, 'name', target.data_path)}"
    elif target_kind == "Constraint Property":
        property_label = f"Constraint > {getattr(target.owner, 'name', 'Constraint')} > {getattr(prop, 'name', target.data_path)}"
    elif target_kind == "Shape Key Value":
        property_label = f"Shape Key > {getattr(target.owner, 'name', 'Key')} > Value"
    else:
        base_name = getattr(prop, "name", target.data_path.replace("_", " ").title())
        property_label = f"{base_name} {channel}".strip()

    display_label = f"{_id_root_label(target.owner)} > {property_label}"
    technical_path = _technical_target_path(target.owner, target.data_path, target.index)
    apply_scope = "Single channel" if target.index >= 0 else "Whole property"
    return TargetInspectorInfo(
        display_label=display_label,
        technical_path=technical_path,
        target_kind=target_kind,
        apply_scope=apply_scope,
        suggestion_ids=_suggestion_ids_for_kind(target_kind),
    )


def _property_indices(prop, array_index):
    if not getattr(prop, "is_array", False):
        return [-1]
    if array_index is not None:
        return [array_index]
    length = int(getattr(prop, "array_length", 0) or 0)
    # An RGBA colour exposes four components, but alpha is opacity rather than
    # colour - see _whole_colour_rgb_indices. Every other array keeps all of
    # its components.
    if length == 4 and getattr(prop, "subtype", "") in {"COLOR", "COLOR_GAMMA"}:
        length = 3
    return list(range(length)) if length > 0 else [-1]


def resolve_button_driver_targets(context):
    direct_targets = _targets_from_context_property(context)
    if direct_targets:
        return direct_targets

    owner = getattr(context, "button_pointer", None)
    prop = getattr(context, "button_prop", None)
    if owner is None or prop is None:
        # The N-panel has no right-click ``button_pointer``. Reuse the route
        # deliberately stored by Use as Espresso Target, resolved against the
        # active selected object so a material/node-tree owner is not shared
        # accidentally across the batch.
        scene = getattr(context, "scene", None)
        props = getattr(scene, "espresso_props", None)
        active = getattr(context, "active_object", None)
        entry = apply_target.read(props) if props is not None else None
        if entry and active is not None:
            targets, _reason = apply_target.targets_for(entry, active)
            return targets
        return []

    identifier = getattr(prop, "identifier", "")
    if not identifier or identifier == "rna_type":
        return []
    if getattr(prop, "is_readonly", False):
        return []
    if not getattr(prop, "is_animatable", True):
        return []

    return [
        ButtonDriverTarget(owner, identifier, index)
        for index in _property_indices(prop, _button_array_index(context))
    ]


def resolve_button_multi_targets(context):
    clicked_targets = resolve_button_driver_targets(context)
    if len(clicked_targets) != 1:
        return []
    clicked = clicked_targets[0]
    if clicked.index < 0:
        return []

    # Component fields can arrive through ``context.property`` even when
    # ``button_prop`` is missing or describes only the scalar button. Recover
    # the family from the resolved owner/data path instead of treating that UI
    # detail as proof that X/Y/Z siblings do not exist.
    array_length = int(getattr(getattr(context, "button_prop", None), "array_length", 0) or 0)
    if array_length < 2:
        try:
            rna_prop = clicked.owner.bl_rna.properties.get(clicked.data_path)
        except Exception:
            rna_prop = None
        array_length = int(getattr(rna_prop, "array_length", 0) or 0)
    if array_length < 2:
        try:
            array_length = len(clicked.owner.path_resolve(clicked.data_path))
        except Exception:
            array_length = 0
    return expand_multi_targets(clicked, multi_target_count(array_length))


def _foreign_motion_on_button_targets(targets):
    for target in targets:
        owner = target.owner
        root = getattr(owner, "id_data", None)
        if root is None or root is owner:
            root = owner
            data_path = target.data_path
        else:
            prefix = owner.path_from_id()
            data_path = "%s.%s" % (prefix, target.data_path) if prefix else target.data_path
        animation = getattr(root, "animation_data", None)
        if target.index < 0 and animation is not None:
            indices = {curve.array_index for curve in animation.drivers
                       if curve.data_path == data_path}
        else:
            indices = {target.index}
        for index in indices:
            foreign = core_driver_manager.foreign_live_motion_for_channel(
                root, data_path, index,
            )
            if foreign is not None:
                return "%s[%d] has motion from an unavailable edition (%s); it was left unchanged." % (
                    data_path, index, foreign["label"],
                )
    return ""


def apply_expression_to_targets(
    targets,
    expression,
    template,
    scene=None,
    rest_start_mode=utils.REST_START_OFF,
    source_entry=None,
    output_baseline=None,
    prepare_driver=None,
):
    props = getattr(scene, "espresso_props", None) if scene is not None else None
    block_message = _button_flow_block_message(
        template, scene_props=props, source_entry=source_entry,
    )
    if block_message:
        return False, block_message, []

    plan_entry = {
        "apply_kind": "context_menu",
        "targets": [
            target_memory.serialize_target(
                target.owner, target.data_path, target.index,
            )
            for target in targets
        ],
        "source_entry": source_entry or {},
        "source_required": source_binding.needs_input_source(template),
    }
    plan = application_plan.from_entry(plan_entry, template)
    source_status = (
        source_binding.source_status(source_entry)
        if plan.source_required else {"code": source_binding.SOURCE_ACTIVE}
    )
    report = application_plan.preflight(plan, source_status=source_status)
    if not report.ok:
        return False, report.errors[0], []
    foreign_message = _foreign_motion_on_button_targets(targets)
    if foreign_message:
        return False, foreign_message, []

    applied = 0
    target_states = []
    captured = target_memory.capture_cleanup_for_targets(targets, props)
    for target in targets:
        routed_expression = audio_reactivity.prepare_expression(
            template, expression, scene, output_baseline,
        ) if scene is not None else expression
        rest_state = apply_behavior.capture_target_rest_state(
            target,
            routed_expression,
            template,
            scene,
            rest_start_mode,
            output_baseline=output_baseline,
        ) if scene is not None else {}
        wrapped_expression = utils.wrap_expression_with_rest_state(routed_expression, template, rest_state)
        validation_template = audio_reactivity.validation_template(template, scene) if scene is not None else template
        valid, message = utils.validate_driver_expression(
            wrapped_expression, validation_template, scene,
        )
        if not valid:
            return False, message, []
        try:
            fcurve_result = target.owner.driver_add(target.data_path, target.index)
        except TypeError:
            fcurve_result = target.owner.driver_add(target.data_path)
        except Exception as exc:
            return False, f"Could not add driver to {target.data_path}: {exc}", []

        fcurves = fcurve_result if isinstance(fcurve_result, list) else [fcurve_result]
        for fcurve in fcurves:
            if fcurve is None:
                continue
            audio_bound = audio_reactivity.bind_driver(fcurve.driver, template, scene) if scene is not None else False
            if not audio_bound:
                ok, message = source_binding.bind_required_variables(
                    fcurve.driver, template, source_entry,
                    owner_object=getattr(target, "owner_object", None))
                if not ok:
                    return False, message, []
                audio_reactivity.clear_variables(fcurve.driver, template)
            # A caller that owns a variable the expression reads (a camera
            # recipe's live ``dist``) binds it here, before the expression is
            # assigned, so the driver is never evaluated without it.
            if prepare_driver is not None:
                prepared = prepare_driver(fcurve.driver)
                if prepared is False:
                    return False, "Could not bind the driver's variables.", []
                if isinstance(prepared, tuple) and not prepared[0]:
                    return False, prepared[1], []
            ok, message = utils.assign_driver_expression(
                fcurve.driver, wrapped_expression, validation_template, scene,
            )
            if not ok:
                return False, message, []
            # The new expression is now safely stored. Any private helper
            # bindings left by the expression it replaced are obsolete.
            applied += 1
        target_states.append(rest_state)

    if applied == 0:
        return False, "No properties were driven.", []

    # A direct driver on a socket replaces a generated spatial node route that
    # fed that same socket. Remove only the group connected to this target;
    # other Espresso groups in the node tree remain untouched.
    for target in targets:
        shared_material_sweep.remove_group_feeding_target_path(
            target.owner, target.data_path,
        )
    target_memory.cleanup_captured_resources(captured, scene, props)

    base = "Driver Espresso expression applied." if applied == 1 else f"Applied to {applied} properties."
    return True, base, target_states


def apply_current_template_to_button_targets(targets, scene_props, template, scene):
    """Apply the current single expression or complete multi-channel plan."""
    if template_catalogue.has_motion_plan(template):
        # A ONE-channel motion plan names an axis, but that axis is only a
        # sensible default for applying from the panel. Right-clicking a
        # property names the target explicitly, and that has to win.
        #
        # It did not: Loose Panel Impact Rattle declares rotation_euler[2], so
        # right-clicking Rotation X put the driver on Rotation Z. The operator
        # reported success, a driver existed, and the property the artist was
        # looking at never moved - indistinguishable from the apply doing
        # nothing at all.
        #
        # Multi-channel plans still take the object route. Ball Bounce drives
        # location AND three scale axes together; there is no single clicked
        # property that could stand for the set.
        channels = template_catalogue.template_channels(template)
        if targets and len(channels) == 1:
            built = espresso_props.built_channel_previews(scene_props)
            first = built[0] if built else None
            expression = ""
            if isinstance(first, dict):
                expression = first.get("expression") or ""
            elif isinstance(first, str):
                expression = first
            if not expression:
                expression = scene_props.preview
            ok, message, target_states = apply_expression_to_targets(
                targets,
                expression,
                template,
                scene=scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(scene_props),
                source_entry=source_binding.latest_source(scene_props),
                output_baseline=scene_props.preview_output_baseline,
            )
            return ok, message, target_states, targets

        obj = motion_channels.resolve_motion_object(targets[0].owner) if targets else None
        result = motion_channels.apply_motion_template_to_object(
            obj,
            template,
            espresso_props.built_channel_previews(scene_props),
            scene=scene,
            rest_start_mode=espresso_props.effective_rest_start_mode(scene_props),
            enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(scene_props, template),
            template_values=espresso_props.collect_values(scene_props, template),
        ) if obj is not None else motion_channels.MotionApplyResult(
            False,
            "Right-click a transform on an Object to apply this motion template.",
        )
        return result.ok, result.message, result.target_states, result.targets

    ok, message, target_states = apply_expression_to_targets(
        targets,
        scene_props.preview,
        template,
        scene=scene,
        rest_start_mode=espresso_props.effective_rest_start_mode(scene_props),
        source_entry=source_binding.latest_source(scene_props),
        output_baseline=scene_props.preview_output_baseline,
    )
    return ok, message, target_states, targets


def paste_copied_driver_to_targets(targets, scene_props, scene):
    expression = getattr(scene_props, "copied_driver_expression", "")
    template_id = getattr(scene_props, "copied_driver_template", "")
    if not expression or not template_id:
        return False, "Copy an Espresso driver first.", []
    from ...catalogue.core.templates import TEMPLATE_BY_ID

    template = TEMPLATE_BY_ID.get(template_id)
    if template is None:
        return False, "Copied Espresso driver template is no longer available.", []
    copied_scope = str(getattr(scene_props, "copied_driver_scope", "") or "")
    if copied_scope and copied_scope not in application_plan.SCOPES:
        return False, "The copied driver has an unknown application scope.", []
    return apply_expression_to_targets(
        targets,
        expression,
        template,
        scene=scene,
        rest_start_mode=getattr(scene_props, "copied_driver_rest_mode", utils.REST_START_OFF),
        source_entry=source_binding._read_entry(getattr(scene_props, "copied_driver_source", "{}")),
        output_baseline=getattr(scene_props, "copied_driver_output_baseline", 0.0),
    )


def _cache_popup_context(targets, inspector):
    cache_key = uuid.uuid4().hex
    _POPUP_TARGET_CACHE[cache_key] = {
        "targets": list(targets),
        "inspector": inspector,
    }
    return cache_key


def _popup_cache_targets(cache_key):
    payload = _POPUP_TARGET_CACHE.get(cache_key) or {}
    return payload.get("targets", [])


def _popup_cache_inspector(cache_key):
    payload = _POPUP_TARGET_CACHE.get(cache_key) or {}
    return payload.get("inspector")


def _clear_popup_cache(cache_key):
    if cache_key:
        _POPUP_TARGET_CACHE.pop(cache_key, None)


def _serialize_popup_targets(targets, inspector):
    """Keep a resolvable target copy on the operator across dialog confirmation."""
    entry = target_memory.build_entry(
        targets,
        inspector.display_label if inspector else "",
        "popup_pending",
    )
    return json.dumps(entry, sort_keys=True) if entry else ""


def _resolve_popup_target_snapshot(snapshot_json):
    if not snapshot_json:
        return [], ""
    try:
        entry = json.loads(snapshot_json)
    except (TypeError, ValueError):
        return [], ""
    if not isinstance(entry, dict):
        return [], ""

    resolved, _reason = target_memory.resolve_entry(entry)
    if not resolved:
        return [], entry.get("display_label", "")
    targets = [
        ButtonDriverTarget(item["owner"], item["data_path"], int(item.get("index", -1)))
        for item in resolved
    ]
    return targets, entry.get("display_label", "")


def _output_range_warning(context, template):
    """Warn when a template's output range overshoots the clicked property.

    A colour component runs 0-1; Neon Tube Warm-Up runs 0-8 because it is a
    BRIGHTNESS, meant for an emission strength. Applying it to a colour still
    works - the driver is valid, the value simply pins at white - so this warns
    rather than blocking. Candle Flicker legitimately peaks at 1.5 for emissive
    materials, and refusing that would forbid real work.

    Returns a sentence, or None when there is nothing worth saying.
    """
    prop = getattr(context, "button_prop", None)
    if prop is None or getattr(prop, "type", "") != "FLOAT":
        return None
    soft_max = getattr(prop, "soft_max", None)
    soft_min = getattr(prop, "soft_min", None)
    if soft_max is None or soft_min is None or soft_max > 1e30:
        return None      # unbounded property - there is nothing to overshoot

    values = {param["token"]: param.get("default") for param in template.get("params", [])}
    low, high = values.get("MIN"), values.get("MAX")
    if not isinstance(high, (int, float)) or not isinstance(low, (int, float)):
        return None      # the template does not declare an output range

    if high > soft_max * 1.05 or low < soft_min - 1e-6:
        where = getattr(prop, "name", "") or ""
        if not where or where == "default_value":
            where = "a colour" if getattr(prop, "subtype", "") == "COLOR" else "this property"
        return (
            "{name} outputs {lo:g} to {hi:g}, but {where} expects {smin:g} to "
            "{smax:g}. It will still apply - values past the top simply pin. "
            "Lower Maximum, or drive a strength instead."
        ).format(name=template.get("name", "This template"), lo=low, hi=high,
                 where=where, smin=soft_min, smax=soft_max)
    return None


def template_fits_button_target(context, scene_props=None, template=None):
    """Can the loaded template actually drive the property under the cursor?

    The single fitness test every apply entry in this menu shares. It was once
    written into plain apply's poll alone, so the multi-object entries stayed
    lit for templates that could not reach the clicked property at all - they
    route through the same apply helper, so being lit bought nothing but a
    warning per object and a cancel.
    """
    if scene_props is None:
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if not (scene_props and scene_props.is_valid):
        return False
    targets = resolve_button_driver_targets(context)
    if not targets:
        return False
    if template is None:
        template = espresso_props.get_current_template(scene_props)
    if template is None:
        return False
    # A motion plan writes a whole channel set onto an Object. A shader socket
    # or a node input has no Object to resolve, and the plan has nowhere to go.
    if template_catalogue.has_motion_plan(template):
        return motion_channels.resolve_motion_object(targets[0].owner) is not None
    return not _button_flow_block_message(template, scene_props)


class ESPRESSO_OT_apply_to_button(bpy.types.Operator):
    bl_idname = "espresso.apply_to_button"
    bl_label = "Apply Current Template"
    bl_description = "Add a driver and apply the template currently configured in Driver Espresso"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def description(cls, context, _properties):
        """Say it in the tooltip rather than greying the entry out.

        The apply is legal - a 0-8 brightness on a 0-1 colour produces a valid
        driver that simply pins - so blocking it would be nannying. Naming the
        mismatch where the artist is already looking lets them decide.
        """
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if scene_props is None:
            return cls.bl_description
        template = espresso_props.get_current_template(scene_props)
        warning = _output_range_warning(context, template)
        if not warning:
            return cls.bl_description
        return cls.bl_description + ".\n\n" + warning

    @classmethod
    def poll(cls, context):
        return template_fits_button_target(context)

    def execute(self, context):
        scene_props = context.scene.espresso_props
        targets = resolve_button_driver_targets(context)
        if not targets:
            self.report({"WARNING"}, "No drivable UI property found.")
            return {"CANCELLED"}

        template = espresso_props.get_current_template(scene_props)

        # A palette template drives a POSITION along a ramp, not a colour. On a
        # colour socket the plain apply would drive red, green and blue to that
        # same number and produce grey - a valid driver that does nothing
        # anyone wants. The setup it was written for is one click away in this
        # same menu, so take it rather than leaving the artist to find out by
        # looking at a grey object.
        if wants_palette(template) and _colour_socket_target(context) is not None:
            return ESPRESSO_OT_apply_ramp_to_socket.execute(self, context)

        ok, message, target_states, applied_targets = apply_current_template_to_button_targets(
            targets, scene_props, template, context.scene,
        )
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        inspector = inspect_button_target(context)
        target_memory.remember_targets(
            context,
            applied_targets,
            inspector.display_label if inspector else "",
            "template",
            template["id"],
            template["name"],
            target_states=target_states,
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        self.report({"INFO"}, message)
        return {"FINISHED"}


# Templates whose output is a position along a palette rather than a value in
# its own right. Applying one to a colour means building the ramp it selects
# from; applying it to a scalar means driving that scalar directly.
PALETTE_FACTOR_TEMPLATES = frozenset()


def wants_palette(template):
    return bool(template) and template.get("id") in PALETTE_FACTOR_TEMPLATES


def _colour_socket_target(context):
    """The clicked socket, when it is a colour input on a shader node.

    Returns ``(node_tree, node, socket)`` or None. A ramp only makes sense
    feeding a colour: offering it on a scalar would produce a driver that
    cannot be wired.
    """
    button = getattr(context, "button_pointer", None)
    prop = getattr(context, "button_prop", None)
    if button is None or prop is None:
        return None
    if getattr(prop, "identifier", "") != "default_value":
        return None
    node = getattr(button, "node", None)
    if node is None or getattr(button, "is_output", False):
        return None
    if getattr(button, "type", "") not in {"RGBA", "VECTOR"}:
        return None
    tree = getattr(node, "id_data", None)
    if tree is None or not hasattr(tree, "links"):
        return None
    return tree, node, button


class ESPRESSO_OT_apply_ramp_to_socket(bpy.types.Operator):
    """Insert a Colour Ramp into this socket and drive its Factor.

    The ramp templates drive a single number and let a Colour Ramp hold the
    palette, which is what allows unlimited colours. Applying one straight to a
    colour socket cannot work - it would drive red, green and blue to the same
    number and produce grey. This builds the setup the template was written
    for, in one click, instead of leaving the artist to discover it.
    """

    bl_idname = "espresso.apply_ramp_to_socket"
    bl_label = "Apply as Colour Ramp"
    bl_description = (
        "Insert a Colour Ramp feeding this colour, drive its Factor with the "
        "current template, and copy the palette configured in Parameters. "
        "After applying, edit the material copy in the Shader Editor"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None or not props.is_valid:
            return False
        return _colour_socket_target(context) is not None

    def execute(self, context):
        from ...apply import colour_ramp

        found = _colour_socket_target(context)
        if found is None:
            self.report({"WARNING"}, "Right-click a colour input on a node to use this.")
            return {"CANCELLED"}
        tree, node, socket = found

        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        values = espresso_props.collect_values(props, template)
        stops = int(values.get("STOPS", 4) or 4)
        source_ramp = colour_ramp.template_ramp(context.scene)

        ramp = colour_ramp.build_ramp_for_socket(
            tree, node, socket, stops=stops, source_ramp=source_ramp)
        factor = colour_ramp.factor_socket(ramp)
        data_path = factor.path_from_id("default_value")

        # Reuse the ordinary button-apply path rather than hand-rolling the
        # driver: it already handles source binding, validation and rest-start
        # exactly as every other apply does, so this cannot drift from them.
        target = ButtonDriverTarget(tree, data_path, -1)
        ok, message, target_states, applied = apply_current_template_to_button_targets(
            [target], props, template, context.scene,
        )
        if not ok:
            colour_ramp.remove_palette_group(tree, ramp)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        target_memory.remember_targets(
            context, applied,
            "%s > %s Factor" % (getattr(tree, "name", "Nodes"), ramp.name),
            "template", template["id"], template["name"],
            target_states=target_states,
        )
        self.report(
            {"INFO"},
            "Colour Ramp added with %d colours and driven by %s. Edit its stops "
            "in the Shader Editor." % (stops, template["name"]),
        )
        return {"FINISHED"}


def material_slot_index(reference_owner, reference_object):
    """Which material slot ``reference_owner`` is, or None if it is not one."""
    data = getattr(reference_object, "data", None)
    for index, material in enumerate(getattr(data, "materials", None) or ()):
        if reference_owner is material or reference_owner is getattr(
                material, "node_tree", None):
            return index
    return None


def unshare_material(obj, slot_index, want_node_tree):
    """Give ``obj`` its own copy of the material in ``slot_index``.

    A material shared between objects is ONE datablock, so its driver is
    evaluated once and every object gets the identical value - measured, four
    objects all reading 1.0. No amount of variable rewiring changes that; the
    only way to vary a material per object is for each object to have its own.
    """
    data = getattr(obj, "data", None)
    materials = getattr(data, "materials", None)
    if materials is None or slot_index >= len(materials):
        return None, "has no material in that slot"
    # A shared MESH means the material slots are shared too, so copying the
    # material would still write to every user of that mesh.
    if getattr(data, "users", 1) > 1:
        return None, "shares its mesh data, so its material cannot be split"

    material = materials[slot_index]
    if material is None:
        return None, "has no material in that slot"
    if material.users > 1:
        material = material.copy()
        materials[slot_index] = material
    return (material.node_tree if want_node_tree else material), ""


def equivalent_owner(reference_owner, reference_object, other_object):
    """The datablock on ``other_object`` matching ``reference_owner``.

    Right-clicking a light's Power gives the LIGHT datablock, not the object,
    so applying the same property across a selection means mapping each
    object's own equivalent. Returns None when there is no honest counterpart,
    which the caller reports rather than guessing at.
    """
    if reference_owner is reference_object:
        return other_object
    if reference_owner is getattr(reference_object, "data", None):
        other_data = getattr(other_object, "data", None)
        # Same KIND of data, or the property will not exist on it - a light
        # curve applied to a camera would resolve to nothing useful.
        if type(other_data) is type(reference_owner):
            return other_data
        return None
    reference_data = getattr(reference_object, "data", None)
    materials = list(getattr(reference_data, "materials", None) or ())
    for index, material in enumerate(materials):
        trees = (material, getattr(material, "node_tree", None))
        if reference_owner in trees:
            other_materials = list(getattr(getattr(other_object, "data", None), "materials", None) or ())
            if index < len(other_materials) and other_materials[index] is not None:
                other = other_materials[index]
                return other if reference_owner is material else getattr(other, "node_tree", None)
            return None
    return None


class ESPRESSO_OT_use_as_apply_target(bpy.types.Operator):
    """Remember this property as what "apply to my selection" should drive.

    The mirror of Use as Espresso Input. That one names where a value comes
    FROM; this names where it goes TO. Before it existed, applying across a
    selection could only ever write to a light's Power - so a brightness
    template could reach a lamp but not an emission shader, and a selection of
    meshes made the button do nothing at all, silently.
    """

    bl_idname = "espresso.use_as_apply_target"
    bl_label = "Use as Espresso Target"
    bl_description = (
        "Remember this property as the one to drive when applying to a "
        "selection. Each selected object gets its own driver on its own "
        "equivalent of this property"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if not resolve_button_driver_targets(context):
            return False
        # A channel set has nothing to write to one nominated property, so
        # operators.template_suits_lights refuses it outright and the "Apply to
        # N Objects" button never appears. A target named while one is loaded
        # is a value nothing will ever read.
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        template = espresso_props.get_current_template(scene_props) if scene_props else None
        if template is None:
            return True
        return button_menu_map.SINGLE in button_menu_map.capabilities_for(template)

    def execute(self, context):
        targets = resolve_button_driver_targets(context)
        active = context.active_object
        if not targets or active is None:
            self.report({"WARNING"}, "Right-click a property on an object.")
            return {"CANCELLED"}

        target = targets[0]
        entry = apply_target.describe(
            target.owner, active, target.data_path, target.index)
        if entry is None:
            self.report(
                {"WARNING"},
                "That property is not on this object, its data, or its "
                "materials, so it cannot be found on other objects.")
            return {"CANCELLED"}

        apply_target.write(context.scene.espresso_props, entry)
        self.report({"INFO"}, "Espresso target: %s" % entry["label"])
        return {"FINISHED"}


class ESPRESSO_OT_clear_apply_target(bpy.types.Operator):
    """Forget the nominated target and go back to the default."""

    bl_idname = "espresso.clear_apply_target"
    bl_label = "Clear Espresso Target"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        apply_target.clear(context.scene.espresso_props)
        self.report({"INFO"}, "Espresso target cleared.")
        return {"FINISHED"}


class ESPRESSO_OT_apply_to_selected_objects(bpy.types.Operator):
    """Apply this template to the same property on every selected object.

    Blender's own "Copy Drivers to Selected" copies the variable TARGETS along
    with the driver, so a position-aware template would leave every object
    reading the ACTIVE object's position - measured, and the whole effect
    collapses to one value. This applies the template afresh per object so each
    driver's variables bind to the object they land on.
    """

    bl_idname = "espresso.apply_to_selected_objects"
    bl_label = "Apply to Selected Objects"

    split_shared_materials: bpy.props.BoolProperty(
        name="Split Shared Materials", default=False,
        description=(
            "Give each object its own copy of a shared material. A shared "
            "material is one datablock and is evaluated once, so without this "
            "every object would show the identical value"
        ),
    )
    bl_description = (
        "Apply the current template to this same property on every selected "
        "object. Each object gets its own driver, and any position or distance "
        "variables are bound to that object rather than to the active one"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # Same fitness test as applying to the one property, because this runs
        # exactly that apply once per object. Anything plain apply cannot do
        # here, this cannot do here seventeen times.
        if not template_fits_button_target(context):
            return False
        return len(getattr(context, "selected_objects", None) or ()) > 1

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        targets = resolve_button_driver_targets(context)
        if not targets:
            self.report({"WARNING"}, "No drivable property found here.")
            return {"CANCELLED"}

        active = context.active_object
        if active is None:
            self.report({"WARNING"}, "No active object to take the property from.")
            return {"CANCELLED"}

        selected = [ob for ob in context.selected_objects]
        if active in selected:                      # apply to the active FIRST
            selected.remove(active)
            selected.insert(0, active)
        for obj in selected:
            planned = []
            for target in targets:
                owner = equivalent_owner(target.owner, active, obj)
                if owner is not None:
                    planned.append(ButtonDriverTarget(owner, target.data_path, target.index))
            if not planned:
                continue
            channels = template_catalogue.template_channels(template)
            if template_catalogue.has_motion_plan(template) and len(channels) > 1:
                motion_object = motion_channels.resolve_motion_object(planned[0].owner)
                enabled = espresso_props.enabled_channel_ids_for_template(props, template)
                if motion_object is not None:
                    planned = [ButtonDriverTarget(motion_object, channel["data_path"],
                                                   int(channel.get("index", -1)))
                               for channel in channels
                               if channel.get("data_path") and
                               (enabled is None or channel.get("id") in enabled)]
            foreign_message = _foreign_motion_on_button_targets(planned)
            if foreign_message:
                self.report({"WARNING"}, "%s: %s" % (obj.name, foreign_message))
                return {"CANCELLED"}
        espresso_props.refresh_position_wave_auto_fit(props, context, selected)

        applied_all, states_all, skipped, done = [], [], [], 0
        seen_owners, shared, unsplittable = {}, [], []
        for obj in selected:
            per_object = []
            for target in targets:
                owner = equivalent_owner(target.owner, active, obj)
                if owner is None:
                    continue

                # The same datablock reached twice means these objects SHARE
                # it. One datablock is one driver and one evaluation, so the
                # effect would collapse - the same failure as copying a driver
                # without rebinding, arrived at from the other direction.
                key = (owner.as_pointer(), target.data_path, target.index)
                if key in seen_owners:
                    slot = material_slot_index(target.owner, active)
                    replacement = None
                    if self.split_shared_materials and slot is not None:
                        replacement, reason = unshare_material(
                            obj, slot, want_node_tree=not isinstance(
                                target.owner, bpy.types.Material))
                        if replacement is None:
                            unsplittable.append("%s %s" % (obj.name, reason))
                            continue
                    if replacement is None:
                        shared.append(obj.name)
                        continue
                    owner = replacement
                    key = (owner.as_pointer(), target.data_path, target.index)

                seen_owners[key] = obj.name
                per_object.append(ButtonDriverTarget(
                    owner, target.data_path, target.index, owner_object=obj))
            if not per_object:
                skipped.append(obj.name)
                continue
            ok, message, states, applied = apply_current_template_to_button_targets(
                per_object, props, template, context.scene,
            )
            if not ok:
                self.report({"WARNING"}, "%s: %s" % (obj.name, message))
                return {"CANCELLED"}
            applied_all.extend(applied)
            states_all.extend(states)
            done += 1

        if not done:
            self.report({"WARNING"}, "None of the selected objects has that property.")
            return {"CANCELLED"}

        target_memory.remember_targets(
            context, applied_all,
            "%d objects > %s" % (done, template["name"]),
            "template", template["id"], template["name"],
            target_states=states_all,
        )
        note = "Applied %s to %d objects." % (template["name"], done)
        if skipped:
            note += " Skipped %d without that property: %s." % (
                len(skipped), ", ".join(skipped[:3]) + ("..." if len(skipped) > 3 else ""))
        if shared:
            note += (
                " %d objects share that datablock, so they would all show the "
                "same value - use \"Apply to Selected (Split Shared "
                "Materials)\" to give each its own copy." % len(shared))
        if unsplittable:
            note += " Could not split: %s." % "; ".join(unsplittable[:3])
        espresso_props.set_last_apply_status(props, note)
        self.report(
            {"WARNING"} if (skipped or shared or unsplittable) else {"INFO"}, note)
        return {"FINISHED"}


class ESPRESSO_OT_apply_to_button_multi(bpy.types.Operator):
    bl_idname = "espresso.apply_to_button_multi"
    bl_label = "Apply Current Template (Multi)"
    bl_description = "Add drivers and apply the current Driver Espresso template to every value in this arrayed property"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if not (scene_props and scene_props.is_valid):
            return False
        if len(resolve_button_multi_targets(context)) < 2:
            return False
        template = espresso_props.get_current_template(scene_props)
        # A MULTI-channel plan owns its own channels - Ball Bounce drives
        # location and three scale axes together - so "every index of the
        # clicked array" means nothing for it and the entry stays hidden.
        #
        # A SINGLE-channel plan is one expression with a default axis, and now
        # that the clicked property wins over that default, spreading it across
        # the whole array is exactly as sensible as for any plain template.
        # Blocking every motion plan alike hid a useful entry.
        if template_catalogue.has_motion_plan(template):
            if len(template_catalogue.template_channels(template)) > 1:
                return False
        return not _button_flow_block_message(template, scene_props)

    def execute(self, context):
        scene_props = context.scene.espresso_props
        targets = resolve_button_multi_targets(context)
        if len(targets) < 2:
            self.report({"WARNING"}, "No array property family found for multi-apply.")
            return {"CANCELLED"}

        template = espresso_props.get_current_template(scene_props)
        # Route through the shared helper rather than repeating its branch.
        # This had its own copy of the motion-plan logic, so the fix that makes
        # a single-channel template honour the clicked property landed in one
        # path and not the other - multi would still have driven the template's
        # declared axis while plain apply drove the click.
        ok, message, target_states, applied_targets = (
            apply_current_template_to_button_targets(
                targets, scene_props, template, context.scene,
            )
        )
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        target_memory.remember_targets(
            context,
            applied_targets,
            "",
            "template",
            template["id"],
            template["name"],
            target_states=target_states,
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_paste_copied_to_button(bpy.types.Operator):
    bl_idname = "espresso.paste_copied_to_button"
    bl_label = "Paste Copied Driver"
    bl_description = "Paste the template driver you most recently copied from Driver Espresso"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if not (scene_props and scene_props.copied_driver_expression and resolve_button_driver_targets(context)):
            return False
        from ...catalogue.core.templates import TEMPLATE_BY_ID

        template = TEMPLATE_BY_ID.get(scene_props.copied_driver_template)
        copied_source = source_binding._read_entry(getattr(scene_props, "copied_driver_source", "{}"))
        return bool(template and not _button_flow_block_message(template, source_entry=copied_source))

    def execute(self, context):
        scene_props = context.scene.espresso_props
        targets = resolve_button_driver_targets(context)
        if not targets:
            self.report({"WARNING"}, "No drivable UI property found.")
            return {"CANCELLED"}

        ok, message, target_states = paste_copied_driver_to_targets(targets, scene_props, context.scene)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        inspector = inspect_button_target(context)
        target_memory.remember_targets(
            context,
            targets,
            inspector.display_label if inspector else "",
            "copied",
            scene_props.copied_driver_template,
            scene_props.copied_driver_label,
            target_states=target_states,
            template_values=target_memory.json_safe_values(
                json.loads(getattr(scene_props, "copied_driver_parameters", "{}") or "{}")
            ),
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_open_panel_popup(bpy.types.Operator):
    bl_idname = "espresso.open_panel_popup"
    bl_label = "Configure Espresso Driver"
    bl_description = "Open a floating Driver Espresso panel to configure a template, copy it, or apply it to the clicked property"

    apply_on_confirm: bpy.props.BoolProperty(
        name="Apply Driver on OK",
        description="When enabled, clicking OK applies the current Espresso template to the property you right-clicked",
        default=True,
    )
    # SKIP_SAVE prevents Blender from wiping this on operator redo/repeat,
    # which would break the cache lookup in execute().
    target_cache_key: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    target_snapshot_json: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    # Popup-local UI collapse state. Kept on the operator (not the scene
    # props) so the condensed floating panel's expand/collapse toggles are
    # scoped to this dialog and don't leak into the main sidebar panel.
    inspector_open: bpy.props.BoolProperty(
        name="Show Target Detail",
        description="Expand the target inspector to show the technical path and channel scope",
        default=False,
    )
    variants_open: bpy.props.BoolProperty(
        name="Show Variants",
        description="Expand the template variants as a compact grid",
        default=False,
    )
    show_graph: bpy.props.BoolProperty(
        name="Show Graph",
        description="Sample the expression and draw the waveform preview in this popup",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        # Deliberately NOT gated on a button under the cursor. Blender re-runs
        # poll() immediately before execute(), in the context the OK button
        # lives in -- and by then the right-click button context is gone. A
        # poll that demanded it made OK fail its poll silently: no execute(),
        # no driver, no warning, and the dialog simply closed. Measured: the
        # same execute() lands the driver the moment poll() is satisfied.
        #
        # The menu item is already gated where the gating belongs:
        # draw_button_context_menu() returns before drawing anything when
        # nothing under the cursor is drivable, and invoke() below refuses to
        # open a dialog without a target. Nothing is lost by relaxing this;
        # the confirm path is what was broken.
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return bool(scene_props)

    def invoke(self, context, event):
        targets = resolve_button_driver_targets(context)
        if not targets:
            # Reachable from F3 search or a script, never from the menu.
            self.report({"WARNING"},
                        "Right-click a property to open the floating panel for it.")
            return {"CANCELLED"}
        inspector = inspect_button_target(context)
        self.target_snapshot_json = _serialize_popup_targets(targets, inspector)
        self.target_cache_key = _cache_popup_context(targets, inspector)
        return context.window_manager.invoke_props_dialog(self, width=400)

    def execute(self, context):
        # The right-click button context is gone by the time OK is pressed.
        # Use the live cache first, then the operator-owned resolvable snapshot.
        # Never fall back to the current button context here: it is already gone.
        targets = _popup_cache_targets(self.target_cache_key)
        inspector = _popup_cache_inspector(self.target_cache_key)
        remembered_label = inspector.display_label if inspector else ""
        if not targets:
            targets, remembered_label = _resolve_popup_target_snapshot(self.target_snapshot_json)

        if not targets:
            _clear_popup_cache(self.target_cache_key)
            self.report({"WARNING"}, "Apply target lost — please re-open the popup from the right-click menu.")
            return {"CANCELLED"}

        if not self.apply_on_confirm:
            _clear_popup_cache(self.target_cache_key)
            self.report({"INFO"}, "Popup closed without applying. Use Copy Expression or Copy Driver from the preview.")
            return {"FINISHED"}

        scene_props = context.scene.espresso_props
        template = espresso_props.get_current_template(scene_props)

        # Guard: never attempt to apply an invalid or empty expression.
        if not scene_props.is_valid or not scene_props.preview:
            _clear_popup_cache(self.target_cache_key)
            self.report({"WARNING"}, scene_props.validation_message or "Expression is invalid — check your template parameters.")
            return {"CANCELLED"}

        # Guard: block templates that require driver variables the button flow can't set up.
        block_message = _button_flow_block_message(template, scene_props)
        if template_catalogue.has_motion_plan(template):
            popup_targets = _popup_cache_targets(self.target_cache_key)
            if not popup_targets:
                popup_targets, _label = _resolve_popup_target_snapshot(self.target_snapshot_json)
            if not popup_targets or motion_channels.resolve_motion_object(popup_targets[0].owner) is None:
                block_message = "Right-click an Object transform to apply this multi-expression motion template."
        if block_message:
            _clear_popup_cache(self.target_cache_key)
            self.report({"WARNING"}, block_message)
            return {"CANCELLED"}

        ok, message, target_states, applied_targets = apply_current_template_to_button_targets(
            targets, scene_props, template, context.scene,
        )
        _clear_popup_cache(self.target_cache_key)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        target_memory.remember_targets(
            context,
            applied_targets,
            remembered_label,
            "template",
            template["id"],
            template["name"],
            target_states=target_states,
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        self.report({"INFO"}, message)
        return {"FINISHED"}

    def draw(self, context):
        # Condensed floating-panel layout: a quick right-click flow for
        # picking a category + template and applying it. Heavier main-panel
        # sections (suggested templates, technical inspector path, Rest Start,
        # target-memory Update row, always-on graph) are intentionally left
        # to the sidebar panel to keep this dialog short enough to fit on
        # screen.
        from ..views import panels
        layout = self.layout
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        inspector = _popup_cache_inspector(self.target_cache_key)

        # --- Collapsed target inspector: one line, expand for detail ---
        if inspector:
            insp_row = layout.row(align=True)
            insp_row.prop(
                self, "inspector_open", text="", emboss=False,
                icon="TRIA_DOWN" if self.inspector_open else "TRIA_RIGHT",
            )
            insp_row.label(text=inspector.display_label, icon="OBJECT_DATA")
            if self.inspector_open:
                detail = layout.column(align=True)
                panels.draw_wrapped(detail, inspector.technical_path, icon="DECORATE_DRIVER", width=54)
                info_row = detail.row(align=True)
                info_row.label(text=inspector.target_kind, icon="INFO")
                info_row.label(text=inspector.apply_scope, icon="ORIENTATION_LOCAL")
            layout.separator()

        # --- Category + template selector ---
        panels.draw_template_selector(layout, props)

        # Condensed template nav row (no description/use-case icons — variants
        # get their own collapsible section below).
        nav_row = layout.row(align=True)
        nav_row.label(text="Template:")
        nav = nav_row.row(align=True)
        op = nav.operator("espresso.navigate_template", text="", icon="TRIA_LEFT")
        op.direction = -1
        nav.prop(props, "template", text="")
        op = nav.operator("espresso.navigate_template", text="", icon="TRIA_RIGHT")
        op.direction = 1

        if template.get("warning"):
            warn_box = layout.box()
            warn_box.alert = True
            panels.draw_wrapped(warn_box, template["warning"], icon="ERROR", width=54)

        # --- Condensed collapsible variants (2-column grid on expand) ---
        variant_ids = [vid for vid in (template.get("variants") or []) if vid in TEMPLATE_BY_ID]
        pair_id = template.get("pair_with")
        has_pair = bool(pair_id and pair_id in TEMPLATE_BY_ID)
        if variant_ids or has_pair:
            count = len(variant_ids) + (1 if has_pair else 0)
            vrow = layout.row(align=True)
            vrow.prop(
                self, "variants_open", text="", emboss=False,
                icon="TRIA_DOWN" if self.variants_open else "TRIA_RIGHT",
            )
            vrow.label(text=f"Variants ({count})", icon="GRAPH")
            if self.variants_open:
                vbox = layout.box()
                if has_pair:
                    pair = TEMPLATE_BY_ID[pair_id]
                    op = vbox.operator("espresso.switch_variant", text=f"Pair: {pair['name']}", icon="LINKED")
                    op.variant_id = pair_id
                grid = vbox.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
                for vid in variant_ids:
                    variant = TEMPLATE_BY_ID[vid]
                    op = grid.operator("espresso.switch_variant", text=variant["name"])
                    op.variant_id = vid

        # --- Parameters (reused; 2-column where the template supports it) ---
        panels.draw_parameters(layout, props, template, context)

        # --- Condensed preview: validity + graph toggle + expression + copy ---
        layout.separator()
        prev_box = layout.box()
        head = prev_box.row(align=True)
        if props.is_valid:
            head.label(text="Valid", icon="CHECKMARK")
        else:
            head.label(text="Error", icon="ERROR")
        head.prop(self, "show_graph", text="Graph", icon="IPO_BEZIER", toggle=True)

        if template_catalogue.has_motion_plan(template):
            panels.draw_motion_channel_list(prev_box, props, template)
        else:
            if props.preview:
                expr_box = prev_box.box()
                # One line, same reasoning as the main panel's Expression box.
                expr_box.label(text=props.preview)

            copy_row = prev_box.row(align=True)
            copy_row.enabled = props.is_valid
            split = copy_row.split(factor=0.5, align=True)
            split.operator("espresso.copy_expression", text="Copy Expression", icon="COPYDOWN")
            split.operator("espresso.copy_driver", text="Copy Driver", icon="DRIVER")

        if self.show_graph and props.is_valid:
            style_row = prev_box.row(align=True)
            style_row.prop(props, "visualizer_style", text="")
            style_row.prop(props, "visualizer_detailed", text="Detailed", toggle=True)
            panels._draw_graph_block(prev_box, props, template, context)

        block_message = _button_flow_block_message(template, props)

        if template_catalogue.has_motion_plan(template):
            popup_targets = _popup_cache_targets(self.target_cache_key)
            if not popup_targets:
                popup_targets, _label = _resolve_popup_target_snapshot(self.target_snapshot_json)
            if not popup_targets or motion_channels.resolve_motion_object(popup_targets[0].owner) is None:
                block_message = "Right-click an Object transform to apply this multi-expression motion template."

        # Disable the apply toggle when the template can't be applied via button flow.
        toggle_row = layout.row(align=True)
        toggle_row.enabled = not block_message
        toggle_row.prop(self, "apply_on_confirm", text="Apply Driver on OK", icon="DRIVER")

        # Show all error states without early-returning so nothing is hidden.
        if block_message:
            box = layout.box()
            box.alert = True
            panels.draw_wrapped(box, block_message, icon="ERROR", width=54)

        if not props.is_valid and props.validation_message:
            box = layout.box()
            box.alert = True
            panels.draw_wrapped(box, props.validation_message, icon="ERROR", width=54)

    def cancel(self, context):
        _clear_popup_cache(self.target_cache_key)


class ESPRESSO_OT_use_button_as_input(bpy.types.Operator):
    bl_idname = "espresso.use_button_as_input"
    bl_label = "Use as Espresso Input"
    bl_description = "Use this property as the input source for templates such as Dead Zone, Threshold, and Remap"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(getattr(getattr(context, "scene", None), "espresso_props", None) and resolve_button_driver_targets(context))

    def execute(self, context):
        props = context.scene.espresso_props
        targets = resolve_button_driver_targets(context)
        if not targets:
            self.report({"WARNING"}, "No usable input property found.")
            return {"CANCELLED"}
        target = targets[0]
        prop = getattr(context, "button_prop", None)
        # Ambiguous either way: index -1 means the whole array, and more than
        # one resolved target means the click landed on the array rather than a
        # component (a colour swatch now resolves to R/G/B). Reading "the"
        # input value from any of those would be an arbitrary guess.
        if getattr(prop, "is_array", False) and (target.index < 0 or len(targets) > 1):
            self.report({"WARNING"}, "Pick one channel of this array property as the Espresso input.")
            return {"CANCELLED"}
        inspector = inspect_button_target(context)
        entry = source_binding.build_source_entry([target], inspector.display_label if inspector else "")
        if not entry:
            self.report({"WARNING"}, "Could not remember this input source.")
            return {"CANCELLED"}
        source_binding.remember_source(props, entry)
        self.report({"INFO"}, f"Espresso input source set: {entry.get('display_label', 'property')}")
        return {"FINISHED"}


class ESPRESSO_OT_relink_button_input(bpy.types.Operator):
    bl_idname = "espresso.relink_button_input"
    bl_label = "Relink Espresso Input & Applied Drivers"
    bl_description = (
        "Replace the remembered Espresso input and update matching Espresso "
        "driver variables on the active object"
    )
    bl_options = {"REGISTER", "UNDO"}

    replacement_entry: bpy.props.StringProperty(options={"HIDDEN"})
    old_label: bpy.props.StringProperty(options={"HIDDEN"})
    new_label: bpy.props.StringProperty(options={"HIDDEN"})
    affected_count: bpy.props.IntProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return bool(
            scene_props
            and source_binding.latest_source(scene_props)
            and getattr(context, "active_object", None)
            and resolve_button_driver_targets(context)
        )

    def _replacement_from_context(self, context):
        targets = resolve_button_driver_targets(context)
        if not targets:
            return None
        target = targets[0]
        prop = getattr(context, "button_prop", None)
        if target.index < 0 and getattr(prop, "is_array", False):
            return None
        inspector = inspect_button_target(context)
        return source_binding.build_source_entry(
            [target], inspector.display_label if inspector else "",
        )

    def invoke(self, context, event):
        scene_props = context.scene.espresso_props
        old_entry = source_binding.latest_source(scene_props)
        new_entry = self._replacement_from_context(context)
        if not new_entry:
            self.report({"WARNING"}, "Pick one usable scalar property or array channel.")
            return {"CANCELLED"}
        plan = source_binding.plan_input_relink(
            context.active_object, old_entry, new_entry,
        )
        if not plan["ok"]:
            self.report({"WARNING"}, plan["message"])
            return {"CANCELLED"}
        self.replacement_entry = json.dumps(new_entry, sort_keys=True)
        self.old_label = old_entry.get("display_label", "Remembered property")
        self.new_label = new_entry.get("display_label", "Replacement property")
        self.affected_count = plan["count"]
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Replace the remembered Espresso input?", icon="QUESTION")
        old_row = layout.row(align=True)
        old_row.label(text="Old:")
        old_row.label(text=self.old_label, icon="ERROR")
        new_row = layout.row(align=True)
        new_row.label(text="New:")
        new_row.label(text=self.new_label, icon="LINKED")
        layout.separator()
        layout.label(
            text=f"Matching variables on active object: {self.affected_count}",
            icon="DRIVER",
        )

    def execute(self, context):
        scene_props = context.scene.espresso_props
        old_entry = source_binding.latest_source(scene_props)
        new_entry = source_binding._read_entry(self.replacement_entry)
        if not old_entry or not new_entry:
            self.report({"WARNING"}, "The input relink context is no longer available.")
            return {"CANCELLED"}
        plan = source_binding.plan_input_relink(
            context.active_object, old_entry, new_entry,
        )
        if not plan["ok"]:
            self.report({"WARNING"}, plan["message"])
            return {"CANCELLED"}
        ok, message, _count = source_binding.apply_input_relink(
            plan, scene_props, new_entry,
        )
        if not ok:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        context.view_layer.update()
        self.report({"INFO"}, message)
        return {"FINISHED"}


# ESPRESSO_OT_remove_driver_from_button was removed: it duplicated Blender's own
# Delete Drivers / Delete Single Driver, which appear at the top of the same
# right-click menu and handle every case correctly. Clearing whole transform
# channels across a selection is a different job and lives in
# espresso.clear_transform_drivers.


def _pairable_vector_target(context):
    """Return (owner, data_path) when the clicked property is an array of >=2
    components and the current template has a paired template; else None."""
    scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if not (scene_props and scene_props.is_valid):
        return None
    template = espresso_props.get_current_template(scene_props)
    pair_id = template.get("pair_with")
    if not pair_id or pair_id not in TEMPLATE_BY_ID:
        return None
    if _required_variable_names(template) or _required_variable_names(TEMPLATE_BY_ID[pair_id]):
        return None
    prop = getattr(context, "button_prop", None)
    if prop is None or not getattr(prop, "is_array", False):
        return None
    if int(getattr(prop, "array_length", 0) or 0) < 2:
        return None
    targets = resolve_button_driver_targets(context)
    if not targets:
        return None
    return targets[0].owner, targets[0].data_path


class ESPRESSO_OT_apply_pair_to_button(bpy.types.Operator):
    bl_idname = "espresso.apply_pair_to_button"
    bl_label = "Apply Paired Templates (X/Y)"
    bl_description = "Drive the first two channels with this template and its paired template (e.g. circle/figure-8 X and Y)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _pairable_vector_target(context) is not None

    def execute(self, context):
        info = _pairable_vector_target(context)
        if info is None:
            self.report({"WARNING"}, "No pairable vector property found.")
            return {"CANCELLED"}
        owner, data_path = info
        scene_props = context.scene.espresso_props
        template = espresso_props.get_current_template(scene_props)
        pair = TEMPLATE_BY_ID[template["pair_with"]]

        from ...engine import utils

        pair_expr, _warn = utils.build_expression(pair, {}, context.scene)
        plans = [(0, scene_props.preview), (1, pair_expr)]
        for _index, expression in plans:
            valid, message = utils.validate_driver_expression(
                expression, template, context.scene,
            )
            if not valid:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
        foreign_message = _foreign_motion_on_button_targets(
            [ButtonDriverTarget(owner, data_path, index) for index, _expression in plans]
        )
        if foreign_message:
            self.report({"WARNING"}, foreign_message)
            return {"CANCELLED"}
        applied = 0
        for index, expression in plans:
            try:
                result = owner.driver_add(data_path, index)
            except Exception as exc:
                self.report({"WARNING"}, f"Could not drive channel {index}: {exc}")
                return {"CANCELLED"}
            fcurves = result if isinstance(result, list) else [result]
            for fcurve in fcurves:
                if fcurve is None:
                    continue
                ok, message = utils.assign_driver_expression(
                    fcurve.driver, expression, template, context.scene,
                )
                if not ok:
                    self.report({"WARNING"}, message)
                    return {"CANCELLED"}
                applied += 1
        applied = [ButtonDriverTarget(owner, data_path, index) for index, _expression in plans]
        inspector = inspect_button_target(context)
        target_memory.remember_targets(
            context,
            applied,
            inspector.display_label if inspector else "",
            "template",
            template["id"],
            template["name"],
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        self.report({"INFO"}, f"Applied {template['name']} + {pair['name']} to X/Y.")
        return {"FINISHED"}


def _channel_plan_on_clicked_array(context):
    """Return (owner, data_path, channels) when the clicked array property can
    host the current template's whole channel plan.

    A multi-channel template declares WHERE it expects to land (an object's
    ``color``, say). That declaration is a convenience, not a restriction: the
    channels are really just "expression per index", and any array with enough
    components can host them. A node socket's colour is a four-float array and
    animatable, so an RGB cycle belongs on it as much as on Object.color - the
    normal apply route simply could not see it, because it resolves an Object
    and a shader socket has none.

    Deliberately narrow:
    * only when the ordinary object route is unavailable, so this never
      duplicates the entry above it;
    * only when every channel shares one data path - a plan that drives a
      location AND a rotation cannot be poured into a single colour.
    """
    scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if not (scene_props and scene_props.is_valid):
        return None
    template = espresso_props.get_current_template(scene_props)
    if not template_catalogue.has_motion_plan(template):
        return None
    if _button_flow_block_message(template, scene_props):
        return None

    channels = [c for c in template_catalogue.template_channels(template) if c.get("data_path")]
    if len(channels) < 2:
        return None
    if len({c["data_path"] for c in channels}) != 1:
        return None

    prop = getattr(context, "button_prop", None)
    if prop is None or not getattr(prop, "is_array", False):
        return None
    if not getattr(prop, "is_animatable", True) or getattr(prop, "is_readonly", False):
        return None
    needed = max(int(c.get("index", 0)) for c in channels) + 1
    if int(getattr(prop, "array_length", 0) or 0) < needed:
        return None

    targets = resolve_button_driver_targets(context)
    if not targets:
        return None
    if motion_channels.resolve_motion_object(targets[0].owner) is not None:
        return None      # the ordinary route already covers this
    return targets[0].owner, targets[0].data_path, channels


class ESPRESSO_OT_apply_channels_to_button(bpy.types.Operator):
    bl_idname = "espresso.apply_channels_to_button"
    bl_label = "Apply Template Channels"
    bl_description = (
        "Drive each component of this property with its matching channel from "
        "the current multi-channel template"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _channel_plan_on_clicked_array(context) is not None

    def execute(self, context):
        info = _channel_plan_on_clicked_array(context)
        if info is None:
            self.report({"WARNING"}, "No array property found for this template's channels.")
            return {"CANCELLED"}
        owner, data_path, channels = info
        scene_props = context.scene.espresso_props
        template = espresso_props.get_current_template(scene_props)

        from ...engine import utils

        built = {entry.get("id"): entry for entry in espresso_props.built_channel_previews(scene_props)}
        plans = []
        for channel in channels:
            entry = built.get(channel.get("id"))
            expression = (entry or {}).get("expression")
            if not expression:
                self.report({"WARNING"}, f"No built expression for {channel.get('label', channel.get('id'))}.")
                return {"CANCELLED"}
            plans.append((int(channel.get("index", 0)), expression, channel))

        # Validate every channel BEFORE writing any, so a failure halfway does
        # not leave the property half-driven.
        for _index, expression, channel in plans:
            valid, message = utils.validate_driver_expression(expression, template, context.scene)
            if not valid:
                self.report({"WARNING"}, f"{channel.get('label', '')}: {message}")
                return {"CANCELLED"}

        foreign_message = _foreign_motion_on_button_targets(
            [ButtonDriverTarget(owner, data_path, index) for index, _expression, _channel in plans]
        )
        if foreign_message:
            self.report({"WARNING"}, foreign_message)
            return {"CANCELLED"}

        applied = 0
        for index, expression, channel in plans:
            try:
                result = owner.driver_add(data_path, index)
            except Exception as exc:
                self.report({"WARNING"}, f"Could not drive {channel.get('label', index)}: {exc}")
                return {"CANCELLED"}
            for fcurve in (result if isinstance(result, list) else [result]):
                if fcurve is None:
                    continue
                ok, message = utils.assign_driver_expression(
                    fcurve.driver, expression, template, context.scene,
                )
                if not ok:
                    self.report({"WARNING"}, message)
                    return {"CANCELLED"}
                applied += 1
        # Record what was driven. Without this, "Update Last Targets" and
        # "Clear Applied Drivers" have nothing to act on - the apply worked but
        # the panel could not follow it up. Copied the apply pattern from
        # apply_pair_to_button, which had the same gap; both are fixed now.
        applied = [ButtonDriverTarget(owner, data_path, index) for index, _e, _c in plans]
        inspector = inspect_button_target(context)
        target_memory.remember_targets(
            context,
            applied,
            inspector.display_label if inspector else "",
            "template",
            template["id"],
            template["name"],
        )
        espresso_props.set_last_apply_status(
            scene_props,
            f"Remembered: {target_memory.latest_label(scene_props) or 'target'}",
        )
        names = " / ".join(c.get("label", "") for _i, _e, c in plans)
        self.report({"INFO"}, f"Applied {template['name']} to {names}.")
        return {"FINISHED"}


class ESPRESSO_OT_apply_multi_to_button(bpy.types.Operator):
    bl_idname = "espresso.apply_multi_to_button"
    bl_label = "Apply Multi"
    bl_description = (
        "Apply the current recipe across the whole property; motion recipes "
        "keep their individual channel expressions"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if not (scene_props and scene_props.is_valid):
            return False
        template = espresso_props.get_current_template(scene_props)
        if not template_catalogue.has_motion_plan(template):
            return ESPRESSO_OT_apply_to_button_multi.poll(context)

        channels = template_catalogue.template_channels(template)
        if len(channels) <= 1:
            return ESPRESSO_OT_apply_to_button_multi.poll(context)
        return (
            template_fits_button_target(context, scene_props, template)
            or _channel_plan_on_clicked_array(context) is not None
        )

    def execute(self, context):
        scene_props = context.scene.espresso_props
        template = espresso_props.get_current_template(scene_props)
        if template_catalogue.has_motion_plan(template):
            channels = template_catalogue.template_channels(template)
            if len(channels) > 1:
                if _channel_plan_on_clicked_array(context) is not None:
                    return ESPRESSO_OT_apply_channels_to_button.execute(self, context)
                return ESPRESSO_OT_apply_to_button.execute(self, context)
        return ESPRESSO_OT_apply_to_button_multi.execute(self, context)


def _draw_apply_groups(layout, context, template, groups):
    """Draw table-resolved apply entries in a submenu or directly in parent."""
    for index, group in enumerate(groups):
        if index:
            layout.separator()
        for entry in group:
            if entry.companion is None:
                row = layout
            else:
                row = layout.row().split(factor=0.82, align=True)
            operator = row.operator(
                entry.idname, text=entry.label(context, template), icon=entry.icon)
            for name, value in entry.properties.items():
                setattr(operator, name, value)
            if entry.companion is not None:
                idname, text, icon = entry.companion
                row.row(align=True).operator(idname, text=text, icon=icon)


class ESPRESSO_MT_apply_template(bpy.types.Menu):
    """The apply operators, gathered behind one entry.

    Loose in the parent menu they were seven rows of a fifteen-row block, all
    starting with the word "Apply", in a right-click menu that already carries
    Blender's own driver commands and two other add-ons. One entry that opens
    them keeps the block readable and puts the whole family in one place.

    The parent draws this only when something inside it applies, so opening it
    is never a dead end.
    """

    bl_idname = "ESPRESSO_MT_apply_template"
    bl_label = "Apply Template"

    def draw(self, context):
        layout = self.layout
        scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)
        template = espresso_props.get_current_template(scene_props) if scene_props else None

        groups = button_menu_map.grouped_entries(context, scene_props, template)
        _draw_apply_groups(layout, context, template, groups)


def draw_button_context_menu(self, context):
    if not resolve_button_driver_targets(context):
        return

    layout = self.layout
    layout.separator()
    layout.label(text=_PRODUCT_NAME, icon="DRIVER")

    scene_props = getattr(getattr(context, "scene", None), "espresso_props", None)

    # The block, in three groups: what to APPLY, what to BIND, and the two
    # utilities. It grew one entry at a time and read as one flat list of
    # fifteen, most of them starting with the word "Apply".
    #
    # An entry that cannot act on the property under the cursor is left out
    # rather than drawn greyed - a dead row says nothing its absence does not,
    # and this sits at the bottom of a menu that already carries Blender's own
    # driver commands and two other add-ons.

    current_template = espresso_props.get_current_template(scene_props) if scene_props else None

    # 1. Apply. Two or fewer actual actions are quicker to use directly. A
    # submenu earns its extra click only once it is organizing three or more.
    apply_groups = button_menu_map.grouped_entries(
        context, scene_props, current_template)
    if apply_groups:
        if button_menu_map.should_inline_apply_entries(apply_groups):
            _draw_apply_groups(layout, context, current_template, apply_groups)
        else:
            layout.menu("ESPRESSO_MT_apply_template", icon="DRIVER")
        layout.separator()

    # 2. Binding: where a value comes FROM, where it goes TO, and repointing
    # both at something else. From the same table as the apply family, so
    # Target can be gated on the templates that can actually consume one
    # without a second opinion living here.
    for entry in button_menu_map.entries_in_group(
            context, scene_props, current_template, button_menu_map.BINDING):
        layout.operator(
            entry.idname, text=entry.label(context, current_template), icon=entry.icon)

    layout.separator()

    # 3. Utilities that act on the property under the cursor rather than on the
    # template on screen.
    #
    # Paste is here rather than beside Apply on purpose: it applies a driver
    # COPIED from somewhere else, not the template the panel is showing, and
    # sitting under Apply Template it read as a third way to apply that.
    paste_enabled = bool(scene_props and scene_props.copied_driver_expression)
    if paste_enabled:
        from ...catalogue.core.templates import TEMPLATE_BY_ID

        copied_template = TEMPLATE_BY_ID.get(scene_props.copied_driver_template)
        copied_source = source_binding._read_entry(getattr(scene_props, "copied_driver_source", "{}"))
        paste_enabled = bool(copied_template and not _button_flow_block_message(copied_template, source_entry=copied_source))
    if paste_enabled:
        label = "Paste Copied Driver"
        if scene_props.copied_driver_label:
            label = "Paste Copied Driver: " + scene_props.copied_driver_label
        layout.operator("espresso.paste_copied_to_button", text=label, icon="PASTEDOWN")

    # Baking existing drivers belongs HERE rather than in the N-panel, because
    # it acts on the property under the cursor rather than on the template on
    # screen. The panel keeps only Bake Last Drivers, which finishes what the
    # template itself applied.
    #
    # This entry was once removed on the reasoning that baking any driver is
    # Driver Tools' job. That still holds for a driver Espresso never made, but
    # the action was living in the N-panel in the meantime, which is the one
    # place it does not belong - the panel is about the current template.
    layout.operator(
        "espresso.bake_drivers",
        text="Bake Drivers to Keyframes…",
        icon="KEYFRAME_HLT",
    )

    layout.separator()

    layout.operator("espresso.open_panel_popup", text="Open Espresso Panel (Floating)", icon="PREFERENCES")

    # No "Apply as Colour Ramp" entry. The ramp is configured in Parameters
    # before applying now, and plain apply already builds it from those stops
    # when a palette template lands on a colour socket - see the wants_palette
    # branch in ESPRESSO_OT_apply_to_button.execute.

    # No "Remove Driver" entry: Blender's own Delete Drivers / Delete Single
    # Driver sit at the top of this very menu and do the job properly. Repeating
    # it here only made our block longer and the choice ambiguous.


# The bake popup lives in operators.py: context_menu imports operators, so a
# shared definition can only sit on that side without a circular import.
# Aliased rather than re-declared so the two entry points cannot drift apart.
_BakeOptionsMixin = operators.BakeOptionsMixin


from ...engine.baking.transaction import atomic_bake_operator


@atomic_bake_operator
class ESPRESSO_OT_apply_and_bake_to_button(_BakeOptionsMixin, bpy.types.Operator):
    bl_idname = "espresso.apply_and_bake_to_button"
    bl_label = "Apply and Bake Template"
    bl_description = "Apply the current Driver Espresso template to this property, then immediately bake it to keyframes - one step from template to frozen animation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # Same reach as plain Apply Current Template: if that could apply here,
        # this can apply then bake here.
        return ESPRESSO_OT_apply_to_button.poll(context)

    def invoke(self, context, event):
        self._seed_range(context)
        return context.window_manager.invoke_props_dialog(self, width=280)

    def execute(self, context):
        self._commit_range(context)
        from ...engine import bake

        # Anchor the apply at the bake START frame. Additive Rest Start snaps to
        # the current frame and holds everything before it flat - so applying at
        # the playhead and baking from frame 1 would bake a null run up to the
        # playhead. Moving the frame to bake_start first makes the effect begin
        # where the bake begins, and we restore the playhead afterwards.
        scene = context.scene
        original_frame = scene.frame_current
        scene.frame_set(int(self.bake_start))

        # 1. Apply exactly as the plain button does, reusing its whole path so
        # channel plans, single expressions and multi-index all behave the same.
        result = bpy.ops.espresso.apply_to_button()
        if "FINISHED" not in result:
            scene.frame_set(original_frame)
            return {"CANCELLED"}

        # 2. The just-applied targets are the freshly remembered entry.
        scene_props = context.scene.espresso_props
        entry = target_memory.latest_entry(scene_props)
        targets = _entry_button_targets(entry)
        if not targets:
            self.report({"WARNING"}, "The new drivers could not be located to bake.")
            scene.frame_set(original_frame)
            return {"CANCELLED"}
        cleanup = target_memory.capture_cleanup_for_entry(entry, scene_props)
        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            baked, keys, message = bake.bake_targets(
                context.scene, targets,
                start=self.bake_start, end=self.bake_end,
                step=self.bake_step, remove_driver=True,
                smart=self.bake_smart,
                smart_tolerance=self.bake_smart_tolerance / 100.0,
                smart_passes=self.bake_smart_passes,
                progress=lambda done, total: wm.progress_update(done / max(1, total)),
            )
        finally:
            wm.progress_end()
        if not baked:
            scene.frame_set(original_frame)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        target_memory.cleanup_captured_resources(
            cleanup, context.scene, scene_props,
        )
        target_memory.clear_latest_entry(scene_props)  # it is keyframes now, not a driver
        scene.frame_set(original_frame)
        self.report({"INFO"}, message)
        return {"FINISHED"}


def _driven_button_targets(context):
    """Right-click targets that actually have a driver on them."""
    driven = []
    for target in resolve_button_driver_targets(context):
        owner = target.owner
        id_data = getattr(owner, "id_data", None)
        anim = getattr(id_data, "animation_data", None) if id_data else getattr(owner, "animation_data", None)
        if anim is None:
            continue
        try:
            local = owner.path_from_id() if id_data and id_data is not owner else ""
        except Exception:
            local = ""
        full = f"{local}.{target.data_path}" if local else target.data_path
        for driver in anim.drivers:
            if driver.data_path == full and (target.index < 0 or driver.array_index == target.index):
                driven.append(target)
                break
    return driven


def _entry_button_targets(entry):
    """Rebuild ButtonDriverTargets from a remembered apply entry."""
    from ...apply import target_memory as _tm

    resolved, _reason = _tm.resolve_entry(entry or {})
    targets = []
    for item in resolved or []:
        owner = item.get("owner")
        if owner is None:
            continue
        targets.append(ButtonDriverTarget(owner, item.get("data_path", ""), int(item.get("index", -1))))
    return targets


CLASSES = (
    ESPRESSO_MT_apply_template,
    ESPRESSO_OT_apply_to_button,
    ESPRESSO_OT_apply_ramp_to_socket,
    ESPRESSO_OT_use_as_apply_target,
    ESPRESSO_OT_clear_apply_target,
    ESPRESSO_OT_apply_to_selected_objects,
    ESPRESSO_OT_apply_to_button_multi,
    ESPRESSO_OT_paste_copied_to_button,
    ESPRESSO_OT_open_panel_popup,
    ESPRESSO_OT_use_button_as_input,
    ESPRESSO_OT_relink_button_input,
    ESPRESSO_OT_apply_pair_to_button,
    ESPRESSO_OT_apply_channels_to_button,
    ESPRESSO_OT_apply_multi_to_button,
    ESPRESSO_OT_apply_and_bake_to_button,
)


def _remove_our_menu_draws():
    """Drop our draw function even when it came from a previous module instance.

    ``Menu.remove`` matches by object identity. Reloading the add-on rebuilds
    this module, so the function appended by the previous load is a *different*
    object that unregister can no longer reach: it stays in the menu, and the
    next register appends another one. That is exactly why the right-click menu
    grows an extra "Driver Espresso" block on every reload or update. Matching
    by name removes ours whichever module instance created it.
    """
    menu = getattr(bpy.types, "UI_MT_button_context_menu", None)
    if menu is None:
        return
    existing = getattr(getattr(menu, "draw", None), "_draw_funcs", None) or ()
    for func in list(existing):
        if getattr(func, "__name__", "") == draw_button_context_menu.__name__:
            menu.remove(func)


def _shipped_classes():
    """CLASSES minus the operators this build has no template for.

    `Apply Paired Templates (X/Y)` needs a template with a `pair_with`. In a
    build that ships none, its poll can never pass and its menu entry is hidden,
    so registering it only puts a dead command into F3 search.
    """
    if any(item.get("pair_with") for item in TEMPLATE_BY_ID.values()):
        return CLASSES
    return tuple(cls for cls in CLASSES if cls is not ESPRESSO_OT_apply_pair_to_button)


def register():
    for cls in _shipped_classes():
        bpy.utils.register_class(cls)
    # Self-healing: clears strays a previous load's unregister could not reach,
    # so an already-duplicated menu repairs itself on the next enable rather
    # than needing a Blender restart.
    _remove_our_menu_draws()
    bpy.types.UI_MT_button_context_menu.append(draw_button_context_menu)


def unregister():
    _remove_our_menu_draws()
    for cls in reversed(_shipped_classes()):
        bpy.utils.unregister_class(cls)
