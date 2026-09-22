"""Reading a lighting rig's arrangement, so the spatial templates can be aimed.

The spatial lighting templates turn each lamp's own POSITION into its place in
a pattern. That only produces a pattern if the lamps are actually spread out
along whatever the template measures, which is what the checks here are for.
Three lamps in a column, given Radial Sweep, span 2 degrees of a 360 degree
sweep and animate as one; the add-on applied it and said nothing, which reads
as "the feature is broken" rather than "these lamps are in the wrong
arrangement for it".

So this module measures the three quantities the templates can read - distance
along an axis, distance from a centre, and angle about a centre - and reports
which of them the rig actually varies in. That drives two things: picking the
axis automatically (Position Wave was hardwired to world X, which is no use for
a row running along Y), and telling the artist plainly when the arrangement and
the template disagree.
"""

from __future__ import annotations

import dataclasses
import math

AXES = ("X", "Y", "Z")
AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
TRANSFORM_FOR_AXIS = {"X": "LOC_X", "Y": "LOC_Y", "Z": "LOC_Z"}

# What each spatial template reads off a lamp. Templates not listed here are
# time-only - every lamp does the same thing by design, and that is not a fault
# to warn about.
AXIS = "AXIS"
DISTANCE = "DISTANCE"
RADIAL = "RADIAL"

SPATIAL_KIND = {
    # These node-route families also read each object's position. Treat the
    # gradient and stepped fill like a travelling axis wave so the panel gives
    # the artist an explicit axis choice and arrangement wizard.
}

# Below this the lamps are close enough in phase to read as simultaneous. A
# tenth of a cycle is roughly 36 degrees of the wave - visible, but only just,
# so it is the right place to start advising rather than to start complaining.
MIN_USEFUL_CYCLES = 0.1
# Radial is judged in degrees of the sweep rather than in cycles, because the
# sweep always covers exactly one turn and SHARP, not SPREAD, sets its width.
MIN_USEFUL_DEGREES = 30.0


@dataclasses.dataclass
class Advice:
    """What the panel and the operator say about an arrangement."""

    ok: bool
    detail: str
    # Template id that suits this arrangement better, when one clearly does.
    suggestion: str = ""
    suggestion_label: str = ""
    # Spatial frequency that would spread the rig across exactly one cycle.
    suggested_spread: float = 0.0


def positions(objects):
    return [tuple(float(v) for v in obj.matrix_world.translation) for obj in objects]


def axis_span(points, axis):
    """How far the rig extends along one world axis, in metres."""
    if len(points) < 2:
        return 0.0
    values = [p[AXIS_INDEX[axis]] for p in points]
    return max(values) - min(values)


def widest_axis(points):
    """The axis the rig is most spread along - the one worth waving through.

    Ties go to X, then Y, then Z, purely so the choice is stable rather than
    dependent on floating point noise between two equal spans.
    """
    spans = {axis: axis_span(points, axis) for axis in AXES}
    best = max(AXES, key=lambda axis: (spans[axis], -AXES.index(axis)))
    return best, spans


def distance_span(points, centre=(0.0, 0.0, 0.0)):
    if len(points) < 2:
        return 0.0
    distances = [math.dist(p, centre) for p in points]
    return max(distances) - min(distances)


def angular_span(points, centre=(0.0, 0.0, 0.0)):
    """The angle the rig subtends about a centre, looking down Z, in radians.

    Angles wrap, so the widest GAP between neighbouring lamps is what matters:
    the rig occupies everything except its largest gap. Taking max minus min
    would call a rig straddling the -180/+180 seam nearly a full circle when it
    is in fact a tight cluster.
    """
    if len(points) < 2:
        return 0.0
    angles = sorted(
        math.atan2(p[1] - centre[1], p[0] - centre[0]) for p in points
    )
    gaps = [b - a for a, b in zip(angles, angles[1:])]
    gaps.append(angles[0] + 2.0 * math.pi - angles[-1])   # across the seam
    return 2.0 * math.pi - max(gaps)


def _spread_value(values):
    try:
        return float(values.get("SPREAD", values.get("SCALE", 0.25)) or 0.0)
    except (TypeError, ValueError):
        return 0.25


def phase_cycles(span, spread):
    """How much of a wave separates the two extreme lamps."""
    return abs(span) * abs(spread)


def fit_spread(objects, axis):
    """Cycles per metre for one complete wave across ``objects`` on ``axis``."""
    span = axis_span(positions(objects), axis)
    return (1.0 / span) if span > 1e-6 else 0.0


def centroid(points):
    points = list(points)
    if not points:
        return (0.0, 0.0, 0.0)
    count = float(len(points))
    return tuple(sum(p[i] for p in points) / count for i in range(3))


def advise(template_id, objects, values=None, axis=None, centre=None):
    """Whether this arrangement will actually show this template's pattern.

    ``centre`` is where the sweep turns. It is not the world origin: the
    template reads a centre object, so the centre has to be resolved from that
    object rather than assumed. Left unset it falls back to the middle of the
    objects themselves, which is what the sweep would use if a centre were
    created for them right now. Reporting a coverage measured about the origin
    while the driver measures about an Empty would be worse than saying
    nothing.
    """
    kind = SPATIAL_KIND.get(template_id)
    if kind is None:
        return Advice(True, "")
    if len(objects) < 2:
        return Advice(True, "")

    points = positions(objects)
    values = values or {}
    if centre is None:
        centre = centroid(points)

    if kind == RADIAL:
        degrees = math.degrees(angular_span(points, centre))
        if degrees >= MIN_USEFUL_DEGREES:
            return Advice(
                True,
                "These lamps span %.0f° around their centre, so the sweep "
                "reaches them at different times." % degrees,
            )
        best, spans = widest_axis(points)
        return Advice(
            False,
            "These lamps span only %.0f° of the 360° sweep, so it "
            "passes over them almost together. Radial Sweep measures the angle "
            "about the centre, so it wants lamps arranged AROUND one - use "
            "Create Radial below." % degrees,
            suggestion="",
            suggestion_label="Position Wave",
            suggested_spread=(1.0 / spans[best]) if spans[best] > 1e-6 else 0.0,
        )

    spread = _spread_value(values)
    if kind == AXIS:
        chosen = axis or widest_axis(points)[0]
        span = axis_span(points, chosen)
        label = "along %s" % chosen
    else:
        span = distance_span(points, centre)
        label = "in distance from the centre"

    cycles = phase_cycles(span, spread)
    suggested = (1.0 / span) if span > 1e-6 else 0.0

    if span <= 1e-6:
        return Advice(
            False,
            "These lamps are all at the same place %s, so they have no order to "
            "animate in." % label,
        )
    if cycles < MIN_USEFUL_CYCLES:
        return Advice(
            False,
            "These lamps span %.2fm %s, which at a spatial frequency of %.2f/m "
            "is only %.0f%% of a wave - they will look simultaneous. Raise "
            "Spatial frequency to about %.2f/m for one full wave across the "
            "rig." % (span, label, spread, cycles * 100.0, suggested),
            suggested_spread=suggested,
        )
    coverage = ("%.2f waves" % cycles) if cycles >= 1.0 else ("%.0f%% of a wave" % (cycles * 100.0))
    return Advice(
        True,
        "These lamps span %.2fm %s - %s between the first and last."
        % (span, label, coverage),
        suggested_spread=suggested,
    )
