"""N-panel UI for Driver Espresso."""

from __future__ import annotations

import bpy


from ...catalogue import browse_groups
from ...catalogue import catalogue_contracts, templates
from ...apply.setups import parameter_bindings
from ...apply import applied_motion
from ...apply import driver_manager, driver_targets
from ...engine import aliasing
from ...engine import duration as espresso_duration
from ...apply import apply_target
from ..state import props as espresso_props
from ...product.identity import NAME as _PRODUCT_NAME
from . import guided_apply


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
from ...apply import target_memory
from ...catalogue import templates as espresso_templates
from ...engine import utils
from ..actions import operators
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


def _draw_visible_applies_to(layout, destination, icon="DOT"):
    """Render the destination directly instead of hiding it in a tooltip."""
    target_box = layout.box()
    target_box.label(text="APPLIES TO", icon=icon)
    value_row = target_box.row(align=True)
    value_row.label(text=destination or "Choose a target property", icon="DOT")
    return target_box


# Scene pointers with a template-init timer already scheduled. Panel draw() runs in a
# read-only context, so writing props there raises "Writing to ID classes in this
# context is not allowed"; the write is deferred to a timer, which requests a redraw
# once it lands.
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
    """Same read-only draw() constraint as _ensure_template_initialized: the Driver
    Target UIList's backing CollectionProperty must be repopulated whenever the
    active object's drivers change, but panel draw() cannot write it. A mismatch
    schedules a zero-delay timer, so the list may be one frame stale until the timer
    lands and requests a redraw.
    """
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
                # A quiet marker, not a badge: these rows are ordinary choices that also
                # happen to sit on the shelf, and a loud pin icon would read as a status
                # the row is announcing. BLANK1 rather than no icon on the rest, so
                # every label still starts at the same x.
                icon="HANDLETYPE_AUTO_VEC" if is_pinned else "BLANK1",
            )
            op.preset_index = preset_index

        if not order:
            # Only reachable if the shelf holds every preset, in which case the
            # menu button is normally hidden - say so rather than show a blank.
            layout.label(text="Every preset is on the shelf", icon="INFO")


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
        identity = espresso_props.mode_presentation(
            context.scene.espresso_props, context,
        )
        current = identity.get("record_token", "")
        # One recipe can be applied more than once to the same host, on
        # different channels, so the recipe name alone is not a choice an
        # artist can make -- two rows reading "Candle Flicker" pick
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


def pairable_param_groups(template, excluded_tokens=(), props=None):
    params = template.get("params", [])
    excluded_tokens = frozenset(excluded_tokens)
    groups = []
    index = 0
    while index < len(params):
        current = params[index]
        if current.get("token") in excluded_tokens:
            index += 1
            continue
        next_param = params[index + 1] if index + 1 < len(params) else None
        if next_param and next_param.get("token") in excluded_tokens:
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
        # espresso.clear_search, not select_template: this button clears the search, and
        # select_template's tooltip would describe the template it selects rather than
        # what the button does.
        search_row.operator("espresso.clear_search", text="", icon="X")
    kind_row = layout.row(align=True)
    kind_row.prop(props, "template_kind", expand=True)
    layout.prop(props, "category")
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

    # Where this template sits, and what kind of thing it is. The subcategory says which
    # group the user is in; traits are orthogonal facts about the expression (it loops,
    # it needs a variable, it bakes the frame range).
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



# How many channels stay on screen as buttons before the rest fold into a
# dropdown. Three fits the panel width at normal sidebar sizes without the
# labels truncating to initials.


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
    """Domain label, plus the underlying maths token when the user asks for it. Friendly
    parameters hide the expression token they feed ("Seconds per revolution" sets
    TURNS); power users can reveal the mapping instead of reverse-engineering it from
    the formula.
    """
    label = param.get("label", "Parameter")
    if not _show_raw_parameters(context):
        return label
    derives = param.get("derives")
    if derives:
        return f"{label}  [{param['token']} → {', '.join(sorted(derives))}]"
    return f"{label}  [{param['token']}]"


def _draw_single_param_cell(layout, props, param, index, context=None):
    row = layout.row(align=True)
    label_and_value = row.split(factor=0.38, align=True)
    _draw_param_label(label_and_value.row(align=True), param, index, context)

    value_row = label_and_value.row(align=True)
    prop_name = espresso_props.param_property_name(param, index)
    value_row.prop(props, prop_name, text="")
    # The unit rides beside the field rather than inside the label, so the label
    # stays scannable and the number never loses its meaning.
    unit = param.get("unit")
    if unit:
        unit_cell = value_row.row()
        unit_cell.enabled = False   # renders dimmed; it is a caption, not a control
        unit_cell.label(text=unit)

    actions = row.row(align=True)
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

    # The whole section is opt-in from Preferences. When Advanced Controls is off there,
    # nothing is rendered, not even the collapsed header, so the panel does not carry an
    # empty box for a feature the user has not turned on.
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
    # Usage is keyed by label, so a preset that is renamed or removed simply stops
    # matching instead of handing its score to whatever now sits at its old index. Ties
    # keep catalogue order rather than falling to dict order.
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


def _shelf_param_groups(template, hidden_tokens, props):
    shelf_groups = {name: [] for name in ui_shelf.UI_GROUPS}
    for group in pairable_param_groups(
            template, excluded_tokens=set(hidden_tokens), props=props):
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
    prop = {"INT": "int_value", "COLOR": "color_value"}.get(item.value_type, "float_value")
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
        layout.label(text="No parameters needed.", icon="INFO")
        return

    shelf_groups = _shelf_param_groups(template, (), props)
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
    right.operator("espresso.reset_template_defaults", text="", icon="LOOP_BACK")
    if not props.parameters_open:
        return

    params_box.separator()

    controls = params_box
    if parameter_bindings.supports(template):
        mode_row = params_box.row(align=True)
        mode_row.prop(props, "parameter_mode", expand=True)
        identity = espresso_props.mode_presentation(props, context)
        binding = espresso_props.live_parameter_binding(context, controls_template) if context else None
        live_available = bool(binding and binding.available)
        live_editable = bool(live_available and binding.editable)
        binding_key = parameter_bindings.binding_key(binding)
        live_needs_reload = live_mode and binding_key != props.live_parameter_binding_key
        if live_mode:
            choices = parameter_bindings.active_effect_choices(context)
            # Always drawn, not only when there are two or more to pick between. Hiding
            # it below that would make the control appear on some templates and not
            # others with nothing to explain the difference, and would remove the one
            # place that says which applied effect Live is editing, which is worth
            # seeing even when there is only one.
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
            # The scope this list is drawn from, beside the list itself. An artist in
            # Live who cannot see their effect otherwise has no way to tell whether they
            # are looking at one object or the scene.
            #
            # Active and Scene only. Last is a memory of the most recent apply, which
            # answers a different question from "which applied effect am I editing";
            # Live always edits one specific effect. Last stays in Organize, where "what
            # did I just do" is the question.
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

    draw_parameter_value_controls(controls, props, controls_template, context)


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
        #
        # * value_scale: the fixed (min, max) the pixels are drawn against after Fixed
        #   Scale and viewport Zoom are resolved. It changes the rendered buffer, so it
        #   is part of the icon cache key.
        # * display_scale: how large that 224px image is shown in the panel, driven by
        #   the Scale slider. It only resizes an already-rendered icon, so it stays out
        #   of the cache key (no re-render on Scale).
        display_factor = float(getattr(props, "visualizer_scale", 1.0))
        display_scale = (16.0 if props.visualizer_detailed else 12.0) * display_factor
        anchor_tag = ("a%.4g_%.4g" % value_scale) if value_scale else "auto"
        if marks:
            anchor_tag += "-m" + "_".join("%.4g" % float(m) for m in marks)
        # The cursor line is baked into the rendered image, so keying on the current
        # frame would make every frame change a cache miss and a full 224x224 re-render.
        # Dropping the cursor during playback would remove the playhead when it is most
        # useful, and keying per frame is not an option either: one preview is 784 KB,
        # so a 250-frame playback would allocate about 191 MB, and this collection
        # cannot evict individually (at its cap it clears wholesale, discarding every
        # cached graph mid-playback).
        #
        # Playback therefore splits the work: the curve, guides, clamp marks and axis
        # labels are frame-independent and rendered once into a cached cursorless
        # buffer, then each frame stamps the one column that moves and re-uploads into a
        # single stable cache entry.
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


#: Keys already queued for a pre-apply preview, so the same one is
#: not rebuilt on every redraw.


#: Keys already queued for a pre-apply preview, so the same one is
#: not rebuilt on every redraw.


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

    # CENTERED_ZERO says what it means: the kernel oscillates about zero, so zero is its
    # resting level and no sampling is needed. Deriving it from the sampled origin would
    # make the baseline wander with every parameter, so the whole curve would slide
    # vertically while the artist adjusts an amplitude that cannot move a centre. A
    # constant is both correct and stable.
    if profile == "CENTERED_ZERO":
        return 0.0

    # Prefer the template's declared resting level over a sampled one. Every template
    # that states an output range already carries it as output_baseline (0.3 for
    # candle_flicker, 0.7 for a screen-style flicker, and so on), which is the Minimum
    # the artist typed. A sampled level would drift whenever a timing parameter changed,
    # because Speed and Phase move which frames get sampled; a declared value cannot
    # drift.
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
    return float(origin_value)


def _live_preview_expression(props, template, context):
    """The expression as it will be applied, for the Live graph.

    The plain preview is the template's own kernel, which shows the authored shape
    rather than the result (no hold before the apply frame, no clamp). Live wraps it
    through the same rest-state path the apply operators use, so what is drawn is what
    lands.

    It is deliberately independent of any particular property and does not read the
    value of whatever was applied to last: the same template is applied to many
    properties, so anchoring the preview to one of them would misrepresent the others.
    It previews from the template's own resting level, and when a clamp is on it shifts
    that level so the motion sits inside the limits, since what matters then is the
    shape against Min and Max. Returns (expression, rest_value, note).
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

    # The apply frame must not be the playhead. Rest Start bakes it into both the hold
    # threshold and the phase shift, so re-reading frame_current on every redraw would
    # make the whole curve slide while scrubbing. Anchor it to the frame the driver was
    # applied at once there is one, else the scene start.
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
        # Draw the motion where it actually is, unclamped, and let the graph mark the
        # limits. Clamping the drawn values would flatten anything outside them, and
        # shifting the curve to fit would place it at a height the values never occupy.
        # Guide lines show where the clamp cuts without moving or hiding data.
        built = build(rest_value, None)
    except Exception:  # noqa: BLE001 - a preview must never break the panel
        return expression, None, "", None

    # Collapse float dust: a rest of 7.8e-60 is zero, and printing it that way makes the
    # caption look like an error.
    shown = 0.0 if abs(rest_value) < 1e-9 else rest_value
    note = f"rest {shown:g}"
    if clamp_range is not None:
        note += f" · clamp {float(clamp_range[0]):g}–{float(clamp_range[1]):g}"
    return built, rest_value, f"{note} · {frame_note}", clamp_range


def preview_evaluation_switch_applies(props, template, context):
    """Whether Evaluation and Source can draw different things.

    With Rest Start off the two expressions are identical, and a generated systems
    timing model is identical whatever the setting, because there is no rest state for
    the wrapper to act on. A switch between two identical pictures invites the artist to
    look for a difference that is not there. This is answered by building the evaluated
    expression and comparing, rather than by listing templates, so it stays right when
    the rest-state rules change. The build is already done on every Evaluation draw, and
    it returns immediately when Rest Start is off, which is the case that would
    otherwise pay for it.
    """
    if not template:
        return False
    try:
        live, _rest, _note, limits = _live_preview_expression(props, template, context)
    except Exception:
        return True  # If it cannot be determined, keep the control rather than hide it.
    if limits:
        return True
    return str(live) != str(getattr(props, "preview", "") or "")


def _draw_graph_block(
    preview_box, props, template, context, compact=False, condensed=False,
):
    from . import visualizer
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

    live_note = ""
    clamp_marks = ()
    sampled = props.preview
    if getattr(props, "graph_preview_mode", "LIVE") == "LIVE":
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
    visible_range = visualizer.zoomed_value_range(
        result.points, scale, zoom_y,
    )
    _draw_result_graph(
        graph_box, result, props, context, value_scale=visible_range,
        marks=clamp_marks,
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

    Multi-channel templates create several drivers at once, so undoing by hand means
    hunting every channel. The remembered entry already holds the full target list, so
    one button covers single and multi alike. Only drawn once something has been
    applied, since an always-visible button that usually does nothing reads as broken.

    ``draw_clear=False`` and ``draw_update=False`` drop the clear and update buttons
    while keeping the "Last applied" note, for templates where a row further down
    already offers the same button (the Clear Transform Drivers pair for clearing, the
    "Update:" row for updating), so the panel does not offer Clear Last Drivers twice.
    The pair is the one that survives, because sitting beside Clear Transform Drivers
    makes the difference between the two kinds of clearing readable.
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
    """Generated-expression text plus apply settings (Rest Start, Copy, Update): the
    "what will be applied" part of the panel, shown directly under the parameters.
    The waveform graph and the driver-target picker are grouped together in the
    standalone, draggable Preview panel (draw_graph_and_target).
    """
    box = layout.box()
    minimal = _compact_level(context) >= 2
    is_motion = espresso_templates.has_motion_plan(template)
    # The Clear Transform Drivers row further down pairs its own Clear Last
    # Drivers beside itself, on exactly this condition. Where that row draws,
    # the standalone copy above must not - or the button appears twice.
    clear_last_is_paired = not is_motion
    # LIVE already relabels the commit slot to Update Selected on the same
    # operator. Drawing Update Last Target there too duplicates the control.
    update_is_paired = (
        espresso_props.commit_slot_presentation(props, context).get("family") == "update"
    )
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

    if not is_motion and not minimal:
        edit_toggle = header.row(align=True)
        edit_toggle.prop(props, "manual_mode", text="", icon="GREASEPENCIL", toggle=True)

    # Editable expression field while in manual mode.
    if props.manual_mode and not is_motion and not minimal:
        edit_box = box.box()
        edit_box.label(text="Editing expression", icon="GREASEPENCIL")
        edit_box.prop(props, "manual_expression", text="")

    if props.preview_open and not minimal:
        if is_motion:
            draw_motion_channel_list(box, props, template)
        else:
            formula_box = box.box()
            # Single line, deliberately not word-wrapped: this is a formula, not
            # prose, so wrapping mid-term reads worse than Blender's own clip.
            formula_box.label(text=props.preview)

    # How long the motion runs, measured from the built expression.
    #
    # The total is frequently not the sum of the parameters: Landing Compression
    # Recovery with "Recovery time 24" is still visibly moving 120 frames later, because
    # the recovery is a decaying ring rather than a fixed window, so reading it off a
    # parameter would be wrong. Wrapped because a draw function must never raise:
    # collect_values reads the live parameter slots, which only line up when the
    # template being drawn is the selected one. Anything else (a preview of another
    # template, a half-built context) loses the readout rather than the whole panel.
    if not minimal:
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
    if not catalogue_contracts.has_ordered_output_range(template):
        clamp_toggle = rest_row.row(align=True)
        clamp_toggle.enabled = props.rest_start_mode == "ADDITIVE"
        clamp_toggle.prop(props, "clamp_additive", text="Clamp", toggle=True)
        if props.clamp_additive and props.rest_start_mode == "ADDITIVE":
            limits = box.row(align=True)
            limits.prop(props, "clamp_min", text="Min")
            limits.prop(props, "clamp_max", text="Max")
    # There is no per-mode explanation line: the mode names say what they do, and such a
    # sentence would re-render on every redraw. ``utils.rest_start_summary()`` is kept
    # because the tests cover it and the wording is useful for a tooltip. Action
    # controls are kept separate from the collapsible expression preview.
    actions_box = layout.box()
    actions_header = actions_box.row(align=True)
    actions_header.prop(
        props,
        "actions_open",
        text="",
        emboss=False,
        icon="TRIA_DOWN" if props.actions_open else "TRIA_RIGHT",
    )
    # A return key, not PLAY: PLAY is a right-pointing triangle that sits directly
    # beside TRIA_RIGHT, so the heading icon of a collapsed section would read as a
    # second collapse arrow. EVENT_RETURN rather than KEY_RETURN, which does not exist
    # before 5.0: Blender raises on an unknown icon, and EVENT_RETURN is present in
    # every release this product supports.
    actions_header.label(text="APPLY & MANAGE", icon="EVENT_RETURN")
    if not props.actions_open:
        return
    box = actions_box

    # Lighting templates get a one-button route for a whole rig. Right-clicking one
    # lamp's Power and using Copy Drivers to Selected rebinds nothing, leaving every
    # lamp reading the first lamp.
    target_entry = apply_target.read(props)
    if operators.template_suits_lights(template, bool(target_entry)):
        lights = operators.applicable_objects(context)
        # Which of the two routes this selection is on. Lights never consult
        # the nominated target, so the panel must not describe them as if they
        # did - the wording differs, not just the count.
        selected_now = operators.arrangeable_objects(context)
        light_mode = apply_target.is_light_selection(selected_now)
        light_box = box.box()
        if not minimal:
            light_box.label(text="APPLIES TO", icon="LIGHT_DATA")

        if lights:
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
        light_row.operator(
            "espresso.apply_to_lights",
            text="Apply to %d %s%s"
                 % (len(lights), "Light" if light_mode else "Object",
                    "" if len(lights) == 1 else "s")
                 if lights else "Apply to Selected",
            icon="LIGHT_DATA",
        )

    # Non-light, non-semantic destinations still need to be discoverable before
    # the artist commits an apply. Transform motion has an active object/bone
    # destination; scalar and colour recipes use the remembered target route.
    if not minimal and not operators.template_suits_lights(
        template, bool(apply_target.read(props))
    ):
        destination = apply_target.label(props, _selected_objects(context))
        if not destination:
            destination = "Choose a target property from the right-click menu"
        _draw_visible_applies_to(box, destination, icon="DRIVER")

    if is_motion:
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

    # Always drawn, unlike _draw_clear_applied_row: that one only undoes what this
    # template last applied, whereas this clears whatever is on the selection -
    # including drivers made by hand or by another tool. Its poll() greys the
    # button out when nothing is selected, so it never lies about being usable.
    if not minimal and not is_motion:
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

    # This is not the "bake whatever drivers happen to be on the selection" that stays
    # in the right-click menu. Every entry here names the template it came from, read
    # from the stamp that apply left behind. Drawn only when something is applied, as
    # with Bake Last Driver: a button that silently does nothing is worse than none.
    # ``any_applied`` short-circuits on the first hit because this runs on every redraw.
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

    if not minimal and not is_motion:
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


def _draw_driver_target_legacy(apply_box, header, props, template, context):
    """Identical to the driver target box without the picker: the bypass path used when
    the add-on preference is off, or as the resolver's fallback through
    get_active_driver_fcurve.
    """
    fcurve, driver, driver_reason = utils.get_active_driver_fcurve(context)
    if driver is not None:
        active_obj = context.active_object
        obj_name = active_obj.name if active_obj else "Active"
        target_desc = f"{obj_name}.{fcurve.data_path}"
        if fcurve.array_index >= 0:
            target_desc += f"[{fcurve.array_index}]"

        actions = header.row(align=True)
        apply_part = actions.row(align=True)
        apply_part.enabled = props.is_valid
        apply_part.operator("espresso.apply_expression", text="", icon="CHECKMARK")
        actions.operator("espresso.remove_driver", text="", icon="TRASH")
        apply_box.label(text=f"Active: {target_desc}", icon="LINKED")


def _draw_driver_target_fallback(apply_box, props, template, active_fcurve, active_driver):
    """Single-line fallback UI for when the resolver found a driver via Drivers Editor
    selection that is not on the active object's own animation_data (a material
    node-tree driver, say). The per-object row list does not scan those, but
    Apply/Remove should still work on whatever is selected.
    """
    owner = active_fcurve.id_data
    target_desc = f"{getattr(owner, 'name', 'Driver')}.{active_fcurve.data_path}"
    if active_fcurve.array_index >= 0:
        target_desc += f"[{active_fcurve.array_index}]"

    row = apply_box.row(align=True)
    apply_part = row.row(align=True)
    apply_part.enabled = props.is_valid
    apply_part.operator("espresso.apply_expression", text="", icon="CHECKMARK")
    row.operator("espresso.remove_driver", text="", icon="TRASH")
    apply_box.label(text=f"Active: {target_desc}", icon="LINKED")


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
    """Driver Target list: collapses to a fixed row count with Blender's own scrollbar
    once there are more drivers than that, and gets Blender's built-in name-filter
    search box via filter_items(). Backed by props.driver_target_items, a
    path/index/label cache kept in sync by _ensure_driver_target_state_synced (never
    written here: draw_item runs during the same read-only draw() pass as everything
    else).
    """

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        scene_props = context.scene.espresso_props

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
                item.effect_kind == "MOTION_SET"
                or item.template_id == "motion_stack"
            )
            if item.editable and managed:
                if item.template_id == "motion_stack":
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

        apply_part = row.row(align=True)
        apply_part.enabled = (
            item.effect_kind == "SINGLE_PROPERTY" and fcurve is not None
            and scene_props.is_valid
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
            "Select a motion in the list to edit its settings here.",
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

    noun = "Motion"
    heading = str(effect.get("label") or props.pinned_applied_effect_label or noun)
    header.label(text=heading, icon="PREFERENCES")
    header.operator("espresso.refresh_pinned_effect_settings", text="", icon="FILE_REFRESH")
    if not props.pinned_effect_settings_open:
        return

    template = effect.get("template") or templates.TEMPLATE_BY_ID.get(
        str(effect.get("template_id") or ""),
    )

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

    # Resolve regardless of whether the active object has drivers of its own: the
    # resolver's final fallback (Drivers Editor selection) can find a driver on a
    # different ID block entirely (a material node tree, say), which the per-object row
    # list does not scan. Without this, selecting such a driver in the Drivers Editor
    # would do nothing.
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
    # Add spacing whenever the ADVANCED box is present; it appears for any template with
    # at least one advanced control.
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
            # user flips while working, not a setting configured once.
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
    """Standalone preview panel; stays visible regardless of workspace tab."""

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

    This is an operator rather than a plain ``row.prop`` toggle because a BoolProperty's
    tooltip is fixed text and cannot say whether the mode is currently on or off, while
    an operator's ``description()`` is rebuilt on every hover and can read the live
    value. ``INTERNAL`` keeps it out of the redo panel (and the freed-RNA hover crash
    that lives there), since a view toggle has no place in Adjust Last Operation.
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
                "L1 hides Variants. L2 keeps only the template, "
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
        # Type the add-on name into the search box; it filters to Driver Espresso.
        context.window_manager.addon_search = _PRODUCT_NAME
        return {"FINISHED"}


CLASSES = (
    ESPRESSO_UL_driver_targets,
    ESPRESSO_MT_master_presets_drpdwn,
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
