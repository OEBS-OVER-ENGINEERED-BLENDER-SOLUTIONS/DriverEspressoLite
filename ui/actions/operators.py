"""Operators for Driver Espresso."""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

import bpy
from ...engine.baking.transaction import atomic_bake_operator

from ...apply import apply_behavior
from ...apply import applied_motion
from ...catalogue import browse_groups
from ...catalogue import taxonomy
from ...catalogue import palettes
from ...apply import light_layout
from ...apply import apply_target
from ...apply import motion_channels


class _CameraApplicationAbsent:
    """Driver Espresso Lite ships no camera recipe, so this path is unreachable.

    A stub rather than deleted branches: every call site is already guarded by
    `requires_camera_object`, which is now always False, so the camera branches
    are dead while the surrounding apply logic stays exactly as it was.
    """

    @staticmethod
    def requires_camera_object(template):
        return False

    @staticmethod
    def apply_camera_template(*args, **kwargs):
        raise RuntimeError("Driver Espresso Lite ships no camera recipes")


camera_application = _CameraApplicationAbsent()
from ...apply.setups import internal_helpers
from ...apply import layout_preparation
from ...apply import pose_capture
from ...apply import response_rig
from ...apply import spatial_fields
from ...engine import stagger_apply
from ...engine.motion_stack import capabilities as capabilities_module
from ...engine.motion_stack import workflows as workflows_module
from ..state import props as espresso_props
from ...apply import source_binding
from ...apply import target_memory
from ...apply import application_plan
from ...catalogue import templates
from ...engine import utils
from ...generated import helpers as generated_helpers
from ...generated.resources import clear_snapshot as generated_clear_snapshot
from ...apply.setups import audio_reactivity
from ...apply.setups import parameter_bindings


def missing_required_variables(driver, template):
    """Single source of truth for 'which required driver variables are absent'.

    Tolerates ``template=None`` so every caller can share this one implementation
    instead of keeping a private near-copy that can silently drift.
    """
    existing = {item.name for item in driver.variables}
    missing = [
        var["name"]
        for var in (template or {}).get("requires_driver_variables", [])
        if var["name"] not in existing
    ]
    return missing


def apply_expression_to_driver(driver, expression, template, source_entry=None):
    audio_message = audio_reactivity.application_block_message(template, bpy.context.scene)
    if audio_message:
        return False, audio_message
    validation_template = audio_reactivity.validation_template(template, bpy.context.scene)
    valid, message = utils.validate_driver_expression(expression, validation_template)
    if not valid:
        return False, message
    audio_bound = audio_reactivity.bind_driver(driver, template, bpy.context.scene)
    if audio_bound:
        pass
    elif source_binding.template_needs_source(template) and source_entry:
        ok, message = source_binding.bind_required_variables(driver, template, source_entry)
        if not ok:
            return False, message
        audio_reactivity.clear_variables(driver, template)
    else:
        audio_reactivity.clear_variables(driver, template)
    missing = missing_required_variables(driver, template)
    if audio_bound:
        pass
    elif missing:
        ok, message = source_binding.bind_required_variables(driver, template, source_entry)
        if not ok:
            return False, message or "Add required driver variable(s) first: " + ", ".join(missing)
    return utils.assign_driver_expression(driver, expression, validation_template)


def _graph_target(owner, data_path, index):
    return type(
        "GraphTarget",
        (),
        {
            "owner": owner,
            "data_path": data_path,
            "index": index,
        },
    )()


class ESPRESSO_OT_set_active_driver_target(bpy.types.Operator):
    bl_idname = "espresso.set_active_driver_target"
    bl_label = "Set Active Driver Target"
    bl_description = "Make this driver the active target for Apply/Remove and the Active Target preview"
    bl_options = {"INTERNAL"}

    data_path: bpy.props.StringProperty()
    array_index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        utils.set_active_driver_target(context, context.active_object, self.data_path, self.array_index)
        # INTERNAL operators don't get the automatic post-operator redraw that
        # REGISTER/UNDO operators do — tag explicitly so the row's depress
        # state updates immediately instead of waiting for an unrelated redraw.
        screen = getattr(context, "screen", None)
        if screen is not None:
            for area in screen.areas:
                area.tag_redraw()
        return {"FINISHED"}


class ESPRESSO_OT_copy(bpy.types.Operator):
    bl_idname = "espresso.copy_expression"
    bl_label = "Copy Expression"
    bl_description = "Copy the generated driver expression"

    def execute(self, context):
        props = context.scene.espresso_props
        context.window_manager.clipboard = props.preview
        self.report({"INFO"}, "Driver expression copied.")
        return {"FINISHED"}


class ESPRESSO_OT_select_motion_channel(bpy.types.Operator):
    bl_idname = "espresso.select_motion_channel"
    bl_label = "Preview Motion Channel"
    bl_description = "Show this channel in the expression and graph preview"
    bl_options = {"INTERNAL"}

    channel_id: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        valid_ids = {item["id"] for item in templates.template_channels(template)}
        if self.channel_id not in valid_ids:
            self.report({"WARNING"}, "That motion channel is no longer available.")
            return {"CANCELLED"}
        props.selected_motion_channel = self.channel_id
        espresso_props.refresh_preview(props, context)
        return {"FINISHED"}


class ESPRESSO_OT_toggle_motion_channel(bpy.types.Operator):
    bl_idname = "espresso.toggle_motion_channel"
    bl_label = "Toggle Motion Channel"
    bl_description = (
        "Include or exclude this channel the next time this template is "
        "applied. A disabled channel gets no driver at all - whatever is "
        "already on that property (hand-keyed or otherwise) is left alone"
    )
    bl_options = {"INTERNAL", "UNDO"}

    channel_id: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        enabled = espresso_props.motion_channel_enabled(props, template["id"], self.channel_id)
        espresso_props.set_motion_channel_enabled(
            props, template["id"], self.channel_id, not enabled,
        )
        return {"FINISHED"}


def motion_channel_expression(props, channel_id):
    item = next(
        (entry for entry in espresso_props.built_channel_previews(props) if entry.get("id") == channel_id),
        None,
    )
    return item["expression"] if item is not None else ""


class ESPRESSO_OT_copy_motion_channel(bpy.types.Operator):
    bl_idname = "espresso.copy_motion_channel"
    bl_label = "Copy Motion Channel"
    bl_description = "Copy this channel's complete generated expression"

    channel_id: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.espresso_props
        expression = motion_channel_expression(props, self.channel_id)
        if not expression:
            self.report({"WARNING"}, "That motion channel is no longer available.")
            return {"CANCELLED"}
        context.window_manager.clipboard = expression
        self.report({"INFO"}, "Motion channel expression copied.")
        return {"FINISHED"}


def _active_motion_pose_bone(context):
    """The pose bone a motion template should target, or None for the Object.

    Only returns a bone when the user is actually in Pose Mode on the active
    armature with a bone active — so Object Mode keeps its existing behavior.
    """
    if getattr(context, "mode", "") != "POSE":
        return None
    return getattr(context, "active_pose_bone", None)


class ESPRESSO_OT_set_bone_euler(bpy.types.Operator):
    bl_idname = "espresso.set_bone_euler"
    bl_label = "Set Bone to XYZ Euler"
    bl_description = (
        "Switch the active pose bone to XYZ Euler rotation. Convertible "
        "rotation templates already apply as four quaternion drivers on "
        "Quaternion bones automatically — use this if you prefer three Euler "
        "drivers instead. Existing rotation is preserved"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_motion_pose_bone(context) is not None

    def execute(self, context):
        bone = _active_motion_pose_bone(context)
        if bone is None:
            self.report({"WARNING"}, "No active pose bone.")
            return {"CANCELLED"}
        # Assigning rotation_mode re-derives rotation_euler from the bone's
        # current orientation, so the visible pose is preserved.
        bone.rotation_mode = "XYZ"
        self.report({"INFO"}, f'Bone "{bone.name}" set to XYZ Euler.')
        return {"FINISHED"}


# Taken from the taxonomy rather than typed out. The hand-written version had
# "Lighting & FX", which is not a category at all - so it matched nothing, and
# the list silently excluded Light & Flicker, the largest lighting category
# there is. Twenty-four templates were never offered here.
LIGHT_CATEGORIES = (taxonomy.LIGHT, taxonomy.RGBNEON, taxonomy.ENERGY)

# Templates whose output is a 0-1 FACTOR rather than a brightness. Driving a
# lamp's Power with one would ask for 0-1 watts, which reads as "off", so they
# belong on a Colour Ramp instead and this button declines them by name.
FACTOR_ONLY_TEMPLATES = ()


def selected_lights(context):
    return [
        obj for obj in (getattr(context, "selected_objects", None) or ())
        if getattr(obj, "type", "") == "LIGHT" and obj.data is not None
    ]


def applicable_objects(context):
    """What the panel's apply button would write to.

    Two modes, and which one applies is decided by the SELECTION, not by
    whether a target happens to be set:

    * every selected object is a light -> the lights, driving Power
    * anything else -> every selected object, but only once a target has been
      nominated. Without one there is nothing to write to, so the button is
      disabled and the right-click route is the way in.
    """
    selected = arrangeable_objects(context)
    if apply_target.is_light_selection(selected):
        return selected
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is not None and apply_target.read(props):
        return selected
    return []


def template_suits_lights(template, has_target=False):
    """Whether "apply to my selection" makes sense for this template.

    A multi-channel motion template drives a whole transform and has nothing to
    write to a single nominated property, so it never qualifies. Beyond that the
    restriction to lighting categories only exists because Power was the assumed
    target - once the artist has NAMED a property, any single-expression
    template is a legitimate thing to put on it.
    """
    if template is None or templates.has_motion_plan(template):
        return False
    if has_target:
        return True
    if template.get("id") in FACTOR_ONLY_TEMPLATES:
        return False
    return template.get("category") in LIGHT_CATEGORIES


# Fields the wizard's mouse drag can adjust: attribute, label, hotkey, how much
# one pixel of movement is worth, wheel step, unit suffix, and the range.


# Blender draws its own text in the TOP-LEFT of the viewport - view name,
# collection, and the whole statistics block - and it grows with the scene. The
# wizard's first layout sat right on top of it and became unreadable, so the
# settings list lives on the right and the title on the bottom-left, which are
# the two areas Blender leaves alone.
_HUD_MARGIN = 24
_HUD_LINE = 22
# Point size before UI scale. Everything else in Blender's interface follows
# the user's scale, so a HUD pinned to raw pixels reads as too small on exactly
# the high-resolution displays where it is hardest to see.
_HUD_FONT_SIZE = 14
# Baseline of the LAST row. The list is built upwards from the bottom edge,
# which keeps it away from the navigation gizmo and the zoom/move/camera
# buttons stacked down the top-right.
_HUD_BOTTOM_INSET = 40


def hud_scale():
    """Blender's interface scale, so the HUD matches the rest of the UI."""
    try:
        return float(bpy.context.preferences.system.ui_scale) or 1.0
    except (AttributeError, TypeError, ValueError):
        return 1.0


def wizard_hud_layout(width, height, line_count, text_width, scale=1.0):
    """Where every piece of the HUD goes, in region pixels.

    Split out from the drawing so the placement can be checked without a
    viewport - that nothing lands in Blender's top-left statistics block, and
    nothing under the top-right gizmo, which are the two corners it is not
    ours to write in.
    """
    margin = _HUD_MARGIN * scale
    line = _HUD_LINE * scale
    right_x = max(margin, width - margin - text_width)

    # Stacked UP from the bottom, so the block grows away from the gizmo rather
    # than towards it as settings are added.
    bottom_y = _HUD_BOTTOM_INSET * scale
    rows = [
        (right_x, bottom_y + line * (line_count - 1 - index))
        for index in range(line_count)
    ]
    top_y = rows[0][1] if rows else bottom_y
    pad = 14 * scale
    backdrop = (
        right_x - pad,
        bottom_y - pad,
        width - margin + 10 * scale,
        top_y + line,
    )

    # The title and the advisory sit over whatever the scene happens to be, and
    # a lighting rig is a bright one - so they get a panel of their own rather
    # than washing out against the floor.
    title = (margin, bottom_y + line * 1.6)
    note = (margin, bottom_y)
    title_backdrop = (
        margin - pad,
        note[1] - pad,
        margin + max(text_width, 340 * scale) + pad,
        title[1] + line,
    )

    return {
        "rows": rows,
        "backdrop": backdrop,
        "title": title,
        "note": note,
        "title_backdrop": title_backdrop,
    }


# Written onto every arranged object so its centre can be found again. Parenting
# and the sweep both create a findable link, but both are optional - with them
# off there is otherwise NOTHING tying an object to its Empty, and a re-run
# would make a second one. Stored as an ID pointer rather than a name so it
# survives the Empty being renamed.


def arrangeable_objects(context):
    """The objects Create Radial would actually arrange.

    Any object type qualifies - a ring of meshes is as legitimate as a ring of
    lamps. The centre Empties this tool creates are excluded, or re-running it
    on the result would drag the previous centre onto the new circle.
    """
    return [
        obj for obj in (getattr(context, "selected_objects", None) or ())
        if obj.type != "EMPTY" or "Radial Centre" not in obj.name
    ]


def position_wave_row_destinations(points_by_name, axis, spacing):
    """Return one straight, centred row destination for every selected object."""
    index = light_layout.AXIS_INDEX[axis]
    centre = light_layout.centroid(points_by_name.values())
    ordered = sorted(points_by_name.items(), key=lambda item: (item[1][index], item[0]))
    start = centre[index] - spacing * (len(ordered) - 1) * 0.5
    destinations = {}
    for offset, (name, _point) in enumerate(ordered):
        destination = list(centre)
        destination[index] = start + spacing * offset
        destinations[name] = destination
    return destinations


class ESPRESSO_OT_fit_position_wave_to_selection(bpy.types.Operator):
    """Fit one visible Position Wave cycle across the selected objects."""

    bl_idname = "espresso.fit_position_wave_to_selection"
    bl_label = "Fit One Wave to Selected"
    bl_description = (
        "Set Spatial Frequency so one complete wave travels from the first "
        "selected object to the last along the chosen axis"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return len(arrangeable_objects(context)) >= 2

    def execute(self, context):
        props = context.scene.espresso_props
        if not espresso_props.set_position_wave_auto_fit(props, context, True):
            self.report({"WARNING"}, "The selected objects have no usable Position Wave span.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Position Wave Auto Fit enabled.")
        return {"FINISHED"}


class ESPRESSO_OT_create_position_wave_row(bpy.types.Operator):
    """Evenly space selected objects along the axis read by Position Wave."""

    bl_idname = "espresso.create_position_wave_row"
    bl_label = "Arrange Position Wave Row"
    bl_description = (
        "Evenly space the selected objects in a straight travelling-wave order. "
        "The perpendicular coordinates are centred so the result is a true row"
    )
    bl_options = {"REGISTER", "UNDO"}

    axis: bpy.props.EnumProperty(
        name="Axis",
        items=[
            ("AUTO", "Auto", "Use the axis the selection spans most"),
            ("X", "X", "Arrange along world X"),
            ("Y", "Y", "Arrange along world Y"),
            ("Z", "Z", "Arrange along world Z"),
        ],
        default="AUTO",
    )
    spacing: bpy.props.FloatProperty(
        name="Spacing",
        description="Distance between consecutive objects in the row",
        default=2.0,
        min=0.001,
        unit="LENGTH",
    )

    @classmethod
    def poll(cls, context):
        return len(arrangeable_objects(context)) >= 2

    def execute(self, context):
        objects = arrangeable_objects(context)
        points = {obj.name: tuple(obj.matrix_world.translation) for obj in objects}
        axis = self.axis if self.axis != "AUTO" else light_layout.widest_axis(points.values())[0]
        destinations = position_wave_row_destinations(points, axis, self.spacing)
        for obj in objects:
            world = obj.matrix_world.copy()
            world.translation = destinations[obj.name]
            obj.matrix_world = world
        self.report({"INFO"}, "Arranged %d objects along %s" % (len(objects), axis))
        return {"FINISHED"}


class ESPRESSO_OT_position_wave_row_wizard(bpy.types.Operator):
    """Preview a Position Wave row before committing the arrangement."""

    bl_idname = "espresso.position_wave_row_wizard"
    bl_label = "Arrange Position Wave Row"
    bl_description = (
        "Preview an evenly spaced row for Position Wave. Drag to change spacing; "
        "press X, Y, Z or A for axis; Enter confirms and Escape restores"
    )
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    axis: bpy.props.EnumProperty(
        name="Axis",
        items=[("AUTO", "Auto", "Use the widest axis"), ("X", "X", "World X"),
               ("Y", "Y", "World Y"), ("Z", "Z", "World Z")],
        default="AUTO",
    )
    spacing: bpy.props.FloatProperty(name="Spacing", default=2.0, min=0.001, unit="LENGTH")
    initial_state: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return len(arrangeable_objects(context)) >= 2 and (
            getattr(context, "area", None) is not None and context.area.type == "VIEW_3D"
        )

    def _chosen_axis(self):
        return self.axis if self.axis != "AUTO" else light_layout.widest_axis(self._points.values())[0]

    def _starting_points(self, objects):
        try:
            remembered = json.loads(self.initial_state or "{}")
        except ValueError:
            remembered = {}
        return {
            obj.name: tuple(remembered.get(obj.name)
                            or (float(value) for value in obj.matrix_world.translation))
            for obj in objects
        }

    def _preview(self, context):
        axis = self._chosen_axis()
        destinations = position_wave_row_destinations(self._points, axis, self.spacing)
        for obj in self._objects:
            matrix = self._matrices[obj.name].copy()
            matrix.translation = destinations[obj.name]
            obj.matrix_world = matrix
        context.area.tag_redraw()

    def _restore(self, context):
        for obj in self._objects:
            obj.matrix_world = self._matrices[obj.name]
        context.area.tag_redraw()

    def _finish(self, context):
        if getattr(self, "_handler", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, "WINDOW")
            self._handler = None
        space = getattr(self, "_space", None)
        if space is not None and getattr(self, "_sidebar_was_open", False):
            try:
                space.show_region_ui = True
            except (AttributeError, ReferenceError):
                pass
        if context.area is not None:
            context.area.tag_redraw()

    def invoke(self, context, event):
        self._objects = arrangeable_objects(context)
        self.initial_state = json.dumps({
            obj.name: [float(value) for value in obj.matrix_world.translation]
            for obj in self._objects
        })
        self._points = self._starting_points(self._objects)
        self._matrices = {obj.name: obj.matrix_world.copy() for obj in self._objects}
        self._anchor_mouse = event.mouse_x
        self._anchor_spacing = self.spacing
        self._area_pointer = context.area.as_pointer()
        space = context.space_data
        self._sidebar_was_open = bool(getattr(space, "show_region_ui", False))
        if self._sidebar_was_open:
            space.show_region_ui = False
        self._space = space
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_position_wave_row_hud, (self,), "WINDOW", "POST_PIXEL")
        self._preview(context)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        objects = arrangeable_objects(context)
        if not objects:
            self.report({"WARNING"}, "Select two or more objects to arrange.")
            return {"CANCELLED"}
        if not self.initial_state:
            self.initial_state = json.dumps({
                obj.name: [float(value) for value in obj.matrix_world.translation]
                for obj in objects
            })
        starting = self._starting_points(objects)
        axis = self.axis if self.axis != "AUTO" else light_layout.widest_axis(starting.values())[0]
        destinations = position_wave_row_destinations(starting, axis, self.spacing)
        for obj in objects:
            matrix = obj.matrix_world.copy()
            matrix.translation = destinations[obj.name]
            obj.matrix_world = matrix
        self.report({"INFO"}, "Arranged %d objects along %s" % (len(objects), axis))
        return {"FINISHED"}

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            self.spacing = max(0.001, self._anchor_spacing + (event.mouse_x - self._anchor_mouse) * 0.01)
            self._preview(context)
            return {"RUNNING_MODAL"}
        if event.value == "PRESS":
            if event.type in {"X", "Y", "Z"}:
                self.axis = event.type
                self._anchor_mouse = event.mouse_x
                self._anchor_spacing = self.spacing
                self._preview(context)
                return {"RUNNING_MODAL"}
            if event.type == "A":
                self.axis = "AUTO"
                self._preview(context)
                return {"RUNNING_MODAL"}
            if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
                self.spacing = max(0.001, self.spacing + (0.1 if event.type == "WHEELUPMOUSE" else -0.1))
                self._anchor_mouse = event.mouse_x
                self._anchor_spacing = self.spacing
                self._preview(context)
                return {"RUNNING_MODAL"}
            if event.type in {"RET", "NUMPAD_ENTER", "LEFTMOUSE", "SPACE"}:
                self._restore(context)
                self._finish(context)
                return self.execute(context)
            if event.type in {"ESC", "RIGHTMOUSE"}:
                self._restore(context)
                self._finish(context)
                self.report({"INFO"}, "Position Wave row cancelled - everything put back.")
                return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.row(align=True).prop(self, "axis", expand=True)
        layout.prop(self, "spacing")
        layout.label(text="The original layout is reused when this operation is adjusted.", icon="INFO")


def _draw_position_wave_row_hud(wizard):
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    area = getattr(bpy.context, "area", None)
    if area is None or area.as_pointer() != getattr(wizard, "_area_pointer", None):
        return
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        return
    # Reuse the radial wizard's protected layout instead of Blender's crowded
    # top-left overlay.  Position Wave needs the same readable, scene-safe HUD.
    scale = hud_scale()
    font = 0
    size = _HUD_FONT_SIZE * scale * 1.25
    try:
        blf.size(font, size)
    except TypeError:
        blf.size(font, int(size), 72)

    lines = (
        (True, "Spacing     %.2f m  [drag / wheel]" % wizard.spacing),
        (False, "Axis        %s  [X] [Y] [Z] [A]uto" % wizard._chosen_axis()),
    )
    widest = max((blf.dimensions(font, text)[0] for _active, text in lines), default=180)
    title_text = "Arrange Position Wave Row  -  drag to adjust, Enter to confirm, Esc to restore"
    note_text = "Preview only - the original layout is restored before the row is committed."
    title_width = max(blf.dimensions(font, title_text)[0], blf.dimensions(font, note_text)[0])
    layout = wizard_hud_layout(
        region.width, region.height, len(lines), widest, scale=scale * 1.25)

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", (0.05, 0.05, 0.06, 0.82))
    for key, width_override in (("backdrop", None), ("title_backdrop", title_width)):
        x0, y0, x1, y1 = layout[key]
        if width_override is not None:
            x1 = max(x1, x0 + width_override + 28 * scale)
        batch = batch_for_shader(shader, "TRIS", {"pos": [
            (x0, y0), (x1, y0), (x1, y1),
            (x0, y0), (x1, y1), (x0, y1),
        ]})
        batch.draw(shader)
    gpu.state.blend_set("NONE")

    for (is_active, text), (x, y) in zip(lines, layout["rows"]):
        blf.color(font, *((1.0, 0.75, 0.2, 1.0) if is_active else (0.82, 0.82, 0.82, 1.0)))
        blf.position(font, x, y, 0)
        blf.draw(font, text)

    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.position(font, layout["title"][0], layout["title"][1], 0)
    blf.draw(font, title_text)
    blf.color(font, 0.55, 0.9, 0.55, 1.0)
    blf.position(font, layout["note"][0], layout["note"][1], 0)
    blf.draw(font, note_text)


class ESPRESSO_OT_apply_palette(bpy.types.Operator):
    """Fill the applied Colour Ramp with a named palette.

    The ramp templates promise unlimited colours and delivered four seeded
    stops with no way to reach a LOOK without building one at a time. Picking
    "Fire" is what an artist wants to do, and it is the same problem master
    presets already solve for parameters.

    Each palette carries its own interpolation because that is part of the
    look: a rainbow that steps is not a rainbow, and Christmas lights that
    crossfade are not Christmas lights.
    """

    bl_idname = "espresso.apply_palette"
    bl_label = "Apply Palette"
    bl_description = "Replace this ramp's colours with a named palette"
    bl_options = {"REGISTER", "UNDO"}

    palette: bpy.props.EnumProperty(
        name="Palette",
        description="Which set of colours to put on the ramp",
        items=palettes.ENUM_ITEMS,
    )
    # Which ramp, when the panel is showing more than one. Matched by node name
    # rather than by index, so adding or removing a ramp cannot silently
    # repaint a different one.
    node_name: bpy.props.StringProperty(options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        # The stepped-ramp recipe that sourced its own node is not part of
        # this product; every ramp here comes from the last applied target.
        ramps = ramps_for_last_target(props)
        if not ramps:
            self.report({"WARNING"}, "No Colour Ramp to fill.")
            return {"CANCELLED"}

        if self.node_name:
            ramps = [node for node in ramps if node.name == self.node_name] or ramps

        filled = 0
        for node in ramps:
            if palettes.apply_to_ramp(node.color_ramp, self.palette):
                filled += 1
        if not filled:
            self.report({"WARNING"}, "Unknown palette %r." % self.palette)
            return {"CANCELLED"}

        entry = palettes.PALETTE_BY_ID[self.palette]
        self.report(
            {"INFO"},
            "%s: %d colours, %s." % (
                entry["label"], len(entry["colours"]),
                "stepped" if entry["interpolation"] == "CONSTANT" else "blended"),
        )
        return {"FINISHED"}


class ESPRESSO_MT_palettes(bpy.types.Menu):
    """The palette list, as a menu so it stays one row in the panel."""

    bl_idname = "ESPRESSO_MT_palettes"
    bl_label = "Palette"

    def draw(self, context):
        layout = self.layout
        # Grouped by how they read, not alphabetically: an artist choosing a
        # palette is choosing between "a gradient" and "distinct colours"
        # before they are choosing which colours.
        for heading, icon, kind in (
                ("Blended", "IPO_EASE_IN_OUT", palettes.LINEAR),
                ("Stepped", "IPO_CONSTANT", palettes.CONSTANT)):
            entries = [e for e in palettes.PALETTES
                       if e["interpolation"] == kind]
            if not entries:
                continue
            column = layout.column()
            column.label(text=heading, icon=icon)
            for entry in entries:
                column.operator(
                    "espresso.apply_palette", text=entry["label"]).palette = entry["id"]


def ramps_for_last_target(props):
    """Every Espresso ramp behind whatever was applied last.

    Shared by the panel and the operator so the list the artist sees and the
    list that gets filled cannot disagree.
    """
    from ...apply import colour_ramp
    from ...apply import target_memory

    entry = target_memory.latest_entry(props)
    if not entry:
        return []
    resolved, _reason = target_memory.resolve_entry(entry)
    if not resolved:
        return []

    found = []
    seen = set()
    for item in resolved:
        node = colour_ramp.ramp_node_for_driver(
            item.get("owner"), item.get("data_path", ""))
        if node is None:
            continue
        pointer = node.as_pointer()
        if pointer not in seen:
            seen.add(pointer)
            found.append(node)
    return found


class ESPRESSO_OT_apply_to_lights(bpy.types.Operator):
    """Apply this lighting template to every selected light's Power.

    The route this replaces was: right-click one lamp's Power, apply, then use
    Blender's Copy Drivers to Selected. That last step is the trap - it copies
    each variable's TARGET too, so every lamp ends up reading the ACTIVE lamp's
    position and a spatial pattern collapses to a single value. Measured, and
    reported by a user as "they're all playing the same animation".

    So this applies the template afresh per lamp, binds each lamp's position
    variables to itself, and picks the axis from how the rig is actually laid
    out instead of assuming world X.
    """

    bl_idname = "espresso.apply_to_lights"
    bl_label = "Apply to Selected Lights"
    bl_description = (
        "Apply this lighting template to the Power of every selected light. "
        "Each light gets its own driver, and spatial templates read each "
        "light's own position so the pattern travels through the rig"
    )
    bl_options = {"REGISTER", "UNDO"}

    axis: bpy.props.EnumProperty(
        name="Axis",
        description=(
            "Which world axis a travelling wave moves along. Auto uses the "
            "axis the selected lights are most spread out along"
        ),
        items=[
            ("AUTO", "Auto", "Use the axis the lights are most spread along"),
            ("X", "X", "World X"),
            ("Y", "Y", "World Y"),
            ("Z", "Z", "World Z"),
        ],
        default="AUTO",
    )

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None or not props.is_valid:
            return False
        has_target = bool(apply_target.read(props))
        if not template_suits_lights(
                espresso_props.get_current_template(props), has_target):
            return False
        return bool(applicable_objects(context))

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        lights = applicable_objects(context)
        if not lights:
            self.report(
                {"WARNING"},
                "Select the objects to apply to. Without a nominated target "
                "only lights can be applied to.")
            return {"CANCELLED"}
        espresso_props.refresh_position_wave_auto_fit(props, context, lights)

        # Imported here, not at module scope: context_menu imports THIS module,
        # so a top-level import would be circular.
        from ..menus import context_menu
        from ...engine.targeting.button_targeting import ButtonDriverTarget

        points = light_layout.positions(lights)
        chosen_axis = self.axis
        if chosen_axis == "AUTO":
            chosen_axis = light_layout.widest_axis(points)[0]

        entry, defaulted = apply_target.effective_entry(props, lights)
        if entry is None:
            self.report(
                {"WARNING"},
                "Nothing to drive. Right-click the property you want and "
                "choose \"Use as Espresso Target\", or use \"Apply to "
                "Selected Objects\" from that same menu.")
            return {"CANCELLED"}

        # All or nothing. Applying to some of a selection and not others leaves
        # a half-built rig that looks finished, and the artist has no way to
        # tell which ones took without inspecting every driver.
        missing = apply_target.missing_for(entry, lights)
        if missing:
            named = ", ".join("%s (%s)" % pair for pair in missing[:3])
            if len(missing) > 3:
                named += " and %d more" % (len(missing) - 3)
            self.report(
                {"ERROR"},
                "%s is not on all %d selected objects - %s. Pick a property "
                "they share, or right-click the one you want on each."
                % (entry["label"], len(lights), named))
            return {"CANCELLED"}

        applied_all, states_all, done, skipped = [], [], 0, []
        seen_owners, shared = {}, []
        for obj in lights:
            targets, reason = apply_target.targets_for(entry, obj)
            if not targets:
                skipped.append("%s %s" % (obj.name, reason))
                continue

            # Reaching the same datablock twice means these objects SHARE it.
            # One datablock is one driver and one evaluation, so writing again
            # would just overwrite the first and report success for both.
            key = (targets[0].owner.as_pointer(), targets[0].data_path,
                   targets[0].index)
            if key in seen_owners:
                shared.append(obj.name)
                continue
            seen_owners[key] = obj.name
            ok, message, states, applied = context_menu.apply_current_template_to_button_targets(
                targets, props, template, context.scene,
            )
            if not ok:
                self.report({"WARNING"}, "%s: %s" % (obj.name, message))
                return {"CANCELLED"}
            _aim_position_variables(obj, chosen_axis)
            applied_all.extend(applied)
            states_all.extend(states)
            done += 1

        if not done:
            self.report({"WARNING"}, "None of the selection has that property.")
            return {"CANCELLED"}

        target_memory.remember_targets(
            context, applied_all,
            "%d lights > %s" % (done, template["name"]),
            "template", template["id"], template["name"],
            target_states=states_all,
        )

        note = "Applied %s to %d objects > %s." % (
            template["name"], done, entry["label"])
        if skipped:
            note += " Skipped %d: %s." % (len(skipped), "; ".join(skipped[:3]))
        if shared:
            note += (
                " %d objects share that datablock, so one driver covers them "
                "all and they cannot differ. For a per-object pattern use "
                "\"Apply to Shared Material (per object)\", or split the "
                "material." % len(shared))
        advice = light_layout.advise(
            template["id"], lights,
            espresso_props.collect_values(props, template),
            axis=chosen_axis,
        )
        if light_layout.SPATIAL_KIND.get(template["id"]) == light_layout.AXIS:
            note += " Wave travels along %s." % chosen_axis
        if advice.detail:
            note += " " + advice.detail
        espresso_props.set_last_apply_status(props, note)
        self.report(
            {"WARNING"} if (not advice.ok or shared or skipped) else {"INFO"},
            note)
        return {"FINISHED"}


def _aim_position_variables(light_object, axis):
    """Point this lamp's own-position variables at itself, along ``axis``.

    Position Wave ships wired to LOC_X because a template has to name SOME
    axis. A rig running along Y would read a span of zero and sit still, so the
    axis is corrected here rather than left as a trap. Radial Sweep needs BOTH
    X and Y to get an angle, so it is left exactly as authored.
    """
    data = light_object.data
    animation = getattr(data, "animation_data", None)
    if animation is None:
        return
    transform = light_layout.TRANSFORM_FOR_AXIS[axis]
    for fcurve in animation.drivers:
        for variable in fcurve.driver.variables:
            if variable.type != "TRANSFORMS":
                continue
            # Only the single-axis case is ours to redirect. A pair of X and Y
            # variables is an ANGLE, and rewriting either would break it.
            single = [
                v for v in fcurve.driver.variables if v.type == "TRANSFORMS"
            ]
            if len(single) != 1:
                continue
            variable.targets[0].id = light_object
            variable.targets[0].transform_type = transform
            variable.targets[0].transform_space = "WORLD_SPACE"


class ESPRESSO_OT_apply_motion_selected(bpy.types.Operator):
    bl_idname = "espresso.apply_motion_selected"
    # Lite ships no Camera & Cinematic recipe, so the label must not
    # offer one. The other editions keep the camera wording because
    # they have the recipes to back it.
    bl_label = "Apply Motion"
    bl_description = "Apply the current Camera effect to its semantic destination, or route an authored motion plan to the active object or pose bone"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None or not props.is_valid or context.active_object is None:
            return False
        template = espresso_props.get_current_template(props)
        if camera_application.requires_camera_object(template):
            return context.active_object.type == "CAMERA"
        return templates.has_motion_plan(template)

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        is_camera = camera_application.requires_camera_object(template)
        pose_bone = None if is_camera else _active_motion_pose_bone(context)
        if is_camera:
            result = camera_application.apply_camera_template(
                context.active_object,
                template,
                espresso_props.built_channel_previews(props),
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
            )
        else:
            result = motion_channels.apply_motion_template_to_object(
                context.active_object,
                template,
                espresso_props.built_channel_previews(props),
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
                pose_bone=pose_bone,
                enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(props, template),
                template_values=espresso_props.collect_values(props, template),
                source_entry=source_binding.latest_source(props),
            )
        if not result.ok:
            self.report({"WARNING"}, result.message)
            return {"CANCELLED"}
        if is_camera:
            label = f"{context.active_object.name} > {template['name']}"
        elif pose_bone is not None:
            label = f"{context.active_object.name} > {pose_bone.name} > {template['name']}"
        else:
            label = f"{context.active_object.name} > {template['name']}"
        target_memory.remember_targets(
            context,
            result.targets,
            label,
            "camera" if is_camera else "motion",
            template["id"],
            template["name"],
            target_states=result.target_states,
            template_values=espresso_props.collect_values(props, template),
        )
        espresso_props.set_last_apply_status(
            props,
            f"Remembered: {target_memory.latest_label(props) or 'motion target'}",
        )
        self.report({"INFO"}, result.message)
        return {"FINISHED"}


# Templates whose amplitude is a POSE rather than a number. The envelope has to
# run 0 at rest and 1 at the extreme for the recorded pose to be reached exactly.
POSE_DRIVEN_TEMPLATES = set()


class ESPRESSO_OT_apply_pose_as_motion(bpy.types.Operator):
    bl_idname = "espresso.apply_pose_as_motion"
    bl_label = "Apply Blink to Bones"
    bl_description = (
        "Record the current pose of the selected bones as the fully-closed "
        "shape, return those bones to rest, and drive them between the two on "
        "this template's rhythm. Pose the lids shut before pressing this - the "
        "pose you make IS the blink"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None or not props.is_valid:
            return False
        template = espresso_props.get_current_template(props)
        if template.get("id") not in POSE_DRIVEN_TEMPLATES:
            return False
        return bool(getattr(context, "selected_pose_bones", None))

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        armature = context.active_object
        bones = list(context.selected_pose_bones or ())
        if not bones:
            self.report({"WARNING"}, "Select the eyelid bones in Pose Mode first.")
            return {"CANCELLED"}

        # Capture everything BEFORE clearing anything. A bone that fails
        # validation later must not have already lost the artist's pose.
        captures = []
        for bone in bones:
            if bone.rotation_mode == "AXIS_ANGLE":
                self.report(
                    {"WARNING"},
                    'Bone "%s" uses Axis-Angle rotation, which cannot be blended '
                    "as a pose. Switch it to Quaternion or XYZ Euler." % bone.name,
                )
                return {"CANCELLED"}
            captures.append((bone, pose_capture.capture_pose_deltas(bone)))

        posed = [(bone, cap) for bone, cap in captures if cap]
        if not posed:
            self.report(
                {"WARNING"},
                "None of the selected bones are posed away from rest. Pose the "
                "lids into the closed shape first - that pose is what gets "
                "recorded.",
            )
            return {"CANCELLED"}

        values = espresso_props.collect_values(props, template)
        envelope, _ = utils.build_expression(template, values, context.scene)

        # One lid leading the other is what stops a blink reading as a machine
        # closing two shutters. The offset cannot live in the expression - only
        # this layer knows which bone is which side - so the envelope is shifted
        # in scene time per bone, which moves WHEN it blinks without touching
        # the shape of the blink at all.
        lead_offsets = pose_capture.side_time_offsets(values.get("LEAD", 0))
        unsided = []

        applied_bones, driver_count = [], 0
        all_targets, all_states = [], []
        # Undo log for a partial apply. Every bone reached here gets new
        # drivers AND has its recorded pose cleared once those drivers
        # replace it, so a failure on a later bone left half a rig behind
        # and the pose gone from the bones it did reach -- a pose the
        # artist has no way to get back.
        created_curves, cleared_poses = [], []
        try:
            for bone, captured in posed:
                side = pose_capture.bone_side(bone.name)
                if side is None and any(lead_offsets.values()):
                    unsided.append(bone.name)
                offset = lead_offsets.get(side, 0)
                bone_envelope = (
                    stagger_apply.shift_expression_time(envelope, offset)
                    if offset else envelope
                )
                channels = pose_capture.channels_from_pose(bone, bone_envelope, captured)
                targets, states = [], []
                for channel in channels:
                    data_path = bone.path_from_id(channel["data_path"])
                    valid, message = utils.validate_driver_expression(
                        channel["expression"], template, context.scene,
                    )
                    if not valid:
                        raise RuntimeError("%s: %s" % (bone.name, message))
                    try:
                        fcurve = armature.driver_add(data_path, channel["index"])
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError("%s: %s" % (bone.name, exc)) from exc
                    created_curves.append((armature, data_path, channel["index"]))
                    fcurve.driver.type = "SCRIPTED"
                    fcurve.driver.expression = channel["expression"]
                    targets.append(motion_channels.MotionTarget(
                        armature, data_path, channel["index"],
                    ))
                    # Carry the amplitude in the memory, not only in the driver
                    # text. The bones are cleared straight after this, so on a
                    # re-apply there is no pose left to read - without these the
                    # blink would come back with no shape at all.
                    states.append({
                        "mode": utils.REST_START_OFF,
                        "pose_rest": channel["pose_rest"],
                        "pose_delta": channel["pose_delta"],
                    })
                    driver_count += 1

                # Only now is it safe to drop the pose: the drivers that reproduce
                # it are already in place, so nothing the artist made is lost.
                cleared_poses.append((bone, captured))
                pose_capture.clear_pose(bone)

                # Accumulate rather than remember per bone. remember_targets
                # OVERWRITES the latest entry, so calling it inside this loop left
                # only the final bone remembered and every earlier one unreachable
                # from Update Last Target or Clear Last Drivers.
                all_targets.extend(targets)
                all_states.extend(states)
                applied_bones.append("%s (%s)" % (bone.name, pose_capture.describe_capture(captured)))
        except Exception as exc:  # noqa: BLE001
            for owner, data_path, index in reversed(created_curves):
                try:
                    owner.driver_remove(data_path, index)
                except (RuntimeError, TypeError):
                    pass
            for bone, captured in reversed(cleared_poses):
                pose_capture.restore_pose(bone, captured)
            self.report(
                {"WARNING"},
                "Pose apply failed and was rolled back: %s" % exc,
            )
            return {"CANCELLED"}

        # One entry for the whole apply, naming every bone it landed on.
        bone_names = ", ".join(bone.name for bone, _ in posed)
        target_memory.remember_targets(
            context,
            all_targets,
            "%s > %s > %s" % (armature.name, bone_names, template["name"]),
            "motion",
            template["id"],
            template["name"],
            target_states=all_states,
        )

        skipped = len(bones) - len(posed)
        summary = "Recorded the pose on %d bone%s as the blink shape, %d driver%s: %s" % (
            len(posed), "" if len(posed) == 1 else "s",
            driver_count, "" if driver_count == 1 else "s",
            ", ".join(applied_bones),
        )
        if unsided:
            # Say so rather than leaving them mysteriously in sync: an unsided
            # bone is usually a naming slip, and the artist cannot see from the
            # viewport that the lead quietly skipped it.
            summary += ". Eye lead skipped %d bone%s with no side in the name (%s)" % (
                len(unsided), "" if len(unsided) == 1 else "s", ", ".join(unsided[:3]),
            )
        if skipped:
            summary += ". %d selected bone%s at rest and %s skipped" % (
                skipped, " was" if skipped == 1 else "s were",
                "was" if skipped == 1 else "were",
            )
        espresso_props.set_last_apply_status(props, summary)
        self.report({"INFO"}, summary)
        return {"FINISHED"}


@atomic_bake_operator
class ESPRESSO_OT_bake_drivers(bpy.types.Operator):
    """Bake ANY driver to keyframes, not only ones this add-on applied.

    Ownership note: this deliberately overlaps nothing in the catalogue. The
    per-property "Apply and Bake" freezes a template Espresso just applied;
    this freezes drivers that already exist, wherever they came from.
    """

    bl_idname = "espresso.bake_drivers"
    bl_label = "Bake Drivers to Keyframes"
    bl_description = (
        "Sample every driven channel over a frame range and write plain "
        "keyframes. Finds drivers on shape keys, materials, node sockets, "
        "lights and bones, not just on the objects themselves"
    )
    bl_options = {"REGISTER", "UNDO"}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[
            ("SELECTED", "Selected Objects", "Every driver reachable from the selected objects"),
            ("ACTIVE", "Active Object Only", "Only the active object and its data"),
            ("SCENE", "Whole Scene", "Every object in the scene, plus scene and world drivers"),
        ],
        default="SELECTED",
    )
    frame_start: bpy.props.IntProperty(name="Start", default=1)
    frame_end: bpy.props.IntProperty(name="End", default=250)
    frame_step: bpy.props.IntProperty(
        name="Step", default=1, min=1, soft_max=10,
        description="Bake every Nth frame. Keep at 1 for hard-edged motion",
    )
    after: bpy.props.EnumProperty(
        name="Afterwards",
        items=[
            ("REMOVE", "Remove Drivers", "Delete the drivers once their values are keyed"),
            ("MUTE", "Mute Drivers", "Keep the drivers but switch them off, so they can be re-enabled later"),
        ],
        default="REMOVE",
        description=(
            "A live driver overrides the keyframes on its own channel, so the "
            "drivers must either go or be muted for a bake to be visible"
        ),
    )
    foreign_skipped: bpy.props.IntProperty(default=0, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def _objects(self, context):
        if self.scope == "ACTIVE":
            return [context.active_object] if context.active_object else []
        if self.scope == "SCENE":
            return list(context.scene.objects)
        return list(context.selected_objects)

    def _discover(self, context):
        from ...engine import bake
        from ...apply import internal_helpers
        from . import bake_applied

        targets, unresolved = bake.discover_driver_targets(
            self._objects(context),
            scene=context.scene if self.scope == "SCENE" else None,
        )
        discovered = [
            target for target in targets
            if not internal_helpers.is_owned_path(target.data_path)
        ]
        supported = [target for target in discovered
                     if not bake_applied.target_has_foreign_motion(target)]
        self.foreign_skipped = len(discovered) - len(supported)
        return supported, unresolved

    def _bake_preflight(self, context, targets):
        if not targets and self.foreign_skipped:
            return (
                "%d driver(s) belong to motion not available in this edition; left unchanged."
                % self.foreign_skipped
            )
        return ""

    def invoke(self, context, event):
        self.frame_start = context.scene.frame_start
        self.frame_end = context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        from ...engine import bake

        layout = self.layout
        layout.prop(self, "scope")
        row = layout.row(align=True)
        row.prop(self, "frame_start")
        row.prop(self, "frame_end")
        layout.prop(self, "frame_step")
        layout.prop(self, "after")

        # Say what is about to happen BEFORE it happens. A whole-scene bake on
        # a production rig can be tens of thousands of keyframes, and the honest
        # place to surface that is the dialog, not a report afterwards.
        targets, unresolved = self._discover(context)
        frames = len(bake.frame_list(self.frame_start, self.frame_end, self.frame_step))
        box = layout.box()
        if not targets:
            box.label(text="No drivers found in this scope.", icon="INFO")
        else:
            kinds = {}
            for target in targets:
                kinds[target.label] = kinds.get(target.label, 0) + 1
            summary = ", ".join("%d %s" % (count, name) for name, count in sorted(kinds.items()))
            box.label(text="%d drivers x %d frames = %d keyframes"
                           % (len(targets), frames, len(targets) * frames),
                      icon="KEYFRAME_HLT")
            box.label(text=summary)
        if unresolved:
            box.label(text="%d driver(s) point at a missing property and will be skipped"
                           % unresolved, icon="ERROR")
        if self.foreign_skipped:
            box.label(text="%d driver(s) belong to unavailable motion and will be skipped"
                           % self.foreign_skipped, icon="INFO")

    def execute(self, context):
        from ...engine import bake

        targets, unresolved = self._discover(context)
        if not targets:
            message = (
                "%d driver(s) belong to motion not available in this edition; left unchanged."
                % self.foreign_skipped
                if self.foreign_skipped else "No drivers found in this scope."
            )
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        props = getattr(context.scene, "espresso_props", None)
        cleanup = (
            target_memory.capture_cleanup_for_targets(targets, props)
            if self.after == "REMOVE" else None
        )

        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            baked, keys, message = bake.bake_targets(
                context.scene, targets,
                start=self.frame_start, end=self.frame_end, step=self.frame_step,
                remove_driver=(self.after == "REMOVE"),
                progress=lambda done, total: wm.progress_update(done / max(1, total)),
            )
        finally:
            wm.progress_end()

        if not baked:
            # bake_targets reports the property it choked on rather than a
            # generic failure, so pass that through verbatim.
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        if self.after == "MUTE":
            muted = bake.set_driver_mute(targets, True)
            message += " Muted %d driver(s)." % muted
        else:
            target_memory.cleanup_captured_resources(
                cleanup, context.scene, props,
            )
            latest = target_memory.latest_entry(props) if props is not None else None
            if latest and target_memory.entry_overlaps_targets(
                latest, (cleanup or {}).get("targets", []),
            ):
                target_memory.clear_latest_entry(props)
        if unresolved:
            message += " Skipped %d driver(s) pointing at a missing property." % unresolved
        if self.foreign_skipped:
            message += " Skipped %d driver(s) belonging to motion not available in this edition." % self.foreign_skipped
        self.report({"WARNING"} if unresolved or self.foreign_skipped else {"INFO"}, message)
        return {"FINISHED"}


class BakeOptionsMixin:
    """The frame-range/step popup shared by every bake entry point.

    Baking is always confirmed through a popup (the developer chose "always ask"
    for the range) rather than firing on click, because a bake writes a keyframe
    per frame across the whole range - it should never be a surprise.
    """

    bake_start: bpy.props.IntProperty(
        name="Start", description="First frame to bake", default=1,
    )
    bake_end: bpy.props.IntProperty(
        name="End", description="Last frame to bake (always included)", default=250,
    )
    bake_step: bpy.props.IntProperty(
        name="Step", description="Bake every Nth frame. 1 keys every frame - keep it at 1 for hard-edged templates like strobes, where thinning would drop the shape",
        default=1, min=1, soft_max=10,
    )

    # Step thins BLINDLY - every Nth frame, whether or not that frame mattered.
    # Smart bake thins by what the curve is doing, so a hold costs two keys and
    # a peak is never the frame that gets dropped.
    bake_smart: bpy.props.BoolProperty(
        name="Bake Peaks",
        description="Key the moments that carry the motion - turns, peaks, and "
                    "the corners where movement meets a hold - and interpolate "
                    "between them, instead of keying every frame. An eased "
                    "open-hold-close needs about six keys this way rather than "
                    "one per frame. Motion that genuinely changes every frame, "
                    "such as vibration, is left dense",
        default=False,
    )
    bake_smart_tolerance: bpy.props.FloatProperty(
        name="Precision",
        description="How close the thinned curve must stay to the original, as "
                    "a percentage of the motion's own range. 1% is faithful and "
                    "still removes most keys; lower it if a shape you care "
                    "about softens, raise it for fewer keys",
        default=1.0, min=0.01, max=25.0, subtype="PERCENTAGE",
    )
    bake_smart_passes: bpy.props.IntProperty(
        name="Passes",
        description="How many times to re-examine the curve looking for keys it "
                    "can still remove. 1 is quick and already handles most "
                    "motion; 2 is the recommended balance; 5 works hardest on "
                    "complex curves. Raising it never loses accuracy - it only "
                    "costs time - and a curve that has settled stops early "
                    "regardless of the number set here",
        default=2, min=1, max=5,
    )

    # Start+Duration is the default because it is how the question is actually
    # asked: "bake this landing" is a length, not an end frame. An end frame
    # makes the artist do the arithmetic, and redo it every time Start moves.
    use_duration: bpy.props.BoolProperty(
        name="Use Duration",
        description="Set the range as a start frame and a length, instead of a "
                    "start and an end frame",
        default=True,
    )
    bake_duration: bpy.props.IntProperty(
        name="Duration",
        description="How many frames to bake, counting from Start. The start "
                    "frame is included, so a duration of 1 bakes one frame",
        default=250, min=1,
    )
    # The panel already measures how long the loaded motion runs. Retyping that
    # number into the bake dialog is exactly the kind of copying a tool should
    # do for you - and getting it wrong by a few frames clips the recovery,
    # which is the part an artist is usually baking in order to keep.
    match_motion_length: bpy.props.BoolProperty(
        name="Match Motion Length",
        description="Use the loaded motion's own measured length as the "
                    "duration. Unavailable for motion that never ends or never "
                    "repeats, which has no length to match",
        default=True,
    )

    # Baking from where you are looking is the common case when checking one
    # beat: scrub to it, bake from there. Off by default, because the scene
    # start is the conventional answer and a bake that silently begins at the
    # playhead would be a surprise the first time.
    start_at_current_frame: bpy.props.BoolProperty(
        name="Start at Current Frame",
        description="Begin the bake at the playhead instead of the Start field, "
                    "so scrubbing to a moment and baking needs no retyping",
        default=False,
    )

    def _motion_length(self, context):
        """The loaded motion's own length, or None when it has no fixed one.

        A cyclic motion reports one loop: baking a single cycle is the useful
        default, and anything longer is that cycle repeated.
        """
        from ...engine import duration as espresso_duration
        from ..state import props as espresso_props_module

        scene = getattr(context, "scene", None)
        props = getattr(scene, "espresso_props", None)
        if props is None:
            return None
        template = espresso_props_module.get_current_template(props)
        if template is None:
            return None
        try:
            info = espresso_duration.classify(
                template,
                espresso_props_module.collect_values(props, template),
                scene,
            )
        except Exception:
            return None
        frames = info.get("frames")
        if frames and info.get("kind") in (espresso_duration.FINITE,
                                           espresso_duration.CYCLIC):
            return int(frames)
        return None

    def _resolve(self, context):
        """(start, end, step) for whichever way the range is being expressed."""
        scene = getattr(context, "scene", None)
        if self.start_at_current_frame and scene is not None:
            start = int(scene.frame_current)
        else:
            start = int(self.bake_start)
        step = max(1, int(self.bake_step))

        # A caller that passes bake_end and says nothing about the mode means an
        # end frame - honour it. Without this, bake_last_target(bake_end=9) is
        # silently a 250-frame bake, because use_duration defaults on. The
        # dialog is unaffected: _seed_range assigns use_duration, which marks it
        # set, so an artist's choice of mode always wins over this fallback.
        is_set = getattr(getattr(self, "properties", None), "is_property_set", None)
        if is_set is not None and is_set("bake_end") and not is_set("use_duration"):
            return start, int(self.bake_end), step

        if not self.use_duration:
            return start, int(self.bake_end), step
        frames = int(self.bake_duration)
        length = self._motion_length(context)
        if self.match_motion_length and length:
            frames = length
        frames = max(1, frames)
        # Start is inclusive, so a duration of N ends at start + N - 1.
        return start, start + frames - 1, step
    def _seed_range(self, context):
        """Offer the last confirmed range, falling back to the scene's.

        Seeding from the scene every time threw away a deliberate choice: an
        artist who baked 40-90 to check one beat had to re-type it on the next
        bake, and on every bake after that.
        """
        scene = context.scene
        props = getattr(scene, "espresso_props", None)
        if props is not None and props.bake_range_remembered:
            self.bake_start = props.bake_last_start
            self.bake_end = props.bake_last_end
            self.bake_step = max(1, props.bake_last_step)
            self.bake_duration = max(1, props.bake_last_duration)
            self.use_duration = props.bake_last_use_duration
            self.match_motion_length = props.bake_last_match_motion
            self.start_at_current_frame = props.bake_last_start_at_current
            return
        self.bake_start = scene.frame_start
        self.bake_end = scene.frame_end
        self.bake_duration = max(1, scene.frame_end - scene.frame_start + 1)
        # Assigned even though it is already the default, so the property counts
        # as SET. _resolve treats "bake_end set, use_duration not" as a
        # programmatic end-frame call - and without this line the FIRST bake in
        # a scene, before anything is remembered, looked exactly like one. The
        # dialog showed "Match Motion Length (341 frames)" and baked 1 to 250.
        self.use_duration = self.use_duration
        self.start_at_current_frame = self.start_at_current_frame

    def _commit_range(self, context):
        """Resolve the range, write it back, and remember the choice.

        Resolving here rather than at each use is what lets Start+Duration exist
        at all: every bake path already reads self.bake_end, so folding the
        duration into it once means none of them had to learn about the new
        mode.

        Called from execute rather than from the popup's draw: a range the
        artist typed and then cancelled is not a choice, and remembering it
        would make Cancel change state.
        """
        start, end, step = self._resolve(context)
        self.bake_start, self.bake_end, self.bake_step = start, end, step

        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return
        props.bake_last_start = int(start)
        props.bake_last_end = int(end)
        props.bake_last_step = max(1, int(step))
        props.bake_last_duration = max(1, end - start + 1)
        props.bake_last_use_duration = bool(self.use_duration)
        props.bake_last_match_motion = bool(self.match_motion_length)
        props.bake_last_start_at_current = bool(self.start_at_current_frame)
        props.bake_range_remembered = True

    def draw(self, context):
        layout = self.layout
        length = self._motion_length(context)

        row = layout.row(align=True)
        start_field = row.row(align=True)
        # Greyed rather than hidden, same as Duration when matched: the summary
        # line below carries the frame that will actually be used.
        start_field.enabled = not self.start_at_current_frame
        start_field.prop(self, "bake_start")
        if self.use_duration:
            field = row.row(align=True)
            # Greyed rather than hidden while matched: the number is the point,
            # and a field that vanishes makes the toggle look destructive.
            field.enabled = not (self.match_motion_length and length)
            field.prop(self, "bake_duration")
        else:
            row.prop(self, "bake_end")

        layout.prop(self, "start_at_current_frame",
                    text="Start at Current Frame (%d)" % int(
                        getattr(getattr(context, "scene", None), "frame_current", 0) or 0))
        layout.prop(self, "use_duration")

        match_row = layout.row()
        match_row.enabled = bool(length) and self.use_duration
        if length:
            text = "Match Motion Length (%d frames)" % length
        else:
            # Says WHY rather than just greying out - an artist should not have
            # to guess whether the feature is broken or inapplicable.
            text = "Match Motion Length (this motion has no fixed length)"
        match_row.prop(self, "match_motion_length", text=text)

        layout.prop(self, "bake_step")

        # Precision only matters once thinning is on, so it stays out of the
        # way until then rather than sitting greyed out taking a row.
        smart_row = layout.row(align=True)
        smart_row.prop(self, "bake_smart", icon="HANDLETYPE_AUTO_CLAMP_VEC")
        if self.bake_smart:
            smart_row.prop(self, "bake_smart_tolerance", text="")
            smart_row.prop(self, "bake_smart_passes", text="")

        # The resolved range, always. Duration is inclusive of the start frame,
        # which is exactly the kind of off-by-one nobody should have to infer.
        start, end, step = self._resolve(context)
        summary = layout.row()
        summary.enabled = False
        summary.label(text="Bakes frames %d to %d" % (start, end), icon="KEYFRAME_HLT")

        # No keep-driver toggle: baking always removes the driver. Leaving a live
        # driver on a now-keyframed property just double-drives it - the driver
        # keeps winning and the keys sit dead underneath.


@atomic_bake_operator
class ESPRESSO_OT_bake_last_target(BakeOptionsMixin, bpy.types.Operator):
    """Freeze the drivers from the last apply, without re-applying anything.

    The gap this fills: Apply and Bake does both steps at once, so it can only
    freeze a plan at the instant it is created. Once an artist has applied,
    scrubbed the result and tuned it, there was no way to freeze THAT - only to
    re-apply from the current parameters and bake the result, which discards
    any hand edits to the driver since. This samples whatever the remembered
    channels evaluate to right now.
    """

    bl_idname = "espresso.bake_last_target"
    bl_label = "Bake Last Drivers"
    bl_description = (
        "Bake the drivers from the last apply to plain keyframes and remove "
        "them, without re-applying the template first"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return props is not None and target_memory.latest_entry(props) is not None

    @classmethod
    def description(cls, context, _properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        entry = target_memory.latest_entry(props) if props else None
        if not entry:
            return cls.bl_description
        count = len(entry.get("targets") or [])
        label = entry.get("display_label") or "the last apply"
        noun = "driver" if count == 1 else "drivers"
        return (
            "Bake %d %s from %s to plain keyframes and remove them. Does not "
            "re-apply the template, so anything tuned since is kept"
            % (count, noun, label)
        )

    def invoke(self, context, event):
        self._seed_range(context)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        BakeOptionsMixin.draw(self, context)
        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props)
        if not entry:
            return
        from ...engine import bake

        # Name the channels being frozen. The standalone bake can say "every
        # driver in this scope"; here the whole point is that the set is a
        # specific, already-chosen one, so it should be visible before the
        # keyframes land.
        targets = entry.get("targets") or []
        frames = len(bake.frame_list(self.bake_start, self.bake_end, self.bake_step))
        box = self.layout.box()
        box.label(text=entry.get("display_label") or "Last apply", icon="DRIVER")
        box.label(text="%d driver(s) x %d frames = %d keyframes"
                       % (len(targets), frames, len(targets) * frames),
                  icon="KEYFRAME_HLT")

    def _bake_preflight(self, context, targets):
        from . import bake_applied

        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props) or {}
        template_id = str(entry.get("template_id") or "")
        if template_id and template_id not in templates.TEMPLATE_BY_ID:
            return "Last motion is not available in this edition; it was left unchanged."
        if any(bake_applied.target_has_foreign_motion(target) for target in targets):
            return "Last motion is not available in this edition; it was left unchanged."
        return ""

    def execute(self, context):
        self._commit_range(context)
        from ...engine import bake
        from ...engine.targeting.button_targeting import ButtonDriverTarget

        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props)
        if not entry:
            self.report({"WARNING"}, "No remembered apply to bake.")
            return {"CANCELLED"}

        resolved, reason = target_memory.resolve_entry(entry)
        if not resolved:
            # Say WHY rather than a generic failure: the usual cause is the
            # object or bone having been renamed or deleted since the apply.
            self.report({"WARNING"}, reason or "The last apply could not be located.")
            return {"CANCELLED"}

        targets = [
            ButtonDriverTarget(item.get("owner"), item.get("data_path", ""), int(item.get("index", -1)))
            for item in resolved if item.get("owner") is not None
        ]
        if not targets:
            self.report({"WARNING"}, "The last apply's channels could not be located to bake.")
            return {"CANCELLED"}

        reason = self._bake_preflight(context, targets)
        if reason:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}

        cleanup = target_memory.capture_cleanup_for_entry(entry, props)

        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            baked, keys, message = bake.bake_targets(
                context.scene, targets,
                start=self.bake_start, end=self.bake_end, step=self.bake_step,
                remove_driver=True,
                smart=self.bake_smart,
                smart_tolerance=self.bake_smart_tolerance / 100.0,
                smart_passes=self.bake_smart_passes,
                progress=lambda done, total: wm.progress_update(done / max(1, total)),
            )
        finally:
            wm.progress_end()

        if not baked:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        target_memory.cleanup_captured_resources(cleanup, context.scene, props)

        # Those drivers are now keyframes. Leaving __espresso_applied behind
        # keeps Clear Applied Motion / Bake Motion visible and greyed out.
        from ...apply import applied_motion

        hosts = {}
        for item in resolved:
            owner = item.get("owner")
            host = getattr(owner, "id_data", owner) if owner is not None else None
            if host is not None:
                hosts.setdefault(id(host), host)
        for host in hosts.values():
            applied_motion.prune(host)

        # The remembered entry described a LIVE driver that no longer exists, so
        # leaving it would let Update Last Target and Clear Last Drivers act on
        # a channel that is now plain keyframes.
        target_memory.clear_latest_entry(props)
        espresso_props.set_last_apply_status(props, message)
        self.report({"INFO"}, message)
        return {"FINISHED"}


@atomic_bake_operator
class ESPRESSO_OT_apply_and_bake_motion(BakeOptionsMixin, bpy.types.Operator):
    bl_idname = "espresso.apply_and_bake_motion"
    bl_label = "Apply and Bake Motion"
    bl_description = "Apply the motion plan to the selected object, then bake every channel to keyframes so the result is plain animation"
    bl_options = {"REGISTER", "UNDO"}

    # Properties and draw come from BakeOptionsMixin. The bake range properties
    # and the popup drawing are shared rather than declared per operator, so
    # the two popups cannot drift apart. One definition now.

    @classmethod
    def poll(cls, context):
        # Exactly where plain Apply Motion is available.
        return ESPRESSO_OT_apply_motion_selected.poll(context)

    def invoke(self, context, event):
        self._seed_range(context)
        return context.window_manager.invoke_props_dialog(self, width=280)

    def execute(self, context):
        self._commit_range(context)
        from ...engine import bake
        from ...engine.targeting.button_targeting import ButtonDriverTarget

        # Anchor the apply at the bake START frame so Additive Rest Start begins
        # where the bake begins - otherwise applying at the playhead and baking
        # from an earlier frame bakes a null run up to the playhead. Restored
        # after baking.
        scene = context.scene
        original_frame = scene.frame_current
        scene.frame_set(int(self.bake_start))

        # 1. Apply the motion plan through the normal operator, so camera/bone/
        # object routing all behave identically to a plain apply.
        result = bpy.ops.espresso.apply_motion_selected()
        if "FINISHED" not in result:
            scene.frame_set(original_frame)
            return {"CANCELLED"}

        # 2. The plan's channels are the freshly remembered entry.
        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props)
        resolved, _reason = target_memory.resolve_entry(entry or {})
        targets = [
            ButtonDriverTarget(item.get("owner"), item.get("data_path", ""), int(item.get("index", -1)))
            for item in (resolved or []) if item.get("owner") is not None
        ]
        if not targets:
            self.report({"WARNING"}, "The applied channels could not be located to bake.")
            scene.frame_set(original_frame)
            return {"CANCELLED"}
        cleanup = target_memory.capture_cleanup_for_entry(entry, props)
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
        target_memory.cleanup_captured_resources(cleanup, context.scene, props)
        target_memory.clear_latest_entry(props)  # keyframes now, not a live plan
        scene.frame_set(original_frame)
        self.report({"INFO"}, message)
        return {"FINISHED"}


def _offset_target_specs(context, template, reverse=False):
    """Collect deterministic object/bone targets for Apply as Sequence."""
    camera_only = camera_application.requires_camera_object(template)
    channel_paths = {
        channel.get("data_path", "")
        for channel in templates.template_channels(template)
    }
    if getattr(context, "mode", "") == "POSE" and not camera_only:
        if not channel_paths <= {"location", "rotation_euler", "rotation_quaternion", "scale"}:
            return []
        selected = list(getattr(context, "selected_pose_bones", None) or [])
        active = getattr(context, "active_pose_bone", None)
        ordered = stagger_apply.ordered_active_first(
            selected or ([active] if active is not None else []),
            active,
            reverse=reverse,
        )
        return [(context.active_object, bone) for bone in ordered]

    selected = list(getattr(context, "selected_objects", None) or [])
    if camera_only:
        selected = [obj for obj in selected if getattr(obj, "type", "") == "CAMERA"]
    active = context.active_object if context.active_object in selected else None
    ordered = stagger_apply.ordered_active_first(
        selected or ([active] if active is not None else []),
        active,
        reverse=reverse,
    )
    return [(obj, None) for obj in ordered]


TRANSFORM_DRIVER_PATHS = (
    "location",
    "rotation_euler",
    "rotation_quaternion",
    "rotation_axis_angle",
    "scale",
)
DELTA_TRANSFORM_DRIVER_PATHS = (
    "delta_location",
    "delta_rotation_euler",
    "delta_rotation_quaternion",
    "delta_scale",
)


def _transform_clear_targets(context):
    """Selected pose bones in Pose Mode, otherwise selected objects.

    Deliberately independent of the selected template, unlike
    ``_offset_target_specs``: this clears whatever is actually on the selection,
    including drivers Driver Espresso never created.
    """
    if getattr(context, "mode", "") == "POSE":
        armature = getattr(context, "active_object", None)
        if armature is None:
            return []
        bones = list(getattr(context, "selected_pose_bones", None) or [])
        if not bones:
            active_bone = getattr(context, "active_pose_bone", None)
            bones = [active_bone] if active_bone is not None else []
        return [(armature, bone) for bone in bones]
    return [(obj, None) for obj in (getattr(context, "selected_objects", None) or [])]


class ESPRESSO_OT_clear_transform_drivers(bpy.types.Operator):
    bl_idname = "espresso.clear_transform_drivers"
    bl_label = "Clear Transform Drivers"
    bl_description = (
        "Remove every driver on the transform channels of the selected objects, or of "
        "the selected bones in Pose Mode. Clears drivers whatever created them. The "
        "transforms keep the values the drivers last set"
    )
    bl_options = {"REGISTER", "UNDO"}

    include_delta: bpy.props.BoolProperty(
        name="Include Delta Transforms",
        description="Also clear drivers on the delta location, rotation, and scale channels",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return bool(_transform_clear_targets(context))

    def execute(self, context):
        targets = _transform_clear_targets(context)
        if not targets:
            self.report({"WARNING"}, "Select an object, or bones in Pose Mode.")
            return {"CANCELLED"}

        paths = list(TRANSFORM_DRIVER_PATHS)
        if self.include_delta:
            paths += list(DELTA_TRANSFORM_DRIVER_PATHS)

        removed = 0
        setup_resources = 0
        cleared = []
        props = getattr(context.scene, "espresso_props", None)
        cleanup_records = []
        for owner, bone in targets:
            response_target = bone if bone is not None else owner
            removed_for_owner = response_rig.clear(response_target)
            setup_resources += removed_for_owner
            if removed_for_owner:
                cleared.append(
                    bone.name if bone is not None else owner.name
                )
            animation = getattr(owner, "animation_data", None)
            if animation is None:
                continue
            if bone is None:
                prefix = ""
            else:
                # Bone names may legitimately contain quotes or backslashes, and
                # an unescaped name would silently match nothing at all - the
                # worst failure here, because it looks like "no drivers found".
                prefix = f'pose.bones["{bpy.utils.escape_identifier(bone.name)}"].'
            wanted = {prefix + path for path in paths}
            # Materialise the list before removing: mutating the collection
            # while iterating it skips entries.
            doomed = [curve for curve in animation.drivers if curve.data_path in wanted]
            if not doomed:
                continue
            cleanup_records.append(
                target_memory.capture_cleanup_for_fcurves(doomed, props)
            )
            for curve in doomed:
                animation.drivers.remove(curve)
                removed += 1
            clear_name = bone.name if bone is not None else owner.name
            if clear_name not in cleared:
                cleared.append(clear_name)
            owner.update_tag()

        if not removed and not setup_resources:
            self.report({"INFO"}, "No transform drivers on the selection.")
            return {"CANCELLED"}

        for captured in cleanup_records:
            # The F-Curves are already gone, but captured descriptors still own
            # the helper/controller resources that fed them.
            target_memory.cleanup_captured_resources(captured, context.scene, props)
        latest = target_memory.latest_entry(props) if props is not None else None
        if latest and any(
            target_memory.entry_overlaps_targets(
                latest, captured.get("targets", []),
            )
            for captured in cleanup_records
        ):
            target_memory.clear_latest_entry(props)

        where = ", ".join(cleared[:3])
        if len(cleared) > 3:
            where += f" +{len(cleared) - 3} more"
        self.report(
            {"INFO"},
            f"Cleared {removed} transform driver{'' if removed == 1 else 's'}"
            + (f" and {setup_resources} response resource(s)" if setup_resources else "")
            + f" on {where}.",
        )
        return {"FINISHED"}


class ESPRESSO_OT_apply_motion_with_offset(bpy.types.Operator):
    bl_idname = "espresso.apply_motion_with_offset"
    bl_label = "Apply as Sequence"
    bl_description = "Apply the complete motion plan to an ordered selection with art-directable frame delays"
    bl_options = {"REGISTER", "UNDO"}

    start_offset: bpy.props.IntProperty(
        name="Start offset",
        description="Frame offset used by the active or first target",
        default=0,
        soft_min=-10000,
        soft_max=10000,
    )
    offset_step: bpy.props.IntProperty(
        name="Offset step",
        description="Additional frames added for each following target",
        default=6,
        soft_min=-1000,
        soft_max=1000,
    )
    reverse_order: bpy.props.BoolProperty(
        name="Reverse order",
        description="Reverse Active First + Name order before assigning offsets",
        default=False,
    )
    order_mode: bpy.props.EnumProperty(
        name="Order",
        items=(
            ("ACTIVE_NAME", "Active + Name", "Active target first, then the remaining names"),
            ("NAME", "Name", "Alphabetical order"),
            ("X", "X Position", "Left to right in world X"),
            ("Y", "Y Position", "Low to high in world Y"),
            ("Z", "Z Position", "Low to high in world Z"),
            ("DISTANCE", "Distance", "Nearest to farthest from the world origin"),
            ("RANDOM", "Random", "Deterministic random order controlled by Seed"),
        ),
        default="ACTIVE_NAME",
    )
    direction_mode: bpy.props.EnumProperty(
        name="Direction",
        items=(
            ("FORWARD", "Forward", "Use the resolved order"),
            ("REVERSE", "Reverse", "Reverse the resolved order"),
            ("ALTERNATING", "Alternating", "Take alternating members from the ordered selection"),
            ("CENTRE_OUT", "Centre Out", "Start at the middle and move toward both ends"),
        ),
        default="FORWARD",
    )
    distribution_mode: bpy.props.EnumProperty(
        name="Distribution",
        items=(
            ("EVEN", "Even", "Use the same delay between each target"),
            ("ACCELERATE", "Accelerating", "Increase the gaps as the sequence progresses"),
            ("DECELERATE", "Decelerating", "Reduce the gaps as the sequence progresses"),
            ("DISTANCE", "By Distance", "Scale delays by world-space distance from the first target"),
        ),
        default="EVEN",
    )
    seed: bpy.props.IntProperty(
        name="Seed",
        description="Repeatable order seed used by Random",
        default=0,
    )

    def _plan(self, context, template):
        specs = _offset_target_specs(context, template)
        targets = []
        for obj, bone in specs:
            if bone is not None:
                position = tuple(obj.matrix_world @ bone.head)
                label = f"{obj.name} > {bone.name}"
            else:
                position = tuple(obj.matrix_world.translation)
                label = obj.name
            targets.append({"item": (obj, bone), "label": label, "position": position})
        direction = "REVERSE" if self.reverse_order else self.direction_mode
        order = "SELECTION" if self.order_mode == "ACTIVE_NAME" else self.order_mode
        return stagger_apply.sequence_plan(
            targets,
            order=order,
            direction=direction,
            distribution=self.distribution_mode,
            start_offset=self.start_offset,
            offset_step=self.offset_step,
            seed=self.seed,
        )

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None or not props.is_valid or context.active_object is None:
            return False
        template = espresso_props.get_current_template(props)
        if not templates.has_motion_plan(template):
            return False
        return any(
            "frame" in channel.get("expression", "")
            for channel in templates.template_channels(template)
        )

    def invoke(self, context, event):
        specs = _offset_target_specs(context, espresso_props.get_current_template(context.scene.espresso_props))
        if not specs:
            self.report({"WARNING"}, "Select at least one compatible object or pose bone.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=430)

    def draw(self, context):
        layout = self.layout
        template = espresso_props.get_current_template(context.scene.espresso_props)
        plan = self._plan(context, template)
        header = layout.row()
        header.label(text=f"Targets: {len(plan)}", icon="RESTRICT_SELECT_OFF")
        layout.prop(self, "order_mode")
        layout.prop(self, "direction_mode")
        layout.prop(self, "start_offset")
        layout.prop(self, "offset_step")
        layout.prop(self, "distribution_mode")
        if self.order_mode == "RANDOM":
            layout.prop(self, "seed")
        preview = layout.box()
        preview.label(text="Preview", icon="SEQ_PREVIEW")
        for line in stagger_apply.preview_lines(plan, limit=8):
            preview.label(text=line)

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        plan = self._plan(context, template)
        if not plan:
            self.report({"WARNING"}, "No compatible selected targets remain.")
            return {"CANCELLED"}

        base_channels = espresso_props.built_channel_previews(props)
        prepared = []
        for step in plan:
            obj, bone = step.item
            offset = step.offset
            shifted = stagger_apply.shift_built_channels(base_channels, offset)
            result = motion_channels.prepare_motion_template_application(
                obj,
                template,
                shifted,
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
                pose_bone=bone,
                enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(props, template),
                template_values={
                    **espresso_props.collect_values(props, template),
                    **(
                        {"START": espresso_props.collect_values(props, template).get("START", 1) + offset}
                        if template.get("internal_helpers") and any(
                            item.get("token") == "START" for item in template.get("params", [])
                        )
                        else {}
                    ),
                },
            )
            if not result.ok:
                target_label = f"{obj.name} > {bone.name}" if bone else obj.name
                self.report({"WARNING"}, f"{target_label}: {result.message}")
                return {"CANCELLED"}
            prepared.append(result)

        applied = 0
        remembered_targets = []
        remembered_states = []
        for result in prepared:
            committed = motion_channels.commit_prepared_motion(result)
            if not committed.ok:
                self.report({"WARNING"}, committed.message)
                return {"CANCELLED"}
            applied += committed.applied_count
            remembered_targets.extend(committed.targets)
            remembered_states.extend(committed.target_states)

        target_memory.remember_targets(
            context,
            remembered_targets,
            f"{len(plan)} targets > {template['name']} > {self.distribution_mode.title()} sequence",
            "motion_offset",
            template["id"],
            template["name"],
            target_states=remembered_states,
        )
        names = []
        for step in plan:
            obj, bone = step.item
            names.append(f"{obj.name} > {bone.name}" if bone else obj.name)
        named = ", ".join(names[:3])
        if len(names) > 3:
            named += f", +{len(names) - 3} more"
        espresso_props.set_last_apply_status(
            props,
            f"Applied {template['name']} as sequence to {len(plan)} targets ({named})",
        )
        self.report(
            {"INFO"},
            f"Applied {template['name']} as a sequence to {len(plan)} targets ({applied} drivers).",
        )
        return {"FINISHED"}


class ESPRESSO_OT_copy_driver(bpy.types.Operator):
    bl_idname = "espresso.copy_driver"
    bl_label = "Copy Driver"
    bl_description = "Copy the current Espresso expression for pasting from the Driver Espresso right-click menu"

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "espresso_props", None)
        if not (props and props.is_valid):
            return False
        template = espresso_props.get_current_template(props)
        if templates.has_motion_plan(template):
            return False
        if audio_reactivity.application_block_message(template, context.scene):
            return False
        if source_binding.template_needs_source(template) and not source_binding.latest_source(props):
            return False
        return True

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        props.copied_driver_expression = props.preview
        props.copied_driver_template = template["id"]
        props.copied_driver_label = template["name"]
        props.copied_driver_rest_mode = espresso_props.effective_rest_start_mode(props)
        props.copied_driver_source = props.espresso_input_source if source_binding.template_needs_source(template) else "{}"
        props.copied_driver_output_baseline = props.preview_output_baseline
        props.copied_driver_parameters = json.dumps(
            target_memory.json_safe_values(espresso_props.collect_values(props, template)),
            sort_keys=True,
        )
        props.copied_driver_scope = application_plan.template_scope(template)
        props.copied_driver_channel_plan = json.dumps([
            {
                "id": str(channel.get("id") or channel.get("channel_id") or "main"),
                "data_path": str(channel.get("data_path") or ""),
                "index": int(channel.get("index", -1)),
                "expression": props.preview,
            }
            for channel in (template.get("channels") or ({"id": "main"},))
        ], sort_keys=True)
        self.report({"INFO"}, f"Copied Espresso driver: {template['name']}. Right-click a value and choose Paste Espresso Driver.")
        return {"FINISHED"}


class ESPRESSO_OT_apply(bpy.types.Operator):
    bl_idname = "espresso.apply_expression"
    bl_label = "Apply to Selected Driver"
    bl_description = "Apply the expression to the active driver target (or a specific driver when invoked from a Driver Target list row)"
    bl_options = {"REGISTER", "UNDO"}

    # Optional row-level target: when set (from a Driver Target list row),
    # this driver becomes the active target before applying. Left empty for
    # the legacy call sites that rely purely on get_target_driver_fcurve's
    # existing resolution (Drivers-Editor selection or an already-set pick).
    # Callers that set data_path must also set array_index for that same
    # driver (-1 for a scalar property, or the real index for an array
    # property) — the two are matched together by get_target_driver_fcurve.
    data_path: bpy.props.StringProperty(default="")
    array_index: bpy.props.IntProperty(default=-1)
    target_id_type: bpy.props.StringProperty(default="")
    target_id_name: bpy.props.StringProperty(default="")
    target_owner_path: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        # Row-agnostic by design: poll() is a classmethod with no access to
        # this not-yet-invoked instance's data_path, so it can only check
        # whatever is currently the resolved active target — not the specific
        # row a given button represents. Per-row enable/disable is handled by
        # the panel setting UILayout.enabled directly, not by poll().
        props = getattr(context.scene, "espresso_props", None)
        if props is None or not props.is_valid:
            return False
        fcurve, driver, _reason = utils.get_target_driver_fcurve(context)
        if fcurve is None or driver is None:
            return False
        template = espresso_props.get_current_template(props)
        if templates.has_motion_plan(template):
            return False
        missing = missing_required_variables(driver, template)
        return not missing or bool(source_binding.latest_source(props))

    def execute(self, context):
        props = context.scene.espresso_props
        if self.data_path and self.target_id_type:
            utils.set_active_driver_target(context, {
                "id_type": self.target_id_type, "id_name": self.target_id_name,
                "owner_path": self.target_owner_path, "data_path": self.data_path,
                "index": self.array_index,
            })
        elif self.data_path:
            # Record this row's driver as the new active pick before resolving.
            utils.set_active_driver_target(context, context.active_object, self.data_path, self.array_index)
        template = espresso_props.get_current_template(props)
        fcurve, driver, reason = utils.get_target_driver_fcurve(context)
        if driver is None:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        target = _graph_target(fcurve.id_data, fcurve.data_path, fcurve.array_index)
        audio_expression = audio_reactivity.prepare_expression(
            template, props.preview, context.scene, props.preview_output_baseline,
        )
        rest_state = apply_behavior.capture_target_rest_state(
            target,
            audio_expression,
            template,
            context.scene,
            espresso_props.effective_rest_start_mode(props),
            output_baseline=props.preview_output_baseline,
        )
        wrapped_expression = utils.wrap_expression_with_rest_state(audio_expression, template, rest_state)
        ok, message = apply_expression_to_driver(
            driver,
            wrapped_expression,
            template,
            source_binding.latest_source(props),
        )
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        graph_target = target
        label = f"{getattr(fcurve.id_data, 'name', 'Driver')} > {fcurve.data_path}"
        if fcurve.array_index >= 0:
            label += f"[{fcurve.array_index}]"
        target_memory.remember_targets(
            context,
            [graph_target],
            label,
            "graph",
            template["id"],
            template["name"],
            target_states=[rest_state],
        )
        espresso_props.set_last_apply_status(
            props,
            f"Remembered: {target_memory.latest_label(props) or 'target'}",
        )
        self.report({"INFO"}, "Driver expression applied.")
        return {"FINISHED"}


def _entry_motion_context(entry):
    """Resolve the Object (and pose bone) a remembered motion entry was applied to.

    Motion templates drive several channels at once, so re-applying cannot mean
    "push one expression at every remembered target" the way it does for a single
    scalar. It means "run the whole motion plan against that object again", which
    is exactly what the normal apply path already does correctly.
    """
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return None, None, reason or "The remembered target no longer exists."
    owner = resolved[0].get("owner")
    obj = owner if isinstance(owner, bpy.types.Object) else getattr(owner, "id_data", None)
    if not isinstance(obj, bpy.types.Object):
        return None, None, "The remembered target is not on an object."

    # The bone comes from the DATA PATH, not from the owner. A bone's drivers
    # live on the armature Object with the bone named in the path, so the owner
    # is never a PoseBone - testing for one always failed, and Update Last
    # Target re-applied the whole plan to the armature object instead of back
    # to the bone it came from.
    pose_bone = None
    bone_name = target_memory.bone_name_from_path(resolved[0].get("data_path", ""))
    if bone_name:
        pose_bone = obj.pose.bones.get(bone_name) if obj.pose else None
        if pose_bone is None:
            return None, None, 'Bone "%s" is no longer on %s.' % (bone_name, obj.name)
    return obj, pose_bone, ""


def _reapply_channel_plan_to_entry(context, props, template, entry):
    """Re-push each channel's own expression at its remembered index.

    The motion route means "run the whole plan against that object again", which
    needs an Object. A channel plan applied by right-clicking a shader socket has
    no object - the owner is a node socket - so that route reported "the
    remembered target is not on an object" and gave up, which is what made Update
    Last Target look broken on colours.

    Here the remembered targets already say exactly where each channel went, so
    re-applying is just matching them back up by array index.
    """
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return False, reason or "The remembered target no longer exists."

    from ...apply import driver_manager

    for target in resolved:
        channel, reason = target_memory.canonical_driver_channel(target)
        if channel is None:
            return False, reason
        foreign = getattr(
            driver_manager, "foreign_live_motion_for_channel", lambda *_: None,
        )(
            channel["owner"], channel["data_path"], channel["index"],
        )
        if foreign:
            return False, "A remembered channel belongs to an unavailable edition."

    channels = templates.template_channels(template)
    by_index = {}
    for channel, built in zip(channels, espresso_props.built_channel_previews(props)):
        expression = (built or {}).get("expression")
        if expression:
            by_index[int(channel.get("index", 0))] = (expression, channel)
    if not by_index:
        return False, "The template has no built channel expressions."

    written = 0
    for target in resolved:
        owner = target.get("owner")
        data_path = target.get("data_path")
        index = int(target.get("index", -1))
        plan = by_index.get(index)
        if owner is None or not data_path or plan is None:
            continue
        expression, channel = plan
        try:
            result = owner.driver_add(data_path, index)
        except Exception as exc:  # noqa: BLE001 - report the property, not a traceback
            return False, f"Could not drive {channel.get('label', index)}: {exc}"
        for fcurve in (result if isinstance(result, list) else [result]):
            if fcurve is None:
                continue
            ok, message = utils.assign_driver_expression(
                fcurve.driver, expression, template, context.scene,
            )
            if not ok:
                return False, message
            written += 1
    if not written:
        return False, "None of the remembered channels could be matched."
    return True, f"Updated {written} channel driver{'s' if written != 1 else ''}."


class ESPRESSO_OT_update_last_target(bpy.types.Operator):
    bl_idname = "espresso.update_last_target"
    bl_label = "Update Last Target"
    bl_description = "Reapply the current Driver Espresso expression to the most recently remembered target"
    bl_options = {"REGISTER", "UNDO"}

    confirm_scope_conversion: bpy.props.BoolProperty(default=False, options={"HIDDEN"})
    destination_channel: bpy.props.IntProperty(
        name="Destination Channel", min=0, default=0,
        description="Remembered motion channel that receives the single-property recipe",
    )
    clear_remembered_motion_channels: bpy.props.BoolProperty(
        name="Clear remembered motion channels",
        description="Remove the other channels owned by the remembered Motion Set after conversion",
        default=True,
    )

    def _transition(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        entry = espresso_props.live_or_latest_entry(context, template)
        plan = application_plan.from_entry(entry, None)
        requested = application_plan.template_scope(
            template, target_count=len(plan.targets),
        )
        return application_plan.scope_transition(plan.scope, requested), entry, template

    def invoke(self, context, _event):
        transition, entry, _template = self._transition(context)
        if transition == application_plan.SAME_SCOPE:
            return self.execute(context)
        self.confirm_scope_conversion = True
        self.destination_channel = min(
            self.destination_channel, max(0, len((entry or {}).get("targets") or ()) - 1),
        )
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        transition, entry, template = self._transition(context)
        layout = self.layout
        if transition == application_plan.MOTION_TO_SINGLE:
            layout.label(text="Convert remembered Motion Set to one property?", icon="ERROR")
            targets = list((entry or {}).get("targets") or ())
            labels = []
            channels = {str(item.get("id") or item.get("channel_id") or ""): item for item in (template.get("channels") or ())}
            for index, target in enumerate(targets):
                channel_id = str(target.get("channel_id") or "")
                channel = channels.get(channel_id) or {}
                label = str(channel.get("label") or channel_id or target.get("data_path") or "Channel")
                labels.append(f"{index}: {label}")
            if labels:
                box = layout.box()
                for label in labels:
                    box.label(text=label)
            layout.prop(self, "destination_channel")
            layout.prop(self, "clear_remembered_motion_channels")
        else:
            layout.label(text="Convert the remembered property into a Motion Set?", icon="ERROR")
            layout.label(text="The complete channel plan will be applied to its owning Object or Pose Bone.")

    def _preflight(self, context, entry, template):
        plan = application_plan.from_entry(entry, None)
        obj, _pose_bone, _reason = _entry_motion_context(entry)
        source = source_binding.latest_source(context.scene.espresso_props)
        status = (
            source_binding.source_status(source)
            if source_binding.needs_input_source(template)
            else {"code": source_binding.SOURCE_ACTIVE}
        )
        return application_plan.preflight_transition(
            plan, template, source_status=status,
            confirmed=bool(self.confirm_scope_conversion),
            has_object_target=obj is not None,
        )

    @classmethod
    def description(cls, context, _properties):
        """Carry the remembered-target details in the hover tip.

        A dedicated (i) button would spend a slot in a crowded row on something
        the artist only ever wants to read, never to trigger. A tooltip is
        free: it costs no button and appears exactly when someone is already
        looking at this control.
        """
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        try:
            lines = target_memory.latest_info_lines(props)
        except Exception:  # noqa: BLE001 - a tooltip must never break the UI
            return cls.bl_description
        if not lines:
            return cls.bl_description
        return cls.bl_description + ".\n\n" + "\n".join(lines)

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return bool(props and props.is_valid and target_memory.latest_entry(props))

    def execute(self, context):
        # Re-apply from the frame the motion was FIRST applied on, not from
        # wherever the playhead happens to be now.
        #
        # Rest Start captures the target's pose at the current frame and treats
        # it as the rest state, so updating from frame 50 folded the motion's
        # own frame-50 value into the baseline: every frame shifted by that
        # amount, and doing it twice shifted it twice. The artist tweaks one
        # parameter, clicks Update, and the whole curve jumps.
        #
        # The entry has recorded applied_frame all along; nothing was reading
        # it here. Restored in a finally block so an early return or a raising
        # apply cannot strand the playhead somewhere the artist did not put it.
        scene = context.scene
        entry_for_frame = espresso_props.live_or_latest_entry(context)
        applied_frame = (entry_for_frame or {}).get("applied_frame")
        restore_frame = scene.frame_current
        moved = False
        if isinstance(applied_frame, (int, float)) and int(applied_frame) != restore_frame:
            scene.frame_set(int(applied_frame))
            moved = True
        try:
            return self._execute(context)
        finally:
            if moved:
                scene.frame_set(restore_frame)

    def _execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        entry = espresso_props.live_or_latest_entry(context, template)
        transition, report = self._preflight(context, entry, template)
        if not report.ok:
            message = report.errors[0]
            espresso_props.set_last_apply_status(props, message)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        from ..state import live_controls

        resolved_targets, _reason = target_memory.resolve_entry(entry)
        bound_keys = live_controls.read_bindings(props)
        has_controller = False
        for target in resolved_targets or ():
            channel, reason = target_memory.canonical_driver_channel(target)
            if channel is None:
                espresso_props.set_last_apply_status(props, reason)
                self.report({"WARNING"}, reason)
                return {"CANCELLED"}
            key = live_controls._target_key(target_memory.serialize_target(
                channel["owner"], channel["data_path"], channel["index"],
            ))
            fcurve = target_memory._find_owner_driver(
                channel["owner"], channel["data_path"], channel["index"],
            )
            if target_memory._controller_binding_issue(
                fcurve, bound_keys.get(key), binding_present=key in bound_keys,
            ):
                message = "A Controller binding is missing or damaged; the motion was left unchanged."
                espresso_props.set_last_apply_status(props, message)
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            has_controller = has_controller or key in bound_keys
        if has_controller and transition != application_plan.SAME_SCOPE:
            message = "Remove the attached Controller before changing this target's scope."
            espresso_props.set_last_apply_status(props, message)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        if transition == application_plan.MOTION_TO_SINGLE:
            ok, message, converted = target_memory.convert_motion_entry_to_single(
                entry,
                min(self.destination_channel, len(entry.get("targets") or ()) - 1),
                audio_reactivity.prepare_expression(
                    template, props.preview, context.scene, props.preview_output_baseline,
                ),
                template,
                source_entry=source_binding.latest_source(props),
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
                output_baseline=props.preview_output_baseline,
                validation_template=audio_reactivity.validation_template(template, context.scene),
                clear_other_channels=self.clear_remembered_motion_channels,
            )
            espresso_props.set_last_apply_status(props, message)
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            target_memory.remember_entry(context, converted)
            self.report({"INFO"}, message)
            return {"FINISHED"}

        if (entry or {}).get("apply_kind") == "node_route":
            binding = parameter_bindings.binding_for_entry(context, template, entry)
            ok, message = parameter_bindings.write_values(
                binding,
                espresso_props.collect_values(props, template),
                scene=context.scene,
                template=template,
            )
            espresso_props.set_last_apply_status(props, message)
            if ok:
                self.report({"INFO"}, message)
                return {"FINISHED"}
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        # A motion template has one expression PER CHANNEL, so pushing
        # props.preview at every remembered target would write the first
        # channel's formula onto all of them. Re-run the motion plan instead.
        if templates.has_motion_plan(template):
            if has_controller:
                if template.get("internal_helpers") or template.get("sample_internal_helpers"):
                    message = "This helper-based motion cannot be rebuilt under an attached Controller."
                    espresso_props.set_last_apply_status(props, message)
                    self.report({"WARNING"}, message)
                    return {"CANCELLED"}
                values = espresso_props.collect_values(props, template)
                built = utils.build_template_expressions(template, values, context.scene)
                matched, note = motion_channels.channels_for_stored_targets(
                    built, template, context.scene, (entry or {}).get("targets") or (),
                    values=values,
                    enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(
                        props, template,
                    ),
                )
                if matched is None:
                    espresso_props.set_last_apply_status(props, note)
                    self.report({"WARNING"}, note)
                    return {"CANCELLED"}
                ok, message = target_memory.apply_expression_to_entry(
                    entry, [item["expression"] for item in matched], template,
                    source_entry=source_binding.latest_source(props),
                    scene=context.scene,
                    rest_start_mode=espresso_props.effective_rest_start_mode(props),
                    output_baseline=[item.get("output_baseline") for item in matched],
                    validation_template=audio_reactivity.validation_template(
                        template, context.scene,
                    ),
                    prepare_driver=lambda driver: audio_reactivity.prepare_driver(
                        driver, template, context.scene,
                    ),
                )
                espresso_props.set_last_apply_status(props, message)
                if not ok:
                    self.report({"WARNING"}, message)
                    return {"CANCELLED"}
                entry["parameter_values"] = target_memory.json_safe_values(values)
                target_memory.remember_entry(context, entry)
                self.report({"INFO"}, message if not note else f"{message} {note}")
                return {"FINISHED"}
            obj, pose_bone, reason = _entry_motion_context(entry)
            if obj is None:
                # No object behind the remembered targets - a shader socket, say.
                # The plan still knows which index each channel went to, so push
                # them straight back rather than declaring the target lost.
                ok, message = _reapply_channel_plan_to_entry(context, props, template, entry)
                espresso_props.set_last_apply_status(props, message)
                if ok:
                    self.report({"INFO"}, message)
                    return {"FINISHED"}
                target_memory.clear_latest_entry(props)
                message = f"{reason} Apply the driver again to set a new target."
                espresso_props.set_last_apply_status(props, message)
                self.report({"WARNING"}, message)
                return {"FINISHED"}
            if camera_application.requires_camera_object(template):
                result = camera_application.apply_camera_template(
                    obj, template, espresso_props.built_channel_previews(props),
                    scene=context.scene,
                    rest_start_mode=espresso_props.effective_rest_start_mode(props),
                )
            else:
                result = motion_channels.apply_motion_template_to_object(
                    obj, template, espresso_props.built_channel_previews(props),
                    scene=context.scene,
                    rest_start_mode=espresso_props.effective_rest_start_mode(props),
                    pose_bone=pose_bone,
                    enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(props, template),
                    template_values=espresso_props.collect_values(props, template),
                )
            espresso_props.set_last_apply_status(props, result.message)
            if not result.ok:
                self.report({"WARNING"}, result.message)
                return {"CANCELLED"}
            label = f"{obj.name} > {template['name']}"
            target_memory.remember_targets(
                context, result.targets, label,
                "camera" if camera_application.requires_camera_object(template) else "motion",
                template["id"], template["name"], target_states=result.target_states,
            )
            self.report({"INFO"}, result.message)
            return {"FINISHED"}

        ok, message = target_memory.apply_expression_to_entry(
            entry,
            audio_reactivity.prepare_expression(
                template, props.preview, context.scene, props.preview_output_baseline,
            ),
            template,
            source_entry=source_binding.latest_source(props),
            scene=context.scene,
            rest_start_mode=espresso_props.effective_rest_start_mode(props),
            output_baseline=props.preview_output_baseline,
            validation_template=audio_reactivity.validation_template(template, context.scene),
            prepare_driver=lambda driver: audio_reactivity.prepare_driver(
                driver, template, context.scene,
            ),
        )
        if not ok:
            espresso_props.set_last_apply_status(props, message)
            if "no longer exists" in message:
                # Return FINISHED (not CANCELLED) so Blender's undo system commits
                # the clear — CANCELLED rolls back all data changes made in execute,
                # which would restore the stale entry and loop the error forever.
                target_memory.clear_latest_entry(props)
                message += " Apply the driver again to set a new target."
                espresso_props.set_last_apply_status(props, message)
                self.report({"WARNING"}, message)
                return {"FINISHED"}
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        target_memory.remember_entry(context, entry)
        espresso_props.set_last_apply_status(
            props,
            f"{message} {target_memory.latest_label(props)}".strip(),
        )
        self.report({"INFO"}, message)
        return {"FINISHED"}


# There is no operator for the last-target details:
# target_memory.latest_info_lines() is shown in the Update Last Target hover
# tip instead. Information the artist only reads belongs in a tooltip or the
# Help section, not in a button that spends a slot in a crowded row.


class ESPRESSO_OT_switch_variant(bpy.types.Operator):
    bl_idname = "espresso.switch_variant"
    bl_label = "Switch Variant"
    bl_description = "Switch to a related template and remember current values"

    variant_id: bpy.props.StringProperty()

    @classmethod
    def description(cls, _context, properties):
        variant = templates.TEMPLATE_BY_ID.get(getattr(properties, "variant_id", ""))
        if not variant:
            return cls.bl_description
        parts = [f"Switch to {variant['name']}."]
        if variant.get("description"):
            parts.append(variant["description"])
        if variant.get("use_cases"):
            parts.append("Use cases: " + variant["use_cases"])
        return " ".join(parts)

    def execute(self, context):
        props = context.scene.espresso_props
        current = espresso_props.get_current_template(props)
        memory = utils.read_variant_memory(props)
        memory[current["id"]] = espresso_props.collect_values(props, current)
        utils.write_variant_memory(props, memory)

        if not espresso_props.apply_template_selection(props, self.variant_id):
            self.report({"WARNING"}, "Unknown variant.")
            return {"CANCELLED"}
        return {"FINISHED"}


class ESPRESSO_OT_clear_input_source(bpy.types.Operator):
    bl_idname = "espresso.clear_input_source"
    bl_label = "Clear Espresso Input"
    bl_description = "Forget the property currently used as the Espresso input source"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return bool(props and source_binding.latest_source(props))

    def execute(self, context):
        source_binding.clear_source(context.scene.espresso_props)
        self.report({"INFO"}, "Espresso input source cleared.")
        return {"FINISHED"}


class ESPRESSO_OT_reload_live_parameters(bpy.types.Operator):
    bl_idname = "espresso.reload_live_parameters"
    bl_label = "Reload Live Parameters"
    bl_description = "Load the active object's generated setup into the Live controls"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        ok, message = espresso_props.load_live_parameters(props, context, template)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        espresso_props.refresh_preview(props, context)
        self.report({"INFO"}, "Live parameters reloaded for the active setup.")
        return {"FINISHED"}


class ESPRESSO_OT_select_live_effect(bpy.types.Operator):
    bl_idname = "espresso.select_live_effect"
    bl_label = "Select Applied Effect"
    bl_description = "Edit this applied effect with the Live settings"
    bl_options = {"INTERNAL"}

    record_token: bpy.props.StringProperty(default="")

    def execute(self, context):
        from ...apply.motion import applied_motion_manager

        props = context.scene.espresso_props
        source = getattr(props, "driver_target_source", "ACTIVE")
        effect = applied_motion_manager.find_effect(context, self.record_token, source)
        if effect is None:
            self.report({"WARNING"}, "The applied effect no longer resolves.")
            return {"CANCELLED"}
        ok, message = espresso_props.select_live_effect(props, context, effect)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, "Editing %s." % (effect.get("label") or "the applied effect"))
        return {"FINISHED"}


def _parameter_operator_template(context):
    props = context.scene.espresso_props
    return espresso_props.live_parameter_template(
        context, espresso_props.get_current_template(props),
    )


class ESPRESSO_OT_set_master_preset(bpy.types.Operator):
    bl_idname = "espresso.set_master_preset"
    bl_label = "Set Master Preset"
    bl_description = "Apply a Master Preset that sets several parameters together to reproduce one named feel in a single click"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: bpy.props.IntProperty()

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        template = _parameter_operator_template(context)
        presets = template.get("combined_presets", [])
        index = getattr(properties, "preset_index", -1)
        if not 0 <= index < len(presets):
            return cls.bl_description
        preset = presets[index]
        pairs = ", ".join(f"{k}={v}" for k, v in preset["values"].items())
        return f"Set {preset['label']}: {pairs}."

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        presets = template.get("combined_presets", [])
        if not 0 <= self.preset_index < len(presets):
            return {"CANCELLED"}
        espresso_props.apply_combined_preset(props, template, presets[self.preset_index]["values"])
        if getattr(props, "parameter_mode", "SETUP") == "LIVE":
            ok, message = espresso_props.sync_live_parameters(props, context, template)
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
        espresso_props.refresh_preview(props, context)
        self._track_usage(context, template["id"], presets[self.preset_index]["label"])
        return {"FINISHED"}

    @staticmethod
    def _track_usage(context, template_id, preset_label):
        from ... import config
        config.record_preset_click(template_id, preset_label)


class ESPRESSO_OT_set_param_preset(bpy.types.Operator):
    bl_idname = "espresso.set_param_preset"
    bl_label = "Set Preset"
    bl_description = "Set a parameter to a preset value"
    bl_options = {"REGISTER", "UNDO"}

    slot_index: bpy.props.IntProperty()
    value: bpy.props.FloatProperty()
    # Passed by the preset menu purely for usage tracking. Matching the value
    # back to a preset is unreliable — FloatProperty is 32-bit, so 0.7 arrives
    # as 0.69999998 and never compares equal to the catalogue's double.
    preset_label: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        index = getattr(properties, "slot_index", -1)
        if not 0 <= index < len(params):
            return cls.bl_description
        param = params[index]
        return f"Set {param['label']} to preset value {getattr(properties, 'value', 0)}.\n{templates.format_param_tooltip(param)}"

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        if not 0 <= self.slot_index < len(params):
            return {"CANCELLED"}
        espresso_props.set_param_value(props, params[self.slot_index], self.slot_index, self.value)
        espresso_props.refresh_preview(props, context)
        if self.preset_label:
            from ... import config
            config.record_param_preset_click(template["id"], self.slot_index, self.preset_label)
        return {"FINISHED"}


class ESPRESSO_OT_reset_param_default(bpy.types.Operator):
    bl_idname = "espresso.reset_param_default"
    bl_label = "Reset Parameter"
    bl_description = "Reset this parameter to the selected template's default value"
    bl_options = {"REGISTER", "UNDO"}

    slot_index: bpy.props.IntProperty()

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        index = getattr(properties, "slot_index", -1)
        if not 0 <= index < len(params):
            return cls.bl_description
        param = params[index]
        return f"Reset {param['label']} to its template default.\n{templates.format_param_tooltip(param)}"

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        if not 0 <= self.slot_index < len(params):
            return {"CANCELLED"}
        param = params[self.slot_index]
        espresso_props.set_param_value(props, param, self.slot_index, param.get("default", 0))
        espresso_props.refresh_preview(props, context)
        self.report({"INFO"}, f"Reset {param['label']} to template default.")
        return {"FINISHED"}


class ESPRESSO_OT_reset_template_defaults(bpy.types.Operator):
    bl_idname = "espresso.reset_template_defaults"
    bl_label = "Reset Template Defaults"
    bl_description = (
        "Reset all parameters in the current template to their original "
        "template defaults. Alt-click to reset every part of this channel "
        "set (Torso, Hips, Chest, ...) at once, not just the one on screen"
    )
    bl_options = {"REGISTER", "UNDO"}

    all_parts: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.all_parts = event.alt
        return self.execute(context)

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        siblings = templates.channel_siblings(template) if self.all_parts else []
        others = [member for member in siblings if member["id"] != template["id"]]

        # The active template's slots are LIVE properties, so it goes through
        # the normal apply path. A sibling that is not on screen has no live
        # slots to reset - its values live in template memory instead, read
        # back in when the artist switches to it - so resetting it means
        # writing defaults there directly, not touching props at all.
        espresso_props.apply_template_defaults(props, template)
        # A recipe that authors a rig keeps its settings on the OBJECT, not in
        # the template's parameter slots, so apply_template_defaults cannot
        # see them: measured, this button restored 3 of an authoring rig's 43
        # settings and still reported that it had reset the template. The
        # surface key and the authoring kind are the same string, which is
        # what lets this stay general rather than naming an authoring rig here.
        from ..views import settings_reset as _settings_reset
        authored = _settings_reset.reset_all(
            context, template.get("authoring_kind") or "")
        if getattr(props, "parameter_mode", "SETUP") == "LIVE":
            ok, message = espresso_props.sync_live_parameters(props, context, template)
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
        memory = utils.read_template_memory(props)
        if getattr(props, "parameter_mode", "SETUP") != "LIVE":
            memory[template["id"]] = espresso_props.collect_values(props, template)
        for member in others:
            memory[member["id"]] = espresso_props._defaults_for_template(member)
        utils.write_template_memory(props, memory)
        espresso_props.refresh_preview(props, context)

        if others:
            self.report(
                {"INFO"},
                "Reset all %d parts of %s to template defaults."
                % (len(others) + 1, template["name"]),
            )
        else:
            if authored:
                self.report(
                    {"INFO"},
                    "Reset %s to template defaults, including %d flight "
                    "settings." % (template["name"], len(authored)),
                )
            else:
                self.report({"INFO"}, f"Reset {template['name']} parameters to template defaults.")
        return {"FINISHED"}


class ESPRESSO_OT_param_help(bpy.types.Operator):
    bl_idname = "espresso.param_help"
    bl_label = "Parameter Help"
    bl_description = "Show template-specific help for this parameter"

    slot_index: bpy.props.IntProperty()

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        index = getattr(properties, "slot_index", -1)
        if not 0 <= index < len(params):
            return cls.bl_description
        return templates.format_param_tooltip(params[index])

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        if not 0 <= self.slot_index < len(params):
            return {"CANCELLED"}
        param = params[self.slot_index]
        self.report({"INFO"}, templates.format_param_tooltip(param).replace("\n", " | "))
        return {"FINISHED"}


class ESPRESSO_OT_inspect_param(bpy.types.Operator):
    bl_idname = "espresso.inspect_param"
    bl_label = "Parameter Info"
    bl_description = "Show template-specific help for this parameter"

    slot_index: bpy.props.IntProperty()

    @classmethod
    def description(cls, context, properties):
        return ESPRESSO_OT_param_help.description(context, properties)

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=320)

    def draw(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)
        params = template.get("params", [])
        if not 0 <= self.slot_index < len(params):
            self.layout.label(text="No parameter info available.", icon="INFO")
            return
        param = params[self.slot_index]
        for line in templates.format_param_tooltip(param).split("\n"):
            self.layout.label(text=line, icon="INFO" if line.startswith(param["label"]) else "BLANK1")

    def execute(self, context):
        return {"FINISHED"}


# ESPRESSO_OT_show_template_use_cases was removed here. Its popup printed the
# template's use_cases, which its own description() already returned as the
# tooltip - and which the template dropdown beside it also carries. A popup that
# repeats a tooltip is a click for nothing.


class ESPRESSO_OT_show_template_variants(bpy.types.Operator):
    bl_idname = "espresso.show_template_variants"
    bl_label = "Template Variants"
    bl_description = "Show related variants and paired templates for the current template"

    @classmethod
    def description(cls, context, _properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        template = espresso_props.get_current_template(props)
        variants = [templates.TEMPLATE_BY_ID[v]["name"] for v in template.get("variants", []) if v in templates.TEMPLATE_BY_ID]
        if variants:
            return "Template variants: " + ", ".join(variants[:4])
        return cls.bl_description

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=360)

    def draw(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        layout = self.layout

        variants = [templates.TEMPLATE_BY_ID[v] for v in template.get("variants", []) if v in templates.TEMPLATE_BY_ID]
        if variants:
            layout.label(text="Related variants", icon="GRAPH")
            col = layout.column(align=True)
            for variant in variants:
                op = col.operator("espresso.switch_variant", text=variant["name"])
                op.variant_id = variant["id"]
        else:
            layout.label(text="No related variants for this template.", icon="INFO")

    def execute(self, context):
        return {"FINISHED"}


class ESPRESSO_OT_show_motion_status(bpy.types.Operator):
    bl_idname = "espresso.show_motion_status"
    bl_label = "Motion Status"
    bl_description = "Inspect what the current motion will target, create, and replace"

    template_id: bpy.props.StringProperty(default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=560)

    def draw(self, context):
        from ..views import guided_apply

        props = context.scene.espresso_props
        template = templates.TEMPLATE_BY_ID.get(self.template_id)
        if template is None:
            template = espresso_props.get_current_template(props)
        guided_apply.draw_status_details(self.layout, props, template, context)

    def execute(self, context):
        return {"FINISHED"}


class ESPRESSO_OT_navigate_template(bpy.types.Operator):
    bl_idname = "espresso.navigate_template"
    bl_label = "Navigate Template"
    bl_description = "Step to the previous or next template in the current list"

    direction: bpy.props.IntProperty(default=1)

    def execute(self, context):
        props = context.scene.espresso_props
        pool = (
            templates.search_templates(props.search_text, props.category)
            if props.search_text
            else browse_groups.templates_for_group(
                templates.TEMPLATES,
                props.category,
                props.browse_group,
            )
        )
        # The dropdown carries ONE entry per channel set, so the raw pool holds
        # ids the enum has no item for. Stepping onto one of those raised a
        # TypeError from Blender ("enum not found") instead of moving. Narrow
        # the same way the enum does -- catalogue view first, then the channel
        # collapse -- so the arrows can only land on assignable entries; the
        # channel picker is how the other members are reached. Skipping the
        # camera view left the arrows stepping onto legacy camera records the
        # Definitive shelf hides, which raised the very TypeError above.
        pool = espresso_props._catalogue_stage_filter(props, pool)
        pool = espresso_props._collapse_channel_sets(pool, props.template)
        if len(pool) < 2:
            return {"CANCELLED"}

        # Use template_index to locate current position within the pool
        current_global = templates.template_index(props.template)
        pool_globals = [templates.template_index(t["id"]) for t in pool]
        try:
            pos = pool_globals.index(current_global)
        except ValueError:
            pos = 0

        target = pool[(pos + self.direction) % len(pool)]
        props.template = target["id"]
        return {"FINISHED"}


class ESPRESSO_OT_select_template(bpy.types.Operator):
    bl_idname = "espresso.select_template"
    bl_label = "Select Template"
    bl_description = "Select this template"

    template_id: bpy.props.StringProperty()
    category_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, _context, properties):
        template = templates.TEMPLATE_BY_ID.get(getattr(properties, "template_id", ""))
        if not template:
            return cls.bl_description
        parts = [f"Select template: {template['name']}."]
        if template.get("description"):
            parts.append(template["description"])
        if template.get("use_cases"):
            parts.append("Use cases: " + template["use_cases"])
        return " ".join(parts)

    def execute(self, context):
        props = context.scene.espresso_props
        # `category_name` is deliberately ignored: the search-clear button passes
        # props.category, which goes stale the moment a cross-category search
        # result is selected. apply_template_selection derives it from the template.
        if not espresso_props.apply_template_selection(props, self.template_id):
            self.report({"WARNING"}, f"Unknown template: {self.template_id}")
            return {"CANCELLED"}
        return {"FINISHED"}


class ESPRESSO_OT_clear_search(bpy.types.Operator):
    """Clear the template search box.

    A dedicated operator rather than another use of espresso.select_template.
    The X sits inside the search field, but select_template's description()
    describes the TEMPLATE it would select - so hovering the X explained the
    currently selected template ("Select template: Two-Wave Crossfade Scene
    Loop...") instead of saying what the button does.
    """

    bl_idname = "espresso.clear_search"
    bl_label = "Clear Search"
    bl_description = "Clear the search box and go back to browsing by category"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        props = context.scene.espresso_props
        # Re-selecting the current template is what clears the search, and it
        # also re-scopes the template enum to the category that template really
        # lives in - a bare search_text = "" would leave the enum scoped to the
        # search results it was showing.
        if not espresso_props.apply_template_selection(props, props.template):
            props.search_text = ""
        return {"FINISHED"}


class ESPRESSO_OT_create_variables(bpy.types.Operator):
    bl_idname = "espresso.create_variables"
    bl_label = "Setup Missing Variables"
    bl_description = "Automatically create missing driver variables required by this template"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        fcurve, driver, _reason = utils.get_target_driver_fcurve(context)
        return fcurve is not None and driver is not None

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        fcurve, driver, reason = utils.get_target_driver_fcurve(context)
        if driver is None:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}

        missing = missing_required_variables(driver, template)
        if not missing:
            self.report({"INFO"}, "All required variables already exist.")
            return {"FINISHED"}

        for var_name in missing:
            var_def = next((v for v in template.get("requires_driver_variables", []) if v["name"] == var_name), None)
            if not var_def:
                continue

            var = driver.variables.new()
            var.name = var_name
            var.type = var_def.get("type", "SINGLE_PROP")

            if var.type == "SINGLE_PROP":
                target = var.targets[0]
                id_type = "OBJECT"
                id_data = context.active_object

                if not id_data:
                    id_data = context.scene
                    id_type = "SCENE"

                target.id_type = id_type
                if id_data:
                    target.id = id_data

                target.data_path = f'["{var_name}"]'

                if id_data and var_name not in id_data:
                    id_data[var_name] = var_def.get("preview_default", 0)
                    self.report({"INFO"}, f"Created custom property '{var_name}' on {id_data.name}")

        self.report({"INFO"}, f"Created {len(missing)} missing variable(s).")
        espresso_props.refresh_preview(props, context)
        return {"FINISHED"}




class ESPRESSO_OT_remove_driver(bpy.types.Operator):
    bl_idname = "espresso.remove_driver"
    bl_label = "Remove Selected Driver"
    bl_description = "Remove the driver from the active driver target (or a specific driver when invoked from a Driver Target list row)"
    bl_options = {"REGISTER", "UNDO"}

    # Optional row-level target — same contract as ESPRESSO_OT_apply's
    # data_path/array_index: callers that set data_path must also set
    # array_index for that same driver (-1 for a scalar property, or the
    # real index for an array property).
    data_path: bpy.props.StringProperty(default="")
    array_index: bpy.props.IntProperty(default=-1)
    target_id_type: bpy.props.StringProperty(default="")
    target_id_name: bpy.props.StringProperty(default="")
    target_owner_path: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        # Row-agnostic by design — see ESPRESSO_OT_apply.poll for why.
        fcurve, driver, _reason = utils.get_target_driver_fcurve(context)
        return fcurve is not None and driver is not None

    def execute(self, context):
        if self.data_path and self.target_id_type:
            utils.set_active_driver_target(context, {
                "id_type": self.target_id_type, "id_name": self.target_id_name,
                "owner_path": self.target_owner_path, "data_path": self.data_path,
                "index": self.array_index,
            })
        elif self.data_path:
            # Record this row's driver as the new active pick before resolving.
            utils.set_active_driver_target(context, context.active_object, self.data_path, self.array_index)
        fcurve, driver, reason = utils.get_target_driver_fcurve(context)
        if driver is None:
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        owner = fcurve.id_data
        data_path = fcurve.data_path
        index = fcurve.array_index
        props = getattr(context.scene, "espresso_props", None)

        # A driver belongs to whatever applied it. A row on a rig that owns
        # its own teardown -- an authoring rig, say -- cannot be removed on its own:
        # the whole rig goes, through the same route the effect row uses.
        from ...apply.core import driver_manager as core_driver_manager
        from . import bake_applied

        descriptor = target_memory.serialize_target(owner, data_path, index)
        metadata = (
            core_driver_manager.metadata_for_descriptor(descriptor)
            if descriptor else None
        )
        if metadata is not None and applied_motion.resolve_template(
            metadata["record"],
        ) is None:
            self.report({"WARNING"}, "Selected motion is not available in this edition; it was left unchanged.")
            return {"CANCELLED"}
        if metadata is not None and bake_applied.route_for_record(
            metadata["host"], metadata["record"],
        ) is not None:
            ok, message = bake_applied.clear_records(
                context, [(metadata["host"], metadata["record"])],
            )
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            if props is not None:
                utils.clear_active_driver_target(props)
            self.report(
                {"INFO"},
                "%s belongs to %s, which was removed with everything it built."
                % (data_path, metadata["label"]),
            )
            return {"FINISHED"}

        captured = target_memory.capture_cleanup_for_fcurves([fcurve], props)
        try:
            owner.driver_remove(data_path, index)
        except Exception as exc:
            self.report({"WARNING"}, f"Could not remove driver: {exc}")
            return {"CANCELLED"}

        target_memory.cleanup_captured_resources(captured, context.scene, props)
        target_memory.cleanup_entry_node_groups({"targets": [
            target_memory.serialize_target(owner, data_path, index),
        ]})
        # If that was the last driver of the effect it belonged to, the
        # effect's dependencies go with it, as they would from the effect row.
        purged = bake_applied.purge_emptied_records(context, owner)
        latest = target_memory.latest_entry(props) if props is not None else None
        if latest and target_memory.entry_overlaps_targets(
            latest, captured.get("targets", []),
        ):
            target_memory.clear_latest_entry(props)

        owner_name = getattr(owner, "name", "")
        if (
            props is not None
            and props.active_target_owner == owner_name
            and props.active_target_data_path == data_path
            and props.active_target_index == index
        ):
            # The removed driver was the active pick — clear it so the
            # resolver falls back to Drivers-Editor detection (or the empty
            # state) on the next redraw, instead of pointing at a dead target.
            utils.clear_active_driver_target(props)

        if purged:
            self.report(
                {"INFO"},
                f"Removed driver from {data_path}, and the effect it belonged "
                "to with everything it built.",
            )
        else:
            self.report({"INFO"}, f"Removed driver from {data_path}.")
        return {"FINISHED"}


class ESPRESSO_OT_remove_last_target(bpy.types.Operator):
    bl_idname = "espresso.remove_last_target"
    bl_label = "Remove Driver on Last Target"
    bl_description = "Remove the driver(s) from the most recently remembered apply target"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return bool(props and target_memory.latest_entry(props))

    def execute(self, context):
        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props)
        ok, message = target_memory.remove_driver_from_entry(entry, context)
        espresso_props.set_last_apply_status(props, message)
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        target_memory.clear_latest_entry(props)
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_forget_last_target(bpy.types.Operator):
    bl_idname = "espresso.forget_last_target"
    bl_label = "Forget Last Target"
    bl_description = (
        "Stop remembering what was last applied, without touching any driver. "
        "Use this when the remembered target blocks the template you want next - "
        "a multi-channel apply cannot receive a single-channel template"
    )
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    @classmethod
    def poll(cls, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        return props is not None and bool(target_memory.latest_entry(props))

    def execute(self, context):
        props = context.scene.espresso_props
        entry = target_memory.latest_entry(props)
        label = (entry or {}).get("display_label", "the last target")
        target_memory.forget_latest(props)
        # Deliberately NOT "cleared" - nothing was deleted. Saying so avoids the
        # reading that this removed the drivers, which is the neighbouring button.
        self.report({"INFO"}, "No longer remembering %s. Its drivers are untouched." % label)
        return {"FINISHED"}


class ESPRESSO_OT_toggle_favorite(bpy.types.Operator):
    bl_idname = "espresso.toggle_favorite"
    bl_label = "Toggle Favorite"
    bl_description = "Add or remove this template from your favorites"

    template_id: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.espresso_props
        template_id = self.template_id or props.template
        espresso_props.toggle_favorite(props, template_id)
        state = "added to" if espresso_props.is_favorite(props, template_id) else "removed from"
        name = templates.TEMPLATE_BY_ID.get(template_id, {}).get("name", template_id)
        self.report({"INFO"}, f"{name} {state} favorites.")
        return {"FINISHED"}


class ESPRESSO_OT_toggle_manual_expression(bpy.types.Operator):
    bl_idname = "espresso.toggle_manual_expression"
    bl_label = "Edit Expression"
    bl_description = "Hand-edit the generated expression, or return to template-driven mode"

    def execute(self, context):
        props = context.scene.espresso_props
        if not props.manual_mode:
            # Seed the editable field with the current generated expression.
            props.manual_expression = props.preview
            props.manual_mode = True
            self.report({"INFO"}, "Editing expression. Toggle off to return to template parameters.")
        else:
            props.manual_mode = False
            espresso_props.refresh_preview(props, context)
            self.report({"INFO"}, "Returned to template-driven expression.")
        return {"FINISHED"}


class ESPRESSO_OT_note(bpy.types.Operator):
    """Carries a panel explanation in its tooltip instead of on screen.

    Drawn as stacked labels, a three-line sentence would cost three rows of
    panel height on every redraw whether or not anyone was reading it. The rule
    now is that anything which can live in a hover tip does: this renders as a
    single bare icon and says its piece only when the cursor rests on it.

    Warnings and blockers deliberately stay on screen. A warning nobody can see
    until they hover is not a warning.

    Clicking does nothing on purpose - CANCELLED leaves no entry in the undo
    stack, so an accidental click on an information icon cannot bury the
    artist's last real action.
    """

    bl_idname = "espresso.note"
    bl_label = "Details"
    bl_options = {"INTERNAL"}

    note: bpy.props.StringProperty(
        name="Note",
        description="Text shown in this icon's tooltip",
        default="",
    )

    @classmethod
    def description(cls, _context, properties):
        return getattr(properties, "note", "") or "Details"

    def execute(self, _context):
        return {"CANCELLED"}


def _studio_preferences(context):
    addon = context.preferences.addons.get(utils.ADDON_PACKAGE)
    return addon.preferences if addon else None


def _studio_library():
    root = bpy.utils.user_resource("DATAFILES", path="driver_espresso/studio", create=True)
    return Path(root)


def _ensure_studio_asset_library():
    """Expose installed native .blend assets through Blender's own browser."""
    root = _studio_library()
    libraries = bpy.context.preferences.filepaths.asset_libraries
    existing = next((
        item for item in libraries
        if Path(bpy.path.abspath(item.path)).resolve() == root.resolve()
    ), None)
    if existing is None:
        existing = libraries.new(name="Driver Espresso Studio", directory=str(root))
    return existing


def _studio_validation():
    routes = {item.route for item in workflows_module.WORKFLOWS}
    capabilities = set()
    for item in capabilities_module.all_capabilities():
        capabilities.update({
            item.delivery, item.portability, item.cost_tier,
            item.conflict_policy, item.lifecycle_owner,
        })
        capabilities.update(item.output_kinds)
    capabilities.discard("")
    existing = tuple(
        path.name for path in _studio_library().iterdir() if path.is_dir()
    )
    return {
        "blender_version": bpy.app.version,
        "available_routes": routes,
        "available_capabilities": capabilities,
        "existing_pack_ids": existing,
    }


_BROADCAST_EXAMPLES = {
    "LOWER_THIRD": (
        '[{"id":"TITLE","label":"Breaking News","value":0},'
        '{"id":"SUBTITLE","label":"Live Update","value":0}]'
    ),
    "SCOREBOARD": (
        '[{"id":"HOME_SCORE","label":"Home","value":3},'
        '{"id":"AWAY_SCORE","label":"Away","value":2}]'
    ),
    "KPI_DASHBOARD": (
        '[{"id":"REVENUE","label":"Revenue","value":82},'
        '{"id":"GROWTH","label":"Growth","value":48},'
        '{"id":"CONVERSION","label":"Conversion","value":67}]'
    ),
    "MAP_ROUTE": (
        '[{"id":"START","label":"Origin","value":0},'
        '{"id":"END","label":"Destination","value":100},'
        '{"id":"DISTANCE","label":"Distance","value":18}]'
    ),
    "COMPARISON_CARD": (
        '[{"id":"PRIMARY","label":"Current","value":82},'
        '{"id":"SECONDARY","label":"Previous","value":64},'
        '{"id":"DELTA","label":"Change","value":18}]'
    ),
}


CLASSES = (
    ESPRESSO_OT_note,
    ESPRESSO_OT_set_active_driver_target,
    ESPRESSO_OT_copy,
    ESPRESSO_OT_select_motion_channel,
    ESPRESSO_OT_toggle_motion_channel,
    ESPRESSO_OT_copy_motion_channel,
    ESPRESSO_OT_set_bone_euler,
    ESPRESSO_OT_fit_position_wave_to_selection,
    ESPRESSO_OT_create_position_wave_row,
    ESPRESSO_OT_position_wave_row_wizard,
    ESPRESSO_OT_apply_palette,
    ESPRESSO_MT_palettes,
    ESPRESSO_OT_apply_to_lights,
    ESPRESSO_OT_apply_motion_selected,
    ESPRESSO_OT_apply_pose_as_motion,
    ESPRESSO_OT_apply_and_bake_motion,
    ESPRESSO_OT_bake_drivers,
    ESPRESSO_OT_bake_last_target,
    ESPRESSO_OT_apply_motion_with_offset,
    ESPRESSO_OT_clear_transform_drivers,
    ESPRESSO_OT_copy_driver,
    ESPRESSO_OT_apply,
    ESPRESSO_OT_update_last_target,
    ESPRESSO_OT_clear_input_source,
    ESPRESSO_OT_reload_live_parameters,
    ESPRESSO_OT_select_live_effect,
    ESPRESSO_OT_switch_variant,
    ESPRESSO_OT_set_master_preset,
    ESPRESSO_OT_set_param_preset,
    ESPRESSO_OT_reset_param_default,
    ESPRESSO_OT_reset_template_defaults,
    ESPRESSO_OT_param_help,
    ESPRESSO_OT_inspect_param,
    ESPRESSO_OT_show_template_variants,
    ESPRESSO_OT_show_motion_status,
    ESPRESSO_OT_navigate_template,
    ESPRESSO_OT_select_template,
    ESPRESSO_OT_clear_search,
    ESPRESSO_OT_create_variables,
    ESPRESSO_OT_remove_driver,
    ESPRESSO_OT_remove_last_target,
    ESPRESSO_OT_forget_last_target,
    ESPRESSO_OT_toggle_favorite,
    ESPRESSO_OT_toggle_manual_expression,
)


def _shipped_classes():
    """Register F3 actions only when this edition enables their feature."""
    from ...product import identity as _identity

    skip = set()
    if not getattr(_identity, "HAS_FAVORITES", True):
        skip.add(ESPRESSO_OT_toggle_favorite)
    return tuple(cls for cls in CLASSES if cls not in skip)


def register():
    for cls in _shipped_classes():
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_shipped_classes()):
        bpy.utils.unregister_class(cls)
