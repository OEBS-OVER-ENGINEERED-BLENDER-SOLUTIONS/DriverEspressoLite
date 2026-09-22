"""N-panel UI for Driver Espresso."""

from __future__ import annotations

import bpy


from ...catalogue import browse_groups
from ...catalogue import catalogue_contracts, templates
from ...apply import layout_preparation, response_rig, spatial_fields
from ...apply.setups import parameter_bindings
from ...apply.setups import audio_reactivity
from ...apply import applied_motion
from ...apply import driver_manager, driver_targets
from ...apply import motion_channels
from ...apply import pose_capture
from ...apply import light_layout
from ...engine import aliasing
from ...engine import duration as espresso_duration
from ...engine.motion_stack import capabilities as capabilities_module
from ...engine.motion_stack import workflows as workflows_module
from ...generated import geometry_node_bindings
from ...apply import apply_target
from ...apply import shared_material_sweep
from ..state import props as espresso_props
from ...product.identity import NAME as _PRODUCT_NAME
from . import guided_apply, panel_sections, preview_modes, settings_reset, source_display


class _AbsentCameraModule:
    """Stands in for the camera tree, which Driver Espresso Lite does not ship.

    It exists so this panel file keeps the same shape across builds.

    **Drawing answers; building raises.** Raising on every attribute would not
    be safe here: `draw_parameter_controls` calls
    `camera_template_targets.draw` and `camera_rig_tools.draw` guarded only by
    `context is not None`, so drawing the parameters of ANY template would
    reach this stand-in.

    A camera section that renders nothing is the correct result in a product
    with no camera recipes, so the draws and the labels are no-ops. Applying a
    camera template still raises -- reaching that really would be a routing
    bug, which is the case this tripwire is for.
    """

    CLASSES = ()

    #: Anything that would BUILD a camera rig. Unreachable by design, and if
    #: it is ever reached the silence would be worse than the error.
    _BUILDERS = frozenset({"apply_camera_template"})

    @staticmethod
    def requires_camera_object(template):
        return False

    def __getattr__(self, name):
        if name in self._BUILDERS:
            def _unreachable(*args, **kwargs):
                raise RuntimeError(
                    "Driver Espresso Lite ships no camera recipes (%s)" % name)
            return _unreachable
        if name == "live_state":
            # Unpacked as `status, is_live = ...` by the drone controls row.
            return lambda *args, **kwargs: ("", False)
        if name.startswith("draw"):
            return lambda *args, **kwargs: None
        # Labels and notes: an empty string draws nothing and reads as nothing.
        return lambda *args, **kwargs: ""


camera_application = _AbsentCameraModule()
camera_free_roam = _AbsentCameraModule()
camera_template_targets = _AbsentCameraModule()
camera_drone_controls = _AbsentCameraModule()
camera_rig_tools = _AbsentCameraModule()
camera_recipe_findings = _AbsentCameraModule()

# Single Espresso sidebar category; Motion / Organize / Controller are in-panel tabs.
_TAB_ESPRESSO = "Espresso"
_SIDEBAR_TAB_MOTION = "MOTION"
_SIDEBAR_TAB_ORGANIZE = "ORGANIZE"
_SIDEBAR_TAB_CONTROLLER = "CONTROLLER"

# Icons for the duration readout. Chosen so the three states read differently at
# a glance: a finite move, a loop, and something that never stops.
_DURATION_ICON = {
    "FINITE": "KEYFRAME_HLT",
    "CYCLIC": "FILE_REFRESH",
    "INFINITE": "FF",
    "CONTINUOUS": "RNDCURVE",
    "STATIC": "DECORATE",
    # Its length is a property of whatever drives it, not of the template.
    "INPUT_DRIVEN": "LINKED",
}
from ...apply import source_binding
from ...apply import target_memory
from ...catalogue import templates as espresso_templates
from ...engine import utils
from ..actions import operators
from ..actions.operators import missing_required_variables
from ...catalogue.core.templates import TEMPLATE_BY_ID
from ...catalogue.core import ui_shelf


def draw_wrapped(layout, text, icon="NONE", width=54):
    """Stack text across as many rows as it needs.

    Reserved for WARNINGS AND BLOCKERS. Anything the artist must see without
    going looking - a template that will not apply, a bone that will silently
    ignore the rotation - earns its vertical space. Explanation does not: use
    draw_note.
    """
    for index, line in enumerate(utils.wrap_text(text, width)):
        layout.label(text=line, icon=icon if index == 0 else "BLANK1")


def draw_note(layout, text, icon="INFO", label=""):
    """One bare icon whose tooltip carries the explanation.

    The panel is read far more often than any single sentence in it, so prose
    that answers "what will this do" belongs in a hover tip rather than in
    three permanent rows. A three-line note costs one icon here.

    Passing a label puts short text beside the icon - use it only when the row
    would otherwise be a lone icon with nothing to identify it.
    """
    if not text:
        return
    row = layout.row(align=True)
    entry = row.operator("espresso.note", text=label, icon=icon, emboss=False)
    entry.note = text
    return row


def _selected_objects(context):
    """Return the selection used by target routes, including the active object."""
    selected = list(getattr(context, "selected_objects", ()) or ())
    active = getattr(context, "active_object", None)
    if active is not None and active not in selected:
        selected.append(active)
    return selected


def _motion_destination_label(template, context):
    """Describe the active destination for a transform motion plan."""
    channels = espresso_templates.template_channels(template)
    names = {
        "location": "Location",
        "rotation_euler": "Rotation",
        "rotation_quaternion": "Rotation",
        "scale": "Scale",
        "delta_location": "Delta Location",
        "delta_rotation_euler": "Delta Rotation",
        "delta_scale": "Delta Scale",
    }
    destinations = []
    for channel in channels:
        label = names.get(channel.get("data_path"))
        if label and label not in destinations:
            destinations.append(label)
    destination = " + ".join(destinations) or "Motion channels"
    pose_bone = getattr(context, "active_pose_bone", None)
    if getattr(context, "mode", "") == "POSE" and pose_bone is not None:
        return "Active Bone '%s' > %s" % (pose_bone.name, destination)
    active = getattr(context, "active_object", None)
    if active is not None:
        return "Active Object '%s' > %s" % (active.name, destination)
    return "Active Object > %s" % destination


def _draw_visible_applies_to(layout, destination, icon="DOT"):
    """Render the destination directly instead of hiding it in a tooltip."""
    target_box = layout.box()
    target_box.label(text="APPLIES TO", icon=icon)
    value_row = target_box.row(align=True)
    value_row.label(text=destination or "Choose a target property", icon="DOT")
    return target_box


def _browsed_template_owns_destination(template):
    """generated systems recipes that ship their own build/apply route in-panel."""
    template_id = str((template or {}).get("id", "") or "")
    if not template_id:
        return False
    return capabilities_module.owns_destination(template_id)


def selection_has_one_shared_shader_target(props, objects):
    """True only when direct spatial application would collapse to one driver."""
    objects = list(objects)
    if len(objects) < 2:
        return False
    entry, _light = apply_target.effective_entry(props, objects)
    if not entry:
        return False
    keys = []
    for obj in objects:
        targets, _reason = apply_target.targets_for(entry, obj)
        if len(targets) != 1 or not isinstance(targets[0].owner, bpy.types.NodeTree):
            return False
        target = targets[0]
        keys.append((target.owner.as_pointer(), target.data_path, target.index))
    return len(set(keys)) == 1


# Scene pointers with a template-init timer already scheduled — panel draw()
# runs in a read-only context, so writing props there raises
# "Writing to ID classes in this context is not allowed"; the fix must defer
# the write to a timer and request a redraw once it lands.
_pending_template_init = set()


def _ensure_template_initialized(scene, props):
    if props.last_template_id:
        return
    key = scene.as_pointer()
    if key in _pending_template_init:
        return
    _pending_template_init.add(key)

    def _do_init():
        _pending_template_init.discard(key)
        try:
            if scene.espresso_props.last_template_id:
                return None
            espresso_props.on_template_update(scene.espresso_props, bpy.context)
        except Exception:
            pass
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    bpy.app.timers.register(_do_init, first_interval=0.0)


_pending_driver_target_sync = set()
_driver_target_sync_inputs = {}


def _driver_target_input_signature(props, targets, effects, source=None):
    """Cheap redraw signature before the expensive metadata join."""
    source = source or getattr(props, "driver_target_source", "ACTIVE")
    group_state = espresso_props.read_driver_target_group_state(props, source=source)
    effect_signature = "|".join(
        "%s:%s:%s:%s:%s" % (
            item.get("record_token", ""), item.get("template_id", ""),
            item.get("effect_kind", ""), int(bool(item.get("editable", False))),
            item.get("route", ""),
        )
        for item in effects or ()
    )
    groups = "|".join(
        "%s:%d" % (key, int(bool(value)))
        for key, value in sorted(group_state.items())
    )
    return "%s\x1e%s\x1e%s\x1e%s" % (
        source, driver_targets.target_signature(targets), effect_signature, groups,
    )


def _ensure_driver_target_state_synced(scene, props, targets, effects=None):
    """Same read-only-draw() constraint as _ensure_template_initialized: the
    Driver Target UIList's backing CollectionProperty must be repopulated
    whenever the active object's drivers change, but panel draw() can't write
    it directly — so a mismatch schedules a zero-delay timer instead of
    writing here and there's a possibly-one-frame-stale list until it lands,
    self-correcting via the redraw the timer requests."""
    source = getattr(props, "driver_target_source", "ACTIVE")
    cache_key = (int(scene.as_pointer()), str(source))
    signature = _driver_target_input_signature(props, targets, effects, source=source)
    if (
        getattr(props, "driver_target_items_source", "") == source
        and _driver_target_sync_inputs.get(cache_key) == signature
        and (len(props.driver_target_items) or not targets and not effects)
    ):
        return
    key = scene.as_pointer()
    if key in _pending_driver_target_sync:
        return
    _pending_driver_target_sync.add(key)

    def _do_sync():
        _pending_driver_target_sync.discard(key)
        try:
            live_props = scene.espresso_props
            live_targets = driver_targets.panel_targets(bpy.context, live_props)
            from ...apply import applied_motion_manager
            live_effects = applied_motion_manager.collect_effects(
                bpy.context, getattr(live_props, "driver_target_source", "ACTIVE"),
            )
            espresso_props.sync_driver_target_items(
                live_props, live_targets, effects=live_effects,
                source=getattr(live_props, "driver_target_source", "ACTIVE"),
            )
            _write_applied_effects_cache(
                live_props, source=getattr(live_props, "driver_target_source", "ACTIVE"),
            )
            live_key = (
                int(scene.as_pointer()),
                str(getattr(live_props, "driver_target_source", "ACTIVE")),
            )
            _driver_target_sync_inputs[live_key] = _driver_target_input_signature(
                live_props, live_targets, live_effects,
                source=getattr(live_props, "driver_target_source", "ACTIVE"),
            )
            if len(_driver_target_sync_inputs) > 12:
                newest = _driver_target_sync_inputs[live_key]
                _driver_target_sync_inputs.clear()
                _driver_target_sync_inputs[live_key] = newest

            active_fcurve, _driver, _reason = utils.get_target_driver_fcurve(bpy.context, targets=live_targets)
            index = -1
            if active_fcurve is not None:
                active_key = ""
                for target in live_targets:
                    if driver_targets.resolve_driver(target) is active_fcurve:
                        active_key = driver_manager.descriptor_key(target)
                        break
                if active_key:
                    for i, item in enumerate(live_props.driver_target_items):
                        if item.row_kind == "TARGET" and item.descriptor_key == active_key:
                            index = i
                            break
            if live_props.driver_target_active_index != index:
                live_props.driver_target_active_index = index
        except Exception:
            pass
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    bpy.app.timers.register(_do_sync, first_interval=0.0)


def _write_applied_effects_cache(props, source=""):
    """Persist header counts from the last discovery. Header draw only reads this."""
    items = getattr(props, "driver_target_items", ())
    effect_count, driver_count = driver_manager.selected_removal_counts(
        items, selected_only=False,
    )
    props.applied_effects_cached_count = int(effect_count) + int(driver_count)
    props.applied_effects_cached_label = driver_manager.visible_resource_label(
        effect_count, driver_count,
    )
    if source == "SCENE":
        props.applied_effects_scene_cached_count = props.applied_effects_cached_count


def _applied_effects_header_label(props):
    """Collapsed-header text from cache only — never discover here."""
    label = str(getattr(props, "applied_effects_cached_label", "") or "").strip()
    if label:
        return label
    count = int(getattr(props, "applied_effects_cached_count", 0) or 0)
    return str(count)


class ESPRESSO_MT_preset_base(bpy.types.Menu):
    slot_index = 0

    def draw(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        params = template.get("params", [])
        if self.slot_index < len(params):
            param = params[self.slot_index]
            presets = param.get("presets", [])
            if len(presets) > 6:
                from ... import config
                usage = config.get_param_preset_usage(template["id"], self.slot_index)
                if any(usage.values()):
                    presets = sorted(presets, key=lambda p: usage.get(p["label"], 0), reverse=True)
            layout = self.layout
            for preset in presets:
                op = layout.operator("espresso.set_param_preset", text=preset["label"])
                op.slot_index = self.slot_index
                op.value = float(preset["value"])
                op.preset_label = preset["label"]


class ESPRESSO_MT_master_presets_drpdwn(bpy.types.Menu):
    bl_label = "Master Presets"
    bl_idname = "ESPRESSO_MT_master_presets_drpdwn"

    def draw(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        combined_presets = template.get("combined_presets", [])
        layout = self.layout

        # Mark the entries already sitting on the shelf. Without this the
        # dropdown repeats the shelf with no sign of which is which, so the
        # first few look like duplicates rather than the same buttons.
        prefs = utils.addon_preferences(context)
        shelf = _quick_preset_indices(template, prefs)
        pinned = set(shelf)
        hide_pinned = bool(getattr(prefs, "hide_pinned_master_presets", False)) if prefs else False

        # Shelf entries first, in the order they appear ON the shelf, so the top
        # of the menu reads left-to-right the same as the buttons above it. In
        # the usage-ranked modes the shelf reorders itself as you work, and
        # leaving the menu in catalogue order scattered those entries through it.
        rest = [i for i in range(len(combined_presets)) if i not in pinned]
        order = rest if hide_pinned else list(shelf) + rest

        for position, preset_index in enumerate(order):
            is_pinned = preset_index in pinned
            # A rule between the two groups, so "already on the shelf" reads as a
            # section rather than as a run of decorated rows.
            if not hide_pinned and shelf and position == len(shelf):
                layout.separator()
            op = layout.operator(
                "espresso.set_master_preset",
                text=combined_presets[preset_index]["label"],
                # A quiet marker, not a badge: these rows are ordinary choices
                # that happen to also sit on the shelf, and a loud pin icon read
                # as a status the row was announcing. BLANK1 rather than no icon
                # on the rest, so every label still starts at the same x.
                icon="HANDLETYPE_AUTO_VEC" if is_pinned else "BLANK1",
            )
            op.preset_index = preset_index

        if not order:
            # Only reachable if the shelf holds every preset, in which case the
            # menu button is normally hidden - say so rather than show a blank.
            layout.label(text="Every preset is on the shelf", icon="INFO")


class ESPRESSO_MT_channel_picker(bpy.types.Menu):
    bl_label = "Channel"
    bl_idname = "ESPRESSO_MT_channel_picker"

    def draw(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        layout = self.layout
        for sib in espresso_templates.channel_row_entries(template, template["id"]):
            op = layout.operator(
                "espresso.switch_variant", text=sib["role_label"],
                icon="RADIOBUT_ON" if sib["id"] == template["id"] else "RADIOBUT_OFF",
            )
            op.variant_id = sib["id"]


def live_effect_channels(context, source="ACTIVE"):
    """record token -> where that effect drives, in words.

    Read from the draw-cached effect collection rather than re-walked, so
    naming the channels costs nothing the panel was not already paying.
    """
    from ...apply.motion import applied_motion as motion_records
    from ...apply.motion import applied_motion_manager

    out = {}
    for effect in applied_motion_manager.collect_effects_for_draw(context, source):
        host, record = effect.get("host"), effect.get("record")
        if host is None or not record:
            continue
        channel = motion_records.channel_label(host, record)
        if channel:
            out[effect.get("record_token", "")] = channel
    return out


class ESPRESSO_MT_live_effects(bpy.types.Menu):
    """Choose one exact parameterized effect owned by the active object."""

    bl_label = "Live Effect"
    bl_idname = "ESPRESSO_MT_live_effects"

    def draw(self, context):
        layout = self.layout
        choices = parameter_bindings.active_effect_choices(context)
        if not choices:
            layout.label(text="No compatible Live effects", icon="INFO")
            return
        identity = espresso_props.authoring_presentation(
            context.scene.espresso_props, context,
        )
        current = identity.get("record_token", "")
        # One recipe can be applied more than once to the same host, on
        # different channels, so the recipe name alone is not a choice an
        # artist can make -- two rows reading "Campfire Flicker" pick
        # themselves. Say where each one drives.
        channels = live_effect_channels(context)
        # A stamp whose driver was deleted by other means is invalid, and the
        # idle reconciler will purge it; until it has, there is nothing here
        # to edit, so it is not offered.
        from ...apply.motion import applied_motion_manager as _manager
        dead = {
            effect.get("record_token", "")
            for effect in _manager.collect_effects_for_draw(context, "ACTIVE")
            if not effect.get("alive", True)
        }
        for choice in choices:
            if choice["record_token"] in dead:
                continue
            row = layout.row()
            row.enabled = bool(choice["binding"].editable)
            channel = channels.get(choice["record_token"], "")
            op = row.operator(
                "espresso.select_live_effect",
                text=("%s \u00b7 %s" % (choice["label"], channel) if channel
                      else choice["label"]),
                icon="RADIOBUT_ON" if choice["record_token"] == current else "RADIOBUT_OFF",
            )
            op.record_token = choice["record_token"]


class ESPRESSO_MT_presets_0(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 0
class ESPRESSO_MT_presets_1(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 1
class ESPRESSO_MT_presets_2(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 2
class ESPRESSO_MT_presets_3(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 3
class ESPRESSO_MT_presets_4(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 4
class ESPRESSO_MT_presets_5(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 5
class ESPRESSO_MT_presets_6(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 6
class ESPRESSO_MT_presets_7(ESPRESSO_MT_preset_base): bl_label = "Presets"; slot_index = 7


PAIRABLE_TOKEN_SETS = {
    frozenset({"MIN", "MAX"}),
    frozenset({"IN_MIN", "IN_MAX"}),
    frozenset({"OUT_MIN", "OUT_MAX"}),
    frozenset({"BEAT1", "BEAT2"}),
    frozenset({"START_FRAME", "END_FRAME"}),
    frozenset({"ADV_START_FRAME", "ADV_END_FRAME"}),
    frozenset({"LOOP_START", "LOOP_END"}),
    frozenset({"A", "B"}),
    frozenset({"HIGH", "LOW"}),
}

ADVANCED_SECTION_STATE_KEY = "espresso_advanced_section_open"
ADVANCED_GROUP_METADATA = {
    "timing": {
        "label": "TIMING",
        "icon": "TIME",
        "enabled_pref": "advanced_group_timing",
        "open_prop": "advanced_timing_open",
    },
    "output": {
        "label": "OUTPUT",
        "icon": "DRIVER",
        "enabled_pref": "advanced_group_output",
        "open_prop": "advanced_output_open",
    },
}


def _advanced_section_open(context):
    return bool(context.window_manager.get(ADVANCED_SECTION_STATE_KEY, False))


def _group_supported_advanced_controls(template):
    grouped = {group_id: [] for group_id in ADVANCED_GROUP_METADATA}
    for control in espresso_templates.advanced_controls_for_template(template):
        group_id = control.get("advanced_group")
        if group_id in grouped:
            grouped[group_id].append(control)
    return grouped


def _visible_advanced_groups(context, template):
    prefs = utils.addon_preferences(context)
    if prefs is None or not getattr(prefs, "enable_advanced_controls", False):
        return []

    grouped_controls = _group_supported_advanced_controls(template)
    return [
        (group_id, metadata, grouped_controls[group_id])
        for group_id, metadata in ADVANCED_GROUP_METADATA.items()
        if grouped_controls.get(group_id) and getattr(prefs, metadata["enabled_pref"], True)
    ]


def _regular_param_visible(props, template, param):
    condition = param.get("visible_if") or {}
    if not condition or props is None:
        return True
    values = espresso_props.collect_values(props, template)
    return all(bool(values.get(token)) == bool(required) for token, required in condition.items())


def pairable_param_groups(template, excluded_tokens=(), props=None):
    params = template.get("params", [])
    excluded_tokens = frozenset(excluded_tokens)
    groups = []
    index = 0
    while index < len(params):
        current = params[index]
        if current.get("token") in excluded_tokens or not _regular_param_visible(props, template, current):
            index += 1
            continue
        next_param = params[index + 1] if index + 1 < len(params) else None
        if next_param and (
            next_param.get("token") in excluded_tokens
            or not _regular_param_visible(props, template, next_param)
        ):
            next_param = None
        if next_param:
            token_pair = frozenset({current["token"], next_param["token"]})
            labels_compact = len(current["label"]) <= 14 and len(next_param["label"]) <= 14
            if token_pair in PAIRABLE_TOKEN_SETS and labels_compact:
                groups.append(
                    [
                        {"param": current, "index": index},
                        {"param": next_param, "index": index + 1},
                    ]
                )
                index += 2
                continue
        groups.append([{"param": current, "index": index}])
        index += 1
    return groups


def draw_template_selector(layout, props, context=None):
    search_row = layout.row(align=True)
    search_row.prop(props, "search_text", text="", icon="VIEWZOOM", placeholder="Search templates...")
    if props.search_text:
        # espresso.clear_search, not select_template: this button clears the
        # search, and select_template's tooltip would describe the template it
        # selects rather than what the button actually does.
        search_row.operator("espresso.clear_search", text="", icon="X")
    kind_row = layout.row(align=True)
    kind_row.prop(props, "template_kind", expand=True)
    layout.prop(props, "category")
    # The generation strip, above the subcategory row and separate from it.
    # A subcategory says what a recipe is about; this says which generation of
    # it you are looking at.
    if props.category in espresso_props.STAGED_CATEGORIES:
        layout.prop(props, "catalogue_stage_view", expand=True)
    available = {
        item["id"]
        for item in espresso_templates.templates_by_category(props.category)
    }
    if browse_groups.has_groups(props.category, available) and (
        context is None or _section_enabled(context, "show_subcategory_row")
    ):
        layout.prop(props, "browse_group", text="Subcategory")
    if props.search_text:
        results = espresso_props.search_result_templates(props)
        if not results:
            layout.label(text="No templates match")
            layout.operator("espresso.clear_search", text="Clear Search")
        else:
            count = len(results)
            layout.label(
                text=(
                    "1 template matches"
                    if count == 1
                    else "%d templates match" % count
                )
            )
            for item in results[: espresso_props.SEARCH_RESULT_ROW_LIMIT]:
                op = layout.operator("espresso.select_template", text=item["name"])
                op.template_id = item["id"]
                op.category_name = item["category"]


def _draw_template_shortcut_row(layout, props, template_ids, empty_text, more_prop=""):
    ids = [tid for tid in template_ids if tid in TEMPLATE_BY_ID]
    if not ids:
        layout.label(text=empty_text, icon="BLANK1")
        return
    col = layout.column(align=True)
    row = None
    visible = ids[:8]
    overflow = ids[8:]
    for index, tid in enumerate(visible):
        if index % 2 == 0:
            row = col.row(align=True)
        op = row.operator("espresso.select_template", text=TEMPLATE_BY_ID[tid]["name"], icon="FILE_BLEND")
        op.template_id = tid
        op.category_name = TEMPLATE_BY_ID[tid]["category"]
    if overflow and more_prop:
        more = col.row(align=True)
        more.prop(
            props,
            more_prop,
            text="More (%d)" % len(overflow),
            toggle=True,
        )
        if getattr(props, more_prop, False):
            extra = None
            for index, tid in enumerate(overflow):
                if index % 2 == 0:
                    extra = col.row(align=True)
                op = extra.operator(
                    "espresso.select_template",
                    text=TEMPLATE_BY_ID[tid]["name"],
                    icon="FILE_BLEND",
                )
                op.template_id = tid
                op.category_name = TEMPLATE_BY_ID[tid]["category"]
    elif overflow:
        layout.prop(props, "template", text="All templates")


_TRANSFORM_PATHS = ("location", "rotation_euler", "rotation_quaternion", "scale", "delta_location", "delta_rotation_euler", "delta_scale")


def _drives_transforms(template):
    """Whether a channel plan lands on an object's transform.

    Not every multi-channel template is motion. An RGB cycle drives three
    components of a colour, and calling that "Motion" - or offering to apply it
    "Relative to Current Transform" - describes something the template does not
    do. The vocabulary follows what the channels actually target.
    """
    channels = espresso_templates.template_channels(template)
    paths = {c.get("data_path", "") for c in channels if c.get("data_path")}
    return bool(paths) and all(p in _TRANSFORM_PATHS for p in paths)


def _compact_level(context):
    """Return the current panel density, including legacy scene compatibility."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None:
        return 0
    level = max(0, min(2, int(getattr(props, "compact_level", 0))))
    # Files saved before the three-state cycle only have compact_mode.
    if level == 0 and getattr(props, "compact_mode", False):
        return 1
    return level


def _compact(context):
    """Compact Mode: show only what is needed to build a driver."""
    return _compact_level(context) > 0


def _compact_icon(level):
    """Stable header icon mapping shared by the main and Preview panels."""
    return (
        "ALIGN_JUSTIFY" if level == 0
        else "PROP_CON" if level == 2
        else "EVENT_OS"
    )


def _preview_compact_level(context):
    """Return the Preview panel's independent compact density."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None:
        return 0
    return max(0, min(2, int(getattr(props, "preview_compact_level", 0))))


def _section_enabled(context, name):
    """Whether an optional panel section is switched on in preferences."""
    prefs = utils.addon_preferences(context)
    return True if prefs is None else bool(getattr(prefs, name, True))


def _sidebar_tab(props):
    return getattr(props, "sidebar_tab", _SIDEBAR_TAB_MOTION)


def draw_favorites(layout, props, context):
    # Editions that ship a small curated catalogue have nothing to pin.
    from ...product import identity as _identity
    if not getattr(_identity, "HAS_FAVORITES", True):
        return
    if _compact(context) or not _section_enabled(context, "show_favorites_section"):
        return
    favorites = espresso_props.get_favorites(props)
    recents = espresso_props.get_recents(props)
    box = layout.box()
    header = box.row(align=True)
    header.prop(
        props,
        "favorites_open",
        text="",
        emboss=False,
        icon="TRIA_DOWN" if props.favorites_open else "TRIA_RIGHT",
    )
    header.label(text="FAVORITES & RECENT", icon="SOLO_ON")
    # Reuse the list parsed above; is_favorite() would re-parse the JSON blob a
    # second time on every redraw.
    star_icon = "SOLO_ON" if props.template in favorites else "SOLO_OFF"
    star = header.operator("espresso.toggle_favorite", text="", icon=star_icon)
    star.template_id = props.template
    if not props.favorites_open:
        return
    box.separator()
    box.label(
        text="Favorites (%d)" % len(favorites) if favorites else "Favorites",
        icon="SOLO_ON",
    )
    _draw_template_shortcut_row(
        box, props, favorites, "Star a template to pin it here.", "favorites_more_open",
    )
    box.separator()
    box.label(
        text="Recent (%d)" % len(recents) if recents else "Recent",
        icon="TIME",
    )
    _draw_template_shortcut_row(
        box, props, recents, "Recently used templates appear here.", "recents_more_open",
    )


def draw_template_toolbar(layout, props, template, context=None):
    # Template and Channel share one aligned column so the two selectors read
    # as a pair. Channel sits with the other selectors rather than in its own
    # box further down, where it would be easy to miss that a set has other
    # parts at all.
    selectors = layout.column(align=True)
    row = selectors.row(align=True)
    row.label(text="Template:")
    nav = row.row(align=True)
    op = nav.operator("espresso.navigate_template", text="", icon="TRIA_LEFT")
    op.direction = -1
    nav.prop(props, "template", text="")
    op = nav.operator("espresso.navigate_template", text="", icon="TRIA_RIGHT")
    op.direction = 1
    draw_channels(selectors, props, template, context)

    # Where this template sits, and what kind of thing it is. Subcategory tells
    # the user *which shelf* they are on; traits are orthogonal facts about the
    # expression (it loops, it needs a variable, it bakes the frame range).
    if _compact_level(context) < 2:
        meta = layout.row(align=True)
        meta.scale_y = 0.8
        meta.enabled = False  # a caption, not a control
        subcategory = (
            browse_groups.label_for_template(
                template.get("category", ""),
                template.get("id", ""),
            )
            or template.get("subcategory", "")
        )
        if subcategory:
            meta.label(text=subcategory, icon="OUTLINER_OB_GROUP_INSTANCE")
        # Which KIND of template this is - a raw shape you give meaning to, or one
        # real-world behaviour with parameters named for it. The distinction drives
        # what the parameters will look like, so it belongs beside the shelf name.
        category_name = template.get("category", "")
        if category_name:
            generic = espresso_templates.is_generic_category(category_name)
            meta.label(
                text="Generic" if generic else "Tailored",
                icon="IPO_LINEAR" if generic else "WORLD",
            )
        traits = espresso_templates.template_traits(template)
        if traits:
            meta.label(text=" · ".join(traits))

    if template.get("warning") and _compact_level(context) < 2:
        warn_box = layout.box()
        warn_box.alert = True
        draw_wrapped(warn_box, template["warning"], icon="ERROR", width=54)


# How many channels stay on screen as buttons before the rest fold into a
# dropdown. Three fits the panel width at normal sidebar sizes without the
# labels truncating to initials.
PINNED_CHANNEL_BUTTONS = 3


def _channel_render_mode(context, count):
    """Resolve the preference into 'swap' | 'buttons' | 'dropdown' for this count."""
    prefs = utils.addon_preferences(context)
    mode = getattr(prefs, "channel_display", "SWAP_BUTTONS_DROPDOWN") if prefs else "SWAP_BUTTONS_DROPDOWN"
    if mode == "DROPDOWN":
        return "dropdown"
    if mode == "BUTTONS":
        return "buttons"
    if mode == "SWAP_DROPDOWN":
        return "swap" if count == 2 else "dropdown"
    if mode == "BUTTONS_DROPDOWN":
        return "buttons" if count <= 3 else "dropdown"
    # SWAP_BUTTONS_DROPDOWN (default): swap a pair, button a trio, and for four
    # or more keep three on screen with the rest behind a dropdown. A plain
    # dropdown hid every channel behind a click even when there was room for
    # most of them - a five-channel set like Ball Bounce reads as one control
    # rather than as a set you can move around in.
    if count == 2:
        return "swap"
    if count <= PINNED_CHANNEL_BUTTONS:
        return "buttons"
    return "pinned"


def draw_channels(layout, props, template, context):
    """Channel picker: switch between sibling templates that are parts of one
    setup (a signal head's Green/Amber/Red, an ambulance bar's four colours).

    Drawn as one row directly beneath Template, sharing its label column, so the
    two selectors read as a pair: which recipe, then which part of it.

    An authoring recipe may own its channel rather than have siblings: a
    authoring rig's mount is two different cameras on one aircraft, and half its panel
    changes with the choice, so it belongs in the same pair.
    """
    if template.get("authoring_kind") == "CAMERA_DRONE":
        camera_drone_controls.draw_channel_row(layout, context)
        return
    siblings = espresso_templates.channel_row_entries(template, template["id"])
    modes = espresso_templates.mode_siblings(template)
    if len(siblings) < 2:
        # A lone channel with modes is still worth a mode row: Focus would be
        # the only channel of its set if the others were ever retired.
        _draw_channel_modes(layout, template, modes)
        return

    active_id = template["id"]
    active = next((s for s in siblings if s["id"] == active_id), siblings[0])
    mode = _channel_render_mode(context, len(siblings))

    row = layout.row(align=True)
    row.label(text="Channel:")
    picker = row.row(align=True)
    _draw_after = lambda: _draw_channel_modes(layout, template, modes)

    if mode == "swap":
        # One button showing the channel you are ON; clicking flips to the other.
        other = next((s for s in siblings if s["id"] != active_id), siblings[0])
        op = picker.operator("espresso.switch_variant", text=active["role_label"],
                             icon="ARROW_LEFTRIGHT")
        op.variant_id = other["id"]
        _draw_after()
        return

    if mode == "buttons":
        for sib in siblings:
            # depress highlights the active channel; clicking it re-selects
            # itself, which is a harmless no-op through switch_variant.
            op = picker.operator("espresso.switch_variant", text=sib["role_label"],
                                 depress=sib["id"] == active_id)
            op.variant_id = sib["id"]
        _draw_after()
        return

    if mode == "pinned":
        # Three on screen, the rest a click away.
        #
        # The pinned three are the first by role order, EXCEPT that the active
        # channel always takes the last slot when it would otherwise be hidden.
        # Without that, selecting a fifth channel left all three buttons
        # un-highlighted and the only clue to where you were was the dropdown
        # arrow - the row would say Bowling / Golf / Tennis while you were on
        # Ping-Pong.
        pinned = list(siblings[:PINNED_CHANNEL_BUTTONS])
        if not any(sib["id"] == active_id for sib in pinned):
            pinned[-1] = active
        for sib in pinned:
            op = picker.operator("espresso.switch_variant", text=sib["role_label"],
                                 depress=sib["id"] == active_id)
            op.variant_id = sib["id"]
        # Icon only: the buttons already carry the labels, so a named dropdown
        # beside them would repeat whichever one is active.
        picker.menu("ESPRESSO_MT_channel_picker", text="", icon="DOWNARROW_HLT")
        _draw_after()
        return

    picker.menu("ESPRESSO_MT_channel_picker", text=active["role_label"], icon="DOWNARROW_HLT")
    _draw_after()


def _draw_channel_modes(layout, template, modes):
    """The mode row: the same channel, done a different way.

    Drawn directly beneath the Channel row and sharing its label column, so the
    three selectors read downward as one thought: which recipe, which channel
    of it, which way that channel is specified. This is where the Focus rack's
    Distance / Target choice lives: among the SELECTORS rather than among the
    parameters, where it would hide the slots it governs.
    """
    if len(modes) < 2:
        return
    active_id = template["id"]
    row = layout.row(align=True)
    row.label(text="Mode:")
    picker = row.row(align=True)
    for member in modes:
        op = picker.operator(
            "espresso.switch_variant",
            text=member.get("mode_label") or member["name"],
            depress=member["id"] == active_id,
        )
        op.variant_id = member["id"]


def draw_template_variants(layout, props, template, context):
    if _compact(context) or not _section_enabled(context, "show_variants_section"):
        return
    # Variants are OTHER recipes you might reach for instead (Ambulance ↔ Alarm).
    # The suggested-pair row moved to the Channels picker: a pair like Police A/B
    # is two parts of one setup, not an alternative to it.
    if not template.get("variants"):
        return

    variants_box = layout.box()
    header = variants_box.row(align=True)
    header.prop(
        props,
        "template_variants_open",
        text="",
        emboss=False,
        icon="TRIA_DOWN" if props.template_variants_open else "TRIA_RIGHT",
    )
    header.label(text="VARIANTS", icon="GRAPH")
    if not props.template_variants_open:
        return

    # 2-column grid keeps the variant list compact. Every declared variant stays
    # reachable; the grid grows instead of hiding later entries.
    var_grid = variants_box.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
    for variant_id in template["variants"]:
        variant = TEMPLATE_BY_ID.get(variant_id)
        if variant:
            op = var_grid.operator("espresso.switch_variant", text=variant["name"])
            op.variant_id = variant_id


def _draw_param_label(row, param, index, context=None):
    text = _param_row_label(param, context) if context is not None else param["label"]
    op = row.operator("espresso.inspect_param", text=text, emboss=False)
    op.slot_index = index


def _draw_advanced_param_label(row, control):
    op = row.operator("espresso.inspect_advanced_param", text=control["label"], emboss=False)
    op.token = control["token"]


def _show_raw_parameters(context):
    prefs = utils.addon_preferences(context)
    return bool(getattr(prefs, "show_raw_parameters", False))


def _param_row_label(param, context):
    """Domain label, plus the underlying maths token when the user asks for it.

    Friendly parameters hide the expression token they feed ("Seconds per
    revolution" sets TURNS). Power users can reveal the mapping instead of
    reverse-engineering it from the formula.
    """
    label = param.get("label", "Parameter")
    if not _show_raw_parameters(context):
        return label
    derives = param.get("derives")
    if derives:
        return f"{label}  [{param['token']} → {', '.join(sorted(derives))}]"
    return f"{label}  [{param['token']}]"


def _live_owner_parameter(context, props, param):
    """Return the authoritative keyframeable endpoint for a Live control."""
    if context is None or getattr(props, "parameter_mode", "SETUP") != "LIVE":
        return None
    template = espresso_props.live_parameter_template(context, for_draw=True)
    binding = espresso_props.live_parameter_binding(context, template, for_draw=True)
    if binding is None or not binding.available:
        return None
    token = str(param.get("token", ""))
    owner = None
    path = ""
    component = -1
    if binding.route == "Prepared Layout Structure":
        carrier = binding.payload
        owner = layout_preparation.layout_modifier(carrier)
        identifier = layout_preparation.input_identifier(
            carrier, str(param.get("label", "")),
        )
        path = '["%s"]' % identifier if identifier else ""
    if owner is None or not path or path[2:-2] not in owner.keys():
        return None
    return owner, path, component


def _draw_single_param_cell(layout, props, param, index, context=None):
    row = layout.row(align=True)
    label_and_value = row.split(factor=0.38, align=True)
    _draw_param_label(label_and_value.row(align=True), param, index, context)

    value_row = label_and_value.row(align=True)
    # Auto Fit belongs to a position-wave recipe this product does not
    # ship, so nothing here ever locks the frequency field.
    auto_fit_locks_frequency = False
    value_row.enabled = not auto_fit_locks_frequency
    prop_name = espresso_props.param_property_name(param, index)
    live_owner = _live_owner_parameter(context, props, param)
    value_owner, value_path, value_component = live_owner or (props, prop_name, -1)
    if param.get("type") == "BOOL":
        # A pressable button that fills the value column, rather than the small
        # checkbox Blender draws by default: it reads as a mode switch at a
        # glance, matches the width of every other parameter's field, and states
        # which mode is active instead of leaving the user to decode a tick.
        states = param.get("state_labels") or ("Off", "On")
        enabled = bool(
            (value_owner[value_path[2:-2]][value_component]
             if live_owner and value_component >= 0
             else value_owner[value_path[2:-2]])
            if live_owner else getattr(props, prop_name, False)
        )
        value_row.prop(
            value_owner, value_path,
            text=states[1] if enabled else states[0], toggle=True,
            index=value_component,
        )
    else:
        value_row.prop(
            value_owner, value_path, text="", index=value_component,
        )
    # The unit rides beside the field rather than inside the label, so the label
    # stays scannable and the number never loses its meaning.
    unit = param.get("unit")
    if unit:
        unit_cell = value_row.row()
        unit_cell.enabled = False   # renders dimmed; it is a caption, not a control
        unit_cell.label(text=unit)

    actions = row.row(align=True)
    actions.enabled = not auto_fit_locks_frequency
    if param.get("presets"):
        actions.menu(f"ESPRESSO_MT_presets_{index}", text="", icon="PRESET")
    reset_op = actions.operator("espresso.reset_param_default", text="", icon="LOOP_BACK")
    reset_op.slot_index = index


def _draw_paired_param_cells(layout, props, group, context=None):
    pair_row = layout.row(align=True)
    split = pair_row.split(factor=0.5, align=True)
    left = split.row(align=True)
    right = split.row(align=True)
    _draw_single_param_cell(left, props, group[0]["param"], group[0]["index"], context)
    _draw_single_param_cell(right, props, group[1]["param"], group[1]["index"], context)


def _draw_single_advanced_control_cell(layout, props, control):
    if control.get("type") == "BOOL":
        row = layout.row(align=True)
        row.prop(props, espresso_props.advanced_param_property_name(control), text=control["label"], toggle=True)
        return
    row = layout.row(align=True)
    label_and_value = row.split(factor=0.38, align=True)
    _draw_advanced_param_label(label_and_value.row(align=True), control)
    value_row = label_and_value.row(align=True)
    value_row.prop(props, espresso_props.advanced_param_property_name(control), text="")
    unit = control.get("unit")
    if unit:
        unit_cell = value_row.row()
        unit_cell.enabled = False
        unit_cell.label(text=unit)

    reset_op = row.operator("espresso.reset_advanced_param_default", text="", icon="LOOP_BACK")
    reset_op.token = control["token"]


def _advanced_control_is_visible(props, control):
    condition = control.get("visible_if")
    if not condition:
        return True
    dependency = espresso_templates.resolve_advanced_param(condition["token"])
    current = espresso_props.get_advanced_param_value(props, dependency)
    if "truthy" in condition:
        return bool(current) is bool(condition["truthy"])
    return True


def _draw_paired_advanced_control_cells(layout, props, controls):
    pair_row = layout.row(align=True)
    split = pair_row.split(factor=0.5, align=True)
    left = split.row(align=True)
    right = split.row(align=True)
    _draw_single_advanced_control_cell(left, props, controls[0])
    _draw_single_advanced_control_cell(right, props, controls[1])


def _draw_advanced_group(box, props, metadata, controls):
    header = box.row(align=True)
    open_prop = metadata["open_prop"]
    is_open = getattr(props, open_prop)
    header.prop(
        props,
        open_prop,
        text="",
        emboss=False,
        icon="TRIA_DOWN" if is_open else "TRIA_RIGHT",
    )
    header.label(text=metadata["label"], icon=metadata["icon"])
    if not is_open:
        return

    controls = [control for control in controls if _advanced_control_is_visible(props, control)]
    if not controls:
        return
    box.separator()
    index = 0
    while index < len(controls):
        current = controls[index]
        next_control = controls[index + 1] if index + 1 < len(controls) else None
        if next_control:
            token_pair = frozenset({current["token"], next_control["token"]})
            labels_compact = len(current["label"]) <= 14 and len(next_control["label"]) <= 14
            if token_pair in PAIRABLE_TOKEN_SETS and labels_compact:
                _draw_paired_advanced_control_cells(box, props, [current, next_control])
                index += 2
                continue
        _draw_single_advanced_control_cell(box, props, current)
        index += 1


def draw_advanced_controls(layout, props, template, context):
    if not _section_enabled(context, "show_advanced_section"):
        return
    grouped_controls = _group_supported_advanced_controls(template)
    if not any(grouped_controls.values()):
        return

    prefs = utils.addon_preferences(context)
    advanced_enabled = bool(getattr(prefs, "enable_advanced_controls", False)) if prefs else False

    # The whole section is opt-in from Preferences. When Advanced Controls is off
    # there, render NOTHING - not even the collapsed header - so the panel does
    # not carry an empty box for a feature the user has not turned on.
    if not advanced_enabled:
        return

    advanced_box = layout.box()
    header = advanced_box.row(align=True)

    # Checkbox writes directly to prefs so it stays in sync with the Preferences panel.
    if prefs is not None:
        header.prop(prefs, "enable_advanced_controls", text="", toggle=True)
    else:
        header.label(text="", icon="BLANK1")

    if advanced_enabled:
        header.operator(
            "espresso.toggle_advanced_section",
            text="",
            emboss=False,
            icon="TRIA_DOWN" if _advanced_section_open(context) else "TRIA_RIGHT",
        )
    header.label(text="ADVANCED", icon="PREFERENCES")

    if not advanced_enabled or not _advanced_section_open(context):
        return

    advanced_box.separator()
    visible_groups = _visible_advanced_groups(context, template)
    for group_index, (_group_id, metadata, controls) in enumerate(visible_groups):
        _draw_advanced_group(advanced_box.box(), props, metadata, controls)
        if group_index != len(visible_groups) - 1:
            advanced_box.separator(factor=0.4)


_ADAPTIVE_THRESHOLD = 3


def _quick_preset_indices(template, prefs):
    """Return up to 3 preset indices to show in the quick-access shelf."""
    combined = template.get("combined_presets", [])
    if not combined:
        return []
    mode = prefs.quick_preset_mode if prefs else "FEATURED"
    featured = list(range(min(3, len(combined))))
    if mode == "OFF":
        return []
    if mode == "FEATURED":
        return featured
    from ... import config
    usage = config.get_preset_usage(template["id"])
    # Usage is keyed by label, so a preset that was renamed or removed simply
    # stops matching instead of handing its score to whatever now sits at its
    # old index. Ties keep catalogue order rather than falling to dict order.
    total_clicks = sum(usage.values())
    scored = [(i, usage.get(combined[i]["label"], 0)) for i in range(len(combined))]
    used = [(i, n) for i, n in scored if n > 0]
    ranked = [i for i, _ in sorted(used, key=lambda pair: (-pair[1], pair[0]))[:3]]
    if mode == "ADAPTIVE":
        if total_clicks < _ADAPTIVE_THRESHOLD:
            return featured
        return ranked
    return ranked


def _draw_quick_preset_shelf_from_indices(layout, template, indices):
    row = layout.row(align=True)
    for idx in indices:
        preset = template["combined_presets"][idx]
        op = row.operator("espresso.set_master_preset", text=preset["label"])
        op.preset_index = idx


def _draw_param_group_cells(layout, props, template, groups, context):
    for group in groups:
        if len(group) == 2:
            _draw_paired_param_cells(layout, props, group, context)
        else:
            item = group[0]
            _draw_single_param_cell(layout, props, item["param"], item["index"], context)


def _draw_collapsible_param_group(layout, props, template, name, groups, context):
    if not groups:
        return
    box = layout.box()
    header = box.row(align=True)
    prop_name = "param_group_%s_open" % name
    header.prop(
        props,
        prop_name,
        text="",
        emboss=False,
        icon="TRIA_DOWN" if getattr(props, prop_name) else "TRIA_RIGHT",
    )
    header.label(text=ui_shelf.UI_GROUP_LABELS[name].upper())
    if not getattr(props, prop_name):
        return
    _draw_param_group_cells(box, props, template, groups, context)


def _quick_preset_shelf_indices(template, context):
    combined_presets = template.get("combined_presets", [])
    if not combined_presets or not context:
        return []
    addon = context.preferences.addons.get(utils.ADDON_PACKAGE)
    prefs = addon.preferences if addon else None
    return _quick_preset_indices(template, prefs)


def _hidden_param_tokens(template, binding, values, context, extra=None):
    hidden = set(extra or ())
    visible = parameter_bindings.visible_tokens(
        binding if parameter_bindings.supports(template) else None,
        template,
        values,
    )
    hidden.update(
        param.get("token", "")
        for param in template.get("params", ())
        if param.get("token", "") not in visible
    )
    template_id = (template or {}).get("id", "")
    return hidden


def _shelf_param_groups(template, hidden_tokens, props):
    shelf_groups = {name: [] for name in ui_shelf.UI_GROUPS}
    is_ramp_steps = False  # no stepped-ramp recipe ships here
    excluded = ({"STOPS"} if is_ramp_steps else set()) | set(hidden_tokens)
    for group in pairable_param_groups(
            template, excluded_tokens=excluded, props=props):
        shelf_groups[ui_shelf.ui_group(template, group[0]["param"])].append(group)
    return shelf_groups


def _draw_applied_preset_shelf(layout, template, indices, destination):
    row = layout.row(align=True)
    for idx in indices:
        preset = template["combined_presets"][idx]
        op = row.operator(
            "espresso.set_applied_settings_preset", text=preset["label"],
        )
        op.preset_index = idx
        op.template_id = template.get("id", "")
        op.destination = destination


def _draw_param_item_cell(layout, item, spec, context):
    row = layout.row(align=True)
    label_and_value = row.split(factor=0.38, align=True)
    text = _param_row_label(spec, context) if spec else item.label
    label_and_value.row(align=True).label(text=text)
    value_row = label_and_value.row(align=True)
    if item.value_type == "BOOL":
        states = (spec or {}).get("state_labels") or ("Off", "On")
        value_row.prop(
            item, "bool_value",
            text=states[1] if item.bool_value else states[0],
            toggle=True,
        )
    else:
        prop = {
            "INT": "int_value", "COLOR": "color_value",
            "STRING": "string_value", "ENUM": "string_value",
        }.get(item.value_type, "float_value")
        value_row.prop(item, prop, text="")
    unit = item.unit or (spec or {}).get("unit", "")
    if unit:
        unit_cell = value_row.row()
        unit_cell.enabled = False
        unit_cell.label(text=unit)


def _draw_param_item_group_cells(layout, groups, items_by_token, context):
    for group in groups:
        if len(group) == 2:
            pair_row = layout.row(align=True)
            split = pair_row.split(factor=0.5, align=True)
            owners = (split.row(align=True), split.row(align=True))
            for cell, owner in zip(group, owners):
                spec = cell["param"]
                item = items_by_token.get(spec["token"])
                if item is not None:
                    _draw_param_item_cell(owner, item, spec, context)
            continue
        spec = group[0]["param"]
        item = items_by_token.get(spec["token"])
        if item is not None:
            _draw_param_item_cell(layout, item, spec, context)


def _draw_collapsible_param_item_group(layout, props, name, groups, items_by_token, context):
    if not groups:
        return
    box = layout.box()
    header = box.row(align=True)
    prop_name = "param_group_%s_open" % name
    header.prop(
        props,
        prop_name,
        text="",
        emboss=False,
        icon="TRIA_DOWN" if getattr(props, prop_name) else "TRIA_RIGHT",
    )
    header.label(text=ui_shelf.UI_GROUP_LABELS[name].upper())
    if not getattr(props, prop_name):
        return
    _draw_param_item_group_cells(box, groups, items_by_token, context)


def draw_parameter_value_controls(layout, props, template, context):
    """Presets, grouped widgets, and alias notes used by Applied settings."""
    indices = _quick_preset_shelf_indices(template, context)
    if indices:
        _draw_quick_preset_shelf_from_indices(layout, template, indices)

    params = template.get("params", [])
    if not params:
        # An authoring recipe has no CATALOGUE parameters and a rig full of
        # its own controls; saying "none needed" directly above forty of them
        # is a contradiction, so it is said only where it is true.
        if panel_sections.applies("empty_parameter_note", template):
            layout.label(text="No parameters needed.", icon="INFO")
        return

    is_ramp_steps = False  # no stepped-ramp recipe ships here
    if is_ramp_steps and context is not None:
        _draw_preapply_colour_ramp(layout, props, context)

    binding = (
        espresso_props.live_parameter_binding(context, template, for_draw=True)
        if context and props.parameter_mode == "LIVE" else None
    )
    hidden_tokens = _hidden_param_tokens(
        template, binding, espresso_props.collect_values(props, template), context,
    )
    shelf_groups = _shelf_param_groups(template, hidden_tokens, props)
    use_shelf = any(shelf_groups[name] for name in ("timing", "spatial", "additional"))
    if use_shelf:
        _draw_param_group_cells(layout, props, template, shelf_groups["essential"], context)
        for name in ("timing", "spatial", "additional"):
            _draw_collapsible_param_group(
                layout, props, template, name, shelf_groups[name], context,
            )
    else:
        _draw_param_group_cells(layout, props, template, shelf_groups["essential"], context)

    alias_note = aliasing.advice(template, espresso_props.collect_values(props, template))
    if alias_note:
        layout.separator(factor=0.6)
        draw_wrapped(layout, alias_note, icon="ERROR", width=50)


def draw_parameter_item_controls(layout, props, template, items, context, *, destination="DIALOG"):
    """Draw a CollectionProperty with the same shelf, order, and BOOL widgets."""
    indices = _quick_preset_shelf_indices(template, context)
    if indices:
        _draw_applied_preset_shelf(layout, template, indices, destination)
    items_by_token = {item.token: item for item in items}
    hidden = {
        param.get("token", "")
        for param in template.get("params", ())
        if param.get("token", "") not in items_by_token
    }
    shelf_groups = _shelf_param_groups(template, hidden, props)
    use_shelf = any(shelf_groups[name] for name in ("timing", "spatial", "additional"))
    if use_shelf:
        _draw_param_item_group_cells(layout, shelf_groups["essential"], items_by_token, context)
        for name in ("timing", "spatial", "additional"):
            _draw_collapsible_param_item_group(
                layout, props, name, shelf_groups[name], items_by_token, context,
            )
    else:
        _draw_param_item_group_cells(layout, shelf_groups["essential"], items_by_token, context)


def draw_applied_effect_settings(layout, context, effect, template, fallback_items):
    """Popup/pin body that matches the live Applied parameter shelf."""
    if template is None:
        layout.label(text="Template is no longer available.", icon="ERROR")
        return
    props = context.scene.espresso_props
    if effect and espresso_props.settings_share_live_props(context, effect, template):
        draw_parameter_value_controls(layout, props, template, context)
        return
    items = getattr(context.window_manager, "espresso_applied_dialog_parameters", None)
    if items is None or len(items) == 0:
        items = fallback_items
    draw_parameter_item_controls(
        layout, props, template, items, context, destination="DIALOG",
    )


def draw_parameters(layout, props, template, context=None):
    if template.get("authoring_kind") == "CAMERA_FREE_ROAM":
        camera_free_roam.draw_template(layout, props, template, context)
        return
    params_box = layout.box()

    live_mode = props.parameter_mode == "LIVE"
    controls_template = (
        espresso_props.live_parameter_template(context, template, for_draw=True)
        if live_mode and context is not None else template
    )

    combined_presets = controls_template.get("combined_presets", [])
    shelf_indices = _quick_preset_shelf_indices(controls_template, context)
    has_overflow = len(combined_presets) > len(shelf_indices)

    # Two aligned sub-rows, not one flat row: a plain label stretches to fill,
    # which pushed the Master Presets menu to the far edge away from the thing it
    # belongs to. LEFT holds the title and its own menu; RIGHT holds the reset.
    header = params_box.row(align=True)
    left = header.row(align=True)
    left.alignment = "LEFT"
    left.prop(
        props,
        "parameters_open",
        text="",
        emboss=False,
        icon="TRIA_DOWN" if props.parameters_open else "TRIA_RIGHT",
    )
    left.label(text="PARAMETERS", icon="PREFERENCES")
    if has_overflow:
        left.menu("ESPRESSO_MT_master_presets_drpdwn", text="", icon="PRESET_NEW")

    right = header.row(align=True)
    right.alignment = "RIGHT"
    # A recipe that flies a rig can tell the artist how the flight went. The
    # button carries the alert, so a shot that needs attention says so from
    # the header instead of from eight lines at the bottom of the panel.
    if template.get("authoring_kind") == "CAMERA_DRONE":
        camera_drone_controls.draw_diagnosis_button(right, context)
    right.operator("espresso.reset_template_defaults", text="", icon="LOOP_BACK")
    if not props.parameters_open:
        return

    params_box.separator()

    controls = params_box
    if len(templates.application_modes(template)) > 1:
        application_row = params_box.row(align=True)
        application_row.prop(props, "application_mode", expand=True)
    if parameter_bindings.supports(template) and template.get("authoring_kind"):
        # Authoring controls update their rig directly; no Setup/Live switch is needed.
        status, is_live = camera_drone_controls.live_state(
            context, controls_template,
        )
        params_box.label(text=status, icon="LINKED" if is_live else "INFO")
        controls = params_box.column()
    elif parameter_bindings.supports(template):
        mode_row = params_box.row(align=True)
        mode_row.prop(props, "parameter_mode", expand=True)
        identity = espresso_props.authoring_presentation(props, context)
        binding = espresso_props.live_parameter_binding(context, controls_template) if context else None
        live_available = bool(binding and binding.available)
        live_editable = bool(live_available and binding.editable)
        binding_key = parameter_bindings.binding_key(binding)
        live_needs_reload = live_mode and binding_key != props.live_parameter_binding_key
        if live_mode:
            choices = parameter_bindings.active_effect_choices(context)
            # Always drawn, not only when there are two or more to pick
            # between. Hiding it below that made the control appear on some
            # templates and not others with nothing to explain the difference,
            # and it took away the one place that says WHICH applied effect
            # Live is editing -- which is worth seeing even when there is only
            # one of them.
            picker = params_box.row(align=True)
            picker.label(text="Applied effect:")
            menu = picker.row(align=True)
            menu.enabled = bool(choices)
            # The CLOSED control has to say which one too, or the artist has
            # to open the menu to find out what they are already editing.
            chosen = identity["effect_name"]
            if chosen:
                here = live_effect_channels(context).get(
                    identity.get("record_token", ""), "")
                if here:
                    chosen = "%s \u00b7 %s" % (chosen, here)
            menu.menu(
                "ESPRESSO_MT_live_effects",
                text=(chosen
                      or ("Choose applied effect" if choices
                          else "Nothing applied in this scope")),
                icon="DOWNARROW_HLT",
            )
            # The scope this list is drawn from, beside the list itself. It
            # already exists in Organize, but an artist in Live who cannot see
            # their effect has no way to learn from here that they are looking
            # at one object rather than the scene.
            #
            # Active and Scene only. Last is a memory of the most recent
            # APPLY, which answers a different question from "which applied
            # effect am I editing" -- and Live is always editing one specific
            # effect, so it has nothing to offer here. It stays in Organize,
            # where "what did I just do" is the question being asked.
            scope = picker.row(align=True)
            scope.prop_enum(props, "driver_target_source", "ACTIVE", text="A")
            scope.prop_enum(props, "driver_target_source", "SCENE", text="S")
            if live_needs_reload:
                status = "Active setup changed — reload its Live parameters."
            elif identity["resolved"] and not live_editable:
                # Resolved but locked. It prints the reason carried on the
                # binding, not the headline: otherwise the controls grey out
                # with the effect's name above them and nothing saying why. Say
                # it: a locked control with no explanation reads as a broken
                # add-on.
                status = (binding.reason if binding is not None and binding.reason
                          else "This applied effect cannot be edited live.")
            elif identity["resolved"]:
                status = identity["headline"]
            else:
                status = identity["reason"] or (
                    binding.reason if binding is not None else "No applied effect is selected."
                )
            params_box.label(
                text=status,
                icon="FILE_REFRESH" if live_needs_reload else ("LINKED" if identity["resolved"] and live_editable else "INFO"),
            )
            if live_needs_reload:
                params_box.operator(
                    "espresso.reload_live_parameters",
                    text="Reload Live Parameters",
                    icon="FILE_REFRESH",
                )
        if identity["modified"]:
            params_box.label(text="Modified since last update", icon="ERROR")
        controls = params_box.column()
        controls.enabled = not live_mode or (live_editable and not live_needs_reload)

    if context is not None:
        camera_template_targets.draw(
            controls, context, props, controls_template,
        )
    draw_parameter_value_controls(controls, props, controls_template, context)
    if context is not None:
        # After the parameters, because a recipe's findings comment on the
        # values just drawn and an authoring rig's controls are its parameters.
        camera_rig_tools.draw(controls, context, props, controls_template)

    if (props.result_summary and _compact_level(context) < 2
            and controls_template.get("id") == template.get("id")):
        controls.separator(factor=0.6)
        # A description of what the template will do - reference the artist
        # reads once, not something they act on, so it lives in a hover tip.
        draw_note(controls, props.result_summary,
                  label=props.result_summary[:38])


def _lightweight_preview(context):
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is not None:
        return bool(getattr(props, "lightweight_preview", False))
    return False


def image_preview_available(context):
    """True when the rendered pixel graph will be used (so the braille text
    chart is dead weight). Mirrors the condition in _draw_result_graph."""
    from . import image_preview

    return image_preview.available() and not _lightweight_preview(context)


# The cursorless pixel buffer reused while the animation plays, plus the last
# stamped column and its icon id. Holds at most one graph's worth: a 224x224
# RGBA float buffer is 784 KB, so keeping stale ones alive would be a real leak.
# Cleared as soon as playback stops, so nothing survives into an idle session.
_PLAYBACK_BASE = {}


def _draw_result_graph(
    graph_box, result, props, context, value_scale=None, marks=(),
    colour_strip=(), colour_key="",
):
    """Render a PreviewResult as a cached pixel image (default) or a
    text/braille fallback. Shared by the Template and Active Target graph
    blocks so both data sources get identical caching and rendering."""
    # Stats is plain numbers (alignment-independent), so it stays as text.
    if props.visualizer_style == "STATS":
        graph_col = graph_box.column(align=True)
        for line in result.graph.split("\n"):
            graph_col.label(text=line)
        return

    # Each chart style renders as an accurate pixel image by default. The
    # lightweight preference (or a build without preview images) instead draws an
    # aligned braille text curve, which uses far less CPU on weak computers.
    from . import image_preview, visualizer, preview_activity

    drawn = False
    result_frames = getattr(result, "frames", ()) or ()
    frame_start, frame_end = (
        (result_frames[0], result_frames[-1])
        if len(result_frames) >= 2
        else (context.scene.frame_start, context.scene.frame_end)
    )
    if image_preview.available() and not _lightweight_preview(context):
        # template_icon fits the image inside a square sized by `scale`; a square
        # source fills it with no wasted letterbox.
        side = 224
        # Two independent graph controls:
        #  - value_scale: the fixed (min, max) the pixels are drawn against
        #    after Fixed Scale and viewport Zoom are resolved. It changes the
        #    rendered buffer, so it is part of the icon cache key.
        #  - display_scale: how large that 224px image is shown in the panel,
        #    driven by the Scale slider. It only resizes an already-rendered
        #    icon, so it stays OUT of the cache key (no re-render on Scale).
        display_factor = float(getattr(props, "visualizer_scale", 1.0))
        display_scale = (16.0 if props.visualizer_detailed else 12.0) * display_factor
        anchor_tag = ("a%.4g_%.4g" % value_scale) if value_scale else "auto"
        if marks:
            anchor_tag += "-m" + "_".join("%.4g" % float(m) for m in marks)
        if colour_key:
            anchor_tag += "-c" + colour_key
        # The cursor line is baked into the rendered image, so keying on the
        # current frame makes every frame change a cache miss and a full
        # 224x224 re-render (measured 0.77-1.4ms).
        #
        # Dropping the cursor during playback to avoid that cost would make the
        # playhead vanish the moment you hit play, which is when it is most
        # useful for reading the curve. Keying per frame instead is not an
        # option either: one preview is 784 KB, so a 250-frame playback would
        # allocate ~191 MB, and this collection cannot evict individually - at
        # its cap it clears wholesale, discarding every cached graph
        # mid-playback.
        #
        # So playback splits the work: the curve, guides, clamp marks and axis
        # labels are frame-independent and rendered ONCE into a cached
        # cursorless buffer, then each frame stamps the one column that moves
        # and re-uploads into a single stable cache entry.
        is_playing = bool(getattr(context.screen, "is_animation_playing", False))
        cursor_frame = preview_activity.frame(context)
        icon_id = None
        if is_playing:
            base_key = f"{result.cache_key}-{props.visualizer_style}-base-{anchor_tag}"
            play_key = f"{result.cache_key}-{props.visualizer_style}-play-{anchor_tag}"
            base = _PLAYBACK_BASE.get(base_key)
            if base is None:
                base = visualizer.render_image_pixels(
                    result.points, width=side, height=side,
                    style=props.visualizer_style,
                    frame_start=frame_start, frame_end=frame_end,
                    frame_current=None, scale=value_scale, marks=marks,
                    colour_strip=colour_strip,
                )
                # Only ever one base is useful - the graph currently on screen.
                # Holding more would keep 784 KB buffers alive per stale graph.
                _PLAYBACK_BASE.clear()
                _PLAYBACK_BASE[base_key] = base
            column = visualizer.cursor_column_for(side, frame_start, frame_end, cursor_frame)
            # Adjacent frames often land in the same pixel column, and
            # re-uploading identical pixels is pure cost - skip those.
            if _PLAYBACK_BASE.get("_col") != (play_key, column):
                pixels = visualizer.stamp_cursor(base[0], side, side, column)
                icon_id = image_preview.refresh_graph_icon(play_key, pixels, base[1])
                _PLAYBACK_BASE["_col"] = (play_key, column)
                _PLAYBACK_BASE["_id"] = icon_id
            else:
                icon_id = _PLAYBACK_BASE.get("_id")
        else:
            # Idle or scrubbing: one keyed entry per frame is fine, and it
            # means re-showing a frame is a straight cache hit.
            _PLAYBACK_BASE.clear()
            key = f"{result.cache_key}-{props.visualizer_style}-{int(cursor_frame)}-{anchor_tag}"
            icon_id = image_preview.cached_icon_id(key)
            if icon_id is None:
                pixels, size = visualizer.render_image_pixels(
                    result.points,
                    width=side,
                    height=side,
                    style=props.visualizer_style,
                    frame_start=frame_start,
                    frame_end=frame_end,
                    frame_current=cursor_frame,
                    scale=value_scale,
                    marks=marks,
                    colour_strip=colour_strip,
                )
                icon_id = image_preview.get_graph_icon_id(key, pixels, size)
        if icon_id:
            try:
                icon_row = graph_box.row()
                icon_row.alignment = "CENTER"
                icon_row.template_icon(icon_value=icon_id, scale=display_scale)
                drawn = True
            except Exception:
                drawn = False
    if not drawn:
        # result.graph is empty when the pixel path was expected to draw it
        # (want_text=False), so build the braille chart on demand here instead
        # of paying 419-602us for it on every draw that never displays it.
        text_graph = result.graph
        if not text_graph and result.points:
            text_graph = visualizer.render_text(
                list(result.points), props.visualizer_style,
                width=48, height=7 if props.visualizer_detailed else 5,
                frame_start=frame_start,
                frame_end=frame_end,
                frame_current=preview_activity.frame(context),
                scale=value_scale,
                marks=marks,
            )
        graph_col = graph_box.column(align=True)
        for line in text_graph.split("\n"):
            graph_col.label(text=line)
        # The braille chart cannot draw the clamp lines the pixel graph shows (a
        # cell is 2x4 dots, so a horizontal rule reads as the curve crossing it),
        # so state them as text rather than dropping the information silently.
        if marks:
            graph_col.label(
                text="Clamp: " + " / ".join("%.4g" % float(m) for m in marks),
                icon="CON_CLAMPTO",
            )
    numbers = graph_box.row(align=True)
    numbers.label(text="Min %.4g" % result.minimum)
    numbers.label(text="Max %.4g" % result.maximum)
    cursor = (result.stats or {}).get("cursor")
    if cursor is not None:
        numbers.label(text="Now %.4g" % float(cursor))


_motion_preview_states = {}


def _motion_preview_policy(props, context, values):
    from . import visualizer
    screen = getattr(context, "screen", None)
    return None


def _dynamic_motion_icon(cache_key, signature, render):
    """Refresh one stable preview entry only when its semantic state changes."""
    from . import image_preview
    state = _motion_preview_states.get(cache_key)
    if state == signature:
        icon_id = image_preview.cached_icon_id(cache_key)
        if icon_id is not None:
            return icon_id
    pixels, size = render()
    icon_id = image_preview.refresh_graph_icon(cache_key, pixels, size)
    if icon_id is not None:
        if cache_key not in _motion_preview_states and len(_motion_preview_states) >= 32:
            _motion_preview_states.clear()
        _motion_preview_states[cache_key] = signature
    return icon_id


#: Keys already queued for a pre-apply preview, so the same one is
#: not rebuilt on every redraw.
_pending_preapply_ramps = set()


def _draw_showcase_grid_presentation(graph_box, props, template, context):
    """Draw Kinetic Wave as spatial cells rather than a misleading line."""
    from . import image_preview, visualizer

    if not image_preview.available() or _lightweight_preview(context):
        graph_box.label(
            text="Pixel presentation requires Pixel Preview mode.",
            icon="IMAGE_BACKGROUND",
        )
        return
    values = espresso_props.collect_values(props, template)
    carrier = layout_preparation.resolve_carrier(getattr(context, "active_object", None))
    if carrier is not None:
        weights = tuple(value for _source, value in layout_preparation.source_weights(carrier))
    else:
        count = max(1, len([
            obj for obj in getattr(context, "selected_objects", ()) if obj.type == "MESH"
        ]))
        weights = (1.0,) * count
    policy = _motion_preview_policy(props, context, values)
    frame = policy["frame"]
    render_size = policy["size"]
    key_values = tuple(sorted(
        (name, round(float(value), 6)) for name, value in values.items()
        if isinstance(value, (int, float))
    ))
    scene_key = getattr(context.scene, "as_pointer", lambda: id(context.scene))()
    key = "showcase-grid-dynamic-%s-%s" % (scene_key, render_size)
    signature = (key_values, weights, policy["phase_key"], render_size)
    icon_id = _dynamic_motion_icon(
        key, signature,
        lambda: visualizer.render_showcase_grid_pixels(
            values, frame=frame, source_weights=weights,
            width=render_size, height=render_size,
        ),
    )
    if icon_id:
        row = graph_box.row()
        row.alignment = "CENTER"
        display_factor = float(getattr(props, "visualizer_scale", 1.0))
        row.template_icon(icon_value=icon_id, scale=14.0 * display_factor)


#: Keys already queued for a pre-apply preview, so the same one is
#: not rebuilt on every redraw.
_pending_preapply_ramp_previews = set()


def _schedule_preapply_ramp(scene, props):
    key = scene.as_pointer()
    if key in _pending_preapply_ramps:
        return
    _pending_preapply_ramps.add(key)

    def _create():
        _pending_preapply_ramps.discard(key)
        try:
            from ...apply import colour_ramp

            colour_ramp.template_ramp(scene)
            espresso_props.refresh_preview(props, bpy.context)
        except Exception:
            pass
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    bpy.app.timers.register(_create, first_interval=0.0)


def _draw_preapply_colour_ramp(box, props, context):
    """Draw the scene's real template ColorRamp before it is applied."""
    from ...apply import colour_ramp

    node = colour_ramp.template_ramp_node(context.scene, create=False)
    if node is None:
        _schedule_preapply_ramp(context.scene, props)
        row = box.row()
        row.enabled = False
        row.label(text="Preparing palette…", icon="COLOR")
        return

    ramp_box = box.box()
    header = ramp_box.row(align=True)
    header.label(
        text="%d Colour Stops" % len(node.color_ramp.elements),
        icon="COLOR",
    )
    header.menu("ESPRESSO_MT_palettes", text="Palette", icon="PRESET")
    header.prop(node.color_ramp, "interpolation", text="")
    ramp_box.template_color_ramp(node, "color_ramp", expand=True)

    # Motion interpolation is a template parameter, not a ColorRamp setting.
    # Keep it directly below the ramp, but align it with the normal parameter
    # rows that follow so the two interpolation controls cannot be conflated.
    transition = box.row(align=True)
    transition_cells = transition.split(factor=0.38, align=True)
    transition_cells.row(align=True).label(text="Transition Mode")
    transition_cells.row(align=True).prop(
        props, "ramp_transition_mode", text="")

    # ColorRamp elements are not RNA properties on ESPRESSO_Props, so Blender
    # cannot call on_param_update when the artist presses +/-. Detect only the
    # one value the expression depends on (element count), then defer the
    # preview rebuild because panel draw itself is read-only.
    stop_count = len(node.color_ramp.elements)
    if node.get(colour_ramp.TEMPLATE_RAMP_PREVIEW_STOPS_TAG) != stop_count:
        key = (context.scene.as_pointer(), stop_count)
        if key not in _pending_preapply_ramp_previews:
            _pending_preapply_ramp_previews.add(key)

            def _refresh():
                _pending_preapply_ramp_previews.discard(key)
                try:
                    node[colour_ramp.TEMPLATE_RAMP_PREVIEW_STOPS_TAG] = stop_count
                    espresso_props.refresh_preview(props, bpy.context)
                except Exception:
                    pass
                return None

            bpy.app.timers.register(_refresh, first_interval=0.0)


def _draw_active_target_graph_block(
    preview_box, props, context, compact=False, condensed=False,
):
    """Active Target mode: graphs the real, already-applied driver via
    FCurve.evaluate() instead of the template expression being edited.
    Shares the same pixel-image caching/rendering path as Template mode
    via _draw_result_graph."""
    from . import visualizer

    fcurve, driver, _reason = utils.get_target_driver_fcurve(context)
    if driver is None:
        # Nothing to graph, so draw nothing. The Active toggle's own tooltip
        # explains the absence, rather than spending a full row on it.
        return

    owner = fcurve.id_data
    zoom_x, zoom_y = visualizer.preview_axis_zoom_factors(
        getattr(props, "visualizer_zoom", 1.0),
        getattr(props, "visualizer_zoom_x", True),
        getattr(props, "visualizer_zoom_y", True),
    )
    from . import preview_activity

    preview_frame = preview_activity.frame(context)
    frame_start, frame_end = visualizer.zoomed_frame_range(
        context.scene.frame_start,
        context.scene.frame_end,
        preview_frame,
        zoom_x,
    )
    result = visualizer.sample_fcurve_driver(
        fcurve,
        getattr(owner, "name", ""),
        fcurve.data_path,
        fcurve.array_index,
        frame_start,
        frame_end,
        None,
        preview_frame,
        props.visualizer_detailed,
        style=props.visualizer_style,
        zoom=zoom_y,
    )
    if not result.valid:
        err_box = preview_box.box()
        err_box.alert = True
        err_box.label(text=result.message, icon="ERROR")
        return

    graph_box = preview_box.box()
    if not condensed:
        graph_box.label(text=result.message, icon="INFO")
    if result.detail_text and not condensed:
        draw_note(graph_box, result.detail_text, icon="GRAPH",
                  label=result.detail_text[:38])
    # Active Target graphs a real applied driver, not a template, so there is no
    # set of template defaults to anchor Fixed Scale against - always auto-fit.
    visible_range = visualizer.zoomed_value_range(
        result.points, None, zoom_y,
    )
    _draw_result_graph(graph_box, result, props, context, value_scale=visible_range)


def _sample_span(expression, template, context, steps=64):
    """Lowest and highest value an expression reaches over the scene range."""
    from ...engine import utils

    scene = context.scene
    start = int(getattr(scene, "frame_start", 1))
    end = int(getattr(scene, "frame_end", 250))
    step = max(1, (end - start) // max(1, steps))
    low = high = None
    for frame in range(start, end + 1, step):
        try:
            value = float(utils.evaluate_expression_at_frame(expression, template, frame))
        except Exception:  # noqa: BLE001 - skip frames the kernel cannot evaluate
            continue
        low = value if low is None else min(low, value)
        high = value if high is None else max(high, value)
    return low, high


def _result_is_constant(result, tolerance=1e-9):
    """True when the sampled curve never moves - a dead flat line."""
    for low, high in (("value_min", "value_max"), ("minimum", "maximum"), ("min", "max")):
        lo, hi = getattr(result, low, None), getattr(result, high, None)
        if lo is not None and hi is not None:
            try:
                return abs(float(hi) - float(lo)) <= tolerance
            except (TypeError, ValueError):
                return False
    return False


def _template_rest_value(expression, template, profile, context, declared_baseline=None):
    """The level this template sits at when nothing is happening.

    Used as the assumed rest for the Live graph when no target has been applied
    yet. A downward template rests at its TOP (a duck starts loud and dips), an
    upward one rests at its BOTTOM (a thump starts quiet and strikes), so the
    two are read off opposite ends of the sampled range.
    """
    from ...engine import utils

    # CENTERED_ZERO says what it means: the kernel oscillates about zero, so
    # zero IS its resting level - no sampling required. Deriving it from the
    # sampled origin instead made the baseline wander with every parameter
    # (dragging Second beat moved it between -0.0005 and +0.0004), so the whole
    # curve slid vertically while the artist was adjusting an amplitude that
    # cannot move a centre. A constant here is both correct and stable.
    if profile == "CENTERED_ZERO":
        return 0.0

    # Prefer the template's DECLARED resting level over a sampled one. Every
    # template that states an output range already carries it as
    # output_baseline (0.3 for candle_flicker, 0.7 for a screen-style flicker, and so
    # on) - it is exactly the Minimum the artist typed. Sampling for it instead
    # made the baseline drift whenever a TIMING parameter changed, because
    # Speed and Phase move which frames get sampled: a shutter-style flicker
    # shifted 0.851 -> 0.920 on Speed alone. A declared value cannot drift.
    if declared_baseline is not None:
        try:
            return float(declared_baseline)
        except (TypeError, ValueError):
            pass

    scene = context.scene
    start = int(getattr(scene, "frame_start", 1))
    end = int(getattr(scene, "frame_end", 250))
    try:
        origin = utils.find_additive_origin(expression, template, profile, 0.0, start, end)
        origin_value, sampled_min, sampled_max = utils.additive_origin_values(
            expression, template, profile, 0.0, start, end, origin,
        )
    except Exception:  # noqa: BLE001 - a preview must never break the panel
        return 0.0
    # origin_value is the engine's own idea of where this kernel rests - the
    # same reference the additive wrapper measures its excursion from - so it
    # is the value to preview against. Reading sampled_min instead looked
    # right until a damped sine (the kick drum) dipped below its own resting
    # level and handed back a NEGATIVE rest.
    if profile == "BOUNDED_MAX":
        return float(max(origin_value, sampled_max))
    return float(origin_value)


def _live_preview_expression(props, template, context):
    """The expression as it will actually be applied, for the Live graph.

    The plain preview is the template's own kernel, so the graph showed the
    authored shape rather than the result: no hold before the apply frame, no
    clamp. Live wraps it through exactly the same rest-state path the apply
    operators use, so what is drawn is what lands.

    Deliberately independent of any particular property. It does NOT read the
    value of whatever was applied to last - the same template gets applied to
    many properties, so anchoring the preview to one of them makes the graph
    lie about all the others. It previews from the template's own resting
    level, and when a clamp is on it SHIFTS that level so the motion sits
    inside the limits: what matters then is the shape against Min and Max, not
    where it happens to sit.

    Returns (expression, rest_value, note).
    """
    from ...apply import apply_behavior, target_memory
    from ...catalogue import catalogue_contracts
    from ...engine import utils

    expression = props.preview
    mode = getattr(props, "rest_start_mode", utils.REST_START_OFF)
    if not expression or mode == utils.REST_START_OFF:
        return expression, None, "", None

    profile = catalogue_contracts.resolve_additive_profile(template, None)
    rest_value = _template_rest_value(
        expression, template, profile, context,
        declared_baseline=getattr(props, "preview_output_baseline", None),
    )

    # The apply frame must NOT be the playhead. Rest Start bakes it into both
    # the hold threshold and the phase shift, so re-reading frame_current every
    # redraw made the whole curve slide while scrubbing. Anchor it: to the frame
    # the driver was really applied at once there is one, else the scene start.
    scene = context.scene
    snapshot = int(getattr(scene, "frame_start", 1))
    frame_note = f"apply @ {snapshot}"
    entry = target_memory.latest_entry(props)
    if entry:
        for record in entry.get("targets", []) or []:
            stored = (record.get("rest_state") or {}).get("snapshot_frame")
            if stored is not None:
                snapshot = int(stored)
                frame_note = f"applied @ {snapshot}"
                break

    clamp_range = apply_behavior._clamp_range(template, scene, mode)

    def build(rest, limits):
        state = utils.capture_rest_start_state(
            expression, template, scene, mode, rest,
            snapshot_frame=snapshot,
            output_baseline=getattr(props, "preview_output_baseline", None),
            additive_profile=profile,
            clamp_range=limits,
        )
        return utils.wrap_expression_with_rest_state(expression, template, state)

    try:
        # Draw the motion WHERE IT ACTUALLY IS - unclamped - and let the graph
        # mark the limits instead. Clamping the drawn values flattened anything
        # sitting outside them, and shifting the curve to fit made the shape
        # readable only by putting it at a height the values never occupy.
        # Guide lines show where the clamp cuts without moving or hiding data.
        built = build(rest_value, None)
    except Exception:  # noqa: BLE001 - a preview must never break the panel
        return expression, None, "", None

    # Collapse float dust: a rest of 7.8e-60 is zero, and printing it that way
    # makes the caption look like a bug rather than a number.
    shown = 0.0 if abs(rest_value) < 1e-9 else rest_value
    note = f"rest {shown:g}"
    if clamp_range is not None:
        note += f" · clamp {float(clamp_range[0]):g}–{float(clamp_range[1]):g}"
    return built, rest_value, f"{note} · {frame_note}", clamp_range


def _helper_live_preview_channel(props, template, context):
    """Rebuild a helper-backed channel with the same clock used on apply."""
    from ...apply import target_memory
    from ...engine import utils

    values = espresso_props.collect_values(props, template)
    note = ""
    mode = espresso_props.effective_rest_start_mode(props)
    if mode == utils.REST_START_ADDITIVE and any(
        item.get("token") == "START" for item in template.get("params", [])
    ):
        snapshot = int(getattr(context.scene, "frame_start", 1))
        entry = target_memory.latest_entry(props)
        if entry and entry.get("template_id") == template.get("id"):
            snapshot = int(entry.get("applied_frame", snapshot))
            note = f"restarted from applied frame {snapshot}"
        else:
            note = f"restart preview from frame {snapshot}"
        values["START"] = snapshot

    built = utils.build_template_expressions(template, values, context.scene)
    selected = next(
        (
            item for item in built
            if item.get("id") == getattr(props, "selected_motion_channel", "")
        ),
        built[0] if built else {},
    )
    return selected, note


def preview_evaluation_switch_applies(props, template, context):
    """Whether Evaluation and Source can actually draw different things.

    Measured across the catalogue: with Rest Start off the two expressions are
    identical for all 299 templates, and a generated systems timing model is
    identical whatever the setting, because there is no rest state for the
    wrapper to act on. A switch between two identical pictures is furniture --
    it invites the artist to look for a difference that is not there.

    Asked by BUILDING the evaluated expression and comparing, rather than by
    listing templates: the answer then stays right when the rest-state rules
    change, and no catalogue edit can leave a stale list behind. The build is
    already done on every Evaluation draw, and it returns immediately when
    Rest Start is off, which is the case that would otherwise pay for it.
    """
    if not template:
        return False
    if template.get("internal_helpers"):
        return True
    try:
        live, _rest, _note, limits = _live_preview_expression(props, template, context)
    except Exception:
        return True     # if we cannot tell, keep the control rather than hide it
    if limits:
        return True
    return str(live) != str(getattr(props, "preview", "") or "")


def _draw_graph_block(
    preview_box, props, template, context, compact=False, condensed=False,
):
    from . import visualizer, preview_activity
    from ...engine import utils

    # Anchored ("Fixed Scale") mode measures the template at its DEFAULT
    # parameters and scales the graph against that, so changing amplitude
    # visibly grows or shrinks the wave instead of the box auto-refitting.
    # If the pixel image will be drawn, the braille chart is never displayed -
    # tell the sampler not to build it.
    want_text = not (image_preview_available(context))

    scale = None
    if getattr(props, "visualizer_anchored", False):
        scale = visualizer.default_reference_range(
            template, context.scene.frame_start, context.scene.frame_end,
            utils.build_expression, context.scene,
            channel_id=getattr(props, "selected_motion_channel", ""),
            current_values=espresso_props.collect_values(props, template),
        )

    selected_channel = next(
        (
            item for item in espresso_props.built_channel_previews(props)
            if item.get("id") == getattr(props, "selected_motion_channel", "")
        ),
        {},
    )
    live_note = ""
    clamp_marks = ()
    sampled = props.preview
    if getattr(props, "graph_preview_mode", "LIVE") == "LIVE":
        if template.get("internal_helpers"):
            selected_channel, live_note = _helper_live_preview_channel(
                props, template, context,
            )
            sampled = selected_channel.get("expression", sampled)
        else:
            sampled, _rest, live_note, limits = _live_preview_expression(
                props, template, context,
            )
            clamp_marks = tuple(limits) if limits else ()

    zoom_x, zoom_y = visualizer.preview_axis_zoom_factors(
        getattr(props, "visualizer_zoom", 1.0),
        getattr(props, "visualizer_zoom_x", True),
        getattr(props, "visualizer_zoom_y", True),
    )
    frame_start, frame_end = visualizer.zoomed_frame_range(
        context.scene.frame_start,
        context.scene.frame_end,
        context.scene.frame_current,
        zoom_x,
    )
    overlay = visualizer.overlay_presentation(
        template,
        getattr(props, "selected_motion_channel", ""),
        getattr(props, "preview_overlay_enabled", False),
    )
    result = visualizer.sample_preview(
        sampled,
        template,
        frame_start,
        frame_end,
        None,  # automatic per-frame sampling (no user-facing count)
        context.scene.frame_current,
        props.visualizer_detailed,
        style=props.visualizer_style,
        scale=scale,
        zoom=zoom_y,
        want_text=want_text,
        helper_expressions=selected_channel.get("internal_helpers"),
        series_visibility=overlay["series_visibility"],
    )
    if not result.valid:
        err_box = preview_box.box()
        err_box.alert = True
        err_box.label(text=result.message, icon="ERROR")
        return

    graph_box = preview_box.box()
    if not compact and preview_evaluation_switch_applies(props, template, context):
        mode_row = graph_box.row(align=True)
        mode_row.prop(props, "graph_preview_mode", expand=True)
    if not condensed:
        graph_box.label(text=result.message, icon="INFO")
    if live_note:
        # Name the assumed resting value: the Live curve is drawn against a
        # number the artist did not type, so the graph should admit where it
        # came from rather than look authoritative.
        if not condensed:
            note_row = graph_box.row()
            note_row.enabled = False
            note_row.label(text=f"Live · {live_note}", icon="FILE_REFRESH")
        # A flat Live curve is nearly always the clamp swallowing the motion -
        # a downward template starting at or below Min has nowhere to go. Say
        # so, rather than leaving a blank box that reads as "broken".
        if (not condensed and getattr(props, "clamp_additive", False)
                and _result_is_constant(result)):
            warn = graph_box.row()
            warn.alert = True
            warn.label(
                text=f"Clamped flat — the motion sits outside Min {props.clamp_min:g}"
                     f" / Max {props.clamp_max:g}",
                icon="ERROR",
            )
    if result.detail_text and not condensed:
        draw_note(graph_box, result.detail_text, icon="GRAPH",
                  label=result.detail_text[:38])
    # scale is the template's default-parameter range, or None when Fixed Scale is
    # off. The pixel renderer needs it too, not just the braille sampler, or the
    # image silently auto-fits and the toggle appears to do nothing.
    colour_strip = ()
    colour_key = ""
    visible_range = visualizer.zoomed_value_range(
        result.points, scale, zoom_y,
    )
    _draw_result_graph(
        graph_box, result, props, context, value_scale=visible_range,
        marks=clamp_marks, colour_strip=colour_strip, colour_key=colour_key,
    )
    if overlay["compatible_ids"]:
        overlay_row = graph_box.row(align=True)
        overlay_row.prop(props, "preview_overlay_enabled", text="Overlay", toggle=True)
        if props.preview_overlay_enabled and not overlay["can_overlay"]:
            graph_box.label(text=overlay["reason"], icon="INFO")
        elif overlay["can_overlay"]:
            legend = graph_box.row(align=True)
            for item in overlay["legend"]:
                legend.label(text=item["label"])
    elif props.preview_overlay_enabled:
        graph_box.label(text=overlay["reason"] or "No compatible sibling channels.", icon="INFO")


def preview_display_controls(props, template, active_target_mode=False, active_kind=None):
    """Display controls supported by the Graph preview."""
    return {
        "kind": active_kind, "on_visual": False, "mode_row": False,
        "chart_style": True, "fixed_scale": True, "axis_zoom": True,
        "detailed": True, "display_scale": True, "pixel_toggle": True,
        "trail": False, "glow": False, "caption": False, "shows": False,
    }


def _draw_commit_slot(layout, props, context):
    """One physical commit control whose label and operator follow Setup/Applied."""
    slot = espresso_props.commit_slot_presentation(props, context)
    col = layout.column(align=True)
    if slot["destination"]:
        col.label(text=slot["destination"], icon="OBJECT_DATA")
    if slot["conflict"]:
        warn = col.row()
        warn.alert = True
        warn.label(text=slot["conflict"], icon="ERROR")
    if slot["resources"]:
        col.label(text=slot["resources"])
    row = col.row(align=True)
    row.enabled = slot["enabled"]
    row.scale_y = 1.2
    row.operator(
        slot["operator_id"],
        text=slot["label"],
        icon="FILE_REFRESH" if slot["family"] == "update" else "DRIVER",
    )
    if not slot["enabled"] and slot["reason"]:
        col.label(text=slot["reason"], icon="INFO")
    return slot


def _draw_clear_applied_row(layout, props, context=None, draw_clear=True, draw_update=True):
    """Clear the drivers this template last applied.

    Multi-channel templates create several drivers at once, so undoing by hand
    means hunting every channel. The remembered entry already holds the full
    target list, so one button covers single and multi identically.

    Only drawn once something has actually been applied - an always-visible
    button that usually does nothing reads as broken.

    ``draw_clear=False`` and ``draw_update=False`` drop the clear and update
    buttons while keeping the "Last applied" note, for templates where a row
    further down already offers that same button - the Clear Transform Drivers
    pair for clearing, the "Update:" row for updating. Both were drawing, so the panel offered
    Clear Last Drivers twice, a few rows apart. The pair is the one that
    survives: sitting beside Clear Transform Drivers is what makes the
    difference between the two kinds of clearing readable.
    """
    entry = target_memory.latest_entry(props)
    if not entry:
        return
    # Destinations, not channels - see target_memory.destination_count.
    count = target_memory.destination_count(entry)

    # What the remembered entry actually points at, so the buttons below are not
    # acting on an invisible target. Motion applies often land on an object the
    # artist has since deselected.
    where = entry.get("display_label", "")
    if where:
        # The note is greyed as a passive label, but the X beside it must stay
        # live - it is the only way out when a stale multi-channel entry blocks
        # a single-channel template, and an unclickable escape hatch is no
        # escape hatch. Hence a split rather than one disabled row.
        note = layout.row(align=True)
        text_part = note.row()
        text_part.enabled = False
        text_part.label(text=f"Last applied: {where}", icon="FILE_REFRESH")
        note.operator("espresso.forget_last_target", text="", icon="PANEL_CLOSE", emboss=False)

    if not draw_update:
        return
    update_row = layout.row(align=True)
    update_row.enabled = props.is_valid
    # No count in the label and no info button beside it: the "Last applied"
    # line directly above already names the target and carries its own "(+2)"
    # when a motion set landed on several. Plural is the only thing the button
    # needs to say, and it needs the full width to say it.
    update_row.operator(
        "espresso.update_last_target",
        text="Update Last Target" if count <= 1 else "Update Last Targets",
        icon="FILE_REFRESH",
    )

    if draw_clear:
        label = "Clear Last Driver" if count <= 1 else "Clear Last Drivers"
        clear_row = layout.row(align=True)
        clear_row.operator("espresso.remove_last_target", text=label, icon="TRASH")


def draw_expression_block(layout, props, template, context):
    """Generated-expression text plus apply settings (Rest Start, Copy,
    Update) — the 'what will be applied' half of the old preview, shown in
    the main panel directly under the parameters. The waveform graph and the
    driver-target picker are grouped together in the standalone, draggable
    Preview panel (draw_graph_and_target)."""
    # Whether this recipe has an expression half at all. A GENERATED route
    # describes and manages its setup in APPLY & MANAGE; an AUTHORING recipe
    # flies or drives a rig with its own controls. The rule is one table rather
    # than per-name handling here; see panel_sections.
    #
    # Only the AUTHORING half is new: measured, an authoring rig drew a "0.0"
    # expression with a Duration, a Rest Start mode and an additive Clamp,
    # none of which it reads.
    generated_only = panel_sections.owns_its_apply(template)
    # Pointing ``box`` at the parent layout avoids creating a visible empty
    # container when there is no expression, Rest Start, or motion-channel
    # body to draw above that section.
    box = layout if generated_only else layout.box()
    minimal = _compact_level(context) >= 2
    is_motion = espresso_templates.has_motion_plan(template)
    # The Clear Transform Drivers row further down pairs its own Clear Last
    # Drivers beside itself, on exactly this condition. Where that row draws,
    # the standalone copy above must not - or the button appears twice.
    clear_last_is_paired = (not is_motion) or _drives_transforms(template)
    # LIVE already relabels the commit slot to Update Selected on the same
    # operator. Drawing Update Last Target there too duplicates the control.
    update_is_paired = (
        espresso_props.commit_slot_presentation(props, context).get("family") == "update"
    )
    is_camera = camera_application.requires_camera_object(template)
    if not generated_only:
        header = box.row(align=True)
        header.prop(
            props, "preview_open", text="",
            icon="TRIA_DOWN" if props.preview_open else "TRIA_RIGHT",
            emboss=False,
        )
        header.label(
            text="APPLY" if minimal else (
                "MOTION CHANNELS" if is_motion else "TEMPLATE EXPRESSION"
            ),
            icon="ORIENTATION_GLOBAL" if is_motion else "TEXT",
        )
        if props.is_valid:
            header.label(text="Valid", icon="CHECKMARK")
        else:
            header.label(text="Error", icon="ERROR")

    if not is_motion and not minimal and not generated_only:
        edit_toggle = header.row(align=True)
        edit_toggle.prop(props, "manual_mode", text="", icon="GREASEPENCIL", toggle=True)

    # Editable expression field while in manual mode.
    if props.manual_mode and not is_motion and not minimal and not generated_only:
        edit_box = box.box()
        edit_box.label(text="Editing expression", icon="GREASEPENCIL")
        edit_box.prop(props, "manual_expression", text="")

    if props.preview_open and not minimal and not generated_only:
        if is_motion:
            draw_motion_channel_list(box, props, template)
        else:
            formula_box = box.box()
            # Single line, deliberately not word-wrapped: this is a formula, not
            # prose, so wrapping mid-term reads worse than Blender's own clip.
            formula_box.label(text=props.preview)

    # How long the motion actually runs, measured from the built expression.
    #
    # The parameters are all on screen but the total they add up to is not, and
    # for anything with a recovery tail the total is the number you need to
    # place the shot. It is also frequently NOT the sum of the parameters:
    # Landing Compression Recovery with "Recovery time 24" is still visibly
    # moving 120 frames later, because the recovery is a decaying ring rather
    # than a fixed window. Reading it off a parameter would have lied.
    #
    # Wrapped, because a draw function must never raise: collect_values reads
    # the live parameter slots, which only line up when the template being drawn
    # is the one that is selected. Anything else - a preview of another template,
    # a half-built context - loses the readout rather than the whole panel.
    if not minimal and not generated_only:
        try:
            info = espresso_duration.classify(
                template, espresso_props.collect_values(props, template), context.scene)
        except Exception:
            info = {}
        if info.get("label"):
            duration_row = box.row(align=True)
            duration_row.label(text="Duration:", icon="TIME")
            readout = duration_row.row(align=True)
            readout.alignment = "LEFT"
            readout.label(text=info["label"], icon=_DURATION_ICON.get(info["kind"], "BLANK1"))

    if not generated_only:
        rest_row = box.row(align=True)
        rest_row.label(text="Rest Start:")
        rest_row.prop(props, "rest_start_mode", text="")
    # Clamp means the same thing everywhere - "do not leave the range" - but
    # where the range COMES FROM differs, so only one of the two forms is shown:
    #   * template declares Minimum/Maximum -> the toggle alone, holding the
    #     output to those. For kernels that overshoot their own range (springs,
    #     elastic easings) that is the whole point; a second pair of numbers
    #     would be two controls fighting over one job.
    #   * template declares no range (Kick Drum: BPM/Amount/Decay) -> the
    #     toggle plus Min/Max, because nothing else in the panel says how far
    #     it may travel.
    # Only Additive builds a value that can leave the usable range, so the
    # toggle greys out elsewhere rather than vanishing - a row that changes
    # height as the mode changes is harder to use.
    if not generated_only and not catalogue_contracts.has_ordered_output_range(template):
        clamp_toggle = rest_row.row(align=True)
        clamp_toggle.enabled = props.rest_start_mode == "ADDITIVE"
        clamp_toggle.prop(props, "clamp_additive", text="Clamp", toggle=True)
        if props.clamp_additive and props.rest_start_mode == "ADDITIVE":
            limits = box.row(align=True)
            limits.prop(props, "clamp_min", text="Min")
            limits.prop(props, "clamp_max", text="Max")
    # There is no per-mode explanation line: the mode names already say what
    # they do, and such a sentence would re-render on every redraw.
    # utils.rest_start_summary() is intentionally kept — the test suite still
    # covers it, and the wording is worth having if it is ever wanted in a
    # tooltip instead.
    if (not generated_only and is_motion and _drives_transforms(template)
            and not minimal):
        motion_row = box.row(align=True)
        motion_row.label(text="Motion Space:")
        motion_row.label(text="Relative to Current Transform", icon="ORIENTATION_LOCAL")

    # Keep action controls separate from the collapsible expression preview.
    actions_box = layout.box()
    actions_header = actions_box.row(align=True)
    actions_header.prop(
        props,
        "actions_open",
        text="",
        emboss=False,
        icon="TRIA_DOWN" if props.actions_open else "TRIA_RIGHT",
    )
    # A return key, not PLAY: PLAY is a right-pointing triangle and sits
    # directly beside TRIA_RIGHT, so a collapsed section showed two triangles
    # in a row and the heading icon read as a second collapse arrow.
    #
    # EVENT_RETURN rather than KEY_RETURN, which does not exist before 5.0 --
    # and Blender raises on an unknown icon, so on the 4.2 floor this panel
    # would have thrown at draw time, on the customer. Same glyph, and it is
    # present in every release this product declares.
    actions_header.label(text="APPLY & MANAGE", icon="EVENT_RETURN")
    if capabilities_module.supports(template.get("id")):
        card = guided_apply.preflight_card(context, template, props)
        status = actions_header.operator(
            "espresso.show_motion_status",
            text="Status",
            icon="CHECKMARK" if card.ok else "ERROR",
        )
        status.template_id = template.get("id", "")
    if not props.actions_open:
        return
    box = actions_box

    if is_camera and not minimal:
        _draw_visible_applies_to(
            box,
            camera_application.application_label(
                template,
                getattr(context, "active_object", None),
            ),
            icon="CAMERA_DATA",
        )

    if is_camera and template.get("authoring_kind"):
        # An authoring recipe owns its own action, so the generic row is wrong
        # for it -- but the action still belongs HERE, under Apply, where every
        # other recipe's apply lives. recorded-path is the exception: its Apply is
        # per-Take and lives on the Take surface, because a single button cannot
        # express which segments are ready.
        if template.get("authoring_kind") == "CAMERA_DRONE":
            camera_drone_controls.draw_apply(box, context, template)
            # Speed and G-Force are measurements of the flight, not controls
            # over it, so they sit beside the flight rather than among the
            # dials in PARAMETERS where they read as two broken sliders.
            camera_drone_controls.draw_readout(box, context)
        return
    if is_camera:
        active = getattr(context, "active_object", None)
        action_row = box.row(align=True)
        action_row.enabled = bool(
            props.is_valid
            and active is not None
            and getattr(active, "type", "") == "CAMERA"
        )
        action_row.operator(
            "espresso.apply_motion_selected",
            text=camera_application.apply_button_label(template),
            icon="CAMERA_DATA",
        )
        companion = camera_application.apply_companion_note(template)
        if companion and not minimal:
            box.label(text=companion, icon="INFO")
        if not minimal:
            _draw_clear_applied_row(box, props, context, draw_clear=not clear_last_is_paired,
                                draw_update=not update_is_paired)
        if not is_motion and not minimal:
            copy_row = box.row(align=True)
            copy_row.enabled = props.is_valid
            split = copy_row.split(factor=0.5, align=True)
            split.operator("espresso.copy_expression", text="Copy Expression", icon="COPYDOWN")
            split.operator("espresso.copy_driver", text="Copy Driver", icon="DRIVER")

    if is_motion and not is_camera:
        # In Pose Mode with a bone active, motion routes to the bone, not the
        # armature Object — reflect that in the label, and surface the one-click
        # Euler fix when a Quaternion/Axis-Angle bone would silently ignore it.
        pose_bone = None
        if getattr(context, "mode", "") == "POSE":
            pose_bone = getattr(context, "active_pose_bone", None)
            if pose_bone is not None:
                block = motion_channels.bone_euler_block_message(pose_bone, template)
            if block and not minimal:
                warn_col = box.column(align=True)
                warn_col.scale_y = 0.82
                draw_wrapped(warn_col, block, icon="ERROR", width=58)
                box.operator("espresso.set_bone_euler", text="Set Bone to XYZ Euler", icon="CON_ROTLIKE")
            elif motion_channels.bone_will_use_quaternion_drivers(pose_bone, template) and not minimal:
                # The template's Euler rotation converts to four quaternion drivers
                # automatically — no action needed, but surface it so the artist
                # knows what is about to happen. Set Bone to XYZ Euler stays
                # available as a deliberate opt-in (3 drivers vs 4).
                # Nothing is blocked here - the conversion is automatic - so the
                # explanation rides on the button that acts on it rather than
                # occupying three rows above it.
                euler_row = box.row(align=True)
                euler_row.operator(
                    "espresso.set_bone_euler",
                    text="Set Bone to XYZ Euler", icon="CON_ROTLIKE",
                )
                draw_note(
                    euler_row,
                    f'Bone "{pose_bone.name}" uses Quaternion rotation - '
                    "will apply as 4 quaternion drivers. "
                    'Use "Set Bone to XYZ Euler" for 3 Euler drivers instead.',
                )

    # Lighting templates get a one-button route for a whole rig. The old route
    # was right-click one lamp's Power then Copy Drivers to Selected, which
    # silently rebinds nothing and leaves every lamp reading the FIRST lamp -
    # reported in the wild as "they're all playing the same animation".
    target_entry = apply_target.read(props)
    if (
        not generated_only
        and operators.template_suits_lights(template, bool(target_entry))
    ):
        lights = operators.applicable_objects(context)
        # Which of the two routes this selection is on. Lights never consult
        # the nominated target, so the panel must not describe them as if they
        # did - the wording differs, not just the count.
        selected_now = operators.arrangeable_objects(context)
        light_mode = apply_target.is_light_selection(selected_now)
        light_box = box.box()
        if not minimal:
            light_box.label(text="APPLIES TO", icon="LIGHT_DATA")

        kind = light_layout.SPATIAL_KIND.get(template.get("id"))
        if lights:
            points = light_layout.positions(lights)
            auto_axis = light_layout.widest_axis(points)[0]
            advice = light_layout.advise(
                template["id"], lights,
                espresso_props.collect_values(props, template),
                axis=auto_axis,
                centre=None,
            )
            noun = "light" if light_mode else "object"
            if not minimal:
                light_box.label(
                    text="%d selected %s%s > %s"
                    % (len(lights), noun, "" if len(lights) == 1 else "s",
                       apply_target.label(props, lights)),
                    icon="DOT",
                )

            # A target that is not on every selected object would apply to some
            # and refuse the rest, so say which before the button is pressed.
            entry, _light = apply_target.effective_entry(props, lights)
            missing = apply_target.missing_for(entry, lights) if entry else []
            if missing and not minimal:
                draw_wrapped(
                    light_box,
                    "Not on %d of them: %s. Pick a property they all share."
                    % (len(missing),
                       ", ".join(name for name, _reason in missing[:3])),
                    icon="ERROR", width=58,
                )

            if target_entry and not light_mode and not minimal:
                # Sticky and easy to forget, so it gets a visible way out
                # rather than only a menu entry. Hidden in light mode, where it
                # is not what the button is using.
                light_box.operator(
                    "espresso.clear_apply_target",
                    text="Clear Target", icon="X",
                )
            # The axis choice only means anything to a single-axis wave. Radial
            # Sweep reads an ANGLE from X and Y together, so offering to aim it
            # would be offering a control that does nothing.
            if kind == light_layout.AXIS:
                axis_row = light_box.row(align=True)
                axis_row.prop(props, "light_apply_axis", expand=True)
            # Say WHY it will or will not read as a pattern, before applying,
            # while the fix is still a slider away.
            # Auto Fit already reports the selected span, axis, and fitted
            # frequency above.  Showing the normal Position Wave advice too
            # would repeat that same status on a second line.
            # No recipe here reports a fitted frequency, so advice is never
            # a repeat of an Auto Fit status line.
            auto_fit_status_replaces_advice = False
            if advice.detail and not auto_fit_status_replaces_advice and not minimal:
                if advice.ok:
                    # Healthy advice is reference information, not something the
                    # artist has to act on, so it rides in a hover tip.
                    draw_note(light_box, advice.detail, label=advice.detail[:34])
                else:
                    # Not ok - this one stays on screen at full length.
                    draw_wrapped(light_box, advice.detail, icon="ERROR", width=58)
        else:
            selected_now = operators.arrangeable_objects(context)
            if selected_now and not minimal:
                # Objects are selected but nothing is nominated. The instruction
                # is three rows spelled out; one line plus a hover tip says the
                # same thing without owning the panel.
                draw_note(
                    light_box,
                    "No target set. Right-click the property you want to "
                    "drive and choose \"Use as Espresso Target\" - or apply "
                    "straight from that menu. Lights use Power automatically.",
                    label="No target set",
                )
            elif not minimal:
                draw_note(
                    light_box, "Select the objects to apply this to.",
                    icon="DOT", label="Select objects to apply to",
                )

        light_row = light_box.row(align=True)
        light_row.enabled = props.is_valid and bool(lights)
        # The way OUT of a bad arrangement, not just a report of one. Sits
        # directly under the apply button so the advisory above has a remedy
        # within reach instead of leaving the artist to move lamps by hand.
        # Gated on the SELECTION, not on the lights in it. Arranging a circle
        # is pure geometry - the button says Lights/Objects and the operator
        # means it - so greying it out for a set of meshes was just wrong.
        arrangeable = operators.arrangeable_objects(context)
        arrangement_row = light_box.row(align=True)
        arrangement_row.enabled = len(arrangeable) >= 2
        if kind == light_layout.AXIS:
            points = light_layout.positions(arrangeable)
            chosen_axis = props.light_apply_axis
            if chosen_axis == "AUTO" and points:
                chosen_axis = light_layout.widest_axis(points)[0]
            row = arrangement_row.operator(
                "espresso.position_wave_row_wizard",
                text="Arrange %d Objects as a Row Along %s" % (len(arrangeable), chosen_axis)
                     if arrangeable else "Arrange Selected Objects as a Row",
                icon="ALIGN_JUSTIFY",
            )
            row.axis = props.light_apply_axis
        if len(arrangeable) < 2:
            # A disabled button with no reason given is the thing that sent the
            # artist looking for a bug, so the reason is right underneath it.
            why = light_box.row()
            why.enabled = False
            why.label(text="Select two or more objects to arrange them.")

        shared_shader_target = (
            shared_material_sweep.kind_for(template) is not None
            and selection_has_one_shared_shader_target(props, lights)
        )
        if not shared_shader_target:
            applied = light_row.operator(
                "espresso.apply_to_lights",
                text="Apply to %d %s%s"
                     % (len(lights), "Light" if light_mode else "Object",
                        "" if len(lights) == 1 else "s")
                     if lights else "Apply to Selected",
                icon="LIGHT_DATA",
            )
            applied.axis = props.light_apply_axis

    # Non-light, non-semantic destinations still need to be discoverable before
    # the artist commits an apply. Transform motion has an active object/bone
    # destination; scalar and colour recipes use the remembered target route.
    if not minimal and not generated_only and not is_camera and not operators.template_suits_lights(
        template, bool(apply_target.read(props))
    ):
        if is_motion and _drives_transforms(template):
            destination = _motion_destination_label(template, context)
        else:
            destination = apply_target.label(props, _selected_objects(context))
            if not destination:
                destination = "Choose a target property from the right-click menu"
        _draw_visible_applies_to(box, destination, icon="DRIVER")

    # Pose-driven templates get their own apply button, above the usual row.
    # The artist sculpts the extreme instead of typing an amount, so the button
    # has to say what it will DO to their pose - it records it and then clears
    # it, and a button that silently wipes posing would be alarming.
    if template.get("id") in operators.POSE_DRIVEN_TEMPLATES:
        pose_box = box.box()
        selected = list(getattr(context, "selected_pose_bones", None) or ())
        header = pose_box.row()
        header.enabled = False
        header.label(text="Pose the lids closed, then apply", icon="HIDE_OFF")
        pose_row = pose_box.row(align=True)
        pose_row.enabled = props.is_valid and bool(selected)
        pose_row.operator(
            "espresso.apply_pose_as_motion",
            text="Apply Blink to Bones",
            icon="HIDE_OFF",
        )
        note = pose_box.row()
        note.enabled = False
        if not selected:
            note.label(text="Select eyelid bones in Pose Mode")
        else:
            posed = sum(
                1 for bone in selected if pose_capture.capture_pose_deltas(bone)
            )
            note.label(
                text="%d bone%s selected, %d posed"
                % (len(selected), "" if len(selected) == 1 else "s", posed)
            )

    contract = catalogue_contracts.reengineering_contract(template.get("id"))
    # Camera templates render their own apply row above (in the is_camera
    # branch); everything else gets the standard motion / copy action row here.
    if not is_camera and not generated_only:
        # A channel plan that drives TRANSFORMS gets the object-apply button.
        # One that drives a colour does not: the only object-level colour is
        # Object Color, the viewport swatch, which is almost never what the
        # artist meant. Those templates are applied by right-clicking the socket
        # they belong on, so the panel offers the copy actions instead.
        if is_motion and _drives_transforms(template):
            pose_bone = None
            if getattr(context, "mode", "") == "POSE":
                pose_bone = getattr(context, "active_pose_bone", None)
            if pose_bone is not None:
                motion_text = "Apply Motion to Active Bone"
            else:
                motion_text = "Apply Motion to Selected Object"
            _draw_commit_slot(box, props, context)
            # Apply-and-bake as its own full-width row directly beneath, phrased
            # to match: whatever the apply button targets, this bakes. The REC
            # dot ties it to the same freeze action used in the right-click menu.
            bake_text = motion_text.replace("Apply Motion", "Apply and Bake Motion")
            bake_row = box.row(align=True)
            bake_row.enabled = props.is_valid
            bake_row.operator("espresso.apply_and_bake_motion", text=bake_text, icon="REC")
            if not minimal:
                _draw_clear_applied_row(box, props, context, draw_clear=not clear_last_is_paired,
                                draw_update=not update_is_paired)
        elif is_motion:
            # A colour plan. No object-apply button: the only object-level
            # colour is the viewport swatch. But everything that MANAGES an
            # applied plan still belongs - the artist applies by right-clicking
            # the socket, then updates or clears it from here exactly as they
            # would a motion set.
            #
            # The copy pair is gone rather than relabelled: Copy Driver's poll
            # rejects multi-channel templates outright, so it drew permanently
            # greyed, and "Copy Expression" would silently mean "the first
            # channel". Each channel has its own Copy button above.
            if not minimal:
                hint = box.row()
                hint.enabled = False
                hint.label(text="Right-click a colour to drive all channels", icon="RESTRICT_COLOR_ON")
                _draw_commit_slot(box, props, context)
                _draw_clear_applied_row(box, props, context, draw_clear=not clear_last_is_paired,
                                draw_update=not update_is_paired)
        else:
            if not minimal:
                action_row = box.row(align=True)
                action_row.enabled = props.is_valid
                split = action_row.split(factor=0.5, align=True)
                split.operator("espresso.copy_expression", text="Copy Expression", icon="COPYDOWN")
                split.operator("espresso.copy_driver", text="Copy Driver", icon="DRIVER")

    if not minimal and is_motion and _drives_transforms(template) and any(
        "frame" in channel.get("expression", "")
        for channel in espresso_templates.template_channels(template)
    ):
        offset_row = box.row(align=True)
        offset_row.enabled = props.is_valid
        offset_row.operator(
            "espresso.apply_motion_with_offset",
            text="Apply as Sequence…",
            icon="TIME",
        )

    # Always drawn, unlike _draw_clear_applied_row: that one only undoes what this
    # template last applied, whereas this clears whatever is on the selection -
    # including drivers made by hand or by another tool. Its poll() greys the
    # button out when nothing is selected, so it never lies about being usable.
    if not minimal and not generated_only and (not is_motion or _drives_transforms(template)):
        clear_row = box.row(align=True)
        clear_row.operator(
            "espresso.clear_transform_drivers",
            text="Clear Transform Drivers",
            icon="TRASH",
        )
        # The two ways to undo, side by side: this one clears whatever is on
        # the selection, its neighbour clears exactly what this template
        # applied. It is a labelled button rather than an icon-only trash
        # inside the Update row below, where it would read as part of "update"
        # rather than as a delete, and an unlabelled bin beside two labelled
        # buttons is the easiest thing to hit by mistake. Whenever that Update
        # row draws, this row draws too, so the button never disappears by
        # moving here.
        entry = target_memory.latest_entry(props)
        if entry:
            count = target_memory.destination_count(entry)
            clear_row.operator(
                "espresso.remove_last_target",
                text="Clear Last Driver" if count <= 1 else "Clear Last Drivers",
                icon="TRASH",
            )

        # Bake Last Driver finishes this template's last apply. Bake Motion
        # names a stamped effect on the selection. They share one row when
        # both exist so the two freeze actions sit together.
        bake_row = None
        if entry:
            bake_row = box.row(align=True)
            bake_row.operator(
                "espresso.bake_last_target",
                text="Bake Last Driver…" if count <= 1 else "Bake Last Drivers…",
                icon="REC",
            )
    else:
        bake_row = None

    # This is not the "bake whatever drivers happen to be on the selection"
    # that stays in the right-click menu. Every entry here names the template
    # it came from, read from the stamp that apply left behind.
    #
    # Drawn only when something is actually applied, following the same rule as
    # Bake Last Driver: a button that silently does nothing is worse than one
    # that is not there. any_applied short-circuits on the first hit because
    # this runs on every redraw.
    if not minimal:
        selected = list(getattr(context, "selected_objects", ()) or ())
        active = getattr(context, "active_object", None)
        if active is not None and active not in selected:
            selected.append(active)
        # Whatever there is to clear, the panel offers to clear. Scoped to the
        # selection alone, a camera holding motion the artist has not selected
        # would have no way to remove it while the panel reports it three rows
        # above. scope_objects is what the operator acts on, so the button and
        # the action can never disagree.
        from ..actions import bake_applied as bake_applied_actions

        selected = bake_applied_actions.scope_objects(context)
        if applied_motion.any_applied(selected):
            if bake_row is None:
                bake_row = box.row(align=True)
            bake_row.operator(
                "espresso.bake_applied_motion",
                text="Bake Motion…",
                icon="REC",
            )
            clear_applied = box.row(align=True)
            clear_applied.operator(
                "espresso.clear_applied_motion",
                text="Clear Applied Motion",
                icon="TRASH",
            )

        # Scene motion is separate because it belongs to nothing in the
        # outliner - camera flash drives scene exposure. Folding it into the
        # object list would make an artist who selected one object read past
        # entries that have nothing to do with their selection.
        scene = getattr(context, "scene", None)
        if any(applied_motion.read(host) for host in applied_motion.scene_hosts(scene)):
            scene_row = box.row(align=True)
            scene_row.operator(
                "espresso.bake_scene_motion",
                text="Bake Scene Motion…",
                icon="SCENE_DATA",
            )
            clear_scene = box.row(align=True)
            clear_scene.operator(
                "espresso.clear_scene_motion",
                text="Clear Scene Motion",
                icon="TRASH",
            )

    # Camera recipes already draw their dedicated apply/update/clear controls
    # in the camera branch above.  Letting them fall through here adds the
    # generic commit slot and a second Update Last Target button.
  # Generated generated-system routes already draw Build / Update / Bake above.
    # Falling through here adds a second Espresso target picker and Apply New.
    if not minimal and not is_motion and not is_camera and not generated_only:
        _draw_commit_slot(box, props, context)
        _draw_clear_applied_row(
            box, props, context,
            draw_clear=not clear_last_is_paired,
            draw_update=not update_is_paired,
        )

    if props.validation_message and not props.is_valid:
        box.separator()
        err_box = box.box()
        err_box.alert = True
        draw_wrapped(err_box, props.validation_message, icon="ERROR", width=54)


def draw_motion_channel_list(layout, props, template):
    """Draw all built channels with independent preview, copy and enable controls."""
    if not espresso_templates.has_motion_plan(template):
        return
    items = espresso_props.built_channel_previews(props)
    if not items:
        layout.label(text="No channel expressions are available.", icon="ERROR")
        return
    template_id = template.get("id", "")
    any_disabled = False
    for item in items:
        channel_id = item.get("id", "")
        enabled = espresso_props.motion_channel_enabled(props, template_id, channel_id)
        any_disabled = any_disabled or not enabled
        channel_box = layout.box()
        row = channel_box.row(align=True)
        # A separate control from the RADIOBUT preview-selector below: that one
        # picks which single channel the graph/expression box shows, this one
        # decides whether the channel is applied at all. Conflating them would
        # mean previewing a channel could only be done by also committing to
        # apply it, which is not what "just let me look at it" should require.
        toggle_op = row.operator(
            "espresso.toggle_motion_channel",
            text="",
            icon="CHECKBOX_HLT" if enabled else "CHECKBOX_DEHLT",
            emboss=False,
        )
        toggle_op.channel_id = channel_id
        selected = channel_id == props.selected_motion_channel
        select_row = row.row(align=True)
        select_row.enabled = enabled
        select_op = select_row.operator(
            "espresso.select_motion_channel",
            text="",
            icon="RADIOBUT_ON" if selected else "RADIOBUT_OFF",
            depress=selected,
        )
        select_op.channel_id = channel_id
        select_row.label(text=item.get("label", "Channel"))
        copy_row = row.row(align=True)
        copy_row.enabled = enabled
        copy_op = copy_row.operator("espresso.copy_motion_channel", text="Copy", icon="COPYDOWN")
        copy_op.channel_id = channel_id
        expr_row = channel_box.row()
        expr_row.enabled = enabled
        expr_row.label(text=item.get("expression", ""))
        if not enabled:
            channel_box.label(
                text="Disabled - will not receive a driver on the next apply",
                icon="INFO",
            )
    if any_disabled:
        note = layout.row()
        note.enabled = False
        note.label(
            text="Disabled channels are per-template and remembered across sessions.",
            icon="INFO",
        )


def _draw_preview_zoom_row(layout, props):
    """Draw one-height Zoom row with guarded horizontal/vertical toggles."""
    zoom_row = layout.row(align=True)
    zoom_row.prop(props, "visualizer_zoom", text="Zoom", slider=True)

    x_control = zoom_row.row(align=True)
    x_control.enabled = bool(getattr(props, "visualizer_zoom_y", True))
    x_control.prop(props, "visualizer_zoom_x", text="X", toggle=True)

    y_control = zoom_row.row(align=True)
    y_control.enabled = bool(getattr(props, "visualizer_zoom_x", True))
    y_control.prop(props, "visualizer_zoom_y", text="Y", toggle=True)


def draw_graph_and_target(layout, props, template, context):
    """Waveform graph in the standalone Preview panel (visible on every workspace tab)."""
    compact_level = _preview_compact_level(context)
    compact = compact_level >= 2
    condensed = compact_level >= 1
    graph_box = layout.box()
    header = graph_box.row(align=True)
    header.label(text="GRAPH", icon="IPO_BEZIER")
    if props.is_valid:
        header.label(text="Valid", icon="CHECKMARK")
    else:
        header.label(text="Error", icon="ERROR")
    header.prop(
        props, "visualizer_enabled", text="",
        icon="HIDE_OFF" if props.visualizer_enabled else "HIDE_ON",
    )

    if props.visualizer_enabled:
        active_target_mode = (
            utils.driver_target_picker_enabled(context) and props.preview_view_mode == "ACTIVE_TARGET"
        )
        if utils.driver_target_picker_enabled(context) and not compact:
            mode_row = graph_box.row(align=True)
            mode_row.prop(props, "preview_view_mode", expand=True)

        controls = preview_display_controls(props, template, active_target_mode)

        if not compact:
            display = graph_box.box()
            display_header = display.row(align=True)
            display_header.prop(
                props,
                "preview_display_settings_open",
                text="",
                emboss=False,
                icon="TRIA_DOWN" if props.preview_display_settings_open else "TRIA_RIGHT",
            )
            display_header.label(text="DISPLAY SETTINGS")
            if props.preview_display_settings_open:
                style_row = display.row(align=True)
                if controls["chart_style"]:
                    style_row.prop(props, "visualizer_style", text="")
                style_row.prop(props, "visualizer_detailed", text="Detailed", toggle=True)
                if controls["fixed_scale"]:
                    style_row.prop(props, "visualizer_anchored", text="",
                                   icon="FIXED_SIZE", toggle=True)
                lw_label = "Text Preview" if props.lightweight_preview else "Pixel Preview"
                lw_icon = "FONT_DATA" if props.lightweight_preview else "IMAGE_BACKGROUND"
                style_row.operator(
                    "espresso.toggle_lightweight_preview",
                    text=lw_label,
                    icon=lw_icon,
                    depress=props.lightweight_preview,
                )

        if (compact_level == 0 and props.preview_display_settings_open
                and props.visualizer_detailed):
            # Scale changes only the pixel preview's on-screen size. Text mode
            # has no image widget to resize, so the control is irrelevant there.
            if not props.lightweight_preview:
                scale_row = graph_box.row(align=True)
                scale_row.prop(props, "visualizer_scale", text="Scale", slider=True)

            # One slider, independently routed to horizontal time and vertical
            # value viewports. Both are chart viewports: a picture has no axes to
            # zoom, so the row belongs to the graph alone.
            if controls["axis_zoom"]:
                _draw_preview_zoom_row(graph_box, props)

        if active_target_mode:
            _draw_active_target_graph_block(
                graph_box, props, context,
                compact=compact, condensed=condensed,
            )
        else:
            if props.is_valid:
                _draw_graph_block(
                    graph_box, props, template, context,
                    compact=compact, condensed=condensed,
                )


def draw_variable_controls(layout, driver, template):
    """A live number field for each driver variable this template requires,
    editing whatever real property it's actually wired to — usually the
    custom property Setup Missing Variables just created. Lets a Property
    Control template be tested right here instead of hunting through the
    object's Custom Properties tab, and works for any variable the user has
    since repointed at something else (a bone, a shape key, ...) too."""
    required = template.get("requires_driver_variables", [])
    if not required or driver is None:
        return
    rows = []
    for var_def in required:
        var = driver.variables.get(var_def["name"])
        if var is None or var.type != "SINGLE_PROP":
            continue
        binding = utils.resolve_variable_ui_binding(var.targets[0])
        if binding is None:
            continue
        rows.append((var_def["name"], binding))
    if not rows:
        return
    box = layout.box()
    box.label(text="Variable Controls", icon="DRIVER")
    for name, (id_data, prop_path, index) in rows:
        row = box.row(align=True)
        if index >= 0:
            row.prop(id_data, prop_path, index=index, text=name)
        else:
            row.prop(id_data, prop_path, text=name)


def draw_input_source(layout, props, template):
    """Pending Espresso input card. Shared with the Input Controller."""
    if not template.get("requires_driver_variables"):
        return
    source_display.draw_pending_source_card(layout, bpy.context, title="INPUT SOURCE")


def _draw_driver_target_legacy(apply_box, header, props, template, context):
    """Byte-identical to the pre-picker DRIVER TARGET box — the master
    bypass path used when the addon preference is off, or as the resolver's
    own fallback path via get_active_driver_fcurve."""
    if _browsed_template_owns_destination(template):
        return
    fcurve, driver, driver_reason = utils.get_active_driver_fcurve(context)
    if driver is not None:
        missing_vars = missing_required_variables(driver, template)
        active_obj = context.active_object
        obj_name = active_obj.name if active_obj else "Active"
        target_desc = f"{obj_name}.{fcurve.data_path}"
        if fcurve.array_index >= 0:
            target_desc += f"[{fcurve.array_index}]"

        actions = header.row(align=True)
        apply_part = actions.row(align=True)
        apply_part.enabled = props.is_valid and not missing_vars
        apply_part.operator("espresso.apply_expression", text="", icon="CHECKMARK")
        actions.operator("espresso.remove_driver", text="", icon="TRASH")
        apply_box.label(text=f"Active: {target_desc}", icon="LINKED")

        if missing_vars:
            mv_box = apply_box.box()
            mv_box.alert = True
            mv_box.label(text="Missing variables: " + ", ".join(missing_vars), icon="ERROR")
            setup_row = apply_box.row()
            setup_row.operator("espresso.create_variables", text="Setup Missing Variables", icon="ADD")
        else:
            draw_variable_controls(apply_box, driver, template)


def _draw_driver_target_fallback(apply_box, props, template, active_fcurve, active_driver):
    """Single-line fallback UI for when the resolver found a driver via
    Drivers-Editor selection that isn't on the active object's own
    animation_data (e.g. a material node-tree driver) — the per-object row
    list intentionally doesn't scan those, but Apply/Remove should still
    work on whatever's selected, matching pre-picker behavior."""
    if _browsed_template_owns_destination(template):
        return
    owner = active_fcurve.id_data
    target_desc = f"{getattr(owner, 'name', 'Driver')}.{active_fcurve.data_path}"
    if active_fcurve.array_index >= 0:
        target_desc += f"[{active_fcurve.array_index}]"
    missing_vars = missing_required_variables(active_driver, template)

    row = apply_box.row(align=True)
    apply_part = row.row(align=True)
    apply_part.enabled = props.is_valid and not missing_vars
    apply_part.operator("espresso.apply_expression", text="", icon="CHECKMARK")
    row.operator("espresso.remove_driver", text="", icon="TRASH")
    apply_box.label(text=f"Active: {target_desc}", icon="LINKED")

    if missing_vars:
        mv_box = apply_box.box()
        mv_box.alert = True
        mv_box.label(text="Missing variables: " + ", ".join(missing_vars), icon="ERROR")
        setup_row = apply_box.row()
        setup_row.operator("espresso.create_variables", text="Setup Missing Variables", icon="ADD")
    else:
        draw_variable_controls(apply_box, active_driver, template)


def _draw_applied_effect_indent(row, nest_depth, minimum=0):
    """Keep nested-row indent from expanding into the leftover list width.

    After parent titles pack LEFT, an EXPAND spacer steals the remaining row
    and shoves checkbox + label to the far right.
    """
    depth = max(int(nest_depth or 0), int(minimum or 0))
    if depth <= 0:
        return
    indent = row.row()
    indent.alignment = "LEFT"
    indent.ui_units_x = 0.8 * depth
    indent.label(text="")


def _draw_applied_effect_group_title(row, item, scene_props, toggling):
    """Pack disclosure + name so parent titles do not fill the UIList row.

    A text operator in a default list row expands to the remaining width,
    which centers HOST / Motions / effect names in a large empty button.
    """
    cluster = row.row(align=True)
    cluster.alignment = "LEFT"
    if toggling:
        is_open = espresso_props.driver_target_group_is_open(scene_props, item.group_key)
        op = cluster.operator(
            "espresso.toggle_driver_target_group",
            text=item.label,
            icon="TRIA_DOWN" if is_open else "TRIA_RIGHT",
            emboss=False,
        )
        op.group_key = item.group_key
    else:
        cluster.label(text=item.label, icon=item.icon or "DRIVER")
    if item.badge_text:
        cluster.label(text=item.badge_text)


class ESPRESSO_UL_driver_targets(bpy.types.UIList):
    """Driver Target list: collapses to a fixed row count with Blender's own
    scrollbar once there are more drivers than that, and gets Blender's
    built-in name-filter search box for free via filter_items(). Backed by
    props.driver_target_items, a cheap path/index/label cache kept in sync
    by _ensure_driver_target_state_synced (never written here — draw_item
    runs during the same read-only draw() pass as everything else)."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        scene_props = context.scene.espresso_props
        template = espresso_props.get_current_template(scene_props)

        if item.row_kind in {"BUCKET", "HOST"}:
            row = layout.row(align=True)
            _draw_applied_effect_indent(row, item.nest_depth)
            descendants = [
                value for value in driver_manager.descendants_under_row(
                    scene_props.driver_target_items, item.group_key,
                )
                if value.batch_eligible
            ]
            if descendants:
                op = row.operator(
                    "espresso.select_driver_target_group", text="",
                    icon=(
                        "CHECKBOX_HLT"
                        if all(value.batch_selected for value in descendants)
                        else "CHECKBOX_DEHLT"
                    ),
                )
                op.group_key = item.group_key
            _draw_applied_effect_group_title(row, item, scene_props, toggling=True)
            return

        if item.row_kind == "CATEGORY":
            row = layout.row(align=True)
            _draw_applied_effect_indent(row, item.nest_depth, minimum=1)
            descendants = [
                value for value in driver_manager.descendants_under_row(
                    scene_props.driver_target_items, item.group_key,
                )
                if value.batch_eligible
            ]
            if descendants:
                op = row.operator(
                    "espresso.select_driver_target_group", text="",
                    icon=(
                        "CHECKBOX_HLT"
                        if all(value.batch_selected for value in descendants)
                        else "CHECKBOX_DEHLT"
                    ),
                )
                op.group_key = item.group_key
            _draw_applied_effect_group_title(row, item, scene_props, toggling=True)
            return

        if item.row_kind == "GROUP":
            row = layout.row(align=True)
            _draw_applied_effect_indent(row, item.nest_depth, minimum=1)
            nested = [
                value for value in scene_props.driver_target_items
                if value.group_key == item.group_key
                and value.row_kind in {"TARGET", "SETUP"}
            ]
            path_children = [value for value in nested if value.row_kind == "TARGET"]
            selectable = [value for value in nested if value.batch_eligible]
            if selectable:
                op = row.operator(
                    "espresso.select_driver_target_group", text="",
                    icon=(
                        "CHECKBOX_HLT"
                        if all(value.batch_selected for value in selectable)
                        else "CHECKBOX_DEHLT"
                    ),
                )
                op.group_key = item.group_key
            elif item.batch_eligible:
                row.prop(item, "batch_selected", text="")
            _draw_applied_effect_group_title(
                row, item, scene_props, toggling=bool(path_children),
            )
            managed = (
                item.effect_kind in {"MOTION_SET", "GENERATED_SETUP", "STRUCTURAL_EFFECT"}
                or item.template_id == "motion_stack"
            )
            if item.editable and managed:
                if item.effect_kind in {"GENERATED_SETUP", "STRUCTURAL_EFFECT"} or item.template_id == "motion_stack":
                    op = row.operator("espresso.manage_applied_effect", text="", icon="PREFERENCES")
                    op.record_token = item.record_token
                    op.action = "SETTINGS"
                    op.effect_kind = item.effect_kind
                else:
                    op = row.operator("espresso.edit_driver_target", text="", icon="PREFERENCES")
                    op.group_key = item.group_key
            if item.bakeable and managed:
                op = row.operator("espresso.manage_applied_effect", text="", icon="KEYFRAME_HLT")
                op.record_token = item.record_token
                op.action = "BAKE"
                op.effect_kind = item.effect_kind
            if item.removable and managed:
                op = row.operator("espresso.manage_applied_effect", text="", icon="TRASH")
                op.record_token = item.record_token
                op.action = "CLEAR"
                op.effect_kind = item.effect_kind
            return

        if item.row_kind == "SETUP":
            row = layout.row(align=True)
            _draw_applied_effect_indent(row, 1)
            selector = row.row(align=True)
            selector.enabled = item.batch_eligible
            selector.prop(item, "batch_selected", text="")
            row.label(text=item.label, icon=item.icon or "GEOMETRY_NODES")
            return

        descriptor = driver_targets.descriptor_from_item(item)
        fcurve = driver_targets.resolve_driver(descriptor)

        row = layout.row(align=True)
        _draw_applied_effect_indent(row, item.nest_depth, minimum=1)
        selector = row.row(align=True)
        selector.enabled = item.batch_eligible
        selector.prop(item, "batch_selected", text="")
        row.label(text=item.label, icon=item.icon or "DRIVER")
        if fcurve is None:
            row.label(text="Missing driver", icon="ERROR")
        elif (
            item.record_token
            and not item.editable
            and item.effect_kind == "SINGLE_PROPERTY"
        ):
            row.label(text="Read-only", icon="LOCKED")

        settings = row.row(align=True)
        settings.enabled = item.editable and item.effect_kind == "SINGLE_PROPERTY"
        edit_op = settings.operator("espresso.edit_driver_target", text="", icon="PREFERENCES")
        edit_op.descriptor_key = item.descriptor_key

        row_missing = missing_required_variables(fcurve.driver, template) if fcurve is not None else ["?"]
        browse_self_applied = _browsed_template_owns_destination(template)
        apply_part = row.row(align=True)
        apply_part.enabled = (
            not browse_self_applied
            and item.effect_kind == "SINGLE_PROPERTY" and fcurve is not None
            and scene_props.is_valid and not row_missing
        )
        apply_op = apply_part.operator("espresso.apply_expression", text="", icon="CHECKMARK")
        apply_op.data_path = item.data_path
        apply_op.array_index = item.array_index
        apply_op.target_id_type = item.owner_id_type
        apply_op.target_id_name = item.owner_id_name
        apply_op.target_owner_path = item.owner_path

        remove_part = row.row(align=True)
        remove_part.enabled = item.effect_kind == "SINGLE_PROPERTY" and fcurve is not None
        remove_op = remove_part.operator("espresso.remove_driver", text="", icon="TRASH")
        remove_op.data_path = item.data_path
        remove_op.array_index = item.array_index
        remove_op.target_id_type = item.owner_id_type
        remove_op.target_id_name = item.owner_id_name
        remove_op.target_owner_path = item.owner_path

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        helper = bpy.types.UI_UL_list
        driver_manager.set_list_search_filter(self.filter_name)
        if self.filter_name:
            flags = helper.filter_items_by_name(self.filter_name, self.bitflag_filter_item, items, "label")
        else:
            flags = [self.bitflag_filter_item] * len(items)
        for index, item in enumerate(items):
            if not item.visible:
                flags[index] = 0
        return flags, []


def applied_effects_heading(source):
    return {
        "LAST": "Last Effects",
        "SCENE": "Scene Effects",
    }.get(source, "Active Effects")


_LAYOUT_MODIFIER_CONTROL_NAMES = {
    "GRID": (("Columns", "Rows", "Layers"),
             ("Spacing X", "Spacing Y", "Spacing Z"),
             ("Layer Offset X", "Layer Offset Y"),
             ("Origin X", "Origin Y", "Origin Z"),
             ("Rotation X", "Rotation Y", "Rotation Z"),
             ("Scale X", "Scale Y", "Scale Z"),
             ("Taper X", "Taper Y", "Taper Z"),
             ("Jitter X", "Jitter Y", "Jitter Z"), ("Seed", "Omit")),
    "ROW": (("Columns", "Rows", "Layers"),
            ("Spacing X", "Spacing Y", "Spacing Z"),
            ("Layer Offset X", "Layer Offset Y"),
            ("Origin X", "Origin Y", "Origin Z"),
            ("Rotation X", "Rotation Y", "Rotation Z"),
            ("Scale X", "Scale Y", "Scale Z"),
            ("Taper X", "Taper Y", "Taper Z"),
            ("Jitter X", "Jitter Y", "Jitter Z"), ("Seed", "Omit")),
    "RING": (("Rings", "Points Per Ring", "Layers"),
             ("Radius", "Radius Step", "Layer Spacing"),
             ("Start Angle", "Arc"), ("Ring Phase", "Layer Twist"),
             ("Radius Taper", "Spokes", "Helix Rise")),
    "STACK": (("Columns", "Rows", "Layers"),
              ("Spacing X", "Spacing Y", "Spacing Z"),
              ("Layer Offset X", "Layer Offset Y"),
              ("Origin X", "Origin Y", "Origin Z"),
              ("Rotation X", "Rotation Y", "Rotation Z"),
              ("Scale X", "Scale Y", "Scale Z"),
              ("Taper X", "Taper Y", "Taper Z"),
              ("Jitter X", "Jitter Y", "Jitter Z"), ("Seed", "Omit")),
    "CURVE": (("Count",), ("Curve Offset", "Twist")),
    "SHAPE_SURFACE": (("Density", "Seed"), ("Surface Offset",),
                      ("Offset X", "Offset Y", "Offset Z"),
                      ("Rotation X", "Rotation Y", "Rotation Z"),
                      ("Scale X", "Scale Y", "Scale Z")),
    "SHAPE_VOLUME": (("Density", "Seed"), ("Surface Offset",),
                     ("Offset X", "Offset Y", "Offset Z"),
                     ("Rotation X", "Rotation Y", "Rotation Z"),
                     ("Scale X", "Scale Y", "Scale Z")),
    "SPIRAL": (("Count", "Turns"), ("Inner Radius", "Outer Radius"),
               ("Height", "Start Angle"), ("Radial Bias", "Follow Tangent")),
    "SPHERE": (("Count", "Radius"), ("Scale X", "Scale Y", "Scale Z"),
               ("Z Minimum", "Z Maximum"), ("Twist", "Start Angle")),
    "CONCENTRIC": (("Rings", "Points Per Ring", "Layers"),
                   ("Radius", "Radius Step", "Layer Spacing"),
                   ("Start Angle", "Arc"), ("Ring Phase", "Layer Twist"),
                   ("Radius Taper", "Spokes", "Helix Rise")),
}

_LEGACY_LAYOUT_MODIFIER_CONTROL_NAMES = {
    "ROW": (("Count", "Spacing"),),
    "RING": (("Count", "Radius"),),
    "STACK": (("Count",), ("Step X", "Step Y", "Step Z")),
    "CONCENTRIC": (("Rings", "Points Per Ring"), ("Radius Step",)),
    "SPIRAL": (("Count",), ("Inner Radius", "Outer Radius"), ("Turns",)),
    "SPHERE": (("Count", "Radius"),),
}


def _draw_layout_carrier_modifier_controls(layout, carrier):
    modifier = layout_preparation.layout_modifier(carrier)
    if modifier is None:
        layout.label(text="Layout modifier is missing.", icon="ERROR")
        return
    kind = str(carrier.get(layout_preparation.LAYOUT_KIND_TAG, "GRID"))
    control_groups = _LAYOUT_MODIFIER_CONTROL_NAMES.get(kind, ())
    modern_anchor = {
        "ROW": "Columns", "STACK": "Columns",
        "RING": "Points Per Ring", "CONCENTRIC": "Points Per Ring",
        "SPIRAL": "Height", "SPHERE": "Scale X",
    }.get(kind, "")
    if (kind in _LEGACY_LAYOUT_MODIFIER_CONTROL_NAMES
            and not layout_preparation.input_identifier(carrier, modern_anchor)):
        control_groups = _LEGACY_LAYOUT_MODIFIER_CONTROL_NAMES[kind]
    for pair_names in control_groups:
        pair = layout.row(align=True)
        for name in pair_names:
            identifier = layout_preparation.input_identifier(carrier, name)
            if identifier:
                pair.prop(modifier, '["%s"]' % identifier, text=name)
    sources = layout_preparation.source_weights(carrier)
    if len(sources) > 1:
        mix = layout.box()
        mix.label(text="Instance Sources", icon="GROUP")
        total = max(1e-12, sum(value for _source, value in sources))
        for index, (source, value) in enumerate(sources):
            identifier = layout_preparation.input_identifier(
                carrier, "Source %d Weight" % (index + 1),
            )
            mix.prop(
                modifier, '["%s"]' % identifier,
                text="%s · %.0f%%" % (source.name, value / total * 100.0),
            )


def _pinned_settings_noun(effect):
    """Artist-facing unit for pinned update/bake/delete actions."""
    from ...apply.motion import applied_motion_manager

    template_id = str(effect.get("template_id") or "")
    kind = str(effect.get("effect_kind") or "")
    if effect.get("child_effect_kind") == "SHAPE_ADAPTATION":
        return "Shape Adaptation"
    if kind == applied_motion_manager.STRUCTURAL_EFFECT:
        return "Layout"
    return "Motion"


def _draw_pinned_manage_actions(layout, effect, noun):
    token = str(effect.get("record_token") or "")
    kind = str(effect.get("effect_kind") or "")
    can_bake = bool(effect.get("bakeable", True))
    can_remove = bool(effect.get("removable", True))
    if not can_bake and not can_remove:
        return
    actions = layout.row(align=True)
    if can_bake:
        bake_op = actions.operator(
            "espresso.manage_applied_effect",
            text="Bake %s" % noun,
            icon="OUTLINER_DATA_MESH",
        )
        bake_op.record_token = token
        bake_op.effect_kind = kind
        bake_op.action = "BAKE"
    if can_remove:
        clear_op = actions.operator(
            "espresso.manage_applied_effect",
            text="Delete %s" % noun,
            icon="TRASH",
        )
        clear_op.record_token = token
        clear_op.effect_kind = kind
        clear_op.action = "CLEAR"


def _draw_pinned_effect_settings(layout, props, context):
    if not props.applied_effect_settings_pinned:
        return
    from ...apply.motion import applied_motion_manager
    from ..actions.driver_manager import _active_effect_item

    box = layout.box()
    header = box.row(align=True)
    header.prop(
        props, "pinned_effect_settings_open", text="", emboss=False,
        icon="TRIA_DOWN" if props.pinned_effect_settings_open else "TRIA_RIGHT",
    )

    item = _active_effect_item(props)
    if item is None or not item.record_token:
        header.label(text="Settings", icon="PREFERENCES")
        if not props.pinned_effect_settings_open:
            return
        draw_wrapped(
            box,
            "Select a layout or motion in the list to edit its settings here.",
            icon="INFO",
            width=58,
        )
        return

    effect = applied_motion_manager.find_effect(
        context, item.record_token, props.driver_target_source,
    )
    if effect is None:
        header.label(text="Settings", icon="ERROR")
        if not props.pinned_effect_settings_open:
            return
        box.label(text="The selected effect no longer resolves.", icon="ERROR")
        return

    noun = _pinned_settings_noun(effect)
    heading = str(effect.get("label") or props.pinned_applied_effect_label or noun)
    header.label(text=heading, icon="PREFERENCES")
    header.operator("espresso.refresh_pinned_effect_settings", text="", icon="FILE_REFRESH")
    if not props.pinned_effect_settings_open:
        return

    kind = str(effect.get("effect_kind") or "")
    host = effect.get("host")
    template = effect.get("template") or templates.TEMPLATE_BY_ID.get(
        str(effect.get("template_id") or ""),
    )
    template_id = str(effect.get("template_id") or "")


    if kind == applied_motion_manager.STRUCTURAL_EFFECT and host is not None:
        _draw_layout_carrier_modifier_controls(box, host)
        draw_note(
            box,
            "Layout socket changes apply immediately.",
            label="Live layout controls",
        )
        _draw_pinned_manage_actions(box, effect, noun)
        return

    if template is None:
        box.label(text="Template is no longer available.", icon="ERROR")
        return


    if espresso_props.settings_share_live_props(context, effect, template):
        draw_parameter_value_controls(box, props, template, context)
    elif props.pinned_effect_parameters:
        draw_parameter_item_controls(
            box, props, template, props.pinned_effect_parameters, context,
            destination="PINNED",
        )

    _draw_pinned_manage_actions(box, effect, noun)


def _draw_prepare_layout_guidance(layout, message, icon="INFO"):
    text = str(message or "").strip()
    if not text:
        text = (
            "Prepare a layout in Prepare & Organize Effects, then apply this motion."
        )
    draw_wrapped(layout, text, icon=icon, width=58)


_LAST_APPLY_STATUS_PLACEHOLDER = "No remembered target yet."


def _last_apply_status_icon(text):
    lowered = str(text or "").lower()
    if any(token in lowered for token in (
        "fail", "error", "could not", "no longer", "lost", "missing", "broken",
    )):
        return "ERROR"
    if any(token in lowered for token in (
        "applied", "updated", "removed", "remembered", "baked", "cleared",
    )):
        return "CHECKMARK"
    return "INFO"


def _draw_last_apply_status_strip(layout, props):
    text = str(getattr(props, "last_apply_status", "") or "").strip()
    if not text or text == _LAST_APPLY_STATUS_PLACEHOLDER:
        return
    if getattr(props, "last_apply_status_dismissed", False):
        return
    row = layout.row(align=True)
    row.label(text=text, icon=_last_apply_status_icon(text))
    row.operator(
        "espresso.dismiss_last_apply_status", text="", icon="X", emboss=False,
    )


def _status_mentions_bake(props):
    text = str(getattr(props, "last_apply_status", "") or "").lower()
    return "baked" in text or "keyframe" in text


def _active_stamp_records(context):
    obj = getattr(context, "active_object", None) if context is not None else None
    if obj is None:
        return []
    hosts = list(applied_motion.hosts_for_object(obj) or ())
    carrier = layout_preparation.resolve_carrier(obj)
    if carrier is not None and carrier not in hosts:
        hosts.extend(applied_motion.hosts_for_object(carrier) or ())
    records = []
    for host in hosts:
        for record in applied_motion.read(host) or ():
            records.append((host, record))
    return records


def _record_has_missing_driver(host, record):
    paths = applied_motion.paths_of(record or {})
    if not paths or host is None:
        return False
    live = applied_motion.entries(host, validate=True)
    token = applied_motion.entry_token(host, record)
    return all(applied_motion.entry_token(host, item) != token for item in live)


def _draw_listed_recovery_notes(layout, effects, targets):
    missing_driver = False
    readonly = False
    for effect in effects or ():
        record = effect.get("record") or {}
        if _record_has_missing_driver(effect.get("host"), record):
            missing_driver = True
        if (
            effect.get("record_token")
            and not effect.get("editable")
            and effect.get("effect_kind") == "SINGLE_PROPERTY"
        ):
            readonly = True
    if not missing_driver:
        for target in targets or ():
            resolved, _reason = target_memory.resolve_target_record(target)
            if resolved is None or driver_targets.resolve_driver(target) is None:
                missing_driver = True
                break
    if missing_driver:
        layout.label(text="The applied driver is missing.", icon="ERROR")
    if readonly:
        layout.label(text="A listed effect is read-only.", icon="LOCKED")


def _draw_applied_effects_empty_state(layout, props, source, context=None):
    if source == "ACTIVE":
        stamps = _active_stamp_records(context)
        if stamps:
            if any(applied_motion.paths_of(record) for _host, record in stamps):
                layout.label(text="The applied driver is missing.", icon="ERROR")
                layout.label(
                    text="Remove the leftover or reapply the effect.",
                    icon="INFO",
                )
                return
            layout.label(text="The applied setup is missing.", icon="ERROR")
            layout.label(
                text="Its generated resources are no longer in the scene.",
                icon="INFO",
            )
            return
        if _status_mentions_bake(props):
            layout.label(
                text="The last effect was baked to keyframes.",
                icon="KEYFRAME_HLT",
            )
            layout.label(
                text="Native animation remains. Espresso settings are read-only.",
                icon="LOCKED",
            )
            return
        layout.label(text="No effects on the active object.", icon="INFO")
        token = str(getattr(props, "last_applied_effect_token", "") or "")
        scene_count = int(getattr(props, "applied_effects_scene_cached_count", 0) or 0)
        remembered = bool(target_memory.latest_entry(props))
        if token or scene_count or remembered:
            layout.label(
                text="Open Scene to manage recorded effects.",
                icon="SCENE_DATA",
            )
        return
    if source == "LAST":
        token = str(getattr(props, "last_applied_effect_token", "") or "")
        remembered = bool(target_memory.latest_entry(props))
        if token or remembered:
            layout.label(text="The last effect no longer resolves.", icon="ERROR")
            layout.label(text="Its host or driver is missing.", icon="INFO")
            return
        layout.label(text="No last effect is remembered.", icon="INFO")
        return
    layout.label(text="No recorded effects in this scene.", icon="INFO")


def _draw_hidden_host_scope_note(layout, source, effects, targets):
    if source not in {"SCENE", "LAST"}:
        return
    hosts = [effect.get("host") for effect in effects or ()]
    for target in targets or ():
        name = str(target.get("id_name") or "")
        if name and name in bpy.data.objects:
            hosts.append(bpy.data.objects[name])
    badges = {
        driver_manager.host_visibility_badge(host)
        for host in hosts if host is not None
    }
    if "Hidden" in badges or "Excluded" in badges:
        layout.label(
            text="Hidden and excluded hosts stay listed and operable.",
            icon="HIDE_ON",
        )


def _draw_driver_target_picker(apply_box, props, template, context):
    """Scrollable, searchable driver list (ESPRESSO_UL_driver_targets) — click
    a row to make it the active target, per-row Apply/Remove stay inline."""
    browse_self_applied = _browsed_template_owns_destination(template)
    source = getattr(props, "driver_target_source", "ACTIVE")
    screen = getattr(context, "screen", None)
    reuse_during_playback = bool(
        getattr(screen, "is_animation_playing", False)
        and len(props.driver_target_items)
    )
    if reuse_during_playback:
        # The UIList already owns a stable snapshot. Rewalking every scene
        # host and generated manifest on every playback redraw only burns the
        # frame budget; mutation resumes discovery as soon as playback stops.
        all_targets = ()
        effects = ()
    else:
        all_targets = driver_targets.panel_targets(context, props)
        from ...apply import applied_motion_manager
        effects = applied_motion_manager.collect_effects_for_draw(context, source)

    # Resolve regardless of whether the active object has drivers of its own —
    # the resolver's final fallback (Drivers-Editor selection detection) can
    # still find a driver on a different ID-block entirely (e.g. a material
    # node tree), which the per-object row list below intentionally doesn't
    # scan. Without this, selecting such a driver in the Drivers Editor would
    # silently do nothing.
    if reuse_during_playback:
        active_fcurve = active_driver = None
    else:
        active_fcurve, active_driver, _reason = utils.get_target_driver_fcurve(
            context, targets=all_targets,
        )

    last_is_stale = (
        source == "LAST"
        and not reuse_during_playback
        and not effects
        and not any(
            target_memory.resolve_target_record(item)[0] is not None
            for item in all_targets
        )
    )
    if not reuse_during_playback and (not all_targets and not effects or last_is_stale):
        _draw_applied_effects_empty_state(apply_box, props, source, context)
        if active_driver is not None and not last_is_stale:
            _draw_driver_target_fallback(apply_box, props, template, active_fcurve, active_driver)
        return

    if not reuse_during_playback:
        _ensure_driver_target_state_synced(
            context.scene, props, all_targets, effects,
        )

    _draw_hidden_host_scope_note(apply_box, source, effects, all_targets)
    _draw_listed_recovery_notes(apply_box, effects, all_targets)

    list_box = apply_box.box()
    heading = applied_effects_heading(source)
    if props.driver_target_items:
        effect_count, driver_count = driver_manager.selected_removal_counts(
            props.driver_target_items, selected_only=False,
        )
        visible_count = driver_manager.visible_resource_label(
            effect_count, driver_count,
        )
    else:
        visible_count = len(effects) or len(all_targets)
    heading_row = list_box.row(align=True)
    heading_row.label(text=f"{heading} ({visible_count}):")
    # Records whose drivers are gone, and drivers of ours with no record.
    # The idle reconciler clears the first kind on its own; this is the
    # explicit way, scoped like the list, and it lists what it found with a
    # tick per finding before anything is cleared. The count is read off the
    # draw-cached collection, so the button costs the panel nothing.
    invalid_count = sum(
        1 for effect in (effects or ()) if effect.get("alive", True) is False
    )
    clear = heading_row.row(align=True)
    clear.alignment = "RIGHT"
    if invalid_count:
        clear.alert = True
    clear.operator(
        "espresso.clear_invalid_motions",
        text=("Clear Invalid (%d)" % invalid_count) if invalid_count else "Clear Invalid",
        icon="TRASH",
    )
    # rows is the MINIMUM height, not just the default, so the drag grip can
    # never shrink the list below it - a single target was reserving seven rows
    # of empty box. Keep the floor low and let maxrows do the growing instead.
    list_box.template_list(
        "ESPRESSO_UL_driver_targets", "", props, "driver_target_items",
        props, "driver_target_active_index", rows=3, maxrows=7,
    )
    actions = list_box.row(align=True)
    actions.operator("espresso.driver_targets_select", text="All").mode = "ALL"
    actions.operator("espresso.driver_targets_select", text="Invert").mode = "INVERT"
    actions.operator("espresso.driver_targets_select", text="None").mode = "NONE"
    overwrite_count = sum(
        1 for item in props.driver_target_items
        if item.row_kind == "TARGET" and item.batch_eligible and item.batch_selected
    )
    effect_count, driver_count = driver_manager.selected_removal_counts(
        props.driver_target_items,
    )
    if effect_count or driver_count:
        batch = list_box.row(align=True)
        apply_part = batch.row(align=True)
        apply_part.enabled = (
            bool(overwrite_count)
            and not templates.has_motion_plan(template)
            and not browse_self_applied
        )
        apply_part.operator(
            "espresso.apply_selected_driver_targets",
            text="Overwrite %d Selected" % overwrite_count,
            icon="FILE_REFRESH",
        )
        batch.operator(
            "espresso.remove_selected_driver_targets",
            text=driver_manager.remove_selected_label(effect_count, driver_count),
            icon="TRASH",
        )

    if active_driver is not None and not browse_self_applied:
        missing_vars = missing_required_variables(active_driver, template)
        if missing_vars:
            mv_box = apply_box.box()
            mv_box.alert = True
            mv_box.label(text="Missing variables: " + ", ".join(missing_vars), icon="ERROR")
            setup_row = apply_box.row()
            setup_row.operator("espresso.create_variables", text="Setup Missing Variables", icon="ADD")
        else:
            draw_variable_controls(apply_box, active_driver, template)


def draw_driver_target(layout, props, template, context):
    root = layout.column(align=True)
    motions_box = root.box()
    motions_header = motions_box.row(align=True)
    motions_header.prop(
        props, "applied_motions_open", text="", emboss=False,
        icon="TRIA_DOWN" if props.applied_motions_open else "TRIA_RIGHT",
    )
    motions_header.label(text="APPLIED EFFECTS", icon="NLA")
    if props.applied_motions_open:
        header = motions_box.row(align=True)
        if utils.driver_target_picker_enabled(context):
            header.prop(props, "driver_target_source", expand=True)
        header.operator(
            "espresso.toggle_applied_effect_settings_pin",
            text="",
            icon="PREFERENCES",
            depress=props.applied_effect_settings_pinned,
        )

        _draw_last_apply_status_strip(motions_box, props)

        if not utils.driver_target_picker_enabled(context):
            _draw_driver_target_legacy(motions_box, header, props, template, context)
        else:
            _draw_driver_target_picker(motions_box, props, template, context)

    _draw_pinned_effect_settings(root, props, context)


def _draw_sidebar_tabs(layout, props):
    layout.prop(props, "sidebar_tab", expand=True)
    layout.separator(factor=0.8)


def _draw_motion_panel_body(layout, props, template, context):
    guided_apply.draw_browser_view(layout, props, template, context)
    if (template.get("variants") and not _compact(context)
            and _section_enabled(context, "show_variants_section")):
        layout.separator(factor=1.2)
    guided_apply.draw_setup_live_view(layout, props, template, context)
    # Add spacing whenever the ADVANCED box is present — it appears for any
    # template that has at least one advanced control.
    if _compact_level(context) < 2 and any(_group_supported_advanced_controls(template).values()):
        layout.separator(factor=1.2)

    guided_apply.draw_target_preflight_view(layout, props, template, context)
    layout.separator(factor=1.2)


def _draw_organize_panel_body(layout, props, template, context):
    guided_apply.draw_applied_effects_view(layout, props, template, context)


def _draw_controller_panel_body(layout, props, template, context):
    from ..state import live_controls

    live_controls.draw_panel(layout, props, template, context)


class ESPRESSO_PT_base:
    bl_category = _TAB_ESPRESSO
    # From product.identity, so an extracted product carries its own name
    # rather than the one the unified source happens to have.
    bl_label = _PRODUCT_NAME
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return True

    def draw_header_preset(self, context):
        """Header buttons, right-aligned.

        This is the ONLY way to reach the right side of a panel header. A panel
        header's layout is built at zero width and grows to fit its contents, so
        it has no spare space for separator_spacer to distribute - pushing it
        anyway does not right-align, it expands the block past the region and
        breaks the sidebar. Blender draws draw_header_preset flush to the right
        edge instead, which is what it exists for.
        """
        row = self.layout.row(align=True)
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is not None and _sidebar_tab(props) == _SIDEBAR_TAB_ORGANIZE:
            label = _applied_effects_header_label(props)
            if label:
                row.label(text=label)
        if props is not None and _sidebar_tab(props) == _SIDEBAR_TAB_MOTION:
            # Compact Mode lives in the title bar, not preferences: it is a view the
            # user flips while working, not a setting they configure once.
            row.operator(
                "espresso.toggle_compact_mode", text="",
                icon=_compact_icon(_compact_level(context)),
                depress=_compact_level(context) > 0,
                emboss=False,
            )
        row.operator(
            "espresso.open_preferences",
            text="",
            icon="PREFERENCES",
            emboss=False,
        )

    def draw(self, context):
        props = context.scene.espresso_props
        _ensure_template_initialized(context.scene, props)
        template = espresso_props.get_current_template(props)
        layout = self.layout
        _draw_sidebar_tabs(layout, props)
        tab = _sidebar_tab(props)
        if tab == _SIDEBAR_TAB_MOTION:
            _draw_motion_panel_body(layout, props, template, context)
        elif tab == _SIDEBAR_TAB_ORGANIZE:
            _draw_organize_panel_body(layout, props, template, context)
        elif tab == _SIDEBAR_TAB_CONTROLLER:
            _draw_controller_panel_body(layout, props, template, context)


class ESPRESSO_PT_view3d(ESPRESSO_PT_base, bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_view3d"


class ESPRESSO_PT_graph(ESPRESSO_PT_base, bpy.types.Panel):
    bl_space_type = "GRAPH_EDITOR"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_graph"


SUPPORTED_NODE_TREE_TYPES = frozenset({
    "ShaderNodeTree",
    "GeometryNodeTree",
    "CompositorNodeTree",
})


def node_editor_supported(context):
    space = getattr(context, "space_data", None)
    return getattr(space, "tree_type", "") in SUPPORTED_NODE_TREE_TYPES


class ESPRESSO_PT_node(ESPRESSO_PT_base, bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_node"

    @classmethod
    def poll(cls, context):
        return node_editor_supported(context)


class ESPRESSO_PT_preview_base:
    """Standalone preview panel — stays visible regardless of workspace tab."""

    bl_category = _TAB_ESPRESSO
    bl_label = "Preview"
    bl_order = 0

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        row.operator(
            "espresso.toggle_preview_compact_mode",
            text="",
            icon=_compact_icon(_preview_compact_level(context)),
            depress=_preview_compact_level(context) > 0,
            emboss=False,
        )
        row.separator(factor=0.6)

    @classmethod
    def poll(cls, context):
        return _section_enabled(context, "show_preview_panel")

    def draw(self, context):
        props = context.scene.espresso_props
        _ensure_template_initialized(context.scene, props)
        template = espresso_props.get_current_template(props)
        guided_apply.draw_preview_view(self.layout, props, template, context)


class ESPRESSO_PT_preview_view3d(ESPRESSO_PT_preview_base, bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_preview_view3d"


class ESPRESSO_PT_preview_graph(ESPRESSO_PT_preview_base, bpy.types.Panel):
    bl_space_type = "GRAPH_EDITOR"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_preview_graph"


class ESPRESSO_PT_preview_node(ESPRESSO_PT_preview_base, bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_idname = "ESPRESSO_PT_preview_node"

    @classmethod
    def poll(cls, context):
        return node_editor_supported(context) and super().poll(context)


class ESPRESSO_OT_toggle_advanced_section(bpy.types.Operator):
    bl_idname = "espresso.toggle_advanced_section"
    bl_label = "Toggle Advanced Controls"
    bl_description = "Expand or collapse the advanced controls section"

    def execute(self, context):
        context.window_manager[ADVANCED_SECTION_STATE_KEY] = not _advanced_section_open(context)
        return {"FINISHED"}


class ESPRESSO_OT_inspect_advanced_param(bpy.types.Operator):
    bl_idname = "espresso.inspect_advanced_param"
    bl_label = "Advanced Parameter Info"
    bl_description = "Show template-specific help for this advanced parameter"

    token: bpy.props.StringProperty()

    @classmethod
    def description(cls, _context, properties):
        token = getattr(properties, "token", "")
        if not token:
            return cls.bl_description
        try:
            return espresso_templates.format_param_tooltip(espresso_templates.resolve_advanced_param(token))
        except Exception:
            return cls.bl_description

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=320)

    def draw(self, context):
        try:
            control = espresso_templates.resolve_advanced_param(self.token)
        except Exception:
            self.layout.label(text="No parameter info available.", icon="INFO")
            return
        for line in espresso_templates.format_param_tooltip(control).split("\n"):
            self.layout.label(text=line, icon="INFO" if line.startswith(control["label"]) else "BLANK1")

    def execute(self, context):
        return {"FINISHED"}


class ESPRESSO_OT_reset_advanced_param_default(bpy.types.Operator):
    bl_idname = "espresso.reset_advanced_param_default"
    bl_label = "Reset Advanced Parameter"
    bl_description = "Reset this advanced parameter to the selected template's default value"

    token: bpy.props.StringProperty()

    @classmethod
    def description(cls, _context, properties):
        token = getattr(properties, "token", "")
        if not token:
            return cls.bl_description
        try:
            control = espresso_templates.resolve_advanced_param(token)
        except Exception:
            return cls.bl_description
        return f"Reset {control['label']} to its template default.\n{espresso_templates.format_param_tooltip(control)}"

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        supported_tokens = {
            control["token"] for control in espresso_templates.advanced_controls_for_template(template)
        }
        if self.token not in supported_tokens:
            return {"CANCELLED"}
        control = espresso_templates.resolve_advanced_param(self.token)
        espresso_props.set_advanced_param_value(props, control, control.get("default", 0))
        espresso_props.refresh_preview(props, context)
        self.report({"INFO"}, f"Reset {control['label']} to template default.")
        return {"FINISHED"}


class ESPRESSO_OT_toggle_lightweight_preview(bpy.types.Operator):
    bl_idname = "espresso.toggle_lightweight_preview"
    bl_label = "Preview Mode"
    bl_description = "Switch between Text Preview and Pixel Preview"

    @classmethod
    def description(cls, context, _properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props and getattr(props, "lightweight_preview", False):
            return (
                "Currently: Text Preview (braille text curve).\n"
                "Click to switch to Pixel Preview — rendered image graph, sharper and great for fast machines."
            )
        return (
            "Currently: Pixel Preview (rendered image graph).\n"
            "Click to switch to Text Preview — braille text curve, much cheaper on slower PCs."
        )

    def execute(self, context):
        props = context.scene.espresso_props
        props.lightweight_preview = not props.lightweight_preview
        return {"FINISHED"}


class ESPRESSO_OT_toggle_compact_mode(bpy.types.Operator):
    """Flip Compact Mode from the panel header.

    This is an operator rather than a plain ``row.prop`` toggle for one reason:
    a BoolProperty's tooltip is fixed text, so it cannot tell the user whether
    the mode is currently on or off. An operator's ``description()`` is rebuilt
    on every hover, so it can read the live value and say so. ``INTERNAL`` keeps
    it out of the redo panel (and the freed-RNA hover-crash class that lives
    there) since a view toggle has no business in Adjust Last Operation.
    """

    bl_idname = "espresso.toggle_compact_mode"
    bl_label = "Compact Mode"
    bl_options = {"INTERNAL"}

    @classmethod
    def description(cls, context, properties):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        level = _compact_level(context)
        names = {0: "Normal", 1: "Compact L1", 2: "Compact L2"}
        next_level = (level + 1) % 3
        # Reuse the property's own description as the body so the two never
        # drift apart when the wording is edited.
        body = ""
        try:
            body = props.bl_rna.properties["compact_mode"].description
        except Exception:
            body = ""
        head = f"{names[level]} (click for {names[next_level]})"
        if body:
            body = (
                "L1 hides Favorites and Variants. L2 keeps only the template, "
                "inputs, parameters, rest settings, and applicable apply actions."
            )
        return f"{head}.\n\n{body}" if body else head

    def execute(self, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return {"CANCELLED"}
        level = _compact_level(context)
        next_level = (level + 1) % 3
        props.compact_level = next_level
        props.compact_mode = next_level > 0
        return {"FINISHED"}


class ESPRESSO_OT_toggle_preview_compact_mode(bpy.types.Operator):
    """Cycle Preview density without changing the main Espresso panel."""

    bl_idname = "espresso.toggle_preview_compact_mode"
    bl_label = "Preview Compact Mode"
    bl_options = {"INTERNAL"}

    @classmethod
    def description(cls, context, _properties):
        level = _preview_compact_level(context)
        names = {0: "Normal", 1: "Compact L1", 2: "Compact L2"}
        return (
            f"Preview {names[level]} (click for {names[(level + 1) % 3]})."
            "\n\nL1 keeps the graph controls; L2 shows only the graph."
        )

    def execute(self, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return {"CANCELLED"}
        props.preview_compact_level = (_preview_compact_level(context) + 1) % 3
        return {"FINISHED"}


class ESPRESSO_OT_dismiss_last_apply_status(bpy.types.Operator):
    bl_idname = "espresso.dismiss_last_apply_status"
    bl_label = "Dismiss Last Result"
    bl_description = "Hide the last apply result until the next operation"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is None:
            return {"CANCELLED"}
        props.last_apply_status_dismissed = True
        return {"FINISHED"}


class ESPRESSO_OT_open_preferences(bpy.types.Operator):
    """Open the Driver Espresso addon preferences panel."""

    bl_idname = "espresso.open_preferences"
    bl_label = "Driver Espresso Preferences"
    bl_description = "Open Driver Espresso addon preferences"

    # Explicit poll so Blender never greys the button out regardless of context.
    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        # Open the preferences window from any context.
        bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        # Jump to the Add-ons tab.
        context.preferences.active_section = "ADDONS"
        # Type the addon name into the search box — instantly filters to Driver Espresso.
        context.window_manager.addon_search = _PRODUCT_NAME
        return {"FINISHED"}


CLASSES = (
    ESPRESSO_UL_driver_targets,
    ESPRESSO_MT_master_presets_drpdwn,
    ESPRESSO_MT_channel_picker,
    ESPRESSO_MT_live_effects,
    ESPRESSO_MT_presets_0,
    ESPRESSO_MT_presets_1,
    ESPRESSO_MT_presets_2,
    ESPRESSO_MT_presets_3,
    ESPRESSO_MT_presets_4,
    ESPRESSO_MT_presets_5,
    ESPRESSO_MT_presets_6,
    ESPRESSO_MT_presets_7,
    ESPRESSO_OT_toggle_advanced_section,
    ESPRESSO_OT_inspect_advanced_param,
    ESPRESSO_OT_reset_advanced_param_default,
    ESPRESSO_OT_toggle_lightweight_preview,
    ESPRESSO_OT_toggle_compact_mode,
    ESPRESSO_OT_toggle_preview_compact_mode,
    ESPRESSO_OT_dismiss_last_apply_status,
    ESPRESSO_OT_open_preferences,
    *settings_reset.CLASSES,
    ESPRESSO_PT_view3d,
    ESPRESSO_PT_graph,
    ESPRESSO_PT_node,
    ESPRESSO_PT_preview_view3d,
    ESPRESSO_PT_preview_graph,
    ESPRESSO_PT_preview_node,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
