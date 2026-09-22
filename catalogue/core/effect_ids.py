"""Frozen effect-ID registry - the identity of every applied motion.

GENERATED, then owned. Codes are stamped into user .blend files, so a code
here is a permanent contract: never edit or reorder an existing entry. New
templates get appended by tools/generate_effect_ids; that script preserves
everything already listed.

Form: STEM + two digits. 00 means the template has no channels; 01.. are
channel indices within one family. A contested stem gains a digit (RP, RP2,
RP3), which is why 29 of these look odd - those stems are genuinely shared.
"""

from __future__ import annotations


EFFECT_IDS = {
    "candle_flicker":                              "CF200",
    "constant_speed":                              "CS00",
    "transition_ease_out":                         "EOT00",
    # SFO/SWA/SPR are already taken elsewhere, so the stems below avoid them.
    "loop_n_frames":                               "LNF00",
    "transition_linear":                           "LT00",
    "modulo_loop":                                 "MRL00",
    "pulse_repeat":                                "RP200",
    "simple_blink":                                "SB00",
    "fade_in":                                     "SF00",
    "fade_out":                                    "SFO00",
    "scene_loop":                                  "SLL00",
    "sine_osc":                                    "SO00",
    "transition_smoothstep":                       "ST200",
    "sawtooth":                                    "SW00",
    "triangle_wave":                               "TW200",
    "rgb_colour_cycle":                            "RCC00",
    "rgb_chase":                                   "RMC00",
    "rgb_twinkle":                                 "RT00",
    # Definitive camera catalogue. Appended as permanent identities; the
    # legacy 39-recipe compatibility shelf retains all of its original codes.
    # The Lens channel set: one stem, one channel number per member, in the
    # order the Channel row shows them. Their earlier lone codes are aliased
    # below so stamps written by older builds still resolve.
}


# Retired codes -> the code that replaced them. A template that gains
# channels moves from STEM00 to STEM01, and its old code is recorded here
# so a stamp written by an earlier build still resolves.
#
# An alias must point at a code THIS product actually has. One that does not
# is worse than no alias: it makes a stamp look resolvable and then hands
# back None. When a recipe is retired, its aliases go with it rather than
# being re-pointed at whatever replaced it on the shelf -- a stamp that
# resolves to the wrong motion is not a migration, it is a mislabel.
ALIASES: dict[str, str] = {
}
