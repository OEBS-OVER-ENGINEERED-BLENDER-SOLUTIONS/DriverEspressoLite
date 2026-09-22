"""Named preset blocks - one place that decides which templates get which presets.

Presets live here rather than inside the shared module-level parameter
constants (``MARKS``, ``START_POS``, ``SPEED``, …). Membership was therefore
implicit: a template that reused ``START_POS`` silently inherited clock
vocabulary, which is how Gauge Ticks ended up offering "12 o'clock" and "Hour
hand (12 h)" for a fuel gauge. The only escape was to hand-copy a replacement
list into ``override()``, which duplicates data and hides the relationship.

A block turns that around. It names a preset set once, states the token it fills,
and lists exactly which templates receive it:

    SWING_REST_ANGLES = block(
        "swing_rest_angles",
        token="START_DEG",
        presets=[("Bottom-left / empty (-135°)", -135.0), ...],
        templates=["sine_osc"],
    )

Two blocks may target the same token for different templates - that is the whole
point, and it is what the old model could not express. To include another
template, add its id to ``templates``.

Blocks are applied over whatever the parameter constant already carried, so
migration is incremental: an unmigrated parameter keeps working untouched.
``apply_blocks`` raises on an unknown template, a template missing that
parameter, or two blocks claiming one slot, and reports - without raising - the
slots no block owns yet.
"""

from __future__ import annotations


class PresetBlockError(Exception):
    """A block is wired to something that does not exist or is ambiguous."""


def block(name, token, presets, templates, note=""):
    """Declare a named preset set and the templates that receive it.

    ``presets`` is a list of ``(label, value)`` pairs - the tuple form keeps the
    registry readable at a glance, and is expanded to the dict shape the UI wants.
    """
    return {
        "name": name,
        "token": token,
        "presets": [{"label": label, "value": value} for label, value in presets],
        "templates": list(templates),
        "note": note,
    }


# ---------------------------------------------------------------------------
# The registry. Add a template id to a block's ``templates`` list to give that
# template the block's presets.
# ---------------------------------------------------------------------------

# --- registry start (hand-editable; seeded by migrating the live catalogue) ---

SPEED_MULTIPLIERS = block(
    "speed_multipliers",
    token="SPEED",
    presets=[
        ("Half speed", 0.5),
        ("Normal", 1.0),
        ("Double speed", 2.0),
        ("Quadruple", 4.0),
    ],
    templates=[
        "constant_speed",
        "scene_loop",
        # loop_n_frames deliberately absent: its Frames control IS the rate, so
        # a Speed multiplier alongside gave two knobs for one quantity.
        "sine_osc",
        "transition_linear",
        "transition_smoothstep",
        "transition_ease_out",
        "pulse_repeat",
        "fade_in",
        "fade_out",
        "sawtooth",
        "triangle_wave",
    ],
    note="The catalogue-wide speed multiplier set.",
)

LOOP_COUNTS = block(
    "loop_counts",
    token="N",
    presets=[
        ("Once", 1),
        ("Twice", 2),
        ("Three times", 3),
        ("Five times", 5),
    ],
    templates=[
        "scene_loop",
    ],
    note="How many times a motion repeats.",
)

SWING_ANGLES = block(
    "swing_angles",
    token="SWING_DEG",
    presets=[
        ("Subtle", 15.0),
        ("Natural", 30.0),
        ("Wide", 45.0),
        ("Quarter turn", 90.0),
    ],
    templates=[
        "sine_osc",
    ],
    note="Oscillation amplitude in degrees.",
)

CYCLE_SECONDS = block(
    "cycle_seconds",
    token="PERIOD",
    presets=[
        ("Half second (12)", 12),
        ("One second (24)", 24),
        ("Two seconds (48)", 48),
        ("Four seconds (96)", 96),
    ],
    templates=[
        "pulse_repeat",
        "sawtooth",
        "triangle_wave",
        "simple_blink",
        # sweep: it now runs on a regulated flashes-per-minute rate instead.
    ],
    note="Cycle length in seconds. See CYCLE_SECONDS_WITH_FRAMES - the two differ only in whether frame counts are shown.",
)

BLOCKS = [
    SPEED_MULTIPLIERS,
    LOOP_COUNTS,
    SWING_ANGLES,
    CYCLE_SECONDS,
]

# --- registry end ---


# ---------------------------------------------------------------------------
# Application + validation
# ---------------------------------------------------------------------------

def apply_blocks(templates, blocks=None):
    """Attach every block's presets to its templates. Returns a report.

    Must run on the *unfiltered* catalogue. Filtering for Lite removes Pro-only
    templates, and a block naming one would otherwise look like a typo.

    Raises ``PresetBlockError`` for the failures the old model could not detect:
      * a template id that does not exist
      * a template that has no parameter with that token
      * two blocks claiming the same (template, token)
      * a block that reaches nothing

    Slots no block owns are *not* an error - inline ``presets=`` remains legal.
    They are listed in the report so the gap stays visible.
    """
    blocks = BLOCKS if blocks is None else blocks
    by_id = {item["id"]: item for item in templates}

    claimed = {}
    problems = []
    applied = 0

    for entry in blocks:
        if not entry["templates"]:
            problems.append(f'block "{entry["name"]}" lists no templates')
            continue
        for template_id in entry["templates"]:
            item = by_id.get(template_id)
            if item is None:
                problems.append(
                    f'block "{entry["name"]}" names unknown template "{template_id}"'
                )
                continue
            slot = (template_id, entry["token"])
            if slot in claimed:
                problems.append(
                    f'"{template_id}" parameter {entry["token"]} claimed by both '
                    f'"{claimed[slot]}" and "{entry["name"]}"'
                )
                continue
            params = item.get("params") or []
            positions = [i for i, p in enumerate(params) if p.get("token") == entry["token"]]
            if not positions:
                problems.append(
                    f'block "{entry["name"]}" targets {entry["token"]}, but template '
                    f'"{template_id}" has no such parameter'
                )
                continue
            claimed[slot] = entry["name"]
            for position in positions:
                # Swap in a private copy before writing. Parameter dicts are
                # shared by reference across templates - SPEED is ONE object used
                # by 59 of them - so setting presets in place would rewrite every
                # template sharing it, handing them another template's presets.
                # Cloning here is what makes a per-template block actually
                # per-template, and it is the whole reason this module exists.
                clone = dict(params[position])
                if clone.get("derives"):
                    clone["derives"] = dict(clone["derives"])
                clone["presets"] = [dict(preset) for preset in entry["presets"]]
                params[position] = clone
                applied += 1

    if problems:
        raise PresetBlockError(
            "Preset block wiring is invalid:\n  - " + "\n  - ".join(problems)
        )

    unclaimed = sorted(
        f'{item["id"]}::{p["token"]}'
        for item in templates
        for p in item.get("params", [])
        if p.get("presets") and (item["id"], p["token"]) not in claimed
    )
    return {"applied": applied, "claimed": len(claimed), "unclaimed": unclaimed}


def format_report(report):
    """Human-readable summary. Not printed automatically.

    Every unmigrated parameter is 'unclaimed' by design, so printing this at
    registration would bury real messages under ~136 lines of expected noise.
    Call it when auditing.
    """
    lines = [
        f'preset blocks: {report["claimed"]} slots claimed, '
        f'{len(report["unclaimed"])} still defined inline',
    ]
    lines += [f"  inline: {slot}" for slot in report["unclaimed"]]
    return "\n".join(lines)
