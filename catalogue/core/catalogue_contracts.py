"""Explicit semantic contracts shared by the expression and apply pipelines.

The catalogue contains several kernels with deliberately different parameter names.
Meaning must therefore live in metadata keyed by template/channel identity, not in
guesses such as treating every token named ``START`` as an output baseline.
"""

from __future__ import annotations


BOUNDED_MIN = "BOUNDED_MIN"
CENTERED_ZERO = "CENTERED_ZERO"
ONE_SHOT_REST = "ONE_SHOT_REST"


# Values may be numbers or expressions evaluated after friendly/derived tokens
# resolve. Channel-specific rows take priority over the template primary row.
BASELINE_OVERRIDES = {
    ("transition_ease_in", "primary"): "A",
    ("transition_ease_out", "primary"): "A",
    ("transition_linear", "primary"): "A",
    ("transition_smoothstep", "primary"): "A",
}


# These were individually inspected after the heuristic audit. The remaining
# catalogue follows deterministic rules below (frame mode, output range,
# destination, and driver-variable requirements).
PROFILE_OVERRIDES = {
    ("candle_flicker", "primary"): BOUNDED_MIN,
}


def channel_key(template, channel=None):
    return template.get("id", ""), (channel or {}).get("id", template.get("_channel_id", "primary"))


def explicit_baseline(template, channel=None):
    """Return an explicit baseline contract or ``None`` for compatibility fallback."""
    key = channel_key(template, channel)
    if key in BASELINE_OVERRIDES:
        return BASELINE_OVERRIDES[key]
    if channel is not None and channel.get("output_baseline") is not None:
        return channel["output_baseline"]
    return template.get("output_baseline")


def has_ordered_output_range(template):
    """MIN/MAX is the catalogue's exact semantic output-bound pair.

    Input ranges (IN_MIN/IN_MAX) and directional/remap endpoints are excluded:
    reversing those can be an authored operation rather than a mistake.
    """
    tokens = {item.get("token") for item in template.get("params", [])}
    return {"MIN", "MAX"} <= tokens and not template.get("allow_reversed_output_range", False)


def resolve_additive_profile(template, channel=None):
    key = channel_key(template, channel)
    explicit = (channel or {}).get("additive_profile") or template.get("additive_profile")
    if explicit:
        return explicit
    if key in PROFILE_OVERRIDES:
        return PROFILE_OVERRIDES[key]

    if template.get("frame_mode") == "ENDPOINT_HIT":
        return ONE_SHOT_REST
    if has_ordered_output_range(template):
        return BOUNDED_MIN
    return CENTERED_ZERO

