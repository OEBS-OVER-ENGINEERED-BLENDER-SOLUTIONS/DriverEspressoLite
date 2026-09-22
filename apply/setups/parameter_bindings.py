"""Shared Setup/Live parameter routing.

The UI owns one pair of Setup/Live tabs.  This module decides whether the
active applied effect can actually be edited and delegates to the native route
that owns its values.  Unsupported routes stay visible and explanatory instead
of pretending a slider update reached them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...catalogue import templates
from ...engine import utils
from ..core import target_memory
from . import layout_preparation, generated_bindings


_GENERATED_ROUTES = frozenset({
    "Prepared Layout", "Prepared Field", "Object Set", "Object Workflow",
    "Generated Graphic", "Travel Reveal", "Event Cascade",
})
_STRUCTURAL_ROUTE = "Prepared Layout Structure"

_ADAPTER_MATRIX = {
    "driver_single": {"state": "SUPPORTED", "reason": "Stored single-property drivers recompile through the expression-integrity pipeline."},
    "driver_motion_set": {"state": "SUPPORTED", "reason": "Declared multi-channel motion sets rebuild every channel as one atomic Live update."},
    "generated_owned": {"state": "SUPPORTED", "reason": "Owned generated setups delegate to their native route adapter and carrier."},
    "shared_material": {"state": "SUPPORTED", "reason": "Shared-material spatial fields update owned group inputs and the time driver in place."},
    "helper_sampled": {"state": "READ_ONLY", "reason": "Sampled native helpers remain portable, but Live editing needs an atomic re-sampling adapter."},
    "legacy_node_route": {"state": "READ_ONLY", "reason": "Legacy generated node routes must be reapplied through an owned native adapter."},
    "legacy_missing_values": {"state": "READ_ONLY", "reason": "Legacy records without friendly parameter values must be reapplied once."},
    "constraint": {"state": "DEFERRED", "reason": "No shipped parameterized recipe currently owns a generic constraint-only Live route."},
    "modifier": {"state": "DEFERRED", "reason": "Generic modifiers remain deferred; owned Geometry Nodes modifiers use generated_owned."},
    "material_node": {"state": "DEFERRED", "reason": "Ordinary node-socket drivers use driver_single; generated material graphs need an owned adapter."},
    "baked_detached": {"state": "READ_ONLY", "reason": "Baked or detached motion has no live Espresso carrier to edit."},
}
_SHARED_MATERIAL_ROUTE = "Shared Material"
_SURFACE_ROUTE = "Surface Motion"


@dataclass
class ParameterBinding:
    available: bool
    editable: bool = False
    template_id: str = ""
    label: str = ""
    route: str = ""
    reason: str = ""
    payload: object = None
    context: object = None
    layer_id: str = ""


def adapter_matrix():
    """Return the complete route decision table without mutable aliases."""
    return {name: dict(decision) for name, decision in _ADAPTER_MATRIX.items()}


def supports(template):
    """Whether the recipe has controls worth a Setup / Live switch.

    Driver parameters are the usual answer, but not the only one. An authoring
    recipe declares none -- an authoring rig is flown, not driven -- and yet its declared
    target slots differ between the two modes: Setup shows the slots being
    staged, Live shows what the flying rig is actually bound to. Gating on
    params alone hid that switch entirely, and with it the only view of a live
    binding.
    """
    if not template:
        return False
    if template.get("params"):
        return True
    return bool(template.get("authoring_kind") and template.get("target_slots"))


def binding_key(binding):
    if not binding or not binding.available:
        return ""
    if binding.route == _STRUCTURAL_ROUTE:
        carrier = binding.payload
        return "layout:%s" % str(carrier.get(layout_preparation.LAYOUT_ID_TAG, ""))
    if binding.route in _GENERATED_ROUTES:
        key = generated_bindings.binding_key(binding.payload)
        return "%s:layer:%s" % (key, binding.layer_id) if binding.layer_id else key
    entry = binding.payload or {}
    targets = entry.get("targets") or ()
    first = targets[0] if targets else {}
    return "driver:%s:%s:%s:%s:%s" % (
        entry.get("template_id", ""), first.get("id_type", ""),
        first.get("id_name", ""), first.get("data_path", ""),
        int(first.get("index", -1)),
    )


def _generated_binding(context, template, active_object=None):
    if not generated_bindings.supports(template.get("id")):
        return None
    native = generated_bindings.resolve(
        active_object if active_object is not None else getattr(context, "active_object", None),
        template.get("id", ""),
    )
    label = native.label
    return ParameterBinding(
        available=bool(native.available),
        editable=bool(native.available),
        template_id=template.get("id", ""),
        label=label,
        route=native.route or "Prepared Layout",
        reason=native.reason,
        payload=native,
        context=context,
    )


def _surface_binding(context, template, active_object=None):
    """Live editing for the definitive surface systems.

    These write straight onto their own Geometry Nodes group -- there is no
    driver to rebind and no carrier to find, so the binding is simply "is this
    system on this object". Without an arm here `live_parameter_binding`
    returned nothing, `sync_live_parameters` answered "Live parameters are
    unavailable for this template", and every control in the Live tab was
    greyed even though the setup was sitting on the object.
    """
    from ..spatial import spatial_fields

    template_id = str((template or {}).get("id") or "")
    if template_id not in spatial_fields.SURFACE_IDS:
        return None
    obj = active_object if active_object is not None else getattr(
        context, "active_object", None)
    if obj is None:
        return ParameterBinding(
            False, template_id=template_id, route=_SURFACE_ROUTE,
            reason="Select the object this system is on.", context=context)

    if template_id == spatial_fields.HAZE_ID:

        scene = getattr(context, "scene", None)
        live = None is not None
        return ParameterBinding(
            available=live, editable=live, template_id=template_id,
            label="%s · %s" % ((template or {}).get("name") or "Effect",
                               obj.name),
            route=_SURFACE_ROUTE,
            reason="" if live else "Heat haze is not on this object.",
            payload=obj, context=context)

    live = bool(False)
    return ParameterBinding(
        available=live, editable=live, template_id=template_id,
        label="%s · %s" % ((template or {}).get("name") or template_id,
                           obj.name),
        route=_SURFACE_ROUTE,
        reason="" if live else "This system is not on the active object.",
        payload=obj, context=context)


def _shared_material_template(template):
    from ..spatial import shared_material_sweep

    return shared_material_sweep.kind_for(template) is not None


def _driver_read_only_reason(template, entry, resolved):
    if entry.get("apply_kind") == "node_route":
        if _shared_material_template(template):
            return ""
        return _ADAPTER_MATRIX["legacy_node_route"]["reason"]
    if len(resolved or ()) != 1:
        from ..motion import motion_channels

        # Structural, not a count: a Quaternion bone holds four rotation
        # targets for three Euler channels, and a disabled channel holds none.
        # The count test called both a mismatch and greyed every Live control
        # on a perfectly healthy motion.
        if not templates.template_channels(template) or not (
            motion_channels.stored_targets_follow_plan(template, resolved)
        ):
            return "The applied targets no longer match this motion's declared channels."
    if template.get("internal_helpers"):
        return _ADAPTER_MATRIX["helper_sampled"]["reason"]
    if not isinstance(entry.get("parameter_values"), dict):
        return _ADAPTER_MATRIX["legacy_missing_values"]["reason"]
    return ""


def binding_for_entry(context, template, entry, label=""):
    """Resolve Live routing for one remembered driver or shared-material entry."""
    return _driver_binding(context, template, entry, label=label)


def _driver_binding(context, template, entry, label=""):
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return ParameterBinding(
            False, template_id=template.get("id", ""), context=context,
            reason=reason or "The applied driver is no longer available.",
        )
    read_only = _driver_read_only_reason(template, entry, resolved)
    route = (
        _SHARED_MATERIAL_ROUTE
        if not read_only and entry.get("apply_kind") == "node_route"
        and _shared_material_template(template)
        else "Driver"
    )
    return ParameterBinding(
        True, editable=not read_only, template_id=template.get("id", ""),
        label=label or entry.get("display_label") or template.get("name", "Applied driver"),
        route=route, reason=read_only, payload=entry, context=context,
    )


def binding_for_effect(context, effect):
    """Resolve one exact Applied Effects record rather than merely its template."""
    template = (effect or {}).get("template")
    if not supports(template):
        return ParameterBinding(False, reason="This recipe has no editable parameters.")
    surface = _surface_binding(
        context, template, active_object=(effect or {}).get("host"))
    if surface is not None:
        return surface
    child_kind = str((effect or {}).get("child_effect_kind") or "")
    if child_kind == "SPATIAL_EFFECTOR":
        binding = _generated_binding(
            context, template, active_object=(effect or {}).get("host"),
        )
        if binding is None:
            return ParameterBinding(
                False, template_id=template.get("id", ""), context=context,
                reason="The Effector's generated field no longer resolves.",
            )
        binding.layer_id = str((effect or {}).get("layer_id") or "")
        binding.label = (
            (effect or {}).get("label")
            or "%s Effector" % template.get("name", "Spatial")
        )
        return binding
    if (effect or {}).get("effect_kind") == "STRUCTURAL_EFFECT":
        carrier = (effect or {}).get("host")
        available = bool(layout_preparation.is_carrier(carrier) and layout_preparation.layout_modifier(carrier))
        return ParameterBinding(
            available=available, editable=available,
            template_id=template.get("id", ""),
            label=(effect or {}).get("label") or template.get("name", "Prepared Layout"),
            route=_STRUCTURAL_ROUTE,
            reason="" if available else "The Prepared Layout no longer resolves.",
            payload=carrier, context=context,
        )
    record = (effect or {}).get("record") or {}
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    entry = extras.get("target_entry")
    if isinstance(entry, dict):
        return _driver_binding(
            context, template, entry,
            label=(effect or {}).get("label") or template.get("name", "Applied driver"),
        )
    return resolve(context, template, active_object=(effect or {}).get("host"))


def active_effect_choices(context):
    """Return parameterized effects owned by the active object only."""
    from ..motion import applied_motion_manager

    choices = []
    for effect in applied_motion_manager.collect_effects_for_draw(context, "ACTIVE"):
        template = effect.get("template")
        if not supports(template):
            continue
        binding = binding_for_effect(context, effect)
        if not binding.available:
            continue
        choices.append({
            "record_token": effect.get("record_token", ""),
            "template_id": template.get("id", ""),
            "label": effect.get("label") or template.get("name", "Applied motion"),
            "binding": binding,
        })
    return tuple(choices)


def resolve(context, template, active_object=None):
    """Resolve the active object's compatible applied effect without scene scans."""
    if not supports(template):
        return ParameterBinding(False, reason="This recipe has no editable parameters.")
    surface = _surface_binding(context, template, active_object=active_object)
    if surface is not None:
        return surface
    generated = _generated_binding(context, template, active_object=active_object)
    if generated is not None and generated.available:
        return generated

    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    active = active_object if active_object is not None else getattr(context, "active_object", None)
    entry = target_memory.entry_for_active_object(
        props, active, template.get("id", ""),
    ) if props is not None and active is not None else None
    if entry is None:
        if generated is not None and generated.reason:
            return generated
        return ParameterBinding(
            False, template_id=template.get("id", ""), context=context,
            reason="No compatible applied effect was found on the active object.",
        )
    return _driver_binding(context, template, entry)


def carrier(binding):
    """Return the route-owned native carrier without leaking adapter payloads."""
    native = getattr(binding, "payload", None) if binding else None
    return getattr(native, "carrier", None)


def read_values(binding):
    if not binding or not binding.available:
        return {}
    if binding.route == _STRUCTURAL_ROUTE:
        return layout_preparation.read_parameter_values(binding.payload)
    if binding.route in _GENERATED_ROUTES:
        if binding.layer_id:

            return {}
        return generated_bindings.read_values(binding.payload)
    values = (binding.payload or {}).get("parameter_values")
    return dict(values) if isinstance(values, dict) else {}


def write_values(binding, values, scene=None, template=None, enabled_channel_ids=None):
    if not binding or not binding.available:
        return False, binding.reason if binding else "No live effect is active."
    if not binding.editable:
        return False, binding.reason or "This applied effect is read-only."
    if binding.route == _STRUCTURAL_ROUTE:
        return layout_preparation.write_parameter_values(binding.payload, values)
    if binding.route in _GENERATED_ROUTES:
        if binding.layer_id:

            return (False, "")
        return generated_bindings.write_values(
            binding.payload, values, scene=scene,
        )
    if binding.route == _SURFACE_ROUTE:
        from ..spatial import spatial_fields

        obj = binding.payload
        if binding.template_id == spatial_fields.HAZE_ID:

            settings = {str(k): v for k, v in (values or {}).items()}
            ok, message = (False, "")
            return ok, message

        (False, "")
        return True, "%s updated." % binding.label
    if binding.route == _SHARED_MATERIAL_ROUTE:
        from ..spatial import shared_material_sweep

        template = template or templates.TEMPLATE_BY_ID.get(binding.template_id)
        ok, message = shared_material_sweep.write_live_values(
            binding.payload, template, values, scene=scene,
        )
        if not ok:
            return False, message
        binding.payload["parameter_values"] = target_memory.json_safe_values(values)
        target_memory.remember_live_entry(binding.context, binding.payload)
        return True, message

    template = template or templates.TEMPLATE_BY_ID.get(binding.template_id)
    if template is None:
        return False, "The applied recipe is no longer in the catalogue."
    scene = scene or getattr(binding.context, "scene", None)
    note = None
    if templates.has_motion_plan(template):
        from ..motion import motion_channels

        built = utils.build_template_expressions(template, values, scene)
        # One expression per STORED target, in the entry's order -- through
        # the quaternion form when the bone took one, minus any channel that
        # was disabled before Apply.
        built, note = motion_channels.channels_for_stored_targets(
            built, template, scene, (binding.payload or {}).get("targets") or (),
            values=values, enabled_channel_ids=enabled_channel_ids,
        )
        if built is None:
            return False, note
        expressions = [item["expression"] for item in built]
        baselines = [item.get("output_baseline") for item in built]
    else:
        expression, warnings, details = utils.build_expression(
            template, values, scene, include_details=True,
        )
        if warnings:
            blocking = [
                item for item in warnings
                if str(item).lower().startswith("unresolved") or utils.is_blocking_warning(item)
            ]
            if blocking:
                return False, blocking[0]
        expressions = expression
        baselines = details.get("output_baseline")
    ok, message = target_memory.apply_expression_to_entry(
        binding.payload, expressions, template, scene=scene,
        output_baseline=baselines,
    )
    if not ok:
        return False, message
    binding.payload["parameter_values"] = target_memory.json_safe_values(values)
    target_memory.remember_live_entry(binding.context, binding.payload)
    if note:
        message = "%s %s" % (message, note) if message else note
    return True, message


def visible_tokens(binding, template, values=None):
    """Return controls that have a real destination on the resolved route."""
    tokens = {param.get("token", "") for param in (template or {}).get("params", ())}
    if binding and binding.available and binding.route == "Object Set":

        tokens.difference_update({"COLUMNS", "ROWS", "SPACING_X", "SPACING_Y"})
        tokens.intersection_update(
            ()
        )
    if binding and binding.available and binding.route == "Travel Reveal":

        tokens.difference_update(
            ()
        )
    if binding and binding.available and binding.route == "Generated Graphic":
        tokens.intersection_update(
            ()
        )
    return tokens
