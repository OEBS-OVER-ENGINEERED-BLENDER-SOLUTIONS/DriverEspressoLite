"""One way to show an Espresso input source, shared by both panels.

Two places show a source and they read DIFFERENT data:

* the main panel shows the *pending* input - the property last right-clicked,
  waiting to feed a template's driver variables;
* the Input Controller shows a *controller's stored source* - the property baked
  into that controller when it was created.

The pending-input card is the same in both panels: header plus one body line.
When a source is present the body is the path, not a separate "Input detected"
status. Empty and broken states keep the status line because there is no useful
path to scan. The compact row is only for a controller that already names its
own source.
"""

from __future__ import annotations

import bpy

from ...apply import input_presentation, source_binding
from ...engine import utils


SOURCE_INPUT = "INPUT"
SOURCE_CONTROLLER = "CONTROLLER"


def _resolve(context, which, controller_uid=""):
    """Return ``(entry, status, title, subject)`` for the requested source."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None:
        return None, None, "", ""
    if which == SOURCE_CONTROLLER:
        from ..state import live_controls

        controller = (
            live_controls.controller_by_uid(props, controller_uid)
            if controller_uid
            else live_controls.selected_controller(props)
        )
        if controller is None:
            return None, None, "SOURCE", ""
        entry = controller.get("source") or {}
        return entry, source_binding.source_status(entry), "SOURCE", controller["name"]
    entry = source_binding.latest_source(props)
    return entry, source_binding.source_status(entry), "INPUT SOURCE", ""


def source_label(entry):
    return (entry or {}).get("display_label", "") or "Remembered property"


def _template_uses_timeline(template):
    expressions = [template.get("expression", "")]
    expressions.extend(
        channel.get("expression", "")
        for channel in template.get("channels", ()) or ()
    )
    return any("frame" in str(expression) for expression in expressions)


def draw_input_flow(layout, context, template, entry=None, status=None):
    """Show the exact Source -> Recipe -> Target contract before apply."""
    if not template:
        return None
    entry = entry if entry is not None else source_binding.latest_source(
        context.scene.espresso_props,
    )
    status = status if status is not None else source_binding.source_status(entry)
    required = source_binding.needs_input_source(template)
    targets = tuple(
        channel.get("label") or channel.get("data_path") or "Target"
        for channel in template.get("channels", ()) or ()
    ) or ((template.get("data_path") or "Choose target"),)
    value = None
    if status.get("code") == source_binding.SOURCE_ACTIVE:
        try:
            value = source_binding.sample_source_value(entry)
        except ValueError:
            value = None
    flow = input_presentation.build_input_flow(
        recipe_label=template.get("name", "Recipe"),
        target_labels=targets,
        source_required=required,
        source_active=status.get("code") == source_binding.SOURCE_ACTIVE,
        source_label=source_label(entry) if entry else "",
        uses_timeline=_template_uses_timeline(template),
        source_value=value,
    )
    flow_box = layout.box()
    flow_box.label(text=flow.chain, icon="DRIVER")
    detail = flow_box.row(align=True)
    detail.label(
        text="Transfer Curve" if flow.preview_mode == input_presentation.PREVIEW_TRANSFER
        else "Over Time",
        icon="FCURVE",
    )
    if value is not None:
        detail.label(text=f"Input {value:.4g}", icon="IPO_EASE_IN_OUT")
    if not flow.can_apply:
        blocked = flow_box.row()
        blocked.alert = True
        blocked.label(text=flow.reason, icon="ERROR")
    return flow


def draw_pending_source_card(layout, context, title="INPUT SOURCE"):
    """Header plus path when set. Shared by the main panel and Input Controller."""
    entry, status, _title, _subject = _resolve(context, SOURCE_INPUT)
    code = (status or {}).get("code", source_binding.SOURCE_EMPTY)
    active = code == source_binding.SOURCE_ACTIVE
    empty = code == source_binding.SOURCE_EMPTY

    box = layout.box()
    header = box.row(align=True)
    header.operator(
        "espresso.input_source_help",
        text=title,
        icon="LINKED",
        emboss=False,
    )
    if entry:
        header.operator("espresso.clear_input_source", text="", icon="X")

    if active:
        box.label(text=source_label(entry), icon="LINKED")
        from ..state import props as espresso_props
        template = espresso_props.get_current_template(context.scene.espresso_props)
        draw_input_flow(box, context, template, entry, status)
        return True

    status_row = box.row()
    status_row.alert = not empty
    if empty:
        status_row.label(text="No Espresso Input detected", icon="RADIOBUT_OFF")
        from ..state import props as espresso_props
        template = espresso_props.get_current_template(context.scene.espresso_props)
        if source_binding.needs_input_source(template):
            draw_input_flow(box, context, template, entry, status)
        return False

    status_row.label(text="Input detected, but unavailable", icon="ERROR")
    box.label(text=source_label(entry), icon="BLANK1")
    reason = (status or {}).get("reason") or "The remembered input source is missing."
    detail_row = box.row()
    detail_row.alert = True
    detail_row.label(text=reason, icon="INFO")
    return False


def draw_source_row(layout, context, which, *, controller=None):
    """One line: title, path or problem, Show. Returns True when healthy."""
    controller_uid = (controller or {}).get("uid", "")
    entry, status, title, _subject = _resolve(context, which, controller_uid)
    active = bool(entry) and status is not None and status["code"] == source_binding.SOURCE_ACTIVE

    row = layout.row(align=True)
    row.alert = not active
    row.label(text=title, icon="LINKED")
    if active:
        row.label(text=source_label(entry), icon="LINKED")
    elif entry:
        row.label(text="Missing or renamed", icon="ERROR")
    else:
        row.label(text="Not set", icon="ERROR")
    # A labelled button, not a bare icon: an icon alone reads as decoration and
    # gets skipped, so the detail behind it never gets found.
    op = row.operator("espresso.show_source", text="Show", icon="INFO")
    op.which = which
    op.controller_uid = controller_uid
    if which == SOURCE_CONTROLLER:
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        pending_ok = False
        if props is not None:
            pending = source_binding.latest_source(props)
            pending_ok = source_binding.source_status(pending)["code"] == source_binding.SOURCE_ACTIVE
        update_row = row.row(align=True)
        update_row.enabled = pending_ok
        update = update_row.operator(
            "espresso.relink_controller_source",
            text="Update",
            icon="FILE_REFRESH",
        )
        update.controller_uid = controller_uid
    return active, row


class ESPRESSO_OT_show_source(bpy.types.Operator):
    bl_idname = "espresso.show_source"
    bl_label = "Input Source"
    bl_description = "Show the property behind this input"

    which: bpy.props.StringProperty(default=SOURCE_INPUT)
    controller_uid: bpy.props.StringProperty(default="", options={"HIDDEN"})

    @classmethod
    def description(cls, context, properties):
        # Put the path in the tooltip so the common case needs no click at all.
        entry, _status, _title, _subject = _resolve(
            context,
            getattr(properties, "which", SOURCE_INPUT),
            getattr(properties, "controller_uid", ""),
        )
        if not entry:
            return "No input source selected"
        return f"Source: {source_label(entry)}"

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=380)

    def draw(self, context):
        entry, status, _title, subject = _resolve(
            context, self.which, self.controller_uid,
        )
        layout = self.layout

        if subject:
            layout.label(text=subject, icon="DRIVER")
            layout.separator()

        if not entry:
            layout.label(text="No input source selected", icon="ERROR")
            for line in utils.wrap_text(
                "Right-click a numeric property and choose Use as Espresso Input.",
                width=52,
            ):
                layout.label(text=line)
            return

        label = source_label(entry)
        if status["code"] == source_binding.SOURCE_ACTIVE:
            for index, line in enumerate(utils.wrap_text(label, width=52)):
                layout.label(text=line, icon="LINKED" if index == 0 else "BLANK1")
            if self.which == SOURCE_INPUT:
                props = context.scene.espresso_props
                from ..state import props as espresso_props

                template = espresso_props.get_current_template(props)
                names = ", ".join(
                    item["name"] for item in template.get("requires_driver_variables", [])
                )
                if names:
                    layout.separator()
                    for line in utils.wrap_text(f"Feeds driver variable(s): {names}", width=52):
                        layout.label(text=line)
            return

        col = layout.column()
        col.alert = True
        for index, line in enumerate(utils.wrap_text(status["reason"] or "Source unavailable.", width=52)):
            col.label(text=line, icon="ERROR" if index == 0 else "BLANK1")
        layout.separator()
        for index, line in enumerate(utils.wrap_text(label, width=52)):
            layout.label(text=line, icon="BLANK1")
        layout.separator()
        for index, line in enumerate(utils.wrap_text(
            "Right-click the replacement property and choose Relink Espresso "
            "Input & Applied Drivers.", width=52,
        )):
            layout.label(text=line, icon="FILE_REFRESH" if index == 0 else "BLANK1")

    def execute(self, context):
        return {"FINISHED"}


CLASSES = (ESPRESSO_OT_show_source,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
