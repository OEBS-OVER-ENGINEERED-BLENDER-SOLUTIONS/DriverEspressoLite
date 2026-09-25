bl_info = {
    "name": "Driver Espresso Lite",
    "author": "OEBS Studios",
    "version": (1, 6, 5),
    "blender": (4, 2, 0),
    "location": "View3D / Graph Editor > Sidebar > Espresso",
    "description": "Template-driven builder for native Blender motion systems.",
    "category": "Animation",
}

import bpy
from textwrap import wrap

from .engine import utils as _utils
from .product import identity
from .apply import target_memory


CHANNEL_DISPLAY_DEFAULT = "SWAP_BUTTONS_DROPDOWN"
CHANNEL_DISPLAY_ITEMS = [
    # How the channel picker renders, by member count. Ordered compact→expanded.
    ("DROPDOWN", "Always Dropdown", "Every channel set uses a dropdown, whatever its size"),
    ("SWAP_DROPDOWN", "Swap, then Dropdown", "Two channels: one swap button. Three or more: a dropdown"),
    ("SWAP_BUTTONS_DROPDOWN", "Swap · Buttons · Dropdown", "Two: swap. Three: a button row. Four or more: three pinned buttons plus a dropdown for the rest"),
    ("BUTTONS_DROPDOWN", "Buttons, then Dropdown", "Up to three channels as buttons; four or more as a dropdown"),
    ("BUTTONS", "Always Buttons", "Every channel shown as its own button, whatever the count"),
]


QUICK_PRESET_MODE_DEFAULT = "FEATURED"
QUICK_PRESET_MODE_ITEMS = [
    ("FEATURED", "Featured", "Show the first three Master Presets chosen by the template author"),
    ("MOST_USED", "Most Used", "Show the three Master Presets you use most often for this template"),
    ("ADAPTIVE", "Adaptive", "Start with Featured, then swap to Most Used as you click"),
    ("OFF", "Dropdown Only", "Hide the quick-access buttons and use the Master Presets dropdown instead"),
]

CONFLICT_POLICY_ITEMS = [
    ("ALWAYS_ASK", "Always Ask", "Stop before replacing an existing effect and let the user choose a route"),
    ("AUTO_REPLACE", "Auto-replace Espresso Effects", "Update the same effect and replace a different managed Espresso effect"),
    ("AUTO_LAYER", "Auto-layer When Supported", "Layer a different effect only when the target explicitly supports layering"),
    ("NEVER_PROMPT", "Never Prompt", "Block conflicts rather than opening a confirmation prompt"),
]


def _draw_wrapped_label(layout, text, icon="BLANK1", width=72):
    wrapped_lines = wrap(text, width=width) or [text]
    for index, line in enumerate(wrapped_lines):
        layout.label(text=line, icon=icon if index == 0 else "BLANK1")


class ESPRESSO_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    enable_advanced_controls: bpy.props.BoolProperty(
        name="Enable Advanced Controls",
        description="Show the ADVANCED section in the Driver Espresso panel for supported templates",
        default=False,
    )
    advanced_group_timing: bpy.props.BoolProperty(
        name="Timing Controls",
        description="Show the TIMING sub-group inside the Advanced Controls section",
        default=True,
    )
    advanced_group_output: bpy.props.BoolProperty(
        name="Output Controls",
        description="Show the OUTPUT sub-group inside the Advanced Controls section",
        default=True,
    )
    remember_template_params: bpy.props.BoolProperty(
        name="Remember Template-Only Parameter State",
        description="When you return to a template, restore values for parameters that are unique to that template",
        default=True,
    )
    carry_manual_params: bpy.props.BoolProperty(
        name="Carry Manual Values Across Matching Parameters",
        description="When switching templates, shared parameters such as Minimum, Maximum, Period, or Speed follow the most recently edited manual value",
        default=True,
    )
    carry_manual_params_channels_only: bpy.props.BoolProperty(
        name="Only Between Channels",
        description=(
            "Limit carried manual values to templates grouped as channels of "
            "the same setup. Turn this off to carry matching values between "
            "unrelated templates too"
        ),
        default=True,
    )
    carry_manual_params_paired_only: bpy.props.BoolProperty(
        name="Only Paired Channels",
        description=(
            "Limit carried manual values to explicitly paired channels — the "
            "Green/Amber/Red aspects of a signal, an ambulance bar's four "
            "colours, or the X and Y halves of an orbit. Alternative profiles "
            "such as Bowling, Golf, Tennis, Rubber, and Ping-Pong keep their "
            "own material defaults"
        ),
        default=True,
    )
    conflict_policy: bpy.props.EnumProperty(
        name="Conflict Handling",
        description="How Driver Espresso handles an existing effect on the channels being applied",
        items=CONFLICT_POLICY_ITEMS,
        # Preserve the established one-click reapply behavior for managed
        # Espresso effects. Users who want an explicit confirmation can pick
        # Always Ask; unmanaged Blender data remains protected in every mode.
        default="AUTO_REPLACE",
    )
    show_raw_parameters: bpy.props.BoolProperty(
        name="Show Raw Parameters",
        description=(
            "Append each parameter's underlying expression token to its label, and show "
            "which maths token a friendly parameter feeds (Seconds per revolution → TURNS). "
            "Nothing is hidden without it — only defaulted well"
        ),
        default=False,
    )
    enable_driver_target_picker: bpy.props.BoolProperty(
        name="Enable Driver Target Picker",
        description=(
            "Show the per-object driver list and the Active Target toggle in "
            "the Preview graph. Disable to bypass this feature and restore "
            "single-selection mode from the Drivers Editor"
        ),
        default=True,
    )
    remember_apply_targets: bpy.props.BoolProperty(
        name="Remember Apply Targets",
        description="Store the most recent successful apply target so Update Last Target can reapply later",
        default=True,
    )
    apply_target_memory_mode: bpy.props.EnumProperty(
        name="Apply Target Memory Mode",
        description="Choose whether Driver Espresso remembers one newest target, separate slots by apply mode, or a recent-history list",
        items=_utils.mark_recommended(target_memory.MEMORY_MODE_ITEMS, "SINGLE"),
        default="SINGLE",
    )
    apply_target_history_size: bpy.props.IntProperty(
        name="Apply Target History Size",
        description="How many successful apply targets to keep when Recent History mode is enabled",
        default=5,
        min=2,
        max=10,
    )
    channel_display: bpy.props.EnumProperty(
        name="Channel Display",
        description="How the channel picker renders sibling templates (a signal head's Green/Amber/Red, a police bar's two heads), by member count",
        items=_utils.mark_recommended(CHANNEL_DISPLAY_ITEMS, CHANNEL_DISPLAY_DEFAULT),
        default=CHANNEL_DISPLAY_DEFAULT,
    )
    quick_preset_mode: bpy.props.EnumProperty(
        name="Mode",
        description="How the Master Preset shelf chooses which buttons to show",
        items=_utils.mark_recommended(QUICK_PRESET_MODE_ITEMS, QUICK_PRESET_MODE_DEFAULT),
        default=QUICK_PRESET_MODE_DEFAULT,
    )
    hide_pinned_master_presets: bpy.props.BoolProperty(
        name="Hide Pinned Presets from the Dropdown",
        description=(
            "Leave the presets already on the shelf out of the dropdown, so it "
            "lists only the ones you cannot already reach. Off by default: the "
            "dropdown then shows the whole set with the shelf entries pinned, "
            "which keeps their order and makes it obvious what is already there"
        ),
        default=False,
    )
    # --- Panel visibility -----------------------------------------------
    # Which sections are drawn. Deliberately NOT offered for Parameters or the
    # expression block: hiding those would leave a panel that cannot do its job,
    # and a user who hid them would reasonably think the add-on was broken.
    show_input_controller_panel: bpy.props.BoolProperty(
        name="Controller Workspace",
        description="Legacy saved preference; the Controller workspace is now part of the main Espresso panel",
        default=True,
    )
    show_preview_panel: bpy.props.BoolProperty(
        name="Preview Panel",
        description="Show the standalone Preview panel with the waveform graph",
        default=True,
    )
    show_diagnostics: bpy.props.BoolProperty(
        name="Diagnostics",
        description="Show internal controller names and binding details in the Input Controller panel",
        default=False,
    )
    show_favorites_section: bpy.props.BoolProperty(
        name="Favorites & Recent",
        description="Show the starred and recently used templates above the template list",
        default=True,
    )
    show_variants_section: bpy.props.BoolProperty(
        name="Variants",
        description="Show related templates and the suggested paired template for the current selection",
        default=True,
    )
    show_subcategory_row: bpy.props.BoolProperty(
        name="Subcategory Filter",
        description="Show the subcategory row that narrows a crowded category before you pick a template",
        default=True,
    )
    show_advanced_section: bpy.props.BoolProperty(
        name="Advanced Controls",
        description="Show the ADVANCED box for templates that expose timing and output controls",
        default=True,
    )
    show_driver_target_section: bpy.props.BoolProperty(
        name="Organize Workspace",
        description="Legacy saved preference; the Organize workspace is now part of the main Espresso panel",
        default=True,
    )

    prefs_tab: bpy.props.EnumProperty(
        name="Section",
        description="Which group of preferences to show",
        items=[
            ("BEHAVIOR", "Behavior", "Template switching, parameters, advanced controls, master presets", "PREFERENCES", 0),
            ("PANELS", "Panels & Channels", "Which panels and sections are shown, and how channels render", "MENU_PANEL", 1),
            ("TARGETS", "Targets", "Driver-target picker and apply-target memory", "DRIVER", 2),
            ("HELP", "Help", "How the add-on works", "INFO", 4),
        ],
        default="BEHAVIOR",
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.scale_y = 1.3
        row.prop(self, "prefs_tab", expand=True)
        layout.separator(factor=0.5)

        tab = self.prefs_tab
        if tab == "BEHAVIOR":
            self._draw_behavior(layout)
        elif tab == "PANELS":
            self._draw_panels(layout)
        elif tab == "TARGETS":
            self._draw_targets(layout)
        else:
            self._draw_help(layout)


    def _draw_behavior(self, layout):
        behavior_box = layout.box()
        behavior_box.label(text="Template Switching", icon="FILE_REFRESH")
        behavior_box.prop(self, "remember_template_params")
        behavior_box.prop(self, "carry_manual_params")
        channels_row = behavior_box.row()
        channels_row.enabled = self.carry_manual_params
        channels_row.prop(self, "carry_manual_params_channels_only")
        paired_row = behavior_box.row()
        paired_row.enabled = (
            self.carry_manual_params
            and self.carry_manual_params_channels_only
        )
        paired_row.prop(self, "carry_manual_params_paired_only")

        conflict_box = layout.box()
        conflict_box.label(text="Apply Conflicts", icon="ERROR")
        conflict_box.prop(self, "conflict_policy")

        params_box = layout.box()
        params_box.label(text="Parameters", icon="PREFERENCES")
        params_box.prop(self, "show_raw_parameters")

        advanced_box = layout.box()
        advanced_box.label(text="Advanced Controls", icon="MODIFIER")
        advanced_box.prop(self, "enable_advanced_controls")
        if self.enable_advanced_controls:
            sub = advanced_box.column(align=True)
            sub.prop(self, "advanced_group_timing")
            sub.prop(self, "advanced_group_output")

        preset_box = layout.box()
        preset_box.label(text="Master Presets Display", icon="PRESET_NEW")
        preset_box.prop(self, "quick_preset_mode")
        hide_row = preset_box.row()
        # Nothing is pinned when the shelf is off, so the toggle would do nothing.
        hide_row.enabled = self.quick_preset_mode != "OFF"
        hide_row.prop(self, "hide_pinned_master_presets")

    def _draw_panels(self, layout):
        panels_box = layout.box()
        panels_box.label(text="Panels", icon="MENU_PANEL")
        panels_box.prop(self, "show_preview_panel")
        panels_box.prop(self, "show_diagnostics")

        main_col = panels_box.column(align=True)
        main_col.label(text="Main panel sections", icon="DOT")
        sub = main_col.column(align=True)
        # Only offer the toggle where the section exists at all --
        # a preference that turns on nothing is worse than no preference.
        if getattr(identity, "HAS_FAVORITES", True):
            sub.prop(self, "show_favorites_section")
        sub.prop(self, "show_subcategory_row")
        sub.prop(self, "show_variants_section")
        sub.prop(self, "show_advanced_section")

        channels_box = layout.box()
        channels_box.label(text="Channels", icon="LINKED")
        channels_box.prop(self, "channel_display")

    def _draw_targets(self, layout):
        picker_box = layout.box()
        picker_box.label(text="Driver Target Picker", icon="ANIM")
        picker_box.prop(self, "enable_driver_target_picker")

        target_box = layout.box()
        target_box.label(text="Apply Target Memory", icon="DRIVER")
        target_box.prop(self, "remember_apply_targets")
        target_box.prop(self, "apply_target_memory_mode")
        if self.apply_target_memory_mode == "HISTORY":
            target_box.prop(self, "apply_target_history_size")

    def _draw_help(self, layout):
        help_box = layout.box()
        lines = [
            ("WHAT A TEMPLATE IS", "BLANK1"),
            ("Think of a template as a recipe. Every one is cooked from the same ingredients - the maths Blender's drivers already understand - and the value is in which ingredients, in what proportion, and which dials are handed to you.", "BLANK1"),
            ("That is why the parameters are named for the thing you are making rather than for the formula. A clock has hands and a start time; a conveyor has belt speed and a roller radius. You set what you know, not what the equation wants.", "BLANK1"),
            ("Templates come in two kinds. GENERIC ones are building blocks - a ramp, an oscillation, a remap - that work on any property and leave the meaning to you. TAILORED ones each reproduce one specific real-world behaviour, with parameters named for that behaviour rather than for the maths.", "BLANK1"),
            ("CHANNELS", "BLANK1"),
            ("Some things are not one behaviour but several running at once. An ambulance bar is a red head, a white head, a rear amber panel and a green command beacon - four parts, lit together, each doing its own thing. A traffic signal is its Green, Amber and Red aspects. A conveyor is a belt and a roller.", "BLANK1"),
            ("Those parts are CHANNELS. The template list shows the set once and a Channel picker in the panel switches between the parts, so a set counts as one template in the library rather than as several. Drive each part from its own channel, on its own object.", "BLANK1"),
            ("Switching channel carries the settings the parts genuinely share - an output range, a signal's cycle timing - and leaves the rest at that part's own defaults, because a channel keeps whatever parameters suit it. The rear amber panel has arrow controls the red head has no use for. Whether values carry at all is set under Template Switching.", "BLANK1"),
            ("A channel is not a preset. Presets are alternatives, one at a time: the same red head responding or parked. Channels run together, which is the whole point - no preset can express red and white flashing at an offset from each other.", "BLANK1"),
            ("How the picker renders - a swap button, a row of buttons, or a dropdown - is set under Panels & Channels.", "BLANK1"),
            ("ACCESS", "BLANK1"),
            ("Open in View3D or Graph Editor > Sidebar > Espresso. Right-click any drivable property for the context-menu workflow.", "BLANK1"),
            ("MAIN PANEL", "BLANK1"),
            ("Pick a category and template, tune parameters, then Copy Expression or Copy Driver. Expression Preview shows the live output.", "BLANK1"),
            ("MULTI-CHANNEL MOTION", "BLANK1"),
            ("Multi-expression templates list each destination and formula separately. Preview or copy any channel, then use Apply Motion to Selected Object to route all channels to the active Object.", "BLANK1"),
            ("RIGHT-CLICK APPLY", "BLANK1"),
            ("Apply Current Template pushes a single template onto the clicked property. For a multi-expression template it routes the full channel plan to the Object that owns the clicked transform.", "BLANK1"),
            ("COPY / PASTE DRIVER", "BLANK1"),
            ("Copy Driver stores the current setup. Right-click any other property and choose Paste Copied Driver to reuse it.", "BLANK1"),
            ("PREPARE & ORGANIZE EFFECTS", "BLANK1"),
            ("Prepare Layout builds Grid, Row, Ring, Stack, or Curve carriers. The list below shows Active, Last, or Scene motions and layouts. Edit layout settings under the list, then pin, bake, or remove applied work.", "BLANK1"),
            ("FLOATING POPUP", "BLANK1"),
            ("Right-click > Open Espresso Panel (Floating) for a quick staging popup. Toggle Apply Driver on OK to apply directly or just copy.", "BLANK1"),
            ("TARGET INSPECTOR", "BLANK1"),
            ("Shows the technical path, type, and suggested templates for the property you right-clicked before you commit.", "BLANK1"),
            ("WAVEFORM PREVIEW", "BLANK1"),
            ("Toggle the waveform icon in the Preview header to sample the expression curve before applying.", "BLANK1"),
            ("Lightweight Preview is the Text/Pixel button at the end of the style row while the graph is open. Text mode uses far less CPU during playback, and the setting is stored per scene rather than in preferences.", "BLANK1"),
            ("TROUBLESHOOTING", "BLANK1"),
            ("Disabled right-click options: property may be read-only or the template needs extra variables. "
             "Disabled Graph apply: select a valid driver F-Curve first. "
             "Wrong result: check the Target Inspector to confirm path and channel.", "BLANK1"),
        ]
        col = help_box.column(align=False)
        for text, icon in lines:
            # Section headings rendered as a labelled divider.
            if text == text.upper() and len(text) < 30:
                col.separator(factor=0.6)
                col.label(text=text, icon="DOT")
            else:
                _draw_wrapped_label(col, text, icon=icon, width=90)


CLASSES = (ESPRESSO_AddonPreferences,)


def register():
    from .ui import (bake_applied, context_menu, driver_manager, image_preview, live_controls,
                     operators, panels, props, source_display)

    for cls in CLASSES:
        bpy.utils.register_class(cls)
    props.register()
    source_display.register()
    operators.register()
    # After operators: bake_applied subclasses BakeOptionsMixin from it.
    bake_applied.register()
    # After bake_applied, which installs the purge hook it reconciles through.
    from .apply.motion import applied_reconcile

    applied_reconcile.register()
    driver_manager.register()
    live_controls.register()
    panels.register()
    context_menu.register()
    image_preview.register()
    # The pointer watcher behind the previews. It never starts or stops the
    # scene's animation: it holds preview time still while the pointer is
    # away from the sidebar, and its event timer is what repaints the panel
    # so the picture moves at all.
    from .ui.views import preview_activity
    preview_activity.register()


def unregister():
    from .ui import (bake_applied, context_menu, driver_manager, image_preview, live_controls,
                     operators, panels, props, source_display, visualizer)

    from .ui.views import preview_activity
    preview_activity.unregister()
    image_preview.unregister()
    context_menu.unregister()
    panels.unregister()
    live_controls.unregister()
    driver_manager.unregister()
    from .apply.motion import applied_reconcile

    applied_reconcile.unregister()
    bake_applied.unregister()
    operators.unregister()
    source_display.unregister()
    props.unregister()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    visualizer.clear_cache()
    # Compile/regex caches hold only immutable code objects and patterns,
    # but clear them so repeated reloads during development cannot pile up.
    from .engine import utils as _utils
    _utils._cached_compile.cache_clear()
    _utils._token_pattern.cache_clear()
