"""Named colour palettes for the ramp templates.

The ramp templates promise unlimited colours, and delivered a ramp with four
seeded stops and no way to get a LOOK out of it without building one stop at a
time. Choosing "Rainbow" or "Fire" is what an artist actually wants to do, and
it is the same problem master presets already solve for parameters - a named
starting point beats an empty canvas.

Each palette carries its own interpolation, because that is part of the look
rather than a separate decision: a rainbow that steps is not a rainbow, and
Christmas lights that crossfade are not Christmas lights.

Colours are linear-space RGB, which is what a Colour Ramp stop stores. They
were picked to read correctly on an emissive surface rather than to match a
swatch on paper - these end up on lights and neon far more often than on
diffuse material.
"""

from __future__ import annotations

CONSTANT = "CONSTANT"
LINEAR = "LINEAR"


def _palette(identifier, label, description, interpolation, colours):
    return {
        "id": identifier,
        "label": label,
        "description": description,
        "interpolation": interpolation,
        "colours": tuple(colours),
    }


PALETTES = (
    # ---- sweeps: colours that belong in a gradient -----------------------
    _palette(
        "rainbow", "Rainbow", "Full hue sweep, red through violet and back", LINEAR,
        [(1.0, 0.0, 0.0), (1.0, 0.5, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
         (0.0, 0.6, 1.0), (0.25, 0.0, 1.0), (1.0, 0.0, 0.0)],
    ),
    _palette(
        "fire", "Fire", "Black through red and orange to a white core", LINEAR,
        [(0.02, 0.0, 0.0), (0.55, 0.05, 0.0), (1.0, 0.25, 0.0),
         (1.0, 0.65, 0.1), (1.0, 0.95, 0.7)],
    ),
    _palette(
        "ice", "Ice", "Deep blue through cyan to white", LINEAR,
        [(0.0, 0.05, 0.25), (0.0, 0.35, 0.75), (0.2, 0.75, 1.0), (0.85, 0.97, 1.0)],
    ),
    _palette(
        "greyscale", "Greyscale", "Black to white, for masks and factors", LINEAR,
        [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
    ),

    # ---- steps: colours meant to be told apart ---------------------------
    _palette(
        "primary", "Primary", "Red, green and blue in hard steps", CONSTANT,
        [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.15, 1.0)],
    ),
)

PALETTE_BY_ID = {palette["id"]: palette for palette in PALETTES}

# Built once at import. A Blender EnumProperty whose items come from a callback
# that rebuilds the list every draw can free the strings still being displayed
# and take the process with it, so the tuple is created here and handed over
# unchanged.
ENUM_ITEMS = tuple(
    (palette["id"], palette["label"], palette["description"])
    for palette in PALETTES
)


def apply_to_ramp(ramp, palette_id):
    """Rewrite ``ramp`` to this palette, spread evenly across 0..1.

    Returns False for an unknown id rather than raising: this is driven from a
    UI enum, and a stale id in a saved file should not break the panel.
    """
    palette = PALETTE_BY_ID.get(palette_id)
    if palette is None or ramp is None:
        return False

    colours = palette["colours"]
    elements = ramp.elements

    # Blender keeps at least one stop, so the list is grown or trimmed to
    # length rather than emptied and rebuilt.
    while len(elements) > len(colours):
        elements.remove(elements[len(elements) - 1])
    while len(elements) < len(colours):
        elements.new(1.0)

    last = max(len(colours) - 1, 1)
    for index, colour in enumerate(colours):
        element = elements[index]
        element.position = index / last
        element.color = (colour[0], colour[1], colour[2], 1.0)

    ramp.interpolation = palette["interpolation"]
    return True
