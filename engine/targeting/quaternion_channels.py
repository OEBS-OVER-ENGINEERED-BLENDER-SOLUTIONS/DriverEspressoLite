"""Turn a template's Euler rotation channels into real quaternion channels.

Pose bones default to Quaternion rotation in Blender, and a ``rotation_euler``
driver on such a bone is silently inert - it applies without error and the bone
never moves. Every rotation template in the catalogue is authored in Euler, so
the whole rotation half of the catalogue was unusable on a normal rig bone.

Two simpler routes do not work:

* **Relay through custom properties.** Parking each angle in a custom property
  and driving the quaternion from those does work, but it leaves three
  properties behind on the bone once the artist deletes the drivers -
  invisible litter on their rig that nothing cleans up.
* **Relay through the bone's own rotation_euler**, which is dead storage while
  the bone is in Quaternion mode. Blender reports a **dependency cycle** for
  this, and evaluation order for a cycle is undefined.

So the conversion happens *here*, at build time, in Python: the four component
expressions are composed once and applied as ordinary drivers with no extra
data of any kind on the rig.

Composition is ``q = qz * qy * qx`` - Blender's default XYZ Euler order, where
X is applied first. Terms that vanish are removed rather than written out: a
template that only rotates on one axis produces two short components and two
literal zeros, not four copies of a full triple product. That simplification is
what keeps most templates inside Blender's 255-character driver limit.
"""

from __future__ import annotations

# q = qz * qy * qx, expanded. Each entry is (positive term, negative term),
# where a term names the three half-angle factors multiplied together.
_COMPONENTS = (
    ("W", ("cx", "cy", "cz"), ("sx", "sy", "sz"), +1),
    ("X", ("sx", "cy", "cz"), ("cx", "sy", "sz"), -1),
    ("Y", ("cx", "sy", "cz"), ("sx", "cy", "sz"), +1),
    ("Z", ("cx", "cy", "sz"), ("sx", "sy", "cz"), -1),
)

_QUATERNION_LABELS = ("Quaternion W", "Quaternion X", "Quaternion Y", "Quaternion Z")


def _half_angle_factors(expression):
    """cos and sin of half the angle, or (None, None) for an unrotated axis.

    None means "this axis contributes nothing": cos(0) is 1 and drops out of a
    product, sin(0) is 0 and kills the whole term. Encoding that as None rather
    than as the strings "1" and "0" lets the term builder below delete the work
    instead of writing it and hoping Blender folds it.
    """
    if expression is None:
        return None, None
    return "cos((%s)/2)" % expression, "sin((%s)/2)" % expression


def _term(factors, parts):
    """One product of three half-angle factors, or None if it vanishes."""
    written = []
    for name in factors:
        value = parts[name]
        if value is None:
            if name.startswith("s"):
                return None      # sin(0) = 0 - the entire term is gone
            continue             # cos(0) = 1 - drop the factor, keep the term
        written.append(value)
    return "*".join(written) if written else "1"


def compose_quaternion_expressions(x_expression=None, y_expression=None, z_expression=None):
    """Four component expressions for the given per-axis Euler angles.

    Each argument is the template's authored expression for that axis, or None
    if the template does not rotate on it. Returns a dict keyed W/X/Y/Z.
    """
    parts = {}
    for axis, expression in (("x", x_expression), ("y", y_expression), ("z", z_expression)):
        parts["c" + axis], parts["s" + axis] = _half_angle_factors(expression)

    composed = {}
    for key, positive, negative, sign in _COMPONENTS:
        a = _term(positive, parts)
        b = _term(negative, parts)
        if a and b:
            composed[key] = "%s%s%s" % (a, "-" if sign < 0 else "+", b)
        elif a:
            composed[key] = a
        elif b:
            composed[key] = ("-%s" % b) if sign < 0 else b
        else:
            composed[key] = "0"
    return composed


def compose_tangent_expressions(x_expression=None, y_expression=None, z_expression=None):
    """The tangent form: exact, with each axis expression appearing ONCE.

    Blender normalises a pose bone's quaternion before posing it, so dividing
    every component by cos(x/2)cos(y/2)cos(z/2) changes nothing about the
    rotation. What is left is the half-angle tangents:

        W = 1 + tx*ty*tz      X = tx - ty*tz
        Y = ty + tx*tz        Z = tz - tx*ty      (tx = tan(x/2), ...)

    Compared to the full composition, where every axis appears in six factors,
    each axis appears once per component - roughly half the built length on a
    three-axis template, and it is still the SAME rotation, not an
    approximation. The catch is the domain: tan(angle/2) diverges as the angle
    approaches 180 degrees, so callers must gate this form on the actual peak
    angle the built expressions produce (motion_channels samples the frame
    range before choosing it). tan() is on Blender's fast-evaluator whitelist,
    so nothing here falls off the bytecode path.
    """
    tangents = {}
    for axis, expression in (("x", x_expression), ("y", y_expression), ("z", z_expression)):
        tangents[axis] = None if expression is None else "tan((%s)/2)" % expression

    def product(*names):
        parts = [tangents[name] for name in names]
        if any(part is None for part in parts):
            return None      # tan(0) = 0 - the whole product vanishes
        return "*".join(parts)

    composed = {}
    for key, head_axis, pair, sign in (
        ("W", None, ("x", "y", "z"), +1),
        ("X", "x", ("y", "z"), -1),
        ("Y", "y", ("x", "z"), +1),
        ("Z", "z", ("x", "y"), -1),
    ):
        head = "1" if head_axis is None else tangents[head_axis]
        tail = product(*pair)
        if head is None and tail is None:
            composed[key] = "0"
        elif tail is None:
            composed[key] = head or "0"
        elif head is None:
            composed[key] = ("-%s" % tail) if sign < 0 else tail
        else:
            composed[key] = "%s%s%s" % (head, "-" if sign < 0 else "+", tail)
    return composed


def compose_small_angle_expressions(x_expression=None, y_expression=None, z_expression=None):
    """The cheap quaternion: W=1 with half each Euler angle in X/Y/Z.

    Blender normalises a pose bone's quaternion before using it (measured: a
    quaternion scaled 11x poses identically), so only the ratio of the four
    components matters. (1, x/2, y/2, z/2) is the exact quaternion with its
    second-and-higher-order terms dropped - each axis expression appears ONCE,
    so this form always fits the driver limit where the exact composition
    multiplies each expression six times and usually cannot.

    The price is accuracy at large angles. Measured against Blender's own
    Euler.to_quaternion() across the catalogue at default parameters: 54 of 61
    rotation templates stay under 0.1 degree, the worst (the widest rotation template at a
    33-degree flap) reaches 3.8 degrees. This is the LAST resort - the exact
    composition and the (also exact) tangent form are both tried first; this
    form only exists for expressions too long for either.

    W is driven as the literal 1 rather than left undriven on purpose: an
    undriven component keeps whatever value the artist's current pose put
    there, and a stale W rescales the whole rotation after normalisation.
    """
    composed = {"W": "1"}
    for key, expression in (("X", x_expression), ("Y", y_expression), ("Z", z_expression)):
        composed[key] = "(%s)/2" % expression if expression is not None else "0"
    return composed


def quaternion_channels_for(channels, allow_approximate=False, form=None):
    """Rebuild a channel plan with its rotation expressed as a quaternion.

    Non-rotation channels (location, scale) pass through untouched - only the
    rotation half of the plan changes. Returns None when the template has no
    Euler rotation to convert, so callers can fall through to the normal path.

    The exact composition is used whenever it fits Blender's driver limit.
    With ``allow_approximate`` the small-angle form is the fallback for plans
    the exact form cannot fit; the returned rotation channels then carry
    ``"approximate": True`` so the apply layer can tell the artist. Without it,
    an over-budget plan is still returned in exact form and the caller's length
    check rejects it, exactly as before.

    ``form`` overrides the choice outright: "tangent" for the exact tangent
    composition (caller must gate on peak angle - see
    compose_tangent_expressions), "small" for the small-angle approximation.
    The apply layer uses this to walk exact -> tangent -> small itself, with
    the built-length check between rungs.
    """
    rotation = {}
    passthrough = []
    for channel in channels:
        if channel.get("data_path") == "rotation_euler":
            rotation[channel.get("index")] = channel.get("expression")
        else:
            passthrough.append(channel)

    if not rotation:
        return None

    axes = (rotation.get(0), rotation.get(1), rotation.get(2))
    if form == "tangent":
        composed = compose_tangent_expressions(*axes)
        approximate = False
    elif form == "small":
        composed = compose_small_angle_expressions(*axes)
        approximate = True
    else:
        composed = compose_quaternion_expressions(*axes)
        approximate = False
        if allow_approximate and any(
            len(expression) > _APPROXIMATION_THRESHOLD for expression in composed.values()
        ):
            composed = compose_small_angle_expressions(*axes)
            approximate = True

    converted = list(passthrough)
    for index, key in enumerate("WXYZ"):
        entry = {
            "id": "rotation_quaternion_%s" % key.lower(),
            "label": _QUATERNION_LABELS[index],
            "data_path": "rotation_quaternion",
            "index": index,
            "expression": composed[key],
        }
        if approximate:
            entry["approximate"] = True
        converted.append(entry)
    return converted


# Authored length above which the exact composition has no realistic chance of
# fitting once built and wrapped. Deliberately conservative: the authoritative
# gate stays the built-length check in the apply layer - this only decides when
# to switch to the small-angle form instead of letting that gate reject.
_APPROXIMATION_THRESHOLD = 176


def longest_quaternion_expression(channels):
    """Longest authored component expression, for the length budget check."""
    converted = quaternion_channels_for(channels)
    if not converted:
        return 0
    return max(
        (len(c["expression"]) for c in converted
         if c["data_path"] == "rotation_quaternion"),
        default=0,
    )
