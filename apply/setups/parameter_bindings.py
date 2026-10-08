"""Setup/Live parameter routing.

The UI owns one pair of Setup/Live tabs. This module decides whether the active
applied effect can be edited and, if so, rewrites its stored driver expressions
from the new parameter values. An effect that cannot be edited stays visible
with the reason, instead of pretending a slider update reached it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...catalogue import templates
from ...engine import utils
from ..core import target_memory


_LEGACY_VALUES_REASON = (
    "Legacy records without friendly parameter values must be reapplied once."
)
_PLAN_MISMATCH_REASON = (
    "The applied targets no longer match this motion's declared channels."
)


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


def supports(template):
    """Whether the recipe has parameters worth a Setup / Live switch."""
    return bool(template and template.get("params"))


def binding_key(binding):
    """A stable identity for one live binding, used to notice when it changes."""
    if not binding or not binding.available:
        return ""
    entry = binding.payload or {}
    targets = entry.get("targets") or ()
    first = targets[0] if targets else {}
    return "driver:%s:%s:%s:%s:%s" % (
        entry.get("template_id", ""), first.get("id_type", ""),
        first.get("id_name", ""), first.get("data_path", ""),
        int(first.get("index", -1)),
    )


def _driver_read_only_reason(template, entry, resolved):
    # A single-expression recipe applied to several properties (every axis of a
    # vector, say) writes the one rebuilt expression to each; only a motion
    # plan has to match its stored targets to its declared channels.
    if len(resolved or ()) != 1 and templates.has_motion_plan(template):
        from ..motion import motion_channels

        # Structural, not a count: a Quaternion bone holds four rotation
        # targets for three Euler channels, and a disabled channel holds none.
        # The count test called both a mismatch and greyed every Live control
        # on a perfectly healthy motion.
        if not motion_channels.stored_targets_follow_plan(template, resolved):
            return _PLAN_MISMATCH_REASON
    if not isinstance(entry.get("parameter_values"), dict):
        return _LEGACY_VALUES_REASON
    return ""


def binding_for_entry(context, template, entry, label=""):
    """Resolve Live routing for one remembered driver entry."""
    return _driver_binding(context, template, entry, label=label)


def _driver_binding(context, template, entry, label=""):
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return ParameterBinding(
            False, template_id=template.get("id", ""), context=context,
            reason=reason or "The applied driver is no longer available.",
        )
    read_only = _driver_read_only_reason(template, entry, resolved)
    return ParameterBinding(
        True, editable=not read_only, template_id=template.get("id", ""),
        label=label or entry.get("display_label") or template.get("name", "Applied driver"),
        route="Driver", reason=read_only, payload=entry, context=context,
    )


def binding_for_effect(context, effect):
    """Resolve one exact Applied Effects record rather than merely its template."""
    template = (effect or {}).get("template")
    if not supports(template):
        return ParameterBinding(False, reason="This recipe has no editable parameters.")
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
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    active = active_object if active_object is not None else getattr(context, "active_object", None)
    entry = target_memory.entry_for_active_object(
        props, active, template.get("id", ""),
    ) if props is not None and active is not None else None
    if entry is None:
        return ParameterBinding(
            False, template_id=template.get("id", ""), context=context,
            reason="No compatible applied effect was found on the active object.",
        )
    return _driver_binding(context, template, entry)


def read_values(binding):
    if not binding or not binding.available:
        return {}
    values = (binding.payload or {}).get("parameter_values")
    return dict(values) if isinstance(values, dict) else {}


def write_values(binding, values, scene=None, template=None, enabled_channel_ids=None):
    if not binding or not binding.available:
        return False, binding.reason if binding else "No live effect is active."
    if not binding.editable:
        return False, binding.reason or "This applied effect is read-only."

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
            enabled_channel_ids=enabled_channel_ids,
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


