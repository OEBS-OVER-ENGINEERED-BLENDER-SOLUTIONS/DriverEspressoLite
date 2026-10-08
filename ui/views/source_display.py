"""One way to show an Espresso input source, shared by both panels.

Two places show a source and they read different data:

* the Controller tab shows the pending input, the property last right-clicked and
  waiting to drive a Controller;
* a Controller shows a controller's stored source, the property baked into that
  controller when it was created.

The pending-input card is the same in both panels: a header plus one body line. When a
source is present the body is the path, not a separate "Input detected" status. Empty
and broken states keep the status line because there is no useful path to scan. The
compact row is only for a controller that already names its own source.
"""

from __future__ import annotations

import bpy

from ...apply import source_binding
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
        try:
            box.label(text=f"Input {source_binding.sample_source_value(entry):.4g}", icon="IPO_EASE_IN_OUT")
        except ValueError:
            pass
        return True

    status_row = box.row()
    status_row.alert = not empty
    if empty:
        status_row.label(text="No Espresso Input detected", icon="RADIOBUT_OFF")
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
