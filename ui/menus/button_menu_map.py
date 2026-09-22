# -*- coding: utf-8 -*-
"""What the right-click menu offers, as one table.

One table decides whether an apply entry suits the loaded template and the
clicked property, rather than each entry working it out from its own logic.
They drifted apart, which is how a greyed "Apply Current Template" came to sit
directly above an enabled "Apply to 17 Selected Objects" that routed through
the same code and could only have failed.

This is the single place that decides. An entry declares the CAPABILITY it
needs from the template and carries the test for the property shape it needs.
The menu draws the entries whose requirements the current pair satisfies and
leaves out the rest: an operator that cannot help with the template on screen
is not a choice, it is a thing to read past on the way to the one that works.

Adding a TEMPLATE needs nothing here. Capabilities are derived from what a
template already declares - whether it has a channel plan, a paired sibling, a
spatial term - so a new recipe is classified the moment it exists rather than
when someone remembers to register it. Every template in the catalogue must
land on at least one capability, so a recipe whose shape no entry serves is
caught here rather than becoming a menu that quietly offers nothing.

Adding an OPERATOR is one MenuEntry in ENTRIES.
"""

from __future__ import annotations

from ...catalogue import templates as template_catalogue


# --------------------------------------------------------------- capabilities

# What a template can be driven AS. A template usually has more than one: a
# spatial field is a single expression that also happens to vary over space, so
# it is offered both the plain apply and the shared-material sweep.
SINGLE = "SINGLE"        # one expression drives one value
MOTION = "MOTION"        # a channel set, landing on an Object or on an array
PAIRED = "PAIRED"        # has a sibling template for the other axis
SPATIAL = "SPATIAL"      # carries a spatial term, so it can vary per object
PALETTE = "PALETTE"      # drives a position along a ramp rather than a value

ALL_CAPABILITIES = (SINGLE, MOTION, PAIRED, SPATIAL, PALETTE)


def capabilities_for(template):
    """Derive what this template can be driven as.

    Read from the template's own declarations rather than a hand-kept list -
    270 recipes is well past where a second list stays honest, and the failure
    mode of a stale one is an operator that silently stops being offered.
    """
    if not template:
        return frozenset()

    found = set()

    if template_catalogue.has_motion_plan(template):
        found.add(MOTION)
    else:
        found.add(SINGLE)

    pair_id = template.get("pair_with")
    if pair_id and pair_id in template_catalogue.TEMPLATE_BY_ID:
        found.add(PAIRED)

    # Imported lazily: shared_material_sweep reaches back into the catalogue,
    # and this module is imported while the UI package is still assembling.
    from ...apply import shared_material_sweep

    if shared_material_sweep.kind_for(template) is not None:
        found.add(SPATIAL)

    from . import context_menu

    if template.get("id") in context_menu.PALETTE_FACTOR_TEMPLATES:
        found.add(PALETTE)

    return frozenset(found)


# ------------------------------------------------------- what it drives INTO

# The second map: capabilities say HOW a template drives, these say WHAT it is
# meant to drive. An RGB template on a Roughness slider is not a niche choice,
# it is a mistake the menu should not have offered.
COLOUR = "COLOUR"        # three channels that are a colour
TRANSFORM = "TRANSFORM"  # location / rotation / scale
VALUE = "VALUE"          # one number, at home anywhere

_TRANSFORM_PATHS = frozenset({
    "location", "rotation_euler", "rotation_quaternion", "scale",
    "delta_location", "delta_rotation_euler", "delta_scale",
})


def drives_for(template):
    """What this template is FOR, read from the channels it declares.

    A channel set on ``color`` is a colour template however it is named, and a
    channel set on location or rotation is a transform one. Everything else is
    a single number, which is at home on anything.
    """
    if not template:
        return VALUE

    if template_catalogue.has_motion_plan(template):
        paths = {c.get("data_path") for c in template_catalogue.template_channels(template)}
        if paths == {"color"}:
            return COLOUR
        if paths & _TRANSFORM_PATHS:
            return TRANSFORM
        return VALUE

    # A palette template drives a scalar, but that scalar is a position along a
    # ramp built ON a colour socket - so a colour input is what it wants, even
    # though its expression is one number.
    from . import context_menu

    if template.get("id") in context_menu.PALETTE_FACTOR_TEMPLATES:
        return COLOUR
    return VALUE


# Which input shapes each kind of template will accept. Asymmetric on purpose:
#
# A colour template is strict, because its three channels ARE a red, a green
# and a blue - poured into a vector they drive X, Y and Z with a colour, which
# is never what anyone meant.
#
# A transform template is permissive, because pouring a channel plan into some
# other array is a deliberate feature - an object's rotation curve driving a
# node's colour is a real thing to want, and _channel_plan_on_clicked_array
# exists to allow exactly that.
#
# A single value fits anything by definition.
ACCEPTS = {
    COLOUR: frozenset({COLOUR}),
    TRANSFORM: frozenset({TRANSFORM, COLOUR, VALUE}),
    VALUE: frozenset({VALUE, COLOUR, TRANSFORM}),
}


def shape_of(context):
    """Classify the property under the cursor, or None when it cannot be read.

    None means "do not gate on this" rather than "reject". A menu that hides
    the right entry because it could not identify the property is worse than
    one that shows a wrong entry - the artist can read the label, but cannot
    click what is not drawn.
    """
    prop = getattr(context, "button_prop", None)
    if prop is None:
        return None

    if getattr(prop, "subtype", "") in {"COLOR", "COLOR_GAMMA"}:
        return COLOUR

    pointer = getattr(context, "button_pointer", None)
    # A node socket's colour is an RGBA float array whose subtype is NONE, so
    # the socket's own type is the only thing that identifies it.
    if pointer is not None and getattr(pointer, "type", None) == "RGBA":
        return COLOUR
    if getattr(prop, "identifier", "") == "default_value" and pointer is not None:
        if getattr(pointer, "type", None) in {"RGBA"}:
            return COLOUR

    if getattr(prop, "identifier", "") in _TRANSFORM_PATHS:
        return TRANSFORM

    return VALUE


def input_suits(template, context):
    """Is the clicked property the kind of thing this template drives?"""
    shape = shape_of(context)
    if shape is None:
        return True
    return shape in ACCEPTS[drives_for(template)]


# --------------------------------------------------------------------- entries


# The two halves of the Apply submenu, separated by a rule. Everything in
# HERE acts on the property under the cursor; everything in SELECTION widens
# that to the objects selected. Reading the list top to bottom should go from
# "this one" to "all of them" without the reader having to work out which is
# which from the labels.
HERE = "HERE"
SELECTION = "SELECTION"
BINDING = "BINDING"

# The submenu shows the apply family. BINDING is drawn by the parent menu
# instead - naming an input or a target is not applying anything, and burying
# it under "Apply Template" would say it was.
APPLY_GROUPS = (HERE, SELECTION)
GROUP_ORDER = (HERE, SELECTION, BINDING)


class MenuEntry:
    """One row of the Driver Espresso block.

    ``needs`` is satisfied when the template has ANY of the named capabilities,
    not all of them - an entry that serves both a single expression and a
    channel set names both. ``fits`` then answers the half that depends on what
    was actually clicked, and is the existing predicate for that entry rather
    than a reimplementation of it.
    """

    __slots__ = ("key", "idname", "icon", "needs", "fits", "label",
                 "properties", "companion", "group", "checks_input")

    def __init__(self, key, idname, icon, needs, fits, label,
                 properties=None, companion=None, group=HERE, checks_input=True):
        self.key = key
        self.idname = idname
        self.icon = icon
        self.needs = frozenset(needs)
        self.fits = fits
        self.label = label
        self.properties = properties or {}
        self.companion = companion
        self.group = group
        # Whether the drives/accepts gate applies. It asks "is this the kind of
        # property the template WRITES to", so an entry that does not write -
        # naming a value to read FROM, repointing an existing link - must not be
        # judged by it. An RGB template reading a float as its input is a
        # perfectly ordinary thing to want.
        self.checks_input = checks_input

    def applies(self, context, scene_props, template):
        if not template:
            return False
        if self.needs and not (self.needs & capabilities_for(template)):
            return False
        if self.checks_input and not input_suits(template, context):
            return False
        return bool(self.fits(context, scene_props, template))

    def __repr__(self):                      # for test failure messages
        return "<MenuEntry %s>" % self.key


def _selected_count(context):
    return len(getattr(context, "selected_objects", None) or ())


def build_entries():
    """The table. Built on first use so the predicates can live in
    context_menu, which imports this module for the capability constants."""
    from . import context_menu

    def fits_clicked(context, scene_props, template):
        return context_menu.template_fits_button_target(context, scene_props, template)

    def fits_selection(context, scene_props, template):
        # The multi-object route runs the ordinary apply once per object, so it
        # can do exactly what that can do here and nothing more.
        return fits_clicked(context, scene_props, template) and _selected_count(context) > 1

    def fits_any_target(context, scene_props, template):
        return bool(context_menu.resolve_button_driver_targets(context))

    def fits_relink(context, scene_props, template):
        from ...apply import source_binding

        return bool(scene_props and source_binding.latest_source(scene_props))

    def fits_split(context, scene_props, template):
        """Splitting only means something when the property lives on a
        datablock objects SHARE.

        A rotation belongs to one object and cannot be shared, so offering to
        give each object its own copy of it is an operation with no subject -
        it was appearing on transforms, where the answer is always "they are
        already separate". material_slot_index is the same test execute uses
        to decide whether it can actually split, asked before offering rather
        than after clicking.
        """
        if not fits_selection(context, scene_props, template):
            return False
        targets = context_menu.resolve_button_driver_targets(context)
        active = getattr(context, "active_object", None)
        if not targets or active is None:
            return False
        return context_menu.material_slot_index(targets[0].owner, active) is not None

    def fits_multi(context, scene_props, template):
        return context_menu.ESPRESSO_OT_apply_multi_to_button.poll(context)

    def fits_pair(context, scene_props, template):
        return context_menu._pairable_vector_target(context) is not None

    def fits_channels(context, scene_props, template):
        return context_menu._channel_plan_on_clicked_array(context) is not None

    def channel_label(context, template):
        plan = context_menu._channel_plan_on_clicked_array(context)
        names = " / ".join(c.get("label", "") for c in plan[2]) if plan else ""
        return "Apply Template Channels (%s)" % names

    return (
        # --- acting on the property under the cursor ---------------------
        MenuEntry(
            "apply_current", "espresso.apply_to_button", "DRIVER",
            (SINGLE, MOTION, PALETTE), fits_clicked,
            lambda context, template: "Apply Current Template",
            # Bake sits BESIDE plain apply on a split row: the driver action and
            # its freeze-this shortcut belong together, and the little REC dot
            # reads as what it does without needing the width of a full row.
            companion=("espresso.apply_and_bake_to_button", "Bake", "REC"),
        ),
        MenuEntry(
            # One visible command, two safe routes: a plain recipe repeats its
            # expression across the clicked array, while a motion recipe keeps
            # each authored channel distinct and lands it on the matching
            # component. The dispatcher owns that distinction.
            "apply_multi", "espresso.apply_multi_to_button", "DRIVER",
            (SINGLE, MOTION), fits_multi,
            lambda context, template: "Apply Multi",
        ),
        MenuEntry(
            "apply_pair", "espresso.apply_pair_to_button", "ORIENTATION_GLOBAL",
            (PAIRED,), fits_pair,
            lambda context, template: "Apply Paired Templates (X/Y)",
        ),

        # --- widening to the selection -----------------------------------
        MenuEntry(
            "apply_selected", "espresso.apply_to_selected_objects",
            "OUTLINER_OB_GROUP_INSTANCE", (SINGLE, MOTION, PALETTE), fits_selection,
            lambda context, template: "Apply to %d Selected Objects" % _selected_count(context),
            group=SELECTION,
        ),
        MenuEntry(
            # Offered separately rather than done automatically: copying a
            # material per object is a real change to the file, and which of the
            # two an artist wants is not something to guess at.
            "apply_selected_split", "espresso.apply_to_selected_objects", "MATERIAL",
            (SINGLE, MOTION, PALETTE), fits_split,
            lambda context, template: "Apply to %d Selected (Split Shared Materials)" % _selected_count(context),
            properties={"split_shared_materials": True},
            group=SELECTION,
        ),


        # --- binding, drawn by the parent menu ---------------------------
        MenuEntry(
            "use_input", "espresso.use_button_as_input", "LINKED",
            (), fits_any_target,
            lambda context, template: "Use as Espresso Input",
            group=BINDING, checks_input=False,
        ),
        MenuEntry(
            # SINGLE only, and that is not a style choice - a channel set has
            # nothing to write to one nominated property, so template_suits_
            # lights refuses it outright and the "Apply to N Objects" button
            # never appears. Naming a target while one is loaded set a value
            # that nothing would ever read.
            "use_target", "espresso.use_as_apply_target", "EXPORT",
            (SINGLE,), fits_any_target,
            lambda context, template: "Use as Espresso Target",
            group=BINDING,
        ),
        MenuEntry(
            "relink", "espresso.relink_button_input", "FILE_REFRESH",
            (), fits_relink,
            lambda context, template: "Relink Espresso Input & Applied Drivers",
            group=BINDING, checks_input=False,
        ),
    )


_ENTRIES = None


def entries():
    global _ENTRIES
    if _ENTRIES is None:
        _ENTRIES = build_entries()
    return _ENTRIES


def visible_entries(context, scene_props, template):
    """The entries worth drawing for this template on this property."""
    return tuple(e for e in entries() if e.applies(context, scene_props, template))


def entries_in_group(context, scene_props, template, group):
    """The visible entries of one group, in table order."""
    return tuple(e for e in visible_entries(context, scene_props, template)
                 if e.group == group)


def grouped_entries(context, scene_props, template):
    """visible_entries, split into its groups and in GROUP_ORDER.

    Empty groups are dropped rather than returned empty, so the drawer can put
    a separator between every pair it receives without checking for blanks.
    """
    visible = visible_entries(context, scene_props, template)
    groups = []
    for name in APPLY_GROUPS:
        members = tuple(e for e in visible if e.group == name)
        if members:
            groups.append(members)
    return tuple(groups)


def apply_action_count(groups):
    """Count primary apply rows for the direct-menu threshold.

    A companion such as Bake shares its owner's row.  It is not a separate
    selector choice, so it must not hide a second primary command in a submenu.
    """
    return sum(1 for group in groups for entry in group)


def should_inline_apply_entries(groups):
    """Skip a submenu that would organize no more than two actions."""
    count = apply_action_count(groups)
    return 0 < count <= 2
