# -*- coding: utf-8 -*-
"""How long is this motion?

An artist setting Landing frame 12 and Recovery time 24 needs to know the move is over
by frame 36, and for anything with a recovery tail or a settle the total decides where
the shot goes. The answer is measured from the built expression rather than declared per
template, so it cannot go stale when a kernel changes:

* settles to a constant and stays there: FINITE, report the settle frame
* repeats: CYCLIC, report the loop length
* keeps growing: INFINITE
* none of those: CONTINUOUS (stochastic flicker, noise), where no number is honest

Results are cached, since the panel redraws far more often than parameters change.
"""

from __future__ import annotations

from . import formula_reader, utils

FINITE = "FINITE"
CYCLIC = "CYCLIC"
INFINITE = "INFINITE"
CONTINUOUS = "CONTINUOUS"
STATIC = "STATIC"
INPUT_DRIVEN = "INPUT_DRIVEN"
UNKNOWN = "UNKNOWN"

# How far to look. 900 frames catches a slow settle and any loop up to 300
# frames (a period needs three repeats inside the window to be trusted), while
# keeping the sweep near 20 ms per template - this runs on a panel redraw, so
# the budget is real.
WINDOW = 900

# How close to its final value counts as settled. 0.5% of the total travel,
# NOT an exact match: an exponential recovery never exactly stops, so an exact
# test reported a 12-frame landing with a 24-frame recovery as 232 frames -
# true to the arithmetic and useless to an artist, who wants to know when the
# motion has visibly finished.
SETTLE_TOLERANCE = 0.005

# Loop lengths worth testing, longest last. A template whose period is not on
# this list still classifies, just as CONTINUOUS rather than CYCLIC - which is
# the safe direction to be wrong in, since it promises less.
_PERIODS = tuple(range(2, 301))

_cache = {}


def _sample(channels, frame_start, window=WINDOW, variables=None):
    """Evaluate every channel across the window; None if it cannot be read."""
    namespace = dict(utils.SAFE_NAMESPACE)
    namespace.update(variables or {})
    series = []
    for channel in channels:
        expression = channel["expression"]
        # Read once: reading the text again on every one of 900 frames would be most of
        # the cost of this sweep.
        try:
            code = formula_reader.parse(expression)
        except Exception:
            return None
        values = []
        for offset in range(window):
            namespace["frame"] = frame_start + offset
            try:
                values.append(float(code(namespace)))
            except Exception:
                return None
        series.append(values)
    return series


def _span(values):
    return max(values) - min(values)


# A settle is only a settle if the value then holds. Without a minimum tail the backward
# walk stops at the last change, which for a linear ramp is two frames from the window
# end, so it would report a finite length instead of Infinite, and a colour cycle
# holding its last swatch would report a settle instead of a loop. The tail must be long
# enough that no plausible cycle fits in it.
MIN_SETTLED_TAIL = 300


def _settle_frame(values, tolerance):
    """First offset after which the value holds for the rest of the window.

    Walks backwards, so a motion that settles and is disturbed later reports the
    later settle, not the early one.
    """
    last = values[-1]
    index = len(values) - 1
    while index > 0 and abs(values[index - 1] - last) <= tolerance:
        index -= 1
    if len(values) - index < MIN_SETTLED_TAIL:
        return None                      # still moving, or merely pausing
    return index


def _period(values, tolerance):
    """Smallest repeat length that holds across the whole window, or None."""
    length = len(values)
    for period in _PERIODS:
        if period * 3 > length:
            break
        if all(abs(values[i] - values[i + period]) <= tolerance
               for i in range(0, length - period, max(1, period // 8))):
            return period
    return None


def classify(template, values, scene):
    """Return a dict describing how long this motion runs.

    Keys: kind, frames (int or None), label, detail.
    """
    if not template:
        return {"kind": UNKNOWN, "frames": None, "label": "", "detail": ""}

    frame_start = int(getattr(scene, "frame_start", 1) or 1)
    key = (
        template.get("id"),
        tuple(sorted((k, _hashable(v)) for k, v in values.items())),
        frame_start,
        int(getattr(scene, "frame_end", 250) or 250),
    )
    hit = _cache.get(key)
    if hit is not None:
        return hit

    try:
        plan = utils.build_template_expressions(template, dict(values), scene)
    except Exception:
        result = {"kind": UNKNOWN, "frames": None, "label": "", "detail": ""}
        _cache[key] = result
        return result

    # Templates that read a driver variable cannot be evaluated without one.
    # Each declares a preview_default for exactly this purpose, so use it -
    # holding the input STILL and watching what time alone does.
    required = list(template.get("requires_driver_variables") or [])
    variables = {spec["name"]: float(spec.get("preview_default", 0.0) or 0.0)
                 for spec in required}

    series = _sample(plan, frame_start, variables=variables)
    if not series:
        result = {"kind": UNKNOWN, "frames": None, "label": "", "detail": ""}
        _cache[key] = result
        return result

    result = _classify_series(series)

    # With its input held still, a template whose motion COMES FROM that input
    # is flat - a geared template turns only when the driving gear turns. Reporting
    # "Static" would be true of the probe and false of the template. A spatial
    # variable behaves differently: a spatial template's AXIS is a constant per
    # lamp, so time still moves it, and that reading is kept.
    if required and result["kind"] == STATIC:
        names = ", ".join(
            spec.get("label") or spec.get("name", "").replace("espinp_", "")
            for spec in required)
        result = {"kind": INPUT_DRIVEN, "frames": None,
                  "label": "Follows its input",
                  "detail": "Length comes from whatever drives %s, not from this "
                            "template." % (names or "its input")}

    # Bounded, because the key includes every parameter value: scrubbing a
    # slider mints a new entry per step, and an unbounded dict would grow for
    # the life of the session.
    if len(_cache) > 512:
        _cache.clear()
    _cache[key] = result
    return result


def _hashable(value):
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


def _classify_series(series):
    # Tolerance is relative to how far the motion travels, so a template working
    # in metres and one working in radians are judged on the same terms.
    overall = max(_span(values) for values in series)
    if overall <= 1e-9:
        return {"kind": STATIC, "frames": 0, "label": "Static",
                "detail": "The output never changes at these settings."}
    tolerance = max(overall * 1e-4, 1e-9)
    settle_tolerance = max(overall * SETTLE_TOLERANCE, 1e-9)

    # FINITE - every channel settles. Report the latest settle, because the
    # motion is not over until the slowest channel stops.
    settles = [_settle_frame(values, settle_tolerance) for values in series]
    if all(s is not None for s in settles):
        frames = max(settles) + 1
        return {"kind": FINITE, "frames": frames,
                "label": "%d frames" % frames,
                "detail": "Runs for %d frames from the start frame, including "
                          "any recovery or settle, then holds." % frames}

    # CYCLIC - every channel repeats. Report the longest period, since the whole
    # set only repeats when the slowest one does.
    periods = [_period(values, tolerance) for values in series]
    if all(p is not None for p in periods):
        loop = max(periods)
        return {"kind": CYCLIC, "frames": loop,
                "label": "Cyclic - %d frame loop" % loop,
                "detail": "Repeats every %d frames and continues for as long as "
                          "the scene runs." % loop}

    # INFINITE - still travelling in one direction at the end of the window, and
    # travelling much further than it did at the start.
    if all(_is_unbounded(values) for values in series):
        return {"kind": INFINITE, "frames": None, "label": "Infinite",
                "detail": "Never repeats and never stops - it keeps going for as "
                          "long as the scene runs."}

    return {"kind": CONTINUOUS, "frames": None, "label": "Continuous",
            "detail": "Keeps running without settling or repeating exactly, so "
                      "it has no fixed length."}


def _is_unbounded(values):
    """Is the value still climbing (or falling) away at the end of the window?"""
    quarter = max(2, len(values) // 4)
    first, last = values[:quarter], values[-quarter:]
    drift = abs(sum(last) / len(last) - sum(first) / len(first))
    return drift > _span(first) * 2 and drift > 1e-6
