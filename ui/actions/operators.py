"""Operators for Driver Espresso."""

from __future__ import annotations

import json

import bpy
from ...engine.baking.transaction import atomic_bake_operator

from ...apply import apply_behavior
from ...catalogue import browse_groups
from ...catalogue import taxonomy
from ...apply import apply_target
from ...apply import motion_channels


from ..state import props as espresso_props
from ...apply import source_binding
from ...apply import target_memory
from ...apply import application_plan
from ...catalogue import templates
from ...engine import utils


def apply_expression_to_driver(driver, expression, template):
    valid, message = utils.validate_driver_expression(expression, template)
    if not valid:
        return False, message
    return utils.assign_driver_expression(driver, expression, template)


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


# Taken from the taxonomy rather than typed out.
LIGHT_CATEGORIES = (taxonomy.LIGHT, taxonomy.RGBNEON)

def applicable_objects(context):
    """What the panel's apply button would write to.

    Which of two modes applies is decided by the selection, not by whether a target
    happens to be set:

    * every selected object is a light: the lights, driving Power
    * anything else: every selected object, but only once a target has been nominated

    Without a nominated target there is nothing to write to, so the button is disabled
    and the right-click route is the way in.
    """
    selected = arrangeable_objects(context)
    if apply_target.is_light_selection(selected):
        return selected
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is not None and apply_target.read(props):
        return selected
    return []


def template_suits_lights(template, has_target=False):
    """Whether "apply to my selection" makes sense for this template. A multi-channel
    motion template drives a whole transform and has nothing to write to a single
    nominated property, so it never qualifies. Beyond that, once a property has been
    named, any single-expression template can be put on it; the restriction to
    lighting categories only applies when Power is the assumed target.
    """
    if template is None or templates.has_motion_plan(template):
        return False
    if has_target:
        return True
    return template.get("category") in LIGHT_CATEGORIES


def arrangeable_objects(context):
    """The selected objects a template can be applied to.

    Any object type qualifies - a row of meshes is as legitimate as a row of
    lamps.
    """
    return list(getattr(context, "selected_objects", None) or ())


class ESPRESSO_OT_apply_to_lights(bpy.types.Operator):
    """Apply this lighting template to every selected light's Power.

    Blender's own Copy Drivers to Selected copies each variable's target too, so every
    lamp would read the active lamp's position and a spatial pattern would collapse to a
    single value. This applies the template afresh per lamp so every light gets its own
    driver.
    """

    bl_idname = "espresso.apply_to_lights"
    bl_label = "Apply to Selected Lights"
    bl_description = (
        "Apply this lighting template to the Power of every selected light. "
        "Each light gets its own driver"
    )
    bl_options = {"REGISTER", "UNDO"}

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
        # Imported here, not at module scope: context_menu imports THIS module,
        # so a top-level import would be circular.
        from ..menus import context_menu

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
                "\"Split Shared Materials\" when applying." % len(shared))
        espresso_props.set_last_apply_status(props, note)
        self.report({"WARNING"} if (shared or skipped) else {"INFO"}, note)
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

        targets, unresolved = bake.discover_driver_targets(
            self._objects(context),
            scene=context.scene if self.scope == "SCENE" else None,
        )
        return list(targets), unresolved

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

    def execute(self, context):
        from ...engine import bake

        targets, unresolved = self._discover(context)
        if not targets:
            self.report({"WARNING"}, "No drivers found in this scope.")
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
        self.report({"WARNING"} if unresolved else {"INFO"}, message)
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

    # Start+Duration is the default because "bake this landing" is a length, not an end
    # frame. An end frame makes the artist redo the arithmetic every time Start moves.
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
        # Assigned even though it is already the default, so the property counts as set.
        # _resolve treats "bake_end set, use_duration not" as a programmatic end-frame
        # call, and without this line the first bake in a scene, before anything is
        # remembered, would look exactly like one: the dialog would show "Match Motion
        # Length (341 frames)" and bake 1 to 250.
        self.use_duration = self.use_duration
        self.start_at_current_frame = self.start_at_current_frame

    def _commit_range(self, context):
        """Resolve the range, write it back, and remember the choice.

        Resolving here rather than at each use is what lets Start+Duration exist: every
        bake path reads ``self.bake_end``, so folding the duration into it once means
        none of them needs to know about the mode. Called from execute rather than from
        the popup's draw, because a range the artist typed and then cancelled is not a
        choice, and remembering it would make Cancel change state.
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
        # Greyed rather than hidden, as Duration is when matched: the summary line below
        # carries the frame that will be used.
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

        # The remembered entry described a live driver that no longer exists; keeping it
        # would let Update Last Target and Clear Last Drivers act on a channel that is
        # now plain keyframes.
        target_memory.clear_latest_entry(props)
        espresso_props.set_last_apply_status(props, message)
        self.report({"INFO"}, message)
        return {"FINISHED"}


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
    """Selected pose bones in Pose Mode, otherwise selected objects. Independent of the
    selected template, unlike ``_offset_target_specs``: this clears whatever is on
    the selection, including drivers Driver Espresso did not create.
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
        cleared = []
        props = getattr(context.scene, "espresso_props", None)
        cleanup_records = []
        for owner, bone in targets:
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

        if not removed:
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
            f"Cleared {removed} transform driver{'' if removed == 1 else 's'} on {where}.",
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
        return True

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        props.copied_driver_expression = props.preview
        props.copied_driver_template = template["id"]
        props.copied_driver_label = template["name"]
        props.copied_driver_rest_mode = espresso_props.effective_rest_start_mode(props)
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

    # Optional row-level target. When set (from a Driver Target list row), this driver
    # becomes the active target before applying. Left empty for callers that rely on
    # ``get_target_driver_fcurve``'s existing resolution (Drivers Editor selection or an
    # already-set pick). Callers that set data_path must also set array_index for that
    # driver (-1 for a scalar property, or the real index for an array property); the
    # two are matched together by ``get_target_driver_fcurve``.
    data_path: bpy.props.StringProperty(default="")
    array_index: bpy.props.IntProperty(default=-1)
    target_id_type: bpy.props.StringProperty(default="")
    target_id_name: bpy.props.StringProperty(default="")
    target_owner_path: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        # poll() is a classmethod with no access to this uninvoked instance's data_path,
        # so it can only check the currently resolved active target, not the specific
        # row a button represents. Per-row enable/disable is handled by the panel
        # setting ``UILayout.enabled`` directly.
        props = getattr(context.scene, "espresso_props", None)
        if props is None or not props.is_valid:
            return False
        fcurve, driver, _reason = utils.get_target_driver_fcurve(context)
        if fcurve is None or driver is None:
            return False
        template = espresso_props.get_current_template(props)
        if templates.has_motion_plan(template):
            return False
        return True

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
        preview_expression = props.preview
        rest_state = apply_behavior.capture_target_rest_state(
            target,
            preview_expression,
            template,
            context.scene,
            espresso_props.effective_rest_start_mode(props),
            output_baseline=props.preview_output_baseline,
        )
        wrapped_expression = utils.wrap_expression_with_rest_state(preview_expression, template, rest_state)
        ok, message = apply_expression_to_driver(
            driver,
            wrapped_expression,
            template,
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
    """Resolve the Object a remembered motion entry was applied to.

    Motion templates drive several channels at once, so re-applying cannot mean
    "push one expression at every remembered target" the way it does for a single
    scalar. It means "run the whole motion plan against that object again", which
    is exactly what the normal apply path already does correctly.
    """
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return None, reason or "The remembered target no longer exists."
    owner = resolved[0].get("owner")
    obj = owner if isinstance(owner, bpy.types.Object) else getattr(owner, "id_data", None)
    if not isinstance(obj, bpy.types.Object):
        return None, "The remembered target is not on an object."
    return obj, ""


def _reapply_channel_plan_to_entry(context, props, template, entry):
    """Re-push each channel's own expression at its remembered index.

    The motion route means "run the whole plan against that object again", which needs
    an Object. A channel plan applied by right-clicking a shader socket has no object
    (the owner is a node socket), so that route cannot be used there. The remembered
    targets already say where each channel went, so re-applying is matching them back up
    by array index.
    """
    resolved, reason = target_memory.resolve_entry(entry)
    if not resolved:
        return False, reason or "The remembered target no longer exists."

    for target in resolved:
        channel, reason = target_memory.canonical_driver_channel(target)
        if channel is None:
            return False, reason

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
            layout.label(text="The complete channel plan will be applied to its owning Object.")

    def _preflight(self, context, entry, template):
        plan = application_plan.from_entry(entry, None)
        obj, _reason = _entry_motion_context(entry)
        return application_plan.preflight_transition(
            plan, template,
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
        # Re-apply from the frame the motion was first applied on, not from wherever the
        # playhead is now.
        #
        # Rest Start captures the target's pose at the current frame and treats it as
        # the rest state, so updating from frame 50 would fold the motion's own frame-50
        # value into the baseline and shift every frame by that amount, twice as much if
        # repeated. The entry records applied_frame for this. The frame is restored in a
        # finally block so an early return or a raising apply cannot strand the playhead
        # somewhere the artist did not put it.
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
                props.preview,
                template,
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
                output_baseline=props.preview_output_baseline,
                validation_template=template,
                clear_other_channels=self.clear_remembered_motion_channels,
            )
            espresso_props.set_last_apply_status(props, message)
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            target_memory.remember_entry(context, converted)
            self.report({"INFO"}, message)
            return {"FINISHED"}

        # A motion template has one expression PER CHANNEL, so pushing
        # props.preview at every remembered target would write the first
        # channel's formula onto all of them. Re-run the motion plan instead.
        if templates.has_motion_plan(template):
            if has_controller:
                values = espresso_props.collect_values(props, template)
                built = utils.build_template_expressions(template, values, context.scene)
                matched, note = motion_channels.channels_for_stored_targets(
                    built, template, context.scene, (entry or {}).get("targets") or (),
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
                    scene=context.scene,
                    rest_start_mode=espresso_props.effective_rest_start_mode(props),
                    output_baseline=[item.get("output_baseline") for item in matched],
                    validation_template=template,
                )
                espresso_props.set_last_apply_status(props, message)
                if not ok:
                    self.report({"WARNING"}, message)
                    return {"CANCELLED"}
                entry["parameter_values"] = target_memory.json_safe_values(values)
                target_memory.remember_entry(context, entry)
                self.report({"INFO"}, message if not note else f"{message} {note}")
                return {"FINISHED"}
            obj, reason = _entry_motion_context(entry)
            if obj is None:
                # No object behind the remembered targets (a shader socket, say). The
                # plan still knows which index each channel went to, so push them
                # straight back rather than declaring the target lost.
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
            result = motion_channels.apply_motion_template_to_object(
                obj, template, espresso_props.built_channel_previews(props),
                scene=context.scene,
                rest_start_mode=espresso_props.effective_rest_start_mode(props),
                enabled_channel_ids=espresso_props.enabled_channel_ids_for_template(props, template),
            )
            espresso_props.set_last_apply_status(props, result.message)
            if not result.ok:
                self.report({"WARNING"}, result.message)
                return {"CANCELLED"}
            label = f"{obj.name} > {template['name']}"
            target_memory.remember_targets(
                context, result.targets, label,
                "motion",
                template["id"], template["name"], target_states=result.target_states,
            )
            self.report({"INFO"}, result.message)
            return {"FINISHED"}

        ok, message = target_memory.apply_expression_to_entry(
            entry,
            props.preview,
            template,
            scene=context.scene,
            rest_start_mode=espresso_props.effective_rest_start_mode(props),
            output_baseline=props.preview_output_baseline,
            validation_template=template,
        )
        if not ok:
            espresso_props.set_last_apply_status(props, message)
            if "no longer exists" in message:
                # Return FINISHED (not CANCELLED) so Blender's undo system commits the
                # clear: CANCELLED rolls back all data changes made in execute, which
                # would restore the stale entry and repeat the error.
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
        "template defaults"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.espresso_props
        template = _parameter_operator_template(context)

        # The active template's slots are LIVE properties, so it goes through
        # the normal apply path.
        espresso_props.apply_template_defaults(props, template)
        if getattr(props, "parameter_mode", "SETUP") == "LIVE":
            ok, message = espresso_props.sync_live_parameters(props, context, template)
            if not ok:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
        memory = utils.read_template_memory(props)
        if getattr(props, "parameter_mode", "SETUP") != "LIVE":
            memory[template["id"]] = espresso_props.collect_values(props, template)
        utils.write_template_memory(props, memory)
        espresso_props.refresh_preview(props, context)

        self.report({"INFO"}, f"Reset {template['name']} parameters to template defaults.")
        return {"FINISHED"}


class ESPRESSO_OT_inspect_param(bpy.types.Operator):
    bl_idname = "espresso.inspect_param"
    bl_label = "Parameter Info"
    bl_description = "Show template-specific help for this parameter"

    slot_index: bpy.props.IntProperty()

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return cls.bl_description
        params = _parameter_operator_template(context).get("params", [])
        index = getattr(properties, "slot_index", -1)
        if not 0 <= index < len(params):
            return cls.bl_description
        return templates.format_param_tooltip(params[index])

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=320)

    def draw(self, context):
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


# There is no show_template_use_cases operator: a popup of the template's use_cases
# would repeat the tooltip its own description() returns and the template dropdown
# beside it also carries.


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


class ESPRESSO_OT_remove_driver(bpy.types.Operator):
    bl_idname = "espresso.remove_driver"
    bl_label = "Remove Selected Driver"
    bl_description = "Remove the driver from the active driver target (or a specific driver when invoked from a Driver Target list row)"
    bl_options = {"REGISTER", "UNDO"}

    # Optional row-level target, with the same contract as ESPRESSO_OT_apply's
    # data_path/array_index: callers that set data_path must also set array_index for
    # that driver (-1 for a scalar property, or the real index for an array property).
    data_path: bpy.props.StringProperty(default="")
    array_index: bpy.props.IntProperty(default=-1)
    target_id_type: bpy.props.StringProperty(default="")
    target_id_name: bpy.props.StringProperty(default="")
    target_owner_path: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        # Row-agnostic by design; see ESPRESSO_OT_apply.poll.
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

        from . import bake_applied

        captured = target_memory.capture_cleanup_for_fcurves([fcurve], props)
        try:
            owner.driver_remove(data_path, index)
        except Exception as exc:
            self.report({"WARNING"}, f"Could not remove driver: {exc}")
            return {"CANCELLED"}

        target_memory.cleanup_captured_resources(captured, context.scene, props)
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
            # The removed driver was the active pick; clear it so the resolver falls
            # back to Drivers Editor detection (or the empty state) on the next redraw
            # instead of pointing at a dead target.
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
        # Deliberately not "cleared": nothing was deleted, and saying so avoids the
        # reading that this removed the drivers, which is what the neighbouring button
        # does.
        self.report({"INFO"}, "No longer remembering %s. Its drivers are untouched." % label)
        return {"FINISHED"}


class ESPRESSO_OT_note(bpy.types.Operator):
    """Carries a panel explanation in its tooltip instead of on screen.

    Drawn as stacked labels, a three-line sentence would cost three rows of panel height
    on every redraw whether or not anyone was reading it. Anything that can live in a
    hover tip does, so this renders as a single bare icon and says its piece when the
    cursor rests on it. Warnings and blockers stay on screen, since a warning nobody can
    see until they hover is not a warning. Clicking does nothing: CANCELLED leaves no
    entry in the undo stack, so an accidental click on an information icon cannot bury
    the artist's last real action.
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


CLASSES = (
    ESPRESSO_OT_note,
    ESPRESSO_OT_copy,
    ESPRESSO_OT_select_motion_channel,
    ESPRESSO_OT_toggle_motion_channel,
    ESPRESSO_OT_copy_motion_channel,
    ESPRESSO_OT_apply_to_lights,
    ESPRESSO_OT_bake_drivers,
    ESPRESSO_OT_bake_last_target,
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
    ESPRESSO_OT_inspect_param,
    ESPRESSO_OT_navigate_template,
    ESPRESSO_OT_select_template,
    ESPRESSO_OT_clear_search,
    ESPRESSO_OT_remove_driver,
    ESPRESSO_OT_remove_last_target,
    ESPRESSO_OT_forget_last_target,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
