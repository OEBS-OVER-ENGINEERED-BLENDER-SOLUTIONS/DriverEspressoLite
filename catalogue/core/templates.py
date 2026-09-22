"""Template catalogue for Driver Espresso.

This module is intentionally free of Blender imports so the expression data can
be validated outside Blender.
"""

from __future__ import annotations

import os
import re

from .use_cases import SPECIFIC_USE_CASES


PLAYBACK_WRAP = "PLAYBACK_WRAP"
ENDPOINT_HIT = "ENDPOINT_HIT"
CUSTOM_RANGE = "CUSTOM_RANGE"
FIXED_PERIOD = "FIXED_PERIOD"

ESPRESSO_INPUT_PREFIX = "espinp_"


def _normalize_espresso_input_variables(expression, channels, requirements):
    """Tag Espresso-owned variables and keep their expressions in lockstep."""
    normalized = []
    replacements = {}
    for requirement in requirements or []:
        item = dict(requirement)
        old_name = str(item["name"])
        new_name = (
            old_name if old_name.startswith(ESPRESSO_INPUT_PREFIX)
            else ESPRESSO_INPUT_PREFIX + old_name
        )
        item["name"] = new_name
        normalized.append(item)
        replacements[old_name] = new_name

    def rewrite(value):
        result = str(value)
        for old_name in sorted(replacements, key=len, reverse=True):
            result = re.sub(
                rf"\b{re.escape(old_name)}\b", replacements[old_name], result,
            )
        return result

    rewritten_channels = []
    for channel in channels or []:
        item = dict(channel)
        item["expression"] = rewrite(item["expression"])
        rewritten_channels.append(item)
    return rewrite(expression), rewritten_channels, normalized


# CATEGORIES is no longer a literal. Each template's category, subcategory,
# Traits come from taxonomy.py, which is applied to the catalogue at
# the bottom of this module. CATEGORIES is then derived from what is actually
# populated, so an empty category (e.g. Wind & Nature before its content exists)
# never reaches the UI enum.
#
# The `category` argument still passed to template() below is *ignored* — it is
# retained only so the 101 existing call sites keep their shape until the next
# content pass rewrites them. taxonomy.py is the single source of truth.


# Keyed by the *legacy* category strings still passed to template(), not by the
# taxonomy categories. A template that supplies no `use_cases` of its own falls
# back to this table; the assertion at the bottom of this module fails loudly if
# a lookup ever misses, so a new template can never ship with empty use-cases.
LEGACY_CATEGORY_USE_CASES = {
    "Rotation": "Fans, wheels, turntables, props, looping object rotation.",
    "Eased Rotation": "Camera reveals, mechanical levers, doors, hero object turns.",
    "Oscillating Rotation": "Pendulums, antennas, breathing parts, gentle idle motion.",
    "Seamless Loop": "Looping values for games, GIFs, background motion, cyclic effects.",
    "Value Transition": "One-shot fades, ramps, reveal controls, timed parameter changes.",
    "Remapping": "Normalize frame ranges, map timing windows, convert frame motion to values.",
    "Property Control": "Map one driver variable/property range into another, with clamps, inversion, thresholds, dead zones, and response curves.",
    "Stepped & Triggered": "Switches, gates, stepped meters, pulses, on/off animation logic.",
    "Circular & Path": "Orbit paths, figure-eight motion, spirals, paired X/Y motion.",
    "Pseudo-Random & Noise": "Organic drift, flicker, jitter, procedural variation.",
    "Delay & Time Control": "Offsets, staggered object timing, delayed starts, reverse timing.",
    "Wave Shapes": "Sawtooth, triangle, and square-wave controls for rhythmic motion.",
    "Lighting & FX": "Blinkers, emergency lights, beacons, strobes, candles, screens, pulses.",
}


def param(token, label, kind="FLOAT", default=0.0, minimum=None, maximum=None, tip="",
          presets=None, unit=None, derives=None, state_labels=None, apply_time=False,
          visible_if=None, enum_items=None):
    """Declare a template parameter.

    ``unit``    a short suffix shown in the UI and tooltips ("s", "°", "frames").
    ``derives`` maps expression tokens to formulas over the *other* tokens plus
                the frame constants (FRAME_START/FRAME_END/FRAME_LEN/FPS).

    A parameter with ``derives`` is the friendly-parameter layer: the user sets
    something they understand ("seconds per revolution") and the maths token the
    expression actually contains ("TURNS") is computed from it. The parameter's
    own token need not appear in the expression at all.

    Templates that declare no ``derives`` are byte-for-byte unaffected; see
    ``utils.resolve_derived_tokens``.

    ``apply_time`` marks a parameter the EXPRESSION cannot see, because it is
    acted on when the driver is applied rather than when it is built. The
    blink's Eye lead is the first: it offsets one bone against another, and only
    the apply layer knows which bone is which side. Such a parameter looks dead
    to the every-parameter-must-do-something check, so it declares itself here
    rather than being quietly listed as an exception in that test.
    """
    tip = tip or _infer_param_tip(token, label)
    if unit == "m" and "metre" not in tip.lower():
        tip = f"{tip.rstrip()} Measured in metres."
    data = {
        "token": token,
        "label": label,
        "type": kind,
        "default": default,
        "apply_time": apply_time,
        "tip": tip,
        "presets": presets or [],
    }
    if minimum is not None:
        data["min"] = minimum
    if maximum is not None:
        data["max"] = maximum
    if unit:
        data["unit"] = unit
    if derives:
        data["derives"] = dict(derives)
    if state_labels:
        # A BOOL parameter draws as a pressable button; these name the two
        # states so it reads as a mode switch ("Stepped" / "Smooth sweep")
        # rather than a bare On/Off the user has to interpret.
        data["state_labels"] = tuple(state_labels)
    if visible_if:
        data["visible_if"] = dict(visible_if)
    if enum_items:
        data["enum_items"] = tuple(tuple(item) for item in enum_items)
    return data


def override(base, **changes):
    """Copy a shared parameter, changing a few fields for one template.

    The module-level parameter objects (SPEED, TICKS, …) are shared by reference
    across many templates, so they must never be mutated. This returns a private
    copy — the only safe way to give one template its own default or label.
    """
    data = dict(base)
    data.update(changes)
    data["presets"] = list(data.get("presets") or [])
    if "derives" in data and data["derives"]:
        data["derives"] = dict(data["derives"])
    return data


def advanced_param(token, label, group, kind="FLOAT", default=0.0, minimum=None, maximum=None, tip="", presets=None, unit=None):
    data = param(token, label, kind, default, minimum, maximum, tip=tip, presets=presets, unit=unit)
    data["advanced"] = True
    data["advanced_group"] = group
    return data


def resolve_advanced_param(control):
    if isinstance(control, dict):
        token = control.get("token")
        if not token:
            raise KeyError("Advanced control dict is missing a token.")
        spec = ADVANCED_PARAM_BY_TOKEN.get(token)
        if spec is None:
            raise KeyError(f"Unknown advanced control token: {token}")
        return spec
    if isinstance(control, str):
        spec = ADVANCED_PARAM_BY_TOKEN.get(control)
        if spec is None:
            raise KeyError(f"Unknown advanced control token: {control}")
        return spec
    raise TypeError(f"Unsupported advanced control reference: {control!r}")


def normalize_advanced_controls(controls):
    normalized = []
    seen_tokens = set()
    for control in controls or []:
        spec = resolve_advanced_param(control)
        token = spec["token"]
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        normalized.append(spec)
    return normalized


def _infer_param_tip(token, label):
    key = f"{token} {label}".lower()
    mapping = [
        (("speed",), "Rate multiplier for how fast the motion or effect progresses."),
        (("start delay",), "Frames to wait before the template begins playing."),
        (("start early", "advance"), "Frames to shift the template earlier in time."),
        (("strobe period",), "Frames between strobe beats inside the larger pattern."),
        (("loop start",), "First frame of the custom loop range."),
        (("loop end",), "Last frame of the custom loop range."),
        (("input start",), "First input frame used for remapping."),
        (("input end",), "Last input frame used for remapping."),
        (("input min",), "Lowest expected source value before remapping."),
        (("input max",), "Highest expected source value before remapping."),
        (("output min",), "Lowest value produced after remapping."),
        (("output max",), "Highest value produced after remapping."),
        (("loop", "loops", "cycles"), "Number of full repetitions across the loop or scene range."),
        (("amplitude",), "Maximum distance away from the center value."),
        (("period",), "Cycle length in frames before the pattern repeats."),
        (("range", "radius"), "Overall size of the output span or orbit."),
        (("brightness",), "Peak output strength, commonly used for light or emission intensity."),
        (("minimum", "min"), "Lowest output value this expression is allowed to reach."),
        (("maximum", "max"), "Highest output value this expression is allowed to reach."),
        (("frames", "frame count"), "Number of frames used for the loop or time span."),
        (("center",), "Midpoint value the motion or remap is built around."),
        (("end frame",), "Frame where this expression stops updating."),
        (("output multiplier",), "Multiplier applied after the template expression is evaluated."),
        (("output offset",), "Value added after the template expression is evaluated."),
        (("start frame", "delay frame", "trigger frame", "strike frame", "in start", "out start"), "Frame where this stage of the effect begins."),
        (("start", "from", "low output", "low"), "Starting or lower value used by this expression."),
        (("end", "to", "high output", "high"), "Ending or upper value used by this expression."),
        (("phase",), "Offsets where the cycle begins inside the waveform."),
        (("power",), "Exponent shaping how gently or aggressively the response ramps."),
        (("dead zone",), "Neutral band around zero that is ignored before output begins."),
        (("threshold",), "Cutoff value where the expression switches behavior."),
        (("scale",), "Multiplier applied to the driven result."),
        (("offset",), "Value added after scaling or remapping."),
        (("duration",), "How long the active pulse or effect stays on, measured in frames."),
        (("ramp", "ramp frames"), "Number of frames used to fade between low and high values."),
        (("flash width",), "Length of each flash in frames."),
        (("flash gap", "gap"), "Spacing in frames between repeated flashes."),
        (("duty",), "Fraction of each cycle spent at the high state."),
        (("beat 1",), "Frame offset for the first heartbeat pulse."),
        (("beat 2",), "Frame offset for the second heartbeat pulse."),
        (("spread",), "How wide or soft each pulse peak becomes."),
        (("sharpness",), "How focused or narrow the peak becomes near the brightest point."),
        (("shape",), "Curve shaping control for how soft or sharp the pulse feels."),
        (("hold frames",), "How many frames each pseudo-random value is held before changing."),
        (("decay",), "How quickly the oscillation loses energy over time."),
        (("frequency", "freq"), "How many oscillation peaks occur inside the motion range."),
        (("strength",), "How strongly the limiting or compression effect is applied."),
        (("target value",), "Value reached at the forward end of the motion."),
        (("degrees",), "Input angle in degrees, converted internally for rotation drivers."),
        (("half range",), "Distance from the center to either side of the active range."),
        (("x radius",), "Horizontal radius of the ellipse or orbit."),
        (("max radius",), "Largest radius reached by the spiral path."),
    ]
    for needles, text in mapping:
        if any(needle in key for needle in needles):
            return text
    return "Controls one part of the current driver expression."


def _advanced_group_label(group):
    labels = {
        "timing": "timing",
        "output": "output",
    }
    return labels.get(group, "advanced")


def format_value(param, value):
    """Render a value with its unit, so tooltips read '60 s' rather than '60'."""
    unit = param.get("unit")
    return f"{value} {unit}" if unit else f"{value}"


def format_param_range(param):
    """'Range: min 1 marks' — or '' when the parameter is unbounded."""
    minimum = param.get("min")
    maximum = param.get("max")
    if minimum is None and maximum is None:
        return ""
    bits = []
    if minimum is not None:
        bits.append(f"min {minimum}")
    if maximum is not None:
        bits.append(f"max {maximum}")
    unit = param.get("unit")
    return "Range: " + ", ".join(bits) + (f" {unit}" if unit else "")


def format_param_derives(param):
    """'Sets: TURNS' — names the maths this friendly parameter stands in for."""
    formulas = param.get("derives")
    if not formulas:
        return ""
    return "Sets: " + ", ".join(sorted(formulas))


def format_param_tooltip(param):
    parts = []
    label = param.get("label", "Parameter")
    tip = param.get("tip", "")
    if tip:
        parts.append(f"{label}: {tip}")
    else:
        parts.append(label)

    if param.get("advanced"):
        parts.append(f"Optional {_advanced_group_label(param.get('advanced_group'))} control.")

    parts.append(f"Default: {format_value(param, param.get('default', 0))}")

    range_line = format_param_range(param)
    if range_line:
        parts.append(range_line)

    presets = param.get("presets", [])
    if presets:
        parts.append("Presets: " + ", ".join(item.get("label", str(item.get("value", ""))) for item in presets[:5]))

    derives = format_param_derives(param)
    if derives:
        parts.append(derives)

    return "\n".join(parts)


def infer_param_input_hint(param):
    key = f"{param.get('token', '')} {param.get('label', '')}".lower()
    mapping = [
        (("start delay",), "Raise this to hold the template longer before it begins, or leave it at zero for no extra wait."),
        (("start early", "advance"), "Raise this to make the template behave as if it started earlier, useful when you want the motion already in progress."),
        (("strobe period",), "Use smaller values for faster strobing inside each cycle, or larger values for fewer, slower strobe hits."),
        (("period",), "Use smaller values to make the whole pattern repeat faster, or larger values to stretch the cycle out over more frames."),
        (("duty",), "Raise this to keep the effect high for more of each cycle, or lower it for shorter bursts."),
        (("minimum", "min"), "Set the floor value the expression should never go below."),
        (("maximum", "max"), "Set the ceiling value the expression should never rise above."),
        (("speed",), "Raise this to make the motion run faster without changing the rest of the setup."),
        (("flash width",), "Increase this to make each flash last longer, or reduce it for sharper hits."),
        (("flash gap", "gap"), "Increase this to put more time between flashes, or reduce it for tighter bursts."),
        (("sharpness",), "Raise this for a tighter, more focused peak, or lower it for a broader softer pulse."),
        (("spread",), "Raise this to widen and soften each pulse, or lower it to make the peaks tighter."),
        (("beat 1",), "Move this earlier or later in the cycle to place the first pulse where you want it."),
        (("beat 2",), "Move this earlier or later in the cycle to place the second pulse relative to the first."),
        (("threshold",), "Raise this so the effect triggers less often, or lower it so more values pass through."),
        (("scale",), "Increase this to amplify the result, or reduce it to make the response gentler."),
        (("offset",), "Use this to shift the whole output range upward or downward after scaling."),
        (("power",), "Raise this for a steeper response curve, or lower it for a softer one."),
        (("loop start",), "Set the first frame where the custom loop should begin."),
        (("loop end",), "Set the last frame included in the custom loop range."),
        (("input start",), "Set the first frame of the source range that should be remapped."),
        (("input end",), "Set the last frame of the source range that should be remapped."),
        (("input min",), "Set the lowest source value you expect to feed into this remap."),
        (("input max",), "Set the highest source value you expect to feed into this remap."),
        (("output min",), "Set the lowest output value this remap should produce."),
        (("output max",), "Set the highest output value this remap should produce."),
        (("end frame",), "Set the last frame where this expression should keep updating before the advanced range ends."),
        (("output multiplier",), "Raise this to amplify the final result after the template math is done, or lower it to soften the output."),
        (("output offset",), "Use this to shift the final result upward or downward after the template math is done."),
        (("start frame", "delay frame", "trigger frame", "strike frame", "in start", "out start"), "Set the frame where this stage of the effect should begin."),
        (("duration",), "Increase this to keep the effect active longer, or reduce it for shorter pulses."),
        (("ramp", "ramp frames"), "Increase this to smooth the fade over more frames, or reduce it for a snappier transition."),
        (("seconds per revolution",), "Set how long one full turn takes in real seconds — 60 for a clock's second hand, 3600 for its minute hand. The scene frame rate does the rest, so the hand keeps time whatever the scene length."),
        (("marks on dial",), "Set how many positions the hand clicks through in one turn: 60 for a clock face, 12 for an hour dial, fewer for a chunky gauge."),
        (("start position",), "Rotate the hand's resting angle before the first tick, in degrees."),
        (("ticks per turn", "steps per turn"), "Raise this for finer, more gradual steps, or lower it for a coarser, more mechanical snap."),
        (("turns",), "Increase this to repeat the stepped motion over more full revolutions."),
        (("start degrees", "start angle"), "Set the angle this stage begins at before stepping starts."),
    ]
    for needles, text in mapping:
        if any(needle in key for needle in needles):
            return text
    return "Adjust this value to shape how this part of the expression behaves."


def format_param_input_tooltip(param):
    parts = []
    label = param.get("label", "Parameter")
    tip = param.get("tip", "")
    if tip:
        parts.append(f"{label}: {tip}")
    else:
        parts.append(label)

    if param.get("advanced"):
        parts.append(f"This optional {_advanced_group_label(param.get('advanced_group'))} control is only shown for templates that support it.")

    hint = infer_param_input_hint(param)
    if hint:
        parts.append("How to use: " + hint)

    parts.append(f"Default: {format_value(param, param.get('default', 0))}")

    range_line = format_param_range(param)
    if range_line:
        parts.append(range_line)

    presets = param.get("presets", [])
    if presets:
        parts.append("Presets: " + ", ".join(item.get("label", str(item.get("value", ""))) for item in presets[:4]))

    derives = format_param_derives(param)
    if derives:
        parts.append(derives)

    return "\n".join(parts)


def advanced_controls_for_template(template_or_id):
    template_data = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if template_data is None:
        raise KeyError(f"Unknown template: {template_or_id}")
    return list(normalize_advanced_controls(template_data.get("advanced_controls", [])))


def template(
    template_id,
    name,
    category,
    expression,
    params,
    description,
    *,
    subcategory="",
    use_cases="",
    frame_mode=PLAYBACK_WRAP,
    pair_with=None,
    variants=None,
    warning=None,
    requires_driver_variables=None,
    managed_driver_variables=None,
    requires_python_driver=False,
    advanced_controls=None,
    result_summary="",
    channels=None,
    output_baseline=None,
    combined_presets=None,
    application_target=None,
    shared_material=False,
    role_set=None,
    role_label=None,
    role_order=0,
    parameter_share_group=None,
    constraints=None,
    internal_helpers=None,
    sample_internal_helpers=False,
    effect_id="",
    application_modes=None,
    recipe_contract=None,
    warn_if=None,
):
    """``combined_presets``: optional list of ``{"label": str, "values": {token: value, ...}}``
    dicts — a single button that sets several parameters together (unlike a
    per-parameter preset dropdown, which only ever touches one token at a
    time). Use this when a named "feel" is really defined by the combination
    of several values at once (e.g. a specific heartbeat rhythm needs its
    beat timing, softness, and second-beat strength all set together to
    reproduce it in one click)."""
    expression, channels, requires_driver_variables = _normalize_espresso_input_variables(
        expression, channels, requires_driver_variables,
    )
    normalized_channels = []
    for channel in channels:
        from . import catalogue_contracts

        normalized_channels.append({
            "id": str(channel["id"]),
            "label": str(channel["label"]),
            "data_path": str(channel["data_path"]),
            "index": int(channel["index"]),
            "expression": str(channel["expression"]),
            "driver_expression": str(channel.get("driver_expression") or ""),
            "output_baseline": channel.get("output_baseline"),
            "additive_profile": channel.get("additive_profile"),
            "application_space": channel.get(
                "application_space", catalogue_contracts.default_application_space(channel),
            ),
            # Semantic rotation role ("pitch"/"roll"), when the channel routes by
            # meaning rather than by a fixed Euler index. Absent on most channels.
            "axis_role": channel.get("axis_role"),
            "camera_rotation_role": channel.get("camera_rotation_role"),
            "enabled_if": dict(channel.get("enabled_if") or {}),
        })
    normalized_application_modes = []
    for mode in application_modes or ():
        mode_channels = []
        for channel in mode.get("channels") or ():
            from . import catalogue_contracts

            mode_channels.append({
                "id": str(channel["id"]),
                "label": str(channel["label"]),
                "data_path": str(channel["data_path"]),
                "index": int(channel["index"]),
                "expression": str(channel["expression"]),
                "driver_expression": str(channel.get("driver_expression") or ""),
                "output_baseline": channel.get("output_baseline"),
                "additive_profile": channel.get("additive_profile"),
                "application_space": channel.get(
                    "application_space", catalogue_contracts.default_application_space(channel),
                ),
                "axis_role": channel.get("axis_role"),
            "camera_rotation_role": channel.get("camera_rotation_role"),
                "enabled_if": dict(channel.get("enabled_if") or {}),
            })
        normalized_application_modes.append({
            "id": str(mode["id"]),
            "label": str(mode["label"]),
            "description": str(mode.get("description") or ""),
            "params": [dict(item) for item in (mode.get("params") or ())],
            "channels": mode_channels,
            # A single-property recipe's modes differ in the one expression
            # they drive; a mode may override it. Empty keeps the recipe's own.
            "expression": str(mode.get("expression") or ""),
            # A mode may read its input differently (a crank's evaluated
            # rotation rather than its rotation property): an override of the
            # recipe's required driver variables, whole, not merged.
            "requires_driver_variables": [
                dict(item) for item in (mode.get("requires_driver_variables") or ())
            ],
            # The mode a saved setup resolves to when it names none of these.
            # New setups take the FIRST mode; an entry applied before the
            # modes existed keeps the behaviour it was applied with.
            "legacy_default": bool(mode.get("legacy_default", False)),
        })

    return {
        "id": template_id,
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "description": description,
        # A template's OWN use-case wins over anything passed in. The category
        # blurbs below it describe the shelf, not the thing on it, so they are
        # a last resort rather than the normal answer.
        "use_cases": (
            SPECIFIC_USE_CASES.get(template_id)
            or use_cases
            or LEGACY_CATEGORY_USE_CASES.get(category, "")
        ),
        "expression": expression,
        "frame_mode": frame_mode,
        "params": params,
        "pair_with": pair_with,
        "variants": variants or [],
        # Channels (internal name: roles) — sibling templates that are PARTS of
        # one setup rather than alternatives to it: the green/amber/red aspects
        # of a signal, the two heads of a police bar, the belt and roller of a
        # conveyor. Members share a role_set; the panel offers a channel picker
        # that switches between them, ordered by role_order and labelled by
        # role_label. Manual-value sharing is a separate, explicit contract:
        # parameter_share_group. This distinction lets alternative material
        # profiles such as Bowling/Golf/Tennis share one picker without leaking
        # values into one another. Deliberately NOT "channels"
        # (that key holds the data paths a single template drives).
        "role_set": role_set,
        "role_label": role_label or name,
        "role_order": int(role_order),
        "parameter_share_group": parameter_share_group,
        "warning": warning,
        "requires_driver_variables": requires_driver_variables,
        # Runtime services such as analyzed audio bind these variables
        # themselves.  They participate in validation and graph previews, but
        # must not be presented as ordinary artist-selected Espresso Inputs.
        "managed_driver_variables": [dict(item) for item in (managed_driver_variables or [])],
        "requires_python_driver": requires_python_driver,
        "advanced_controls": normalize_advanced_controls(advanced_controls),
        "result_summary": result_summary,
        "channels": normalized_channels,
        "output_baseline": output_baseline,
        "additive_profile": None,
        "application_target": dict(application_target or {}),
        "shared_material": bool(shared_material),
        "combined_presets": [
            {"label": str(cp["label"]), "values": dict(cp["values"])}
            for cp in (combined_presets or [])
        ],
        "constraints": [dict(item) for item in (constraints or [])],
        "internal_helpers": [dict(item) for item in (internal_helpers or [])],
        "sample_internal_helpers": bool(sample_internal_helpers),
        "effect_id": str(effect_id or "").upper(),
        "application_modes": normalized_application_modes,
        "recipe_contract": dict(recipe_contract or {}),
        # Checks the builder runs against the RESOLVED values: ``when`` is a
        # formula in the parameter tokens (derives included), ``text`` is
        # what the artist reads. ``blocking`` refuses the apply; otherwise it
        # is a warning beside a valid expression -- a wingbeat rate the frame
        # rate cannot show, a linkage that cannot close.
        "warn_if": [dict(item) for item in (warn_if or [])],
    }


SPEED = param(
    "SPEED",
    "Speed",
    "FLOAT",
    1.0,
    0.0001,
    unit="×",
    tip="Multiplies the template's natural rate. 2 runs twice as fast, 0.5 half as fast, 1 leaves it untouched.",
    presets=[
        {"label": "Half speed", "value": 0.5},
        {"label": "Normal", "value": 1.0},
        {"label": "Double speed", "value": 2.0},
        {"label": "Quadruple", "value": 4.0},
    ],
)
N = param(
    "N",
    "Loops",
    "INT",
    3,
    1,
    unit="loops",
    tip="How many complete cycles finish across the active frame range. The motion still lands exactly on the last frame, so the loop stays seamless.",
    presets=[
        {"label": "Once", "value": 1},
        {"label": "Twice", "value": 2},
        {"label": "Three times", "value": 3},
        {"label": "Five times", "value": 5},
    ],
)
SPINUP_FRAMES = param(
    "SPINUP",
    "Spin-up frames",
    "INT",
    30,
    1,
    unit="frames",
    tip=(
        "How many frames it takes to accelerate from a standstill up to full Speed. "
        "After this the rotation holds that constant speed, so playback stays smooth "
        "and consistent instead of accelerating the whole time."
    ),
    presets=[
        {"label": "Half second (15)", "value": 15},
        {"label": "One second (30)", "value": 30},
        {"label": "Two seconds (60)", "value": 60},
        {"label": "Four seconds (120)", "value": 120},
    ],
)
AMPLITUDE = param(
    "AMPLITUDE",
    "Amplitude",
    "FLOAT",
    0.5236,
    tip="Peak departure from the rest value — the motion travels between minus and plus this amount. Its meaning follows whatever property you drive: radians on a rotation, metres on a location.",
)
PERIOD = param(
    "PERIOD",
    "Cycle length",
    "INT",
    48,
    1,
    unit="frames",
    tip="Frames taken by one complete cycle, before Speed is applied. At 24 fps a 24-frame cycle lasts one second.",
    presets=[
        {"label": "Half second (12)", "value": 12},
        {"label": "One second (24)", "value": 24},
        {"label": "Two seconds (48)", "value": 48},
        {"label": "Four seconds (96)", "value": 96},
    ],
)
RANGE = param(
    "RANGE",
    "Peak value",
    "FLOAT",
    1.0,
    tip="The highest value the wave reaches. It ramps between zero and this number.",
)

# The sine templates all drive an angle, so they take DEGREES. The shared
# AMPLITUDE above stays unit-less because the organic drift templates, the wandering drift templates and friends
# use it for plain values, where "radians" would simply be wrong.
#
# The conversion is done by `derives` (build time) rather than by wrapping the
# expression in radians() (run time). Two reasons: the built expression still
# contains a bare number, so it costs ZERO of the 255-character budget; and
# AMPLITUDE becomes machine-derived, so the literal rounder leaves its precision
# alone. Asking an animator for 0.5236 was the actual bug - nobody thinks in
# radians, and every preset had to spell the degrees out in its own label.
SWING_ANGLE = override(
    AMPLITUDE,
    token="SWING_DEG",
    label="Swing angle",
    unit="°",
    default=30.0,
    # Rounded to 4dp IN the formula, not by the global literal rounder - that one
    # deliberately skips machine-derived tokens so TURNS cannot drift. Without this
    # the literal lands as 0.5235987755982988: 13 characters more than the 0.5236
    # this template has always emitted, and expressions are capped at 255. 4dp also
    # happens to reproduce every old preset exactly (0.2618 / 0.5236 / 0.7854 / 1.5708).
    derives={"AMPLITUDE": "round(radians(SWING_DEG), 4)"},
    tip="How far the swing reaches to either side of centre, in degrees. 30° is a natural default; 90° is a quarter turn.",
    presets=[
        {"label": "Subtle", "value": 15.0},
        {"label": "Natural", "value": 30.0},
        {"label": "Wide", "value": 45.0},
        {"label": "Quarter turn", "value": 90.0},
    ],
)
TICKS = param(
    "TICKS",
    "Ticks per turn",
    "INT",
    60,
    1,
    tip="How many discrete positions the hand snaps through in one full revolution.",
    presets=[
        {"label": "10 gauge", "value": 10},
        {"label": "12 clock", "value": 12},
        {"label": "24 dial", "value": 24},
        {"label": "60 hand", "value": 60},
    ],
)
TURNS = param(
    "TURNS",
    "Turns",
    "FLOAT",
    1.0,
    0.0001,
    tip="Number of full revolutions completed across the active frame range.",
    presets=[
        {"label": "0.5 half", "value": 0.5},
        {"label": "1 full", "value": 1.0},
        {"label": "2 double", "value": 2.0},
    ],
)
START_DEG = param(
    "START_DEG",
    "Start degrees",
    "FLOAT",
    0.0,
    tip="Starting resting angle in degrees before the first tick is applied.",
    presets=[
        {"label": "0 front", "value": 0.0},
        {"label": "90 right", "value": 90.0},
        {"label": "180 back", "value": 180.0},
        {"label": "-90 left", "value": -90.0},
    ],
)

# --------------------------------------------------------------------------- #
# Friendly clock parameters.
#
# The maths wants TICKS, TURNS, START_DEG and SPEED. Nobody reasoning about a
# clock thinks in "turns" or a unitless "speed" — they think "60 marks on the
# dial, one revolution a minute". So the user sets seconds per revolution and
# TURNS is derived from it, using the scene's real frame rate.
#
# The pay-off is that the tick rate becomes *scene-length independent*: a second
# hand ticks once per second whether the scene is 120 frames or 6000.
# --------------------------------------------------------------------------- #
MARKS = param(
    "TICKS",
    "Marks on dial",
    "INT",
    60,
    1,
    unit="marks",
    tip="How many tick marks the hand snaps to in one full revolution. A clock face has 60.",
    presets=[
        {"label": "Clock / stopwatch (60)", "value": 60},
        {"label": "24-hour dial (24)", "value": 24},
        {"label": "Hour dial (12)", "value": 12},
        {"label": "Gauge (10)", "value": 10},
    ],
)

SECONDS_PER_REV = param(
    "SEC_PER_REV",
    "Seconds per revolution",
    "FLOAT",
    60.0,
    0.0001,
    unit="s",
    tip=(
        "Real seconds the hand takes to complete one full turn. A clock's second hand "
        "takes 60; its minute hand takes 3600. Uses the scene frame rate, so the hand "
        "keeps correct time whatever the scene length."
    ),
    presets=[
        {"label": "Second hand (60 s)", "value": 60.0},
        {"label": "Minute hand (1 h)", "value": 3600.0},
        {"label": "Hour hand (12 h)", "value": 43200.0},
        {"label": "24-hour dial (24 h)", "value": 86400.0},
    ],
    # No `derives` here on purpose. A derived TURNS constant must never be
    # rounded (a rounded tick rate drifts audibly over a long scene - see
    # utils.build_expression), but a full-precision float literal like
    # 69.15347222222222 is unreadable if anyone opens the driver in Blender.
    # Writing the ratio as division instead - FRAME_LEN/(SEC_PER_REV*FPS) -
    # gives the identical exact value (both are float64 arithmetic) in a
    # fraction of the characters, and reads as what it is: a rate.
)

START_POS = param(
    "START_DEG",
    "Start position",
    "FLOAT",
    0.0,
    unit="°",
    tip="Angle the hand rests at before the first tick, measured from however the hand is modelled.",
    presets=[
        {"label": "12 o'clock (0°)", "value": 0.0},
        {"label": "3 o'clock (90°)", "value": 90.0},
        {"label": "6 o'clock (180°)", "value": 180.0},
        {"label": "9 o'clock (-90°)", "value": -90.0},
    ],
)

BRIGHTNESS = param(
    "BRIGHTNESS",
    "Brightness",
    "FLOAT",
    5.0,
    0.0,
    unit="W",
    tip="Emission strength at the brightest point of the flash. Values above 1 blow out to a glow; light strength is measured in watts.",
    presets=[
        {"label": "Full (1 W)", "value": 1.0},
        {"label": "Glow (5 W)", "value": 5.0},
        {"label": "HDR blowout (20 W)", "value": 20.0},
    ],
)
MINIMUM = param("MIN", "Minimum", "FLOAT", 0.0, tip="The lowest value the output ever reaches — its resting floor.")
MAXIMUM = param("MAX", "Maximum", "FLOAT", 1.0, tip="The highest value the output ever reaches — its ceiling at the peak of the cycle.")
VAR_REQUIREMENT = [{"name": "var", "type": "SINGLE_PROP", "description": "Source controller/property value.", "preview_default": 0.5}]

ADV_START_DELAY = advanced_param("ADV_DELAY", "Start delay", "timing", "INT", 0, 0, tip="Frames to wait before the template begins playing.")
ADV_TIME_ADVANCE = advanced_param("ADV_ADVANCE", "Start early", "timing", "INT", 0, 0, tip="Frames to shift the template earlier in time.")
ADV_START_FRAME = advanced_param("ADV_START_FRAME", "Start frame", "timing", "INT", 0, 0, tip="Frame where the expression becomes active. Leave at 0 to start at the scene start.")
ADV_END_FRAME = advanced_param("ADV_END_FRAME", "End frame", "timing", "INT", 0, 0, tip="Frame where the expression stops updating. Leave at 0 to run through the scene end.")
ADV_LOOP_FIT = advanced_param("ADV_LOOP_FIT", "Fit loop to scene", "timing", "BOOL", 0, tip="Snap the effective repeat period so the scene range contains a whole-number repeat count close to the chosen Period value.")
ADV_OUTPUT_MULT = advanced_param(
    "ADV_MULT", "Output gain", "output", "FLOAT", 1.0,
    tip="Scale the visible change away from the template's rest/minimum value without moving that resting value.",
)
ADV_OUTPUT_OFFSET = advanced_param("ADV_OFFSET", "Output offset", "output", "FLOAT", 0.0, tip="Add a value after the template result is evaluated.")
ADV_OVERRIDE_FPS = advanced_param(
    "ADV_OVERRIDE_FPS", "Override scene FPS", "timing", "BOOL", 0,
    tip="Use a manually entered timing frame rate instead of the scene render frame rate.",
)
ADV_TIMING_FPS = advanced_param(
    "ADV_TIMING_FPS", "Timing FPS", "timing", "FLOAT", 24.0, 1.0, 1000.0,
    tip="Frame rate used for real-world timing only while Override scene FPS is enabled.",
    unit="fps",
)
ADV_TIMING_FPS["visible_if"] = {"token": "ADV_OVERRIDE_FPS", "truthy": True}
ADV_LOC_INFLUENCE = advanced_param(
    "ADV_LOC_INFLUENCE", "Location influence", "output", "FLOAT", 1.0, 0.0, 2.0,
    tip="Scales all location channels without changing the rotation channels.",
)
ADV_ROT_INFLUENCE = advanced_param(
    "ADV_ROT_INFLUENCE", "Rotation influence", "output", "FLOAT", 1.0, 0.0, 2.0,
    tip="Scales all rotation channels without changing the location channels.",
)
ADV_X_INFLUENCE = advanced_param(
    "ADV_X_INFLUENCE", "X channel influence", "output", "FLOAT", 1.0, 0.0, 2.0,
    tip="Scales the X channel after the template's coordinated motion is built.",
)
ADV_Y_INFLUENCE = advanced_param(
    "ADV_Y_INFLUENCE", "Y channel influence", "output", "FLOAT", 1.0, 0.0, 2.0,
    tip="Scales the Y channel after the template's coordinated motion is built.",
)
ADV_Z_INFLUENCE = advanced_param(
    "ADV_Z_INFLUENCE", "Z channel influence", "output", "FLOAT", 1.0, 0.0, 2.0,
    tip="Scales the Z channel after the template's coordinated motion is built.",
)

ADVANCED_PARAMS = [
    ADV_START_DELAY,
    ADV_TIME_ADVANCE,
    ADV_START_FRAME,
    ADV_END_FRAME,
    ADV_LOOP_FIT,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
    ADV_OVERRIDE_FPS,
    ADV_TIMING_FPS,
    ADV_LOC_INFLUENCE,
    ADV_ROT_INFLUENCE,
    ADV_X_INFLUENCE,
    ADV_Y_INFLUENCE,
    ADV_Z_INFLUENCE,
]

ADVANCED_PARAM_BY_TOKEN = {item["token"]: item for item in ADVANCED_PARAMS}

ADVANCED_CONTROLS_TIMING_AND_OUTPUT = [
    ADV_START_DELAY,
    ADV_TIME_ADVANCE,
    ADV_START_FRAME,
    ADV_END_FRAME,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]

ADVANCED_CONTROLS_DELAY_RANGE_AND_OUTPUT = [
    ADV_START_DELAY,
    ADV_START_FRAME,
    ADV_END_FRAME,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]

ADVANCED_CONTROLS_DELAY_ADVANCE_AND_OUTPUT = [
    ADV_START_DELAY,
    ADV_TIME_ADVANCE,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]

ADVANCED_CONTROLS_LOOPFIT_AND_OUTPUT = [
    ADV_LOOP_FIT,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]

ADVANCED_CONTROLS_DELAY_RANGE_LOOPFIT_AND_OUTPUT = [
    ADV_START_DELAY,
    ADV_START_FRAME,
    ADV_END_FRAME,
    ADV_LOOP_FIT,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]


# The held pseudo-random gate Neon Buzz switches on. Named once because it
# appears in all three colour channels, and a typo in one of three would give
# a tube whose colour shifts as it flickers.
NEON_BUZZ_GATE = "(sin((floor((frame-FRAME_START)/HOLD)+19)*127.1)*43758.5453%1) < DROPOUT"

from . import taxonomy as _taxonomy
from . import catalogue_contracts as _catalogue_contracts
from . import effect_ids as _effect_ids

# Records are frozen at build time, so only this build's definitions are
# present in the package; nothing is hidden behind a runtime flag.
from .frozen_records import RECORDS as _FROZEN_RECORDS

TEMPLATES = [dict(record) for record in _FROZEN_RECORDS]


# Only categories that actually hold templates reach the UI. Wind & Nature and
# Text & Counters are declared in the taxonomy but have no content yet; a
# category with zero templates would produce an empty enum and break the panel.
_used_categories = {item["category"] for item in TEMPLATES}
CATEGORIES = [category for category in _taxonomy.CATEGORIES if category in _used_categories]
GENERIC_CATEGORIES = _taxonomy.GENERIC_CATEGORIES
is_generic_category = _taxonomy.is_generic_category


TEMPLATE_BY_ID = {item["id"]: item for item in TEMPLATES}

# Reverse of the registry: a code read back out of a .blend file resolves to the
# template that wrote it. This is the lookup the bake and clear operators use -
# they read a stamp, not an expression.
TEMPLATE_BY_EFFECT_ID = {item["effect_id"]: item for item in TEMPLATES}


def effect_id_for(template_or_id):
    """The permanent code for a template - 'PL01', 'CS00'."""
    item = template_or_id
    if isinstance(item, str):
        item = TEMPLATE_BY_ID.get(item)
    return (item or {}).get("effect_id", "")


def template_for_effect_id(code):
    """Resolve a stamped code back to its template, following retirements.

    ALIASES covers a template that gained channels after a build shipped: its
    code moves from STEM00 to STEM01, and an existing stamp still has to answer.
    """
    code = (code or "").strip().upper()
    if code in TEMPLATE_BY_EFFECT_ID:
        return TEMPLATE_BY_EFFECT_ID[code]
    resolved = _effect_ids.ALIASES.get(code)
    return TEMPLATE_BY_EFFECT_ID.get(resolved) if resolved else None


def effect_id_family(code):
    """Split a code into its stem and channel number: 'PL02' -> ('PL', 2).

    Parsed from the right because the channel field is always exactly two
    digits - without that rule 'TWC200' could read as stem TWC channel 200.
    Channel 0 means the template has no channels.
    """
    code = (code or "").strip().upper()
    match = re.fullmatch(r"([A-Z][A-Z0-9]*?)(\d{2})", code)
    if not match:
        return code, -1
    return match.group(1), int(match.group(2))


def has_channels(template_or_id):
    """True when this template is one channel of a family."""
    return effect_id_family(effect_id_for(template_or_id))[1] > 0


def template_channels(template_or_id):
    """Return a uniform channel view for single and multi templates.

    Historic templates have no destination plan, so their synthetic primary
    channel deliberately has an empty data path. Multi application is enabled
    only when two or more explicit Object transform channels are declared.
    """
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if item is None:
        raise KeyError(f"Unknown template: {template_or_id}")
    channels = item.get("channels") or []
    if channels:
        return [dict(channel) for channel in channels]
    return [{
        "id": "primary",
        "label": "Expression",
        "data_path": "",
        "index": -1,
        "expression": item["expression"],
        "output_baseline": item.get("output_baseline"),
        "additive_profile": item.get("additive_profile"),
        "application_space": "ABSOLUTE",
    }]


def application_modes(template_or_id):
    """Return the explicit user-facing delivery choices for a hybrid recipe."""
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if item is None:
        raise KeyError(f"Unknown template: {template_or_id}")
    declared = item.get("application_modes") or ()
    if declared:
        return [dict(mode) for mode in declared]
    return [{
        "id": "SINGLE", "label": "Single Property",
        "description": "Apply the recipe to the chosen property.",
        "params": [], "channels": [],
    }]


def resolve_application_mode(template_or_id, mode_id="SINGLE"):
    """Return a non-mutating template view for one hybrid delivery route."""
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if item is None:
        raise KeyError(f"Unknown template: {template_or_id}")
    if not item.get("application_modes"):
        resolved = dict(item)
        resolved["params"] = [dict(spec) for spec in item.get("params") or ()]
        resolved["channels"] = [dict(channel) for channel in item.get("channels") or ()]
        resolved["application_mode_id"] = "SINGLE"
        resolved["application_mode_label"] = "Single Property"
        return resolved
    modes = application_modes(item)
    mode = next((entry for entry in modes if entry["id"] == mode_id), None)
    if mode is None:
        # An id the recipe does not declare: a saved entry from before the
        # modes existed ("SINGLE"), or a removed mode. It keeps the behaviour
        # it was applied with -- the mode marked legacy_default -- and only
        # falls to the first mode when the recipe marks none.
        mode = next((entry for entry in modes if entry.get("legacy_default")), modes[0])
    resolved = dict(item)
    resolved["params"] = list(item.get("params") or ()) + [dict(spec) for spec in mode.get("params") or ()]
    resolved["channels"] = [dict(channel) for channel in mode.get("channels") or ()]
    if mode.get("expression"):
        resolved["expression"] = str(mode["expression"])
    if mode.get("requires_driver_variables"):
        resolved["requires_driver_variables"] = [
            dict(spec) for spec in mode["requires_driver_variables"]
        ]
    resolved["application_mode_id"] = mode["id"]
    resolved["application_mode_label"] = mode["label"]
    return resolved


def is_multi_expression(template_or_id):
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    return bool(item and len(item.get("channels") or []) > 1)


def has_motion_plan(template_or_id):
    """Return whether a template declares one or more destination channels."""
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    return bool(
        item
        and item.get("channels")
        and any(channel.get("data_path") for channel in item.get("channels", []))
    )


def category_icon(category):
    spec = _taxonomy.CATEGORY_SPEC.get(category)
    return spec["icon"] if spec else "NONE"


def templates_by_category(category):
    return [item for item in TEMPLATES if item["category"] == category]


def subcategories_for_category(category):
    """Declared subcategory order, filtered to those that hold templates."""
    order = _taxonomy.CATEGORY_SPEC.get(category, {}).get("subcategories", [])
    present = {item["subcategory"] for item in templates_by_category(category)}
    return [sub for sub in order if sub in present]


def templates_by_subcategory(category, subcategory):
    return [
        item
        for item in TEMPLATES
        if item["category"] == category and item["subcategory"] == subcategory
    ]


def template_traits(template_or_id):
    """Stored traits plus the ones derived from the template itself.

    Derived traits are never stored, so they cannot drift out of sync with the
    expression they describe.
    """
    from ...engine import utils  # local: utils imports this module at load time

    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if item is None:
        raise KeyError(f"Unknown template: {template_or_id}")

    traits = list(item.get("traits", ()))
    if item.get("requires_driver_variables"):
        traits.append(_taxonomy.TRAIT_NEEDS_VARIABLE)
    expressions = [channel["expression"] for channel in template_channels(item)]
    if any("FRAME_" in expression for expression in expressions):
        traits.append(_taxonomy.TRAIT_FRAME_BAKED)
    engine = item.get("motion_engine")
    if engine == _catalogue_contracts.COUPLED:
        traits.append(_taxonomy.TRAIT_INPUT_DRIVEN)
    elif engine == _catalogue_contracts.STATEFUL:
        traits.extend((
            _taxonomy.TRAIT_CREATES_RIG,
            _taxonomy.TRAIT_STATEFUL,
        ))
    elif engine == _catalogue_contracts.SPATIAL_FIELD:
        traits.append(_taxonomy.TRAIT_CREATES_NODES)
    if item.get("detach_mode") in {"BAKE_FCURVES", "BAKE_GEOMETRY"}:
        traits.append(_taxonomy.TRAIT_BAKE_AVAILABLE)
    return tuple(traits)


def template_index(template_id):
    for index, item in enumerate(TEMPLATES):
        if item["id"] == template_id:
            return index
    return 0


def channel_siblings(template_or_id):
    """Every template in the same channel set (roles), ordered for display.

    Returns ``[]`` when the template declares no ``role_set`` — the panel treats
    that as "no channel picker". Includes the template itself, so the caller can
    highlight the active member without a separate lookup.
    """
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    role_set = item.get("role_set") if item else None
    if not role_set:
        return []
    members = [t for t in TEMPLATES if t.get("role_set") == role_set]
    members.sort(key=lambda t: (t.get("role_order", 0), t["name"]))
    return members


def mode_siblings(template_or_id):
    """Members of one channel's mode group, ordered for the mode row.

    A mode group is two or more recipes that are the same CHANNEL done
    different ways -- Focus by distance or by target objects. They share a
    ``mode_set``; the channel row shows one button for the group and the mode
    row switches within it. Returns ``[]`` when the template declares no
    ``mode_set``, which is how the panel knows not to draw a mode row.
    """
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    mode_set = item.get("mode_set") if item else None
    if not mode_set:
        return []
    members = [t for t in TEMPLATES if t.get("mode_set") == mode_set]
    members.sort(key=lambda t: (t.get("mode_order", 0), t["name"]))
    return members


def channel_row_entries(template_or_id, active_id=""):
    """One entry per CHANNEL, with mode groups collapsed to one button.

    ``channel_siblings`` stays the full membership -- every part of the set,
    which is what "reset all parts" and the effect-id contract mean by it.
    This is the display list: a mode group contributes the member currently
    selected, so the channel button highlights while the mode row says which
    way it is being done.
    """
    siblings = channel_siblings(template_or_id)
    if not siblings:
        return []
    active_id = active_id or (
        template_or_id["id"] if isinstance(template_or_id, dict) else str(template_or_id)
    )
    entries, seen = [], set()
    for item in siblings:
        mode_set = item.get("mode_set")
        if not mode_set:
            entries.append(item)
            continue
        if mode_set in seen:
            continue
        seen.add(mode_set)
        group = mode_siblings(item)
        active = next((member for member in group if member["id"] == active_id), None)
        entries.append(active or group[0])
    return entries


def search_templates(text, category=None):
    query = (text or "").strip().lower()
    results = TEMPLATES if query else templates_by_category(category or CATEGORIES[0])
    if not query:
        return results
    matches = []
    for item in TEMPLATES:
        haystack = " ".join(
            [
                item["name"],
                item["category"],
                item.get("subcategory", ""),
                item.get("description", ""),
                item.get("use_cases", ""),
            ]
        ).lower()
        if query in haystack:
            matches.append(item)
    return matches
