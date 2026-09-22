"""Wiring and lookup for the grouped Driver Espresso palette templates."""

from __future__ import annotations

import hashlib
import uuid

import bpy

from ...generated import nodes as generated_nodes

RAMP_NODE = "ShaderNodeValToRGB"
FACTOR_INDEX = 0
PALETTE_GROUP_NODE_TAG = "driver_espresso_palette"
PALETTE_GROUP_TREE_TAG = "driver_espresso_palette_tree"
PALETTE_RAMP_TAG = "driver_espresso_palette_ramp"
TEMPLATE_RAMP_TREE_TAG = "driver_espresso_template_ramp_tree"
TEMPLATE_RAMP_NODE_TAG = "driver_espresso_template_ramp_node"
TEMPLATE_RAMP_SCENE_PROP = "driver_espresso_template_ramp_tree_name"
TEMPLATE_RAMP_PREVIEW_STOPS_TAG = "driver_espresso_template_preview_stops"
FACTOR_INPUT = "Factor"
STOP_SAMPLER_NODE = "Espresso Stop Position Sampler"
COLOR_OUTPUT = "Color"
TRANSITION_STEPPED = "STEPPED"
TRANSITION_LINEAR = "LINEAR"
TRANSITION_EASE = "EASE"
TRANSITION_ITEMS = (
    (
        TRANSITION_STEPPED,
        "Stepped",
        "Hold each factor value, then jump directly to the next",
    ),
    (
        TRANSITION_LINEAR,
        "Linear",
        "Move between factor values at a constant rate",
    ),
    (
        TRANSITION_EASE,
        "Ease",
        "Move smoothly between factor values with a gentle start and finish",
    ),
)


def _sequential_transition_expression(mode):
    if mode == TRANSITION_LINEAR:
        return "((frame*RRATE+.5)%STOPS)/STOPS"
    if mode == TRANSITION_EASE:
        phase = "frame*RRATE-floor(frame*RRATE)"
        return (
            "((floor(frame*RRATE)+.5+.5-.5*cos(pi*("
            f"{phase})))%STOPS)/STOPS"
        )
    return "((floor(frame*RRATE)%STOPS+.5)/STOPS)"


def _shuffle_transition_expression(mode):
    """Return a compact, deterministic shuffled-factor expression.

    Each segment owns one pseudo-random target. Linear and Ease interpolate to
    the next segment's target instead of blending colours inside the driver;
    the artist's ColorRamp remains solely responsible for colour interpolation.
    """
    current = ".5+.5*sin(floor(frame*RRATE)*13+SEED)"
    if mode == TRANSITION_STEPPED:
        return current
    following = ".5+.5*sin((floor(frame*RRATE)+1)*13+SEED)"
    phase = "frame*RRATE-floor(frame*RRATE)"
    weight = (
        f".5-.5*cos(pi*({phase}))"
        if mode == TRANSITION_EASE
        else phase
    )
    return f"{current}+({following}-({current}))*({weight})"


def resolve_transition_template(template, mode, shuffle=False):
    """Compile the RGB ramp template for its selected transition behaviour."""
    mode = mode if mode in {
        TRANSITION_STEPPED, TRANSITION_LINEAR, TRANSITION_EASE,
    } else TRANSITION_STEPPED
    resolved = dict(template)
    resolved["expression"] = (
        _shuffle_transition_expression(mode)
        if shuffle
        else _sequential_transition_expression(mode)
    )
    return resolved


def is_ramp_node(node):
    return node is not None and getattr(node, "bl_idname", "") == RAMP_NODE


def _is_palette_group(node):
    return node is not None and node.get(PALETTE_GROUP_NODE_TAG) is True


def factor_socket(node):
    """Return the driven outer Factor socket, with legacy ramp support."""
    if _is_palette_group(node):
        return node.inputs[FACTOR_INPUT]
    return node.inputs[FACTOR_INDEX]


def _discard_group_if_unused(group):
    if group is not None and group.get(PALETTE_GROUP_TREE_TAG) and group.users == 0:
        generated_nodes.remove_group(group, require_unused=True)


def remove_palette_group(node_tree, palette):
    """Remove one Espresso palette node and its now-unused internal tree."""
    if not _is_palette_group(palette):
        return False
    group = palette.node_tree
    node_tree.nodes.remove(palette)
    _discard_group_if_unused(group)
    return True


def _remove_previous_palette_for_socket(node_tree, target_socket):
    for link in list(node_tree.links):
        if link.to_socket != target_socket:
            continue
        previous = link.from_node
        node_tree.links.remove(link)
        if _is_palette_group(previous):
            remove_palette_group(node_tree, previous)


def _new_palette_group(stops, interpolation="CONSTANT", source_ramp=None, *, indexed_stops=False):
    group = generated_nodes.create_group(
        "Driver Espresso Palette",
        "ShaderNodeTree",
        resource_id="palette.%s" % uuid.uuid4().hex[:12],
        effect_id="rgb_palette",
        role="colour_palette",
    )
    group[PALETTE_GROUP_TREE_TAG] = True
    factor = group.interface.new_socket(name=FACTOR_INPUT, in_out="INPUT", socket_type="NodeSocketFloat")
    factor.default_value = 0.0
    group.interface.new_socket(name=COLOR_OUTPUT, in_out="OUTPUT", socket_type="NodeSocketColor")
    group_input = group.nodes.new("NodeGroupInput")
    group_input.location = (-260, 0)
    group_output = group.nodes.new("NodeGroupOutput")
    group_output.location = (260, 0)
    ramp = group.nodes.new(RAMP_NODE)
    ramp.name = "Driver Espresso Palette Colours"
    ramp.label = "Palette colours"
    ramp[PALETTE_RAMP_TAG] = True
    ramp.location = (0, 0)
    group.links.new(group_input.outputs[FACTOR_INPUT], ramp.inputs[FACTOR_INDEX])
    group.links.new(ramp.outputs["Color"], group_output.inputs[COLOR_OUTPUT])
    # CONSTANT for anything that steps between colours, LINEAR for a field
    # that sweeps through them. A plasma or a travelling gradient through a
    # hard-stepped ramp reads as banding rather than as colour moving.
    if source_ramp is not None:
        copy_ramp_settings(source_ramp, ramp.color_ramp)
    else:
        ramp.color_ramp.interpolation = interpolation
        _seed_stops(ramp, stops)
    if indexed_stops:
        _build_stop_sampler(group, group_input, ramp)
    return group


def _build_stop_sampler(group, group_input, ramp):
    """Map equal timing slots to actual editable stop positions natively.

    RGB channels carry the same scalar, so Blender's colour-to-value
    conversion is an identity. Native property drivers follow moved stops;
    adding/removing stops requires explicit reapply to rebuild the slots.
    """
    sampler = group.nodes.new(RAMP_NODE)
    sampler.name = STOP_SAMPLER_NODE
    sampler.label = "Timing slots → artist stop positions"
    sampler.location = (-320, -240)
    sampler.color_ramp.interpolation = "LINEAR"
    count = len(ramp.color_ramp.elements)
    for index in range(2, count):
        sampler.color_ramp.elements.new((index + .5) / count)
    handles = list(sampler.color_ramp.elements)
    for index, point in enumerate(handles):
        point.position = (index + .5) / count
        position = ramp.color_ramp.elements[index].position
        point.color = (position, position, position, 1.)
        for component in range(3):
            curve = point.driver_add("color", component)
            variable = curve.driver.variables.new()
            variable.name = "position"
            variable.type = "SINGLE_PROP"
            variable.targets[0].id_type = "NODETREE"
            variable.targets[0].id = group
            variable.targets[0].data_path = ramp.color_ramp.elements[index].path_from_id("position")
            curve.driver.expression = "position"
    group.links.new(group_input.outputs[FACTOR_INPUT], sampler.inputs[0])
    group.links.new(sampler.outputs["Color"], ramp.inputs[0])
    ramp.location = (0, 0)
    group["espresso_sampled_stop_count"] = count


def build_ramp_for_socket(node_tree, target_node, target_socket, stops=4,
                          interpolation="CONSTANT", source_ramp=None, *, indexed_stops=False):
    """Insert a labelled palette group feeding ``target_socket``.

    Reapply replaces only the previous Espresso palette group. Other artist
    nodes stay in the material even when their link is replaced.
    """
    _remove_previous_palette_for_socket(node_tree, target_socket)
    group_tree = _new_palette_group(stops, interpolation, source_ramp,
                                    indexed_stops=indexed_stops)
    palette = node_tree.nodes.new("ShaderNodeGroup")
    palette.node_tree = group_tree
    palette.name = "Driver Espresso Palette"
    palette.label = "Driver Espresso: Palette"
    palette[PALETTE_GROUP_NODE_TAG] = True
    palette.location = (target_node.location.x - 320, target_node.location.y - 120)
    node_tree.links.new(palette.outputs[COLOR_OUTPUT], target_socket)
    return palette


_SEED_COLOURS = (
    (1.0, 0.15, 0.15, 1.0), (1.0, 0.65, 0.10, 1.0),
    (0.20, 0.85, 0.30, 1.0), (0.15, 0.55, 1.00, 1.0),
    (0.75, 0.30, 1.00, 1.0), (1.00, 0.30, 0.65, 1.0),
    (0.15, 0.90, 0.85, 1.0), (1.00, 0.95, 0.40, 1.0),
)


def _seed_stops(ramp, stops):
    ramp = getattr(ramp, "color_ramp", ramp)
    stops = max(2, min(int(stops), len(_SEED_COLOURS)))
    elements = ramp.elements
    while len(elements) > 1:
        elements.remove(elements[-1])
    for index in range(stops):
        element = elements[0] if index == 0 else elements.new(index / float(stops))
        element.position = index / float(stops)
        element.color = _SEED_COLOURS[index]


def copy_ramp_settings(source, destination):
    """Copy one Blender ColorRamp without linking the two datablocks."""
    if source is None or destination is None:
        return False

    source_elements = list(source.elements)
    destination_elements = destination.elements
    while len(destination_elements) > len(source_elements):
        destination_elements.remove(destination_elements[-1])
    while len(destination_elements) < len(source_elements):
        destination_elements.new(source_elements[len(destination_elements)].position)

    # Keep stable element handles while positions are assigned: Blender sorts
    # the collection live as a stop moves.
    targets = list(destination_elements)
    for target, original in zip(targets, source_elements):
        target.position = original.position
        target.color = tuple(original.color)

    for attribute in ("interpolation", "color_mode", "hue_interpolation"):
        if hasattr(source, attribute) and hasattr(destination, attribute):
            setattr(destination, attribute, getattr(source, attribute))
    return True


def band_factors(stops):
    """The factor the driver steps to for each of ``stops`` bands: the middle
    of band k, (k + 0.5) / stops."""
    stops = max(2, int(stops))
    return tuple((index + 0.5) / stops for index in range(stops))


def resize_ramp(ramp, stops):
    """Resize a template ramp for a named count preset.

    Existing colours are retained where possible; new stops use the catalogue's
    seeded colours, and every stop is placed at the START of its band --
    k / stops, the same layout ``_seed_stops`` uses -- because the driver
    steps to band MIDDLES, (k + 0.5) / stops. Spreading the stops from 0 to 1
    instead (k / (stops - 1)) left the last stop at 1.0 where no middle ever
    lands: a four-stop ramp showed three colours and skipped the fourth.
    """
    if ramp is None:
        return False
    stops = max(2, min(int(stops), 64))
    elements = ramp.elements
    while len(elements) > stops:
        elements.remove(elements[-1])
    while len(elements) < stops:
        element = elements.new(1.0)
        element.color = _SEED_COLOURS[(len(elements) - 1) % len(_SEED_COLOURS)]
    handles = list(elements)
    for index, element in enumerate(handles):
        element.position = index / float(stops)
    return True


def unreachable_stops(ramp, stops):
    """Stops the driver's steps will never land on, as 1-based indices.

    The driver visits band middles in order; a stop is reached when it is the
    last stop at or before that middle. A stop the artist has dragged past
    the next middle, or two stops inside one band, leaves one of them
    unvisited -- the panel says which, rather than letting a colour vanish
    silently. Positions are the artist's; nothing here moves them.
    """
    ramp = getattr(ramp, "color_ramp", ramp)
    if ramp is None:
        return ()
    positions = sorted(
        (float(element.position), index) for index, element in enumerate(ramp.elements)
    )
    if not positions:
        return ()
    visited = set()
    for factor in band_factors(stops):
        holder = None
        for position, index in positions:
            if position <= factor + 1e-9:
                holder = index
            else:
                break
        if holder is None:
            holder = positions[0][1]
        visited.add(holder)
    return tuple(sorted(index + 1 for index in range(len(positions)) if index not in visited))


def reset_template_ramp(ramp, stops=4):
    """Restore the authored default palette, including interpolation."""
    if ramp is None:
        return False
    ramp.interpolation = "CONSTANT"
    _seed_stops(ramp, stops)
    return True


def _template_ramp_group(scene):
    if scene is None:
        return None
    name = scene.get(TEMPLATE_RAMP_SCENE_PROP, "")
    group = bpy.data.node_groups.get(name) if name else None
    if group is not None and group.get(TEMPLATE_RAMP_TREE_TAG):
        return group
    return None


def template_ramp_node(scene, create=True, stops=4):
    """Return the scene's persistent, pre-apply ColorRamp node."""
    group = _template_ramp_group(scene)
    if group is None and create:
        group = generated_nodes.create_group(
            ".Driver Espresso Template Palette - %s" % scene.name,
            "ShaderNodeTree",
            resource_id="template-palette.%s" % uuid.uuid4().hex[:12],
            effect_id="",
            role="template_palette",
        )
        group[TEMPLATE_RAMP_TREE_TAG] = True
        group.use_fake_user = True
        scene[TEMPLATE_RAMP_SCENE_PROP] = group.name
        node = group.nodes.new(RAMP_NODE)
        node.name = "Driver Espresso Template Palette"
        node.label = "Template palette"
        node[TEMPLATE_RAMP_NODE_TAG] = True
        node.color_ramp.interpolation = "CONSTANT"
        _seed_stops(node, stops)
        return node
    if group is None:
        return None
    return next(
        (node for node in group.nodes if node.get(TEMPLATE_RAMP_NODE_TAG)),
        None,
    )


def template_ramp(scene, create=True, stops=4):
    """Return the editable ColorRamp stored for ``scene``."""
    node = template_ramp_node(scene, create=create, stops=stops)
    return node.color_ramp if node is not None else None


def ramp_signature(ramp):
    """Stable cache signature for every setting that changes ramp output."""
    if ramp is None:
        return ""
    parts = [
        str(getattr(ramp, "interpolation", "")),
        str(getattr(ramp, "color_mode", "")),
        str(getattr(ramp, "hue_interpolation", "")),
    ]
    for element in ramp.elements:
        parts.append("%.9g" % float(element.position))
        parts.extend("%.9g" % float(channel) for channel in element.color)
    return hashlib.sha1("|".join(parts).encode("ascii")).hexdigest()[:16]


def evaluate_ramp_samples(ramp, factors, *, indexed_stops=False):
    """Evaluate the actual Blender ColorRamp for each preview factor."""
    if ramp is None:
        return ()
    if indexed_stops:
        positions = [float(element.position) for element in ramp.elements]
        count = len(positions)
        mapped = []
        for factor in factors:
            index = min(count - 1., max(0., float(factor) * count - .5))
            lower = int(index)
            upper = min(count - 1, lower + 1)
            weight = index - lower
            mapped.append(positions[lower] * (1 - weight) + positions[upper] * weight)
        factors = mapped
    return tuple(
        tuple(float(channel) for channel in ramp.evaluate(
            min(1.0, max(0.0, float(factor)))
        ))
        for factor in factors
    )


def _inner_ramp(group):
    if group is None:
        return None
    return next((node for node in group.nodes if node.get(PALETTE_RAMP_TAG)), None)


def ramp_node_for_driver(owner, data_path):
    """Find the editable inner Colour Ramp from a driven Factor path."""
    if owner is None or not data_path or ".default_value" not in data_path:
        return None
    try:
        socket = owner.path_resolve(data_path.rsplit(".default_value", 1)[0])
    except Exception:
        return None
    node = getattr(socket, "node", None)
    if _is_palette_group(node):
        return _inner_ramp(node.node_tree)
    ramp = _palette_downstream_of(node)
    if ramp is not None:
        return ramp
    return node if is_ramp_node(node) else None


# The tag a shared-material spatial group carries. Compared as a string rather
# than imported, because shared_material_sweep imports THIS module to build the
# palette and the import would be circular.
_SPATIAL_GROUP_TAG = "driver_espresso_shared_motion"


def _palette_downstream_of(node):
    """The palette a spatial field feeds, when the field drives a colour.

    A field driving a colour is remembered by its TIME input, so resolving the
    remembered path lands on the field group and stops there - the palette is
    one link further on. Without this the artist gets a colour they cannot
    edit, which is the whole reason the ramp is drawn in the panel.
    """
    # Truthiness, not `is True`: Blender stores a custom-property boolean as an
    # int, so `node.get(tag) is True` is False for a tag that was set to True.
    if node is None or not node.get(_SPATIAL_GROUP_TAG):
        return None
    # Compared by POINTER, not identity. Blender hands back a fresh Python
    # wrapper on every struct access, so `link.from_node is node` is False even
    # for the same node - which made this silently find nothing.
    tree = node.id_data
    pointer = node.as_pointer()
    for link in getattr(tree, "links", ()):
        if link.from_node.as_pointer() == pointer and _is_palette_group(link.to_node):
            return _inner_ramp(link.to_node.node_tree)
    return None
