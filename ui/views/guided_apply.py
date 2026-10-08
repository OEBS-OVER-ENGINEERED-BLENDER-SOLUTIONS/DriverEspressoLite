"""Presentation-only views of the main panel.

This module owns no Blender operators or persistent state. It composes the
established panel functions into the browser, setup, target, preview and
applied-effects views.
"""

from __future__ import annotations

from ...catalogue import templates


def _authored_variants(template):
    """The recipe's own alternatives, without the recipe itself or repeats."""
    seen = {str(template.get("id") or "")}
    options = []
    for template_id in template.get("variants") or ():
        option = templates.TEMPLATE_BY_ID.get(template_id)
        if option is None or template_id in seen:
            continue
        seen.add(template_id)
        options.append((template_id, option["name"]))
    return tuple(options)


def draw_alternatives(layout, props, template, context):
    """Draw one saved, deduplicated alternatives shelf below the picker."""
    if not template.get("variants"):
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
    alternatives = _authored_variants(template)
    if not alternatives:
        box.label(text="No related alternatives for this template.", icon="INFO")
        return
    for template_id, label in alternatives:
        row = box.row(align=True)
        op = row.operator("espresso.switch_variant", text=label, icon="GRAPH")
        op.variant_id = template_id
        row.label(text="Authored variant")


def draw_browser_view(layout, props, template, context):
    """Stable discovery view; compact modes retain explicit shortcuts."""
    from . import panels

    panels.draw_template_selector(layout, props, context)
    compact = panels._compact(context)
    if compact:
        shortcuts = layout.row(align=True)
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
    panels.draw_template_toolbar(layout, props, template, context)
    draw_alternatives(layout, props, template, context)


def draw_setup_live_view(layout, props, template, context):
    from . import panels

    panels.draw_parameters(layout, props, template, context)
    if panels._compact_level(context) < 2:
        panels.draw_advanced_controls(layout, props, template, context)


def draw_target_preflight_view(layout, props, template, context):
    from . import panels

    panels.draw_expression_block(layout, props, template, context)
    # Compact L2 hides the long expression/action body, but it must never hide
    # the one control that commits an ordinary driver recipe.
    if panels._compact_level(context) >= 2:
        panels._draw_commit_slot(layout, props, context)


def draw_preview_view(layout, props, template, context):
    from . import panels

    panels.draw_graph_and_target(layout, props, template, context)


def draw_applied_effects_view(layout, props, template, context):
    from . import panels

    panels.draw_driver_target(layout, props, template, context)
