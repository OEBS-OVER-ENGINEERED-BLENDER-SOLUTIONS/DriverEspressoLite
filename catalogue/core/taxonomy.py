"""Canonical taxonomy for Driver Espresso — the single source of truth.

Categories describe **what the user is animating** (the outcome), not **which
equation is used**. Subcategories describe the behaviour within a domain. Traits
are orthogonal: they are *filters*, never folders.

Two of the old categories were mechanisms rather than outcomes and have been
dissolved into traits:

* ``Seamless Loop``   → the ``Loopable`` trait; members refiled by outcome.
* ``Property Control`` → the ``Needs Variable`` trait; members split across
  Remap & Blend, Trigger & State and Rig Utilities.

Kept free of Blender imports so the whole catalogue can be validated outside
Blender, like ``templates.py`` and ``utils.py``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Traits — orthogonal filters. Only the ones that CANNOT be derived from the
# template itself are stored; the rest are computed (see templates.template_traits).
# --------------------------------------------------------------------------- #
TRAIT_LOOPABLE = "Loopable"
TRAIT_PERIODIC = "Periodic"
TRAIT_BOUNDED = "Bounded"

# Derived at runtime, never stored:
TRAIT_NEEDS_VARIABLE = "Needs Variable"      # from requires_driver_variables
TRAIT_FRAME_BAKED = "Frame-range baked"      # from "FRAME_" in the expression
TRAIT_INPUT_DRIVEN = "Input Driven"           # from COUPLED engine metadata
TRAIT_CREATES_RIG = "Creates Rig"             # from STATEFUL generated setup
TRAIT_CREATES_NODES = "Creates Nodes"         # from SPATIAL_FIELD engine metadata
TRAIT_STATEFUL = "Stateful"                   # from STATEFUL engine metadata
TRAIT_BAKE_AVAILABLE = "Bake Available"       # from detach mode

STORED_TRAITS = (TRAIT_LOOPABLE, TRAIT_PERIODIC, TRAIT_BOUNDED)
DERIVED_TRAITS = (
    TRAIT_NEEDS_VARIABLE,
    TRAIT_FRAME_BAKED,
    TRAIT_INPUT_DRIVEN,
    TRAIT_CREATES_RIG,
    TRAIT_CREATES_NODES,
    TRAIT_STATEFUL,
    TRAIT_BAKE_AVAILABLE,
)
ALL_TRAITS = STORED_TRAITS + DERIVED_TRAITS

# --------------------------------------------------------------------------- #
# Categories, in display order. Each maps to its ordered subcategories.
# --------------------------------------------------------------------------- #
SPIN = "Spin & Rotate"
SWING = "Swing & Oscillate"
EASE = "Ease & Spring"
TRIGGER = "Trigger & State"
TIME = "Time & Clocks"
NOISE = "Noise & Randomize"
PATH = "Path & Orbit"
REMAP = "Remap & Blend"
RIG = "Rig Utilities"
DYNAMICS = "Dynamics & Follow-Through"
WIND = "Wind & Nature"
WEATHER = "Weather & Atmosphere"
WEAPON = "Weapons & Impacts"
LIGHT = "Light & Flicker"
RGBNEON = "RGB & Neon Lighting"
ENERGY = "Fire & Energy"
MACHINE = "Mechanical & Industrial"
VEHICLE = "Vehicles & Transport"
CHARACTER = "Characters & Creatures"
ARCHITECTURE = "Architecture & Household"
AUDIO = "Audio & Rhythm"
ROBOTICS = "Robotics & Electronics"
CAMERA = "Camera & Cinematic"
TEXT = "Text & Counters"

CATEGORY_SPEC = {
    SPIN:    {"icon": "FORCE_VORTEX",
              "subcategories": ["Continuous", "Eased", "Stepped", "Geared"]},
    SWING:   {"icon": "IPO_SINE",
              "subcategories": ["Pendulum", "Bob & Float", "Breathe", "Wobble", "Waveform"]},
    EASE:    {"icon": "IPO_BEZIER",
              "subcategories": ["Ease", "Bounce", "Spring & Elastic", "Damping"]},
    TRIGGER: {"icon": "SNAP_GRID",
              "subcategories": ["Threshold", "Gate & Latch", "One-shot", "Delay", "Sequence & Stagger"]},
    NOISE:   {"icon": "RNDCURVE",
              "subcategories": ["Drift", "Seeded Random", "Layered Noise"]},
    REMAP:   {"icon": "ARROW_LEFTRIGHT",
              "subcategories": ["Remap", "Clamp", "Curve Shape", "Blend & Mix"]},
    PATH:    {"icon": "CURVE_PATH",
              "subcategories": ["Orbit", "Spiral", "Figure & Lissajous"]},
    RIG:     {"icon": "DRIVER",
              "subcategories": ["Correctives", "Constraint Drivers", "Multi-object Stagger"]},
    DYNAMICS:{"icon": "PHYSICS",
              "subcategories": ["Settle & Overshoot", "Squash & Stretch", "Hanging & Mounted", "Secondary Motion"]},
    TIME:    {"icon": "TIME",
              "subcategories": ["Analog Hands", "Digital Display", "Dials & Gauges",
                                "Stopwatch & Timer", "Countdown", "Metronome & Tick"]},
    WIND:    {"icon": "FORCE_WIND",
              "subcategories": ["Gust", "Foliage", "Flag & Cloth", "Water & Ripple"]},
    WEATHER: {"icon": "WORLD",
              "subcategories": ["Rain", "Snow", "Clouds & Fog", "Storm", "Heat"]},
    WEAPON:  {"icon": "TRACKING",
              "subcategories": ["Muzzle Flash", "Recoil", "Ejection & Tracer", "Impacts", "Energy Weapons"]},
    LIGHT:   {"icon": "LIGHT_SUN",
              # "Neon & Sign" retired: every member moved to RGB & Neon Lighting,
              # so the shelf emptied. Leaving an empty subcategory would show the
              # artist a heading with nothing under it.
              # Spatial Fields: templates that turn an object's own POSITION
              # into a value - a wave along an axis, a sweep about a centre,
              # noise, plasma. They output one number rather than a colour,
              # which is why they live here and not in RGB & Neon Lighting.
              "subcategories": ["Blink & Strobe", "Candle & Fire", "Spark & Electric",
                                "Beacon & Emergency", "Spatial Fields"]},
    RGBNEON: {"icon": "COLOR",
              # "Spatial Fields" retired here: all seven members moved to
              # Light & Flicker. They drive one number, not a colour, and an
              # empty shelf shows the artist a heading with nothing under it.
              "subcategories": ["Colour Cycling", "Sequenced & Chase",
                                "Sparkle & Flicker", "Neon Tubes"]},
    ENERGY:  {"icon": "LIGHT_POINT",
              "subcategories": ["Flames", "Embers", "Sparks & Arcs", "Power & Reactor"]},
    MACHINE: {"icon": "MODIFIER",
              "subcategories": ["Gears & Linkages", "Belts & Presses", "Vibration", "Cyclic Machines",
                                "Dials & Gauges"]},
    VEHICLE: {"icon": "AUTO",
              "subcategories": ["Engine & Drivetrain", "Suspension & Body", "Rail & Rotor", "Controls & Wheels"]},
    CHARACTER:{"icon": "ARMATURE_DATA",
              "subcategories": ["Breathing & Pulse", "Locomotion", "Head & Eyes", "Appendages"]},
    ARCHITECTURE:{"icon": "HOME",
              "subcategories": ["Doors & Drawers", "Cabins & Fixtures", "Fabric & Appliances", "Fans"]},
    AUDIO:   {"icon": "SPEAKER",
              "subcategories": ["Audio Reactive", "Beat & Percussion", "Bass & Modulation", "Mix & Accent"]},
    ROBOTICS:{"icon": "OUTLINER_OB_LIGHT",
              "subcategories": ["Servo & Scan", "Cooling"]},
    CAMERA:  {"icon": "CAMERA_DATA",
              "subcategories": ["Focus & Optics", "Framing & Subject", "Deliberate Travel",
                                "Operated & Supported", "Platform & Environment",
                                "Handheld", "Focus & Exposure", "Dolly & Projector",
                                "Operator Motion", "Mounted & Vehicle", "Environmental Rigs",
                                "Sci-Fi & Stabilized"]},
    TEXT:    {"icon": "FONT_DATA",
              "subcategories": ["Numeric Readout", "Clock Readout", "Counter & Score", "Number Wheel"]},
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
# named for that behaviour rather than for the maths: a clock has hands and a
# start time, a conveyor has belt speed and a roller radius. Its value is the
# judgement baked into it, which is why two tailored templates should never be
# reachable from one another.
#
# Categories are declared generic-first, so this only needs the boundary.
GENERIC_CATEGORIES = (SPIN, SWING, EASE, TRIGGER, NOISE, REMAP, PATH, RIG)


def is_generic_category(category):
    return category in GENERIC_CATEGORIES

# --------------------------------------------------------------------------- #
# Every template's id → (category, subcategory, traits).
# --------------------------------------------------------------------------- #
_L = TRAIT_LOOPABLE
_P = TRAIT_PERIODIC
_B = TRAIT_BOUNDED

TEMPLATE_TAXONOMY = {
    # ---- Definitive Camera & Cinematic catalogue --------------------------
    # ---- Spin & Rotate -----------------------------------------------------
    "constant_speed":      (SPIN, "Continuous", ()),
    "scene_loop":          (SPIN, "Continuous", (_L,)),
    "loop_n_frames":       (SPIN, "Continuous", (_L,)),
    "modulo_loop":         (SPIN, "Continuous", (_L,)),

    # ---- Swing & Oscillate -------------------------------------------------
    "sine_osc":            (SWING, "Pendulum", (_P,)),
    # A bio-pulse belongs with the creatures, not among the wave shapes. Safe to
    # far beyond creatures.
    "sawtooth":            (SWING, "Waveform", (_P,)),
    "triangle_wave":       (SWING, "Waveform", (_P,)),

    # ---- Ease & Spring -----------------------------------------------------
    "transition_linear":       (EASE, "Ease", (_B,)),
    "transition_ease_out":     (EASE, "Ease", (_B,)),
    "transition_smoothstep":   (EASE, "Ease", (_B,)),

    # ---- Trigger & State ---------------------------------------------------
    "fade_in":                 (TRIGGER, "One-shot", (_B,)),
    "fade_out":                (TRIGGER, "One-shot", (_B,)),
    "pulse_repeat":            (TRIGGER, "Sequence & Stagger", (_P, _B)),

    # ---- Light & Flicker ---------------------------------------------------
    "simple_blink":     (LIGHT, "Blink & Strobe", (_P, _B)),
    "candle_flicker":   (LIGHT, "Candle & Fire", (_B,)),

    # ---- Time & Clocks -----------------------------------------------------
    # and pressure needles. Filing it with the clocks is what put "12 o'clock"
    # dial really is a clock.

    # ---- Wind & Nature ----------------------------------------------------
    # The definitive surface-motion systems. Traits describe the motion the
    # artist gets, the same as any other entry -- these are Geometry Nodes and
    # compositor recipes, but the taxonomy is about what the result does.

    # ---- Weather & Atmosphere --------------------------------------------

    # ---- Weapons & Impacts -----------------------------------------------

    # ---- Fire & Energy ----------------------------------------------------

    # ---- Mechanical & Industrial -----------------------------------------

    # ---- Camera & Cinematic ----------------------------------------------

    # ---- Noise & Randomize -------------------------------------------------
    # brightness flicker, not a transform jitter; it was the only "Jitter" entry.

    # ---- Path & Orbit ------------------------------------------------------

    # ---- Remap & Blend -----------------------------------------------------

    # the six essential remap utilities every build carries

    # advanced property-driven control shaping — the Pro boundary


    # ---- Rig Utilities -----------------------------------------------------
}

# Broad real-world recipes and the camera motion systems.
TEMPLATE_TAXONOMY.update({
    # Dynamics & Follow-Through (8)

    # Vehicles & Transport (10)

    # Characters & Creatures (10)

    # Architecture & Household (18)
    "rgb_colour_cycle": (RGBNEON, "Colour Cycling", (_L, _P, _B)),
    "rgb_chase": (RGBNEON, "Sequenced & Chase", (_L, _P, _B)),
    "rgb_twinkle": (RGBNEON, "Sparkle & Flicker", (_P, _B)),
})


def validate(template_ids):
    """Raise if the taxonomy and the catalogue have drifted apart.

    Called at import time from templates.py, so a renamed or added template can
    never silently escape the taxonomy.
    """
    known = set(TEMPLATE_TAXONOMY)
    catalogue = set(template_ids)

    missing = catalogue - known
    if missing:
        raise ValueError(f"templates missing a taxonomy entry: {sorted(missing)}")

    orphaned = known - catalogue
    if orphaned:
        raise ValueError(f"taxonomy entries with no template: {sorted(orphaned)}")

    for tid, (category, subcategory, traits) in TEMPLATE_TAXONOMY.items():
        if category not in CATEGORY_SPEC:
            raise ValueError(f"{tid}: unknown category {category!r}")
        if subcategory not in CATEGORY_SPEC[category]["subcategories"]:
            raise ValueError(f"{tid}: {subcategory!r} is not a subcategory of {category!r}")
        for trait in traits:
            if trait not in STORED_TRAITS:
                raise ValueError(f"{tid}: {trait!r} is not a stored trait (derived ones are computed)")
