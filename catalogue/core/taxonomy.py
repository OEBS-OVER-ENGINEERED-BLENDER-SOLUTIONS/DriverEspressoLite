"""Canonical taxonomy for Driver Espresso Lite - the single source of truth.

Categories describe **what the user is animating** (the outcome), not **which
equation is used**. Subcategories describe the behaviour within a domain. Traits
are orthogonal: they are *filters*, never folders.

Kept free of Blender imports so the whole catalogue can be validated outside
Blender, like ``templates.py`` and ``utils.py``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Traits - orthogonal filters. Only the ones that CANNOT be derived from the
# template itself are stored; the rest are computed (see templates.template_traits).
# --------------------------------------------------------------------------- #
TRAIT_LOOPABLE = "Loopable"
TRAIT_PERIODIC = "Periodic"
TRAIT_BOUNDED = "Bounded"

# Derived at runtime, never stored:
TRAIT_NEEDS_VARIABLE = "Needs Variable"      # from requires_driver_variables
TRAIT_FRAME_BAKED = "Frame-range baked"      # from "FRAME_" in the expression
TRAIT_BAKE_AVAILABLE = "Bake Available"       # from detach mode

# --------------------------------------------------------------------------- #
# Categories, in display order. Each maps to its ordered subcategories.
# --------------------------------------------------------------------------- #
SPIN = "Spin & Rotate"
SWING = "Swing & Oscillate"
EASE = "Ease & Spring"
TRIGGER = "Trigger & State"
LIGHT = "Light & Flicker"
RGBNEON = "RGB & Neon Lighting"

CATEGORY_SPEC = {
    SPIN:    {"icon": "FORCE_VORTEX",
              "subcategories": ["Continuous", "Eased"]},
    SWING:   {"icon": "IPO_SINE",
              "subcategories": ["Pendulum", "Waveform"]},
    EASE:    {"icon": "IPO_BEZIER",
              "subcategories": ["Ease"]},
    TRIGGER: {"icon": "SNAP_GRID",
              "subcategories": ["One-shot", "Sequence & Stagger"]},
    LIGHT:   {"icon": "LIGHT_SUN",
              "subcategories": ["Blink & Strobe", "Candle & Fire"]},
    RGBNEON: {"icon": "COLOR",
              "subcategories": ["Colour Cycling", "Sequenced & Chase", "Sparkle & Flicker"]},
}

CATEGORIES = list(CATEGORY_SPEC)

# Templates come in two kinds, and the distinction is worth recording even
# though the category dropdown does not draw it (Blender starts a new column at
# any enum separator, which left a large gap next to the shorter group).
#
# A GENERIC template is a building block: it works on any property and produces
# a shape - a ramp, an oscillation, a remap. The artist supplies the meaning.
#
# A TAILORED template encodes one real-world behaviour, and its parameters are
# named for that behaviour rather than for the maths.
#
# Categories are declared generic-first, so this only needs the boundary.
GENERIC_CATEGORIES = (SPIN, SWING, EASE, TRIGGER)


def is_generic_category(category):
    return category in GENERIC_CATEGORIES


# --------------------------------------------------------------------------- #
# Every template's id -> (category, subcategory, traits).
# --------------------------------------------------------------------------- #
_L = TRAIT_LOOPABLE
_P = TRAIT_PERIODIC
_B = TRAIT_BOUNDED

TEMPLATE_TAXONOMY = {
    # ---- Spin & Rotate -----------------------------------------------------
    "constant_speed":      (SPIN, "Continuous", ()),
    "scene_loop":          (SPIN, "Continuous", (_L,)),
    "loop_n_frames":       (SPIN, "Continuous", (_L,)),
    "modulo_loop":         (SPIN, "Continuous", (_L,)),
    "ease_in_rotation":    (SPIN, "Eased", ()),

    # ---- Swing & Oscillate -------------------------------------------------
    "sine_osc":            (SWING, "Pendulum", (_P,)),
    "sawtooth":            (SWING, "Waveform", (_P,)),
    "triangle_wave":       (SWING, "Waveform", (_P,)),

    # ---- Ease & Spring -----------------------------------------------------
    "transition_linear":       (EASE, "Ease", (_B,)),
    "transition_ease_in":      (EASE, "Ease", (_B,)),
    "transition_ease_out":     (EASE, "Ease", (_B,)),
    "transition_smoothstep":   (EASE, "Ease", (_B,)),

    # ---- Trigger & State ---------------------------------------------------
    "fade_in":                 (TRIGGER, "One-shot", (_B,)),
    "fade_out":                (TRIGGER, "One-shot", (_B,)),
    "pulse_repeat":            (TRIGGER, "Sequence & Stagger", (_P, _B)),

    # ---- Light & Flicker ---------------------------------------------------
    "simple_blink":     (LIGHT, "Blink & Strobe", (_P, _B)),
    "candle_flicker":   (LIGHT, "Candle & Fire", (_B,)),

    # ---- RGB & Neon Lighting -----------------------------------------------
    "rgb_colour_cycle": (RGBNEON, "Colour Cycling", (_L, _P, _B)),
    "rgb_chase":        (RGBNEON, "Sequenced & Chase", (_L, _P, _B)),
    "rgb_twinkle":      (RGBNEON, "Sparkle & Flicker", (_P, _B)),
}
