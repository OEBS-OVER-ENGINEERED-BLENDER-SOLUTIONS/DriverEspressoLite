"""Explicit semantic contracts shared by the expression and apply pipelines.

The catalogue contains many kernels with deliberately different parameter names.
Meaning must therefore live in metadata keyed by template/channel identity, not in
guesses such as treating every token named ``START`` as an output baseline.
"""

from __future__ import annotations

import re

from ...engine.motion_stack import workflows


BOUNDED_MIN = "BOUNDED_MIN"
BOUNDED_MAX = "BOUNDED_MAX"
CENTERED_ZERO = "CENTERED_ZERO"
RELATIVE_SCALE = "RELATIVE_SCALE"
ONE_SHOT_REST = "ONE_SHOT_REST"
ELAPSED_TIME = "ELAPSED_TIME"
INPUT_RELATIVE = "INPUT_RELATIVE"
STATIC_UTILITY = "STATIC_UTILITY"

EXPRESSION = "EXPRESSION"
COUPLED = "COUPLED"
STATEFUL = "STATEFUL"
SPATIAL_FIELD = "SPATIAL_FIELD"
# A Geometry Nodes or compositor system attached to the artist's own data,
# varying per element or per pixel. Distinct from SPATIAL_FIELD, which is a
# driver reaching for that and not getting there.
SURFACE_MOTION = "SURFACE_MOTION"


def _response(engine, source, target, setup, detach, dependencies, acceptance):
    return {
        "motion_engine": engine,
        "source_kind": source,
        "target_kind": target,
        "generated_setup": setup,
        "detach_mode": detach,
        "dependencies": tuple(dependencies),
        "acceptance": tuple(acceptance),
    }


# Ground truth for every still-open P1/P2 finding in the promise audit.  This is
# intentionally explicit: names such as "slosh", "thermal", and "from crank"
# are product contracts, so the implementation may not silently fall back to a
# decorative frame loop when its causal source is unavailable.
REENGINEERING_CONTRACTS = {
    # Empty in this edition: the recipes that carried these
    # contracts are not on its shelf.
}


for _workflow in workflows.WORKFLOWS:
    _engine = COUPLED if _workflow.route == workflows.OBJECT_SET else SPATIAL_FIELD
    _setup = "MGX_%s_V1" % _workflow.effect_id
    _source = {
        workflows.OBJECT_SET: "STABLE_OBJECT_SET",
        workflows.PREPARED_FIELD: "PREPARED_LAYOUT",
        workflows.GENERATED_GRAPHIC: "SELECTED_SOURCE_OR_GENERATED_GRAPHIC",
    }[_workflow.route]
    _target = {
        workflows.OBJECT_SET: "OBJECT_TRANSFORMS",
        workflows.PREPARED_FIELD: "INSTANCED_GEOMETRY",
        workflows.GENERATED_GRAPHIC: (
            "GENERATED_OBJECTS_OR_TEXT"
            if _workflow.mode in {"GLYPH", "WORDS", "CALLOUT"}
            else "GENERATED_GEOMETRY"
        ),
    }[_workflow.route]
    REENGINEERING_CONTRACTS.setdefault(
        _workflow.template_id,
        _response(
            _engine, _source, _target, _setup,
            "BAKE_FCURVES" if _workflow.route == workflows.OBJECT_SET else "BAKE_GEOMETRY",
            ("owned_setup", "stable_effect_id"),
            ("default_reads_as_named_effect", "live_controls_rebuild_native_setup", "clear_removes_owned_resources"),
        ),
    )


# Compact, readable identities for generated resources.  These are deliberately
# catalogue-owned rather than hashes: an artist (and the Bake Applied Motion
# browser) can identify the effect without Driver Espresso having to reverse an
# opaque digest.  IDs remain stable if a display name changes.
#
# It covers every template rather than only those that generate a resource, so
# a code cannot be declared here and nowhere else: this value is written into
# the user's .blend file and needs one source of truth. The registry is now the
# only one; everything below reads from it.
from .effect_ids import EFFECT_IDS  # noqa: F401  (re-exported for callers)


def reengineering_contract(template_id):
    template_id = str(template_id or "")
    contract = REENGINEERING_CONTRACTS.get(template_id)
    if not contract:
        return {}
    return {**contract, "effect_id": EFFECT_IDS[template_id]}

SUPPORTED_ADDITIVE_PROFILES = frozenset({
    BOUNDED_MIN,
    BOUNDED_MAX,
    CENTERED_ZERO,
    RELATIVE_SCALE,
    ONE_SHOT_REST,
    ELAPSED_TIME,
    INPUT_RELATIVE,
    STATIC_UTILITY,
})


# Values may be numbers or expressions evaluated after friendly/derived tokens
# resolve. Channel-specific rows take priority over the template primary row.
BASELINE_OVERRIDES = {
    ("transition_ease_out", "primary"): "A",
    ("transition_linear", "primary"): "A",
    ("transition_smoothstep", "primary"): "A",
}


# These were individually inspected after the heuristic audit. The remaining
# catalogue follows deterministic rules below (frame mode, output range,
# destination, and driver-variable requirements).
PROFILE_OVERRIDES = {
    ("candle_flicker", "primary"): BOUNDED_MIN,
    # A thump STRIKES and decays back to rest; it must not pull the target
    # under its resting value. The expression is a damped sine, so the
    # fall-through rule inferred CENTERED_ZERO and added the signed value
    # unclamped - driving a light of strength 1 down to -19 at Thump amount
    # (they declare a MIN/MAX range, so has_ordered_output_range catches them);
    # this template exposes BPM/AMOUNT/DECAY instead and slipped through. Same
    # reasoning, and currently a no-op: this one is a sum of Gaussian bumps,
    # which cannot go negative, so it never undercut rest by luck of its
    # expression rather than by contract. Declared anyway - it is a strike, so
    # a later edit to that kernel cannot let it undercut rest unnoticed.
    # Verified identical output under either profile.
}


ELAPSED_TIME_KEYS = frozenset({
    ("second_hand", "primary"),
})



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
    if key in ELAPSED_TIME_KEYS:
        return ELAPSED_TIME

    expression = (channel or {}).get("expression", template.get("expression", ""))
    data_path = (channel or {}).get("data_path", template.get("data_path", ""))
    if template.get("requires_driver_variables"):
        return INPUT_RELATIVE
    if not re.search(r"\bframe\b", expression):
        return STATIC_UTILITY
    if data_path == "scale":
        return RELATIVE_SCALE
    if template.get("frame_mode") == "ENDPOINT_HIT":
        return ONE_SHOT_REST
    if has_ordered_output_range(template):
        return BOUNDED_MIN
    return CENTERED_ZERO


def default_application_space(channel):
    if channel.get("data_path") in {"location", "rotation_euler", "scale"}:
        return "RELATIVE"
    return "ABSOLUTE"
