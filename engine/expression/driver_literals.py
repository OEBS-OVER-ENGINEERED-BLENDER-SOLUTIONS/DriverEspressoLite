"""Blender-free numeric literal formatting for driver expressions."""

from __future__ import annotations

import math
import struct


def format_driver_literal(value):
    """Return the shortest literal that preserves Blender's numeric value."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)

    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Driver expression values must be finite numbers.")
    if value == 0.0:
        return "-0" if math.copysign(1.0, value) < 0.0 else "0"

    # Blender FloatProperty values arrive as float32 widened to Python float64.
    # Preserve their exact float32 bits without exposing the widened noise.
    try:
        packed = struct.pack("!f", value)
        float32_value = struct.unpack("!f", packed)[0]
    except OverflowError:
        return repr(value)

    if value != float32_value:
        if value.is_integer():
            integer = str(int(value))
            if float(integer) == value and len(integer) <= len(repr(value)):
                return integer
        return repr(value)

    candidates = []
    for precision in range(1, 10):
        candidate = format(float32_value, f".{precision}g")
        if "e" in candidate.lower():
            mantissa, exponent = candidate.lower().split("e", 1)
            candidate = f"{mantissa}e{int(exponent)}"
        try:
            candidate_packed = struct.pack("!f", float(candidate))
        except OverflowError:
            continue
        if candidate_packed == packed:
            candidates.append(candidate)
            break

    if float32_value.is_integer():
        integer = str(int(float32_value))
        if struct.pack("!f", float(integer)) == packed:
            candidates.append(integer)
    if not candidates:
        raise ValueError(f"Could not preserve float32 value {value!r}.")
    return min(
        candidates,
        key=lambda item: (len(item), "e" in item.lower(), item),
    )


def round_parameter(value):
    """Round a user-chosen parameter to a clean, readable precision.

    Dragging a slider lands on arbitrary float32 values such as
    0.36999997496604919, which serialise to eight ugly digits and waste the
    255-character driver budget for no visible benefit.

    Decimals scale with magnitude rather than truncating significant digits, so
    large constants keep their integrity (43758.5453 -> 43758.55, not 43760):

        |value| >= 10   ->  2 decimals
        1 <= |value| < 10  ->  3 decimals
        |value| < 1     ->  4 decimals

    Safety net: a non-zero value never rounds away to zero. If the rule would
    flatten it, enough significant figures are kept to preserve it.

    Only ever applied to PARAMETER values. Rest-start anchors, gains and
    reference values stay exact: they are measured from the scene to guarantee
    continuity, not chosen by the user, and rounding them would reintroduce the
    apply-time jump they exist to prevent.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number) or number == 0.0:
        return value

    magnitude = abs(number)
    if magnitude >= 10.0:
        decimals = 2
    elif magnitude >= 1.0:
        decimals = 3
    else:
        decimals = 4

    rounded = round(number, decimals)
    if rounded == 0.0:
        # Would have vanished - keep four significant figures instead.
        decimals = 4 - (math.floor(math.log10(magnitude)) + 1)
        rounded = round(number, decimals)
    return int(rounded) if float(rounded).is_integer() else rounded


def format_computed_literal(value):
    """Shortest literal for a value the engine CALCULATED, not one it measured.

    Bounded gains and derived references are float64 results of a division or a
    sum. When such a value is not exactly representable in float32,
    ``format_driver_literal`` falls back to ``repr()`` and emits all seventeen
    significant digits - ``1.0291846714942001`` for a gain of roughly 1.03.
    Two of those in one expression is 36 characters, and rgb_colour_cycle blew
    past Blender's 255-character ceiling on exactly that.

    The digits beyond float32 cannot survive anyway: the driver result is
    written into a float32 property, so anything finer than about seven
    significant figures is discarded on assignment. Snapping the value to
    float32 first therefore changes no rendered result while letting the normal
    shortest-round-trip search do its job.

    NOT for measured values. ``round_parameter`` documents why rest-start
    anchors and scene readings stay exact - rounding those reintroduces the
    apply-time jump they exist to prevent. This function is different in kind:
    a measured float32 snaps to itself, so passing one through is a no-op,
    while a computed float64 loses only the digits the property cannot store.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return format_driver_literal(value)
    if not math.isfinite(number):
        return format_driver_literal(value)
    try:
        snapped = struct.unpack("!f", struct.pack("!f", number))[0]
    except (OverflowError, struct.error):
        return format_driver_literal(value)
    return format_driver_literal(snapped)
