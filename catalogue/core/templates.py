"""Template catalogue for Driver Espresso Lite.

This module is intentionally free of Blender imports so the expression data can
be validated outside Blender.
"""

from __future__ import annotations


PLAYBACK_WRAP = "PLAYBACK_WRAP"
ENDPOINT_HIT = "ENDPOINT_HIT"
CUSTOM_RANGE = "CUSTOM_RANGE"
FIXED_PERIOD = "FIXED_PERIOD"

ESPRESSO_INPUT_PREFIX = "espinp_"


# Each template's category, subcategory and traits come from taxonomy.py.
# CATEGORIES is derived from what is actually populated, so an empty category
# never reaches the UI enum.


def param(token, label, kind="FLOAT", default=0.0, minimum=None, maximum=None, tip="",
          presets=None, unit=None, derives=None):
    """Declare a template parameter.

    ``unit`` is a short suffix shown in the UI and tooltips ("s", "°", "frames").
    ``derives`` maps expression tokens to formulas over the other tokens plus the frame
    constants (FRAME_START/FRAME_END/FRAME_LEN/FPS). A parameter with ``derives`` is a
    friendly parameter: the user sets something they understand ("seconds per
    revolution") and the maths token the expression contains ("TURNS") is computed from
    it. The parameter's own token need not appear in the expression at all. Templates
    that declare no ``derives`` are unaffected; see ``utils.resolve_derived_tokens``.
    """
    if unit == "m" and "metre" not in tip.lower():
        tip = f"{tip.rstrip()} Measured in metres."
    data = {
        "token": token,
        "label": label,
        "type": kind,
        "default": default,
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

# The sine templates all drive an angle, so they take degrees. The shared AMPLITUDE
# above stays unit-less because the organic drift templates, the wandering drift
# templates and similar use it for plain values, where "radians" would be wrong. The
# conversion is done by `derives` (build time) rather than by wrapping the expression in
# radians() (run time), for two reasons: the built expression still contains a bare
# number, so it costs none of the 255-character budget, and AMPLITUDE becomes
# machine-derived, so the literal rounder leaves its precision alone. Nobody thinks in
# radians, so presets state the degrees in their own labels.
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

# Friendly clock parameters.
#
# The maths wants TICKS, TURNS, START_DEG and SPEED, but a clock is described as "60
# marks on the dial, one revolution a minute". The user sets seconds per revolution and
# TURNS is derived from it using the scene's frame rate, so the tick rate does not
# depend on scene length: a second hand ticks once per second in a 120-frame scene or a
# 6000-frame one.


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
ADVANCED_PARAMS = [
    ADV_START_DELAY,
    ADV_TIME_ADVANCE,
    ADV_START_FRAME,
    ADV_END_FRAME,
    ADV_LOOP_FIT,
    ADV_OUTPUT_MULT,
    ADV_OUTPUT_OFFSET,
]

ADVANCED_PARAM_BY_TOKEN = {item["token"]: item for item in ADVANCED_PARAMS}


from . import taxonomy as _taxonomy

# Records are frozen at build time, so only this build's definitions are
# present in the package; nothing is hidden behind a runtime flag.
from .frozen_records import RECORDS as _FROZEN_RECORDS

TEMPLATES = [dict(record) for record in _FROZEN_RECORDS]


# Only categories that actually hold templates reach the UI; a category with
# zero templates would produce an empty enum and break the panel.
_used_categories = {item["category"] for item in TEMPLATES}
CATEGORIES = [category for category in _taxonomy.CATEGORIES if category in _used_categories]
GENERIC_CATEGORIES = _taxonomy.GENERIC_CATEGORIES
is_generic_category = _taxonomy.is_generic_category


TEMPLATE_BY_ID = {item["id"]: item for item in TEMPLATES}

# Reverse of the registry: a code read back out of a .blend file resolves to the
# template that wrote it. This is the lookup the bake and clear operators use -
# they read a stamp, not an expression.
TEMPLATE_BY_EFFECT_ID = {item["effect_id"]: item for item in TEMPLATES}


def template_for_effect_id(code):
    """Resolve a stamped code back to its template, or None for a code this build does not have."""
    return TEMPLATE_BY_EFFECT_ID.get((code or "").strip().upper())


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
    }]


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


def template_traits(template_or_id):
    """Stored traits plus the ones derived from the template itself.

    Derived traits are never stored, so they cannot drift out of sync with the
    expression they describe.
    """
    item = template_or_id if isinstance(template_or_id, dict) else TEMPLATE_BY_ID.get(template_or_id)
    if item is None:
        raise KeyError(f"Unknown template: {template_or_id}")

    traits = list(item.get("traits", ()))
    if item.get("requires_driver_variables"):
        traits.append(_taxonomy.TRAIT_NEEDS_VARIABLE)
    expressions = [channel["expression"] for channel in template_channels(item)]
    if any("FRAME_" in expression for expression in expressions):
        traits.append(_taxonomy.TRAIT_FRAME_BAKED)
    if item.get("detach_mode") in {"BAKE_FCURVES", "BAKE_GEOMETRY"}:
        traits.append(_taxonomy.TRAIT_BAKE_AVAILABLE)
    return tuple(traits)


def template_index(template_id):
    for index, item in enumerate(TEMPLATES):
        if item["id"] == template_id:
            return index
    return 0


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
