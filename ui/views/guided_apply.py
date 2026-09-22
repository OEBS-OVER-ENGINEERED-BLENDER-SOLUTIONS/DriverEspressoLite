"""Presentation-only views and truthful Guided Apply preflight cards.

This module intentionally owns no Blender operators or persistent state.  It
composes the established panel functions and caches only immutable preflight
facts.  Apply operators remain authoritative and rerun preflight immediately
before they create or update resources.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...catalogue import templates
from ...engine import utils
from ...engine.motion_stack import capabilities
from ...apply.setups import generated_routes


PREFLIGHT_CACHE_VERSION = 2
_PREFLIGHT_CACHE = {}
_COST_RANK = {
    capabilities.LIGHT: 0,
    capabilities.MODERATE: 1,
    capabilities.HEAVY: 2,
}
_OUTPUT_LABELS = {
    "DRIVER_EXPRESSION": "Native Driver Expression",
    "OBJECT_TRANSFORMS": "Object Transform Motion",
    "GN_FIELD": "Geometry Nodes Field",
    "GN_GEOMETRY": "Geometry Nodes Geometry",
    "NATIVE_ANIMATION": "Native Animation",
}


@dataclass(frozen=True)
class AlternativeCard:
    template_id: str
    label: str
    reason: str
    available: bool
    cost: str


@dataclass(frozen=True)
class GuidedPreflightCard:
    template_id: str
    target: str
    result: str
    status: str
    controls: tuple[str, ...]
    created_resources: tuple[str, ...]
    conflict_policy: str
    conflict_outcome: str
    cost: str
    delivery: str
    portability: str
    gizmo_compatibility: str
    disabled_reason: str
    alternatives: tuple[AlternativeCard, ...]
    ok: bool


def clear_preflight_cache():
    """Invalidate immutable cards at authoritative lifecycle boundaries."""
    _PREFLIGHT_CACHE.clear()


def _identity(value):
    if value is None:
        return ("", 0)
    try:
        pointer = int(value.as_pointer())
    except (AttributeError, ReferenceError, TypeError, ValueError):
        pointer = 0
    return (str(getattr(value, "name_full", getattr(value, "name", ""))), pointer)


def _cache_key(context, template_id, fingerprint):
    selected = tuple(sorted(
        _identity(value) for value in (getattr(context, "selected_objects", ()) or ())
    ))
    return (
        PREFLIGHT_CACHE_VERSION, str(template_id or ""),
        _identity(getattr(context, "active_object", None)), selected, fingerprint,
    )


def _friendly(value):
    raw = getattr(value, "value", value)
    return str(raw or "").replace("_", " ").title()


def _target_text(result, capability):
    if result.source_names:
        return ", ".join(result.source_names)
    if result.destination:
        return _friendly(result.destination)
    return ", ".join(_friendly(value) for value in capability.target_roles)


def _control_labels(template):
    labels = []
    for item in template.get("params") or ():
        label = str(item.get("label") or item.get("token") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return tuple(labels) or ("Template defaults",)


def _alternative_cards(context, capability, result):
    """Rank nearby recipes from capability facts, never scene-wide data."""
    candidates = []
    requested_roles = frozenset(capability.target_roles)
    for option in capabilities.all_capabilities():
        if option.template_id == capability.template_id:
            continue
        role_overlap = len(requested_roles.intersection(option.target_roles))
        same_route = option.route == capability.route
        if not role_overlap and not same_route:
            continue
        option_result = capabilities.preflight(context, option.template_id)
        reason = option_result.message
        candidates.append((
            0 if same_route else 1,
            -role_overlap,
            _COST_RANK.get(option.cost_tier, 99),
            option.label.casefold(),
            AlternativeCard(
                template_id=option.template_id,
                label=option.label,
                reason=reason,
                available=option_result.ok,
                cost=_friendly(option.cost_tier),
            ),
        ))
    candidates.sort(key=lambda value: value[:-1])
    return tuple(value[-1] for value in candidates[:3])


def preflight_card(context, template, props=None):
    """Return a complete immutable card without mutating Blender state."""
    template_id = str(template.get("id") or "")
    capability = capabilities.get(template_id)
    if capability is None:
        raise ValueError("Guided Apply requires a generated delivery contract.")
    fingerprint = generated_routes.state_fingerprint(context, template_id)
    key = _cache_key(context, template_id, fingerprint)
    cached = _PREFLIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    conflict = fingerprint[0]
    result = capabilities.preflight(context, template_id, conflict=conflict)
    readiness = fingerprint[2] if len(fingerprint) > 2 else ()
    status = result.message
    ok = result.ok
    disabled_reason = result.disabled_reason
    if readiness:
        _kind, participant_count, missing, ready = readiness
        if ready:
            status = "%s · %d participants." % (status.rstrip("."), participant_count)
        else:
            status = "%s needs the %s field." % (capability.label, missing[0])
            disabled_reason = status
            ok = False
    conflict_outcome = {
        "NONE": "No existing conflict",
        "SAME_EFFECT": "Updates the existing Espresso effect",
        "LAYERABLE_OCCUPIED": "Adds a layer beside the existing Espresso effect",
    }.get(conflict, result.disabled_reason or "Conflict requires an explicit decision")
    card = GuidedPreflightCard(
        template_id=template_id,
        target=_target_text(result, capability),
        result=", ".join(
            _OUTPUT_LABELS.get(str(value), _friendly(value))
            for value in capability.output_kinds
        ),
        status=status,
        controls=_control_labels(template),
        created_resources=tuple(_friendly(value) for value in capability.created_resource_kinds),
        conflict_policy=_friendly(capability.conflict_policy),
        conflict_outcome=conflict_outcome,
        cost=_friendly(capability.cost_tier),
        delivery=_friendly(capability.delivery),
        portability=_friendly(capability.portability),
        # No gizmo compatibility to report: this product ships no recipe
        # with a generated-system effector to place.
        gizmo_compatibility="",
        disabled_reason=disabled_reason,
        # Alternatives are a browser concern.  Keeping them out of the cached
        # status card avoids running neighbouring-route preflights merely to
        # draw the Apply header or its read-only Status button.
        alternatives=(),
        ok=ok,
    )
    if len(_PREFLIGHT_CACHE) >= 64:
        _PREFLIGHT_CACHE.clear()
    _PREFLIGHT_CACHE[key] = card
    return card


def _label_row(layout, heading, value, icon="DOT", wrap_width=48):
    lines = utils.wrap_text(str(value or "—"), width=wrap_width)
    for index, line in enumerate(lines):
        row = layout.split(factor=0.34, align=True)
        if index == 0:
            row.label(text=heading, icon=icon)
        else:
            row.label(text="")
        row.label(text=line)


def draw_status_details(layout, props, template, context):
    card = preflight_card(context, template, props)
    title = layout.row(align=True)
    title.label(text="MOTION STATUS", icon="CHECKMARK" if card.ok else "ERROR")
    title.label(text="Ready" if card.ok else "Blocked")
    _label_row(layout, "Target", card.target, "OBJECT_DATA")
    _label_row(layout, "Result", card.result, "INFO")
    _label_row(layout, "Status", card.status, "CHECKMARK" if card.ok else "ERROR")
    _label_row(layout, "Controls", ", ".join(card.controls), "PREFERENCES")
    _label_row(layout, "Resources", ", ".join(card.created_resources), "NODETREE")
    _label_row(
        layout, "Conflict", "%s · %s" % (card.conflict_policy, card.conflict_outcome),
        "ERROR" if not card.ok else "CHECKMARK",
    )
    _label_row(layout, "Cost", card.cost, "TIME")
    _label_row(layout, "Delivery", card.delivery, "EXPORT")
    _label_row(layout, "Portability", card.portability, "FILE_BLEND")
    _label_row(layout, "Spatial authoring", card.gizmo_compatibility, "ORIENTATION_GIMBAL")
    if card.disabled_reason:
        _label_row(layout, "Why unavailable", card.disabled_reason, "ERROR")
    return card


def _merged_alternative_cards(context, template):
    """Return authored variants first, followed by unique route suggestions."""
    merged = []
    seen = {str(template.get("id") or "")}
    for template_id in template.get("variants") or ():
        option = templates.TEMPLATE_BY_ID.get(template_id)
        if option is None or template_id in seen:
            continue
        seen.add(template_id)
        merged.append(("AUTHORED", AlternativeCard(
            template_id=template_id,
            label=option["name"],
            reason="Authored variant",
            available=True,
            cost="",
        )))
    capability = capabilities.get(template.get("id"))
    if capability is not None:
        result = capabilities.preflight(context, capability.template_id)
        for option in _alternative_cards(context, capability, result):
            if option.template_id in seen:
                continue
            seen.add(option.template_id)
            merged.append(("CONTEXTUAL", option))
    return tuple(merged)


def draw_alternatives(layout, props, template, context):
    """Draw one saved, deduplicated alternatives shelf below the picker."""
    authored = tuple(template.get("variants") or ())
    capability = capabilities.get(template.get("id"))
    if not authored and capability is None:
        return
    box = layout.box()
    header = box.row(align=True)
    header.prop(
        props, "template_variants_open", text="", emboss=False,
        icon="TRIA_DOWN" if props.template_variants_open else "TRIA_RIGHT",
    )
    header.label(text="ALTERNATIVES", icon="SORTALPHA")
    if not props.template_variants_open:
        return
    alternatives = _merged_alternative_cards(context, template)
    if not alternatives:
        box.label(text="No related alternatives for this template.", icon="INFO")
        return
    for kind, option in alternatives:
        row = box.row(align=True)
        row.enabled = option.available
        if kind == "AUTHORED":
            op = row.operator("espresso.switch_variant", text=option.label, icon="GRAPH")
            op.variant_id = option.template_id
        else:
            op = row.operator(
                "espresso.select_template", text=option.label,
                icon="CHECKMARK" if option.available else "ERROR",
            )
            op.template_id = option.template_id
            option_template = templates.TEMPLATE_BY_ID.get(option.template_id, {})
            op.category_name = option_template.get("category", "")
            row.label(text=option.reason)


def draw_browser_view(layout, props, template, context):
    """Stable discovery view; compact modes retain explicit shortcuts."""
    from . import panels

    panels.draw_template_selector(layout, props, context)
    compact = panels._compact(context)
    if compact:
        shortcuts = layout.row(align=True)
        # Same gate draw_favorites uses. An edition with a small curated
        # catalogue has nothing to pin, and this star was the one place the
        # shortcut row still offered it -- a control for a section that is
        # not in the product.
        from ...product import identity as _identity

        if getattr(_identity, "HAS_FAVORITES", True):
            star = shortcuts.operator(
                "espresso.toggle_favorite", text="", icon="SOLO_OFF")
            star.template_id = props.template
        recent_ids = panels.espresso_props.get_recents(props)
        if recent_ids:
            recent_id = recent_ids[0]
            recent = templates.TEMPLATE_BY_ID.get(recent_id)
            if recent is not None:
                op = shortcuts.operator(
                    "espresso.select_template", text="Recent: %s" % recent["name"], icon="TIME",
                )
                op.template_id = recent_id
                op.category_name = recent["category"]
    else:
        panels.draw_favorites(layout, props, context)
    panels.draw_template_toolbar(layout, props, template, context)
    draw_alternatives(layout, props, template, context)


def draw_setup_live_view(layout, props, template, context):
    from . import panels

    panels.draw_input_source(layout, props, template)
    panels.draw_parameters(layout, props, template, context)
    if panels._compact_level(context) < 2:
        panels.draw_advanced_controls(layout, props, template, context)


def draw_target_preflight_view(layout, props, template, context):
    from . import panels

    panels.draw_expression_block(layout, props, template, context)
    # Compact L2 hides the long expression/action body, but it must never hide
    # the one control that commits an ordinary driver recipe.
    if panels._compact_level(context) >= 2 and not capabilities.supports(template.get("id")):
        panels._draw_commit_slot(layout, props, context)


def draw_preview_view(layout, props, template, context):
    from . import panels

    panels.draw_graph_and_target(layout, props, template, context)


def draw_applied_effects_view(layout, props, template, context):
    from . import panels

    panels.draw_driver_target(layout, props, template, context)
