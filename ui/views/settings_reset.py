"""Reset-to-default buttons for settings that are not catalogue parameters.

A catalogue parameter resets per row via `espresso.reset_param_default` and
per template via `espresso.reset_template_defaults`. Settings drawn from a
real RNA property reset through the two operators here, using the same
LOOP_BACK idiom, so a reset looks and behaves the same wherever it appears in
the panel.

**A surface registers a resolver rather than being hard-coded here**, because
the default is not always the RNA default. Where a preset owns a group of
fields, resetting one has to return the value that preset carries, not the
property's own default, or the reset would silently undo the mode the artist
picked. Only the surface itself knows that.

Three write modes exist because they are genuinely different operations:

``RNA``     ``setattr``, so the property's own ``update`` callback fires. The
            only safe mode for an enum, whose ID-property slot holds an int.
``QUIET``   ``owner[name] = value``, skipping the update callback. Used when a
            group reset would otherwise fire an expensive callback once per
            field; the resolver's ``finish`` runs it once at the end instead.
``CUSTOM``  a real ID custom property (``obj["espresso_..."]``), which has no
            update callback at all and needs its owner tagged.
"""

from __future__ import annotations

import collections
import math

import bpy


#: ``mode`` is one of RNA / QUIET / CUSTOM, described in the module docstring.
Setting = collections.namedtuple("Setting", "owner name default mode label")

#: surface key -> resolve(context, group, field) -> (list[Setting], finish)
_SURFACES = {}


def register_surface(key, resolve):
    """Declare a settings surface the reset operators can act on.

    Called at module scope by the view that draws the surface, so the
    dependency runs one way only: a view imports this module, never the
    reverse.
    """
    _SURFACES[key] = resolve


def resolve(context, surface, group="", field=""):
    """Return ``(settings, finish)`` for a surface, narrowed to one field.

    ``field`` is handed to the resolver rather than filtered afterwards,
    because a default can depend on WHICH fields are being written. Resetting
    one framing field is not the same operation as resetting the box that
    contains it, and only the resolver can tell the two apart. The filter is
    still applied here so a resolver that ignores the argument stays correct.

    An unknown surface, or a surface whose owner is not currently available
    (no camera selected, no rig built), resolves to an empty list rather than
    raising -- the button simply reports that there is nothing to reset.
    """
    resolver = _SURFACES.get(surface)
    if resolver is None:
        return [], None
    try:
        settings, finish = resolver(context, group, field)
    except (AttributeError, KeyError, TypeError):
        return [], None
    settings = list(settings or [])
    if field:
        settings = [item for item in settings if item.name == field]
    return settings, finish


def _write(setting):
    """Apply one setting, returning True when the stored value actually moved.

    Reporting only real changes is what keeps "Already at default" honest, and
    it keeps a group reset from claiming credit for fields it left alone.
    """
    owner, name = setting.owner, setting.name
    if setting.mode == "CUSTOM":
        if owner.get(name) == setting.default:
            return False
        owner[name] = setting.default
        return True
    current = getattr(owner, name, None)
    if current == setting.default:
        return False
    if setting.mode == "QUIET":
        owner[name] = setting.default
    else:
        setattr(owner, name, setting.default)
    return True


def _tag(settings):
    """Tag every distinct ID that a CUSTOM write touched.

    A custom property has no update callback, so a driver reading one does not
    know it moved until its owner is tagged.
    """
    seen = []
    for setting in settings:
        if setting.mode != "CUSTOM":
            continue
        owner = setting.owner
        tag = getattr(owner, "update_tag", None)
        if callable(tag) and not any(owner is other for other in seen):
            seen.append(owner)
            tag()


def _apply(context, settings, finish):
    """Write every setting, then hand the surface what actually changed.

    ``finish`` is told which settings moved so it can skip work it does not
    owe: resetting an enum whose own update callback already re-solved must not
    trigger a second solve just because the group reset ran.
    """
    changed = [setting for setting in settings if _write(setting)]
    if changed:
        _tag(changed)
        if finish is not None:
            finish(context, changed)
    return changed


def reset_all(context, surface):
    """Reset every setting a surface owns, across all of its boxes.

    A recipe that keeps its settings on the OBJECT rather than in the
    template's parameter slots is invisible to the template reset -- the
    PARAMETERS header button restored 3 of an authoring rig's 43 settings and
    reported success, because the other 40 do not live where it was looking.
    This is the one call that reaches them, so the header button can finish
    the job it says it does.

    Returns the settings that actually moved.
    """
    settings, finish = resolve(context, surface)
    if not settings:
        return []
    return _apply(context, settings, finish)


class ESPRESSO_OT_reset_setting(bpy.types.Operator):
    bl_idname = "espresso.reset_setting"
    bl_label = "Reset Setting"
    bl_description = "Reset this setting to its default value"
    bl_options = {"REGISTER", "UNDO"}

    surface: bpy.props.StringProperty(options={"SKIP_SAVE"})
    group: bpy.props.StringProperty(options={"SKIP_SAVE"})
    field: bpy.props.StringProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        settings, _finish = resolve(
            context, properties.surface, properties.group, properties.field,
        )
        if not settings:
            return cls.bl_description
        setting = settings[0]
        return "Reset %s to its default of %s" % (setting.label, _format(setting))

    def execute(self, context):
        settings, finish = resolve(context, self.surface, self.group, self.field)
        if not settings:
            self.report({"WARNING"}, "Nothing to reset here.")
            return {"CANCELLED"}
        setting = settings[0]
        if not _apply(context, settings, finish):
            self.report(
                {"INFO"},
                "%s is already at its default (%s)."
                % (setting.label, _format(setting)),
            )
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            "Reset %s to %s." % (setting.label, _format(setting)),
        )
        return {"FINISHED"}


class ESPRESSO_OT_reset_settings(bpy.types.Operator):
    bl_idname = "espresso.reset_settings"
    bl_label = "Reset Settings"
    bl_description = "Reset every setting in this group to its default value"
    bl_options = {"REGISTER", "UNDO"}

    surface: bpy.props.StringProperty(options={"SKIP_SAVE"})
    group: bpy.props.StringProperty(options={"SKIP_SAVE"})
    title: bpy.props.StringProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        settings, _finish = resolve(context, properties.surface, properties.group)
        if not settings:
            return cls.bl_description
        return "Reset all %d %s settings to their defaults:\n%s" % (
            len(settings),
            properties.title or "group",
            ", ".join(setting.label for setting in settings),
        )

    def execute(self, context):
        settings, finish = resolve(context, self.surface, self.group)
        if not settings:
            self.report({"WARNING"}, "Nothing to reset here.")
            return {"CANCELLED"}
        changed = _apply(context, settings, finish)
        label = self.title or "these settings"
        if not changed:
            self.report({"INFO"}, "%s is already at its defaults." % label)
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            "Reset %d of %d %s settings." % (len(changed), len(settings), label),
        )
        return {"FINISHED"}


def _format(setting):
    """The default as the artist sees it in the field, not as it is stored.

    An ANGLE property is stored in radians and drawn in degrees, so a tooltip
    reading "default of 0.611" beside a field showing 35 degrees is worse than
    no tooltip at all.
    """
    value = setting.default
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        if _subtype(setting) == "ANGLE":
            return "%s°" % _trim(math.degrees(value))
        return _trim(value)
    return str(value)


def _trim(value):
    return ("%.3f" % value).rstrip("0").rstrip(".")


def _subtype(setting):
    if setting.mode == "CUSTOM":
        return ""
    try:
        return setting.owner.bl_rna.properties[setting.name].subtype
    except (AttributeError, KeyError):
        return ""


def draw_row(layout, owner, name, surface, group="", *, path=None, **kwargs):
    """Draw one setting with its reset button, aligned as one row.

    ``path`` overrides what is drawn when the field is reached by a data path
    rather than an attribute -- a custom property is drawn as ``["key"]`` but
    is still reset by its bare key.
    """
    row = layout.row(align=True)
    row.prop(owner, path or name, **kwargs)
    reset = row.operator("espresso.reset_setting", text="", icon="LOOP_BACK")
    reset.surface = surface
    reset.group = group
    reset.field = name
    return row


def draw_group_header(box, title, icon, surface, group, *, label=""):
    """A section header with the group's reset parked on the right.

    Same placement as the PARAMETERS header's reset, so the two read as the
    same control at two scopes.
    """
    header = box.row(align=True)
    header.label(text=title, icon=icon)
    right = header.row(align=True)
    right.alignment = "RIGHT"
    reset = right.operator("espresso.reset_settings", text="", icon="LOOP_BACK")
    reset.surface = surface
    reset.group = group
    reset.title = label or title
    return header


CLASSES = (
    ESPRESSO_OT_reset_setting,
    ESPRESSO_OT_reset_settings,
)
