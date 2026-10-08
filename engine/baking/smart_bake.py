"""Reduce a densely sampled curve to the keys that carry the motion.

A plain bake writes a keyframe on every frame, which reproduces the curve but is
wasteful for most motion: an eased open-hold-close is a ramp, a plateau and a ramp,
which an animator would key four times. This module is self-contained: it imports only
``bpy`` and knows nothing about drivers, templates or add-on state. Its input is a list
of ``(frame, value)`` samples; its output is which of them to key and how each key
should behave.

    keys, spec, error = plan(samples, tolerance_fraction=0.01, passes=2)
    if keys is not None:
        apply_plan(fcurve, samples, keys, spec)

Three stages, each decided by measuring the fit:

1. FIT: seed the keys no error metric can be trusted to find (the ends, every true turn,
   and every corner where motion meets a flat), then pin the worst-fitting frame
   repeatedly until nothing is off by more than the tolerance.
2. TYPE: choose interpolation per segment and a handle per key side.
3. PRUNE: re-test every key now that the types are right, since stage 1 adds keys to
   cover bows the chosen types no longer produce.

Stages 2 and 3 repeat ``passes`` times, stopping early once nothing changes.

Handle positions, not just handle types. Blender's computed handles (Auto, Auto Clamped,
Vector) cannot express arriving on a slope of -0.8 and leaving on +0.4, which is what a
ball's touchdown is, and Auto Clamped flattens at a local minimum, which fights an
impact. A handle may therefore be placed directly, from the slope the samples have at
that frame. A cubic Bezier with correct end tangents reproduces any cubic exactly, and a
parabola is a cubic, so a bounce arc takes two or three keys; the touchdown becomes a
true cusp, and free handles are the only kind that can hold two different slopes at one
key. Two tangent flavours are offered per side and the optimiser picks by measurement:
SMOOTH (central difference, the exact derivative of a quadratic) and SHARP (one-sided,
what a corner needs).

Rules the fitter follows:

* Every stage-1 baseline is tried and the cheapest kept, because the baseline decides
  whether stage 1 converges at all.
* A handle reshapes the segments on both sides of its key, so a change is committed only
  if the whole curve still passes.
* The metric is always worst-frame error, never average, because an average hides events
  that matter, such as a one-frame flash.

This cannot invent redundancy that is not there: layered vibration changes every frame
and needs a key on every frame.
"""

from __future__ import annotations

import bpy

# Handle candidates offered per key side. The three computed types are Blender's own.
# The two FREE flavours place the handle explicitly along the slope the samples have:
#
# * FREE_SMOOTH: central difference, the exact derivative of a quadratic, so an eased
#   arc fits with almost no keys.
# * FREE_SHARP: one-sided, the slope on this side only, which makes a touchdown a corner
#   instead of a rounded dip.
#
# ALIGNED is not offered: it forces both sides collinear, which is what a cusp must not
# be.
HANDLE_CANDIDATES = ("AUTO_CLAMPED", "AUTO", "VECTOR", "FREE_SMOOTH", "FREE_SHARP")

_BLENDER_HANDLE = {
    "AUTO_CLAMPED": "AUTO_CLAMPED",
    "AUTO": "AUTO",
    "VECTOR": "VECTOR",
    "FREE_SMOOTH": "FREE",
    "FREE_SHARP": "FREE",
}

# Interpolation per segment. Blender's easing modes (Sinusoidal, Quartic) and dynamic
# effects (Back, Bounce, Elastic) are omitted on purpose: they are fixed parametric
# shapes, and fitting real motion to them would replace the motion with a preset that
# merely resembles it.
INTERPOLATIONS = ("CONSTANT", "LINEAR", "BEZIER")

# Baselines stage 1 fits from. Whichever converges on fewest keys wins, and
# stage 2 then re-types every key individually.
BASELINES = ("FREE_SMOOTH", "AUTO_CLAMPED", "VECTOR")

MIN_PASSES, MAX_PASSES = 1, 5
DEFAULT_PASSES = 2

# A Bezier handle reaches a third of the way to its neighbour. This is the
# standard Hermite-to-Bezier conversion and makes the tangent exact.
_HANDLE_REACH = 1.0 / 3.0

_FLAT_EPSILON = 1e-9


def fcurve_of(obj):
    """The single F-curve on an object, across Blender 4.x and 5.x.

    5.x removed ``Action.fcurves`` in favour of layers -> strips -> channelbags.
    """
    action = getattr(getattr(obj, "animation_data", None), "action", None)
    if action is None:
        return None
    direct = getattr(action, "fcurves", None)
    if direct:
        return direct[0]
    for layer in getattr(action, "layers", ()) or ():
        for strip in getattr(layer, "strips", ()) or ():
            for bag in getattr(strip, "channelbags", ()) or ():
                for fcurve in getattr(bag, "fcurves", ()) or ():
                    return fcurve
    return None


def slopes(samples):
    """Per-sample derivative, three ways, in value units per frame. ``smooth`` is the
    central difference, which for a quadratic is the exact derivative, so an eased
    arc fits cheaply. ``left`` and ``right`` are one-sided and differ only where the
    curve corners; that difference is what a touchdown is made of.
    """
    count = len(samples)
    out = []
    for i in range(count):
        frame, value = samples[i]
        back = forward = 0.0
        if i > 0:
            span = samples[i][0] - samples[i - 1][0]
            back = (value - samples[i - 1][1]) / span if span else 0.0
        if i < count - 1:
            span = samples[i + 1][0] - samples[i][0]
            forward = (samples[i + 1][1] - value) / span if span else 0.0
        if i == 0:
            back = forward
        if i == count - 1:
            forward = back
        out.append((0.5 * (back + forward), back, forward))
    return out


def _handle_slope(candidate, sample_slopes, side):
    smooth, back, forward = sample_slopes
    if candidate == "FREE_SMOOTH":
        return smooth
    return back if side == "left" else forward


def apply_plan(fcurve, samples, keys, spec, curve_slopes=None):
    """Write a plan onto an F-curve that already holds exactly those keys.

    Types are set first and positions second: assigning a handle position only
    means anything once the handle type is FREE, and Blender recomputes the
    others on update.
    """
    points = fcurve.keyframe_points
    if len(points) != len(keys):
        return False
    if curve_slopes is None:
        curve_slopes = slopes(samples)

    for point, index in zip(points, keys):
        kind = spec[index]
        point.interpolation = kind["interp"]
        point.handle_left_type = _BLENDER_HANDLE[kind["left"]]
        point.handle_right_type = _BLENDER_HANDLE[kind["right"]]
    fcurve.update()

    for position, (point, index) in enumerate(zip(points, keys)):
        kind = spec[index]
        frame, value = samples[index]
        if kind["left"].startswith("FREE"):
            previous = samples[keys[position - 1]][0] if position else frame - 1.0
            reach = max((frame - previous) * _HANDLE_REACH, 1e-4)
            slope = _handle_slope(kind["left"], curve_slopes[index], "left")
            point.handle_left = (frame - reach, value - slope * reach)
        if kind["right"].startswith("FREE"):
            following = (samples[keys[position + 1]][0]
                         if position < len(keys) - 1 else frame + 1.0)
            reach = max((following - frame) * _HANDLE_REACH, 1e-4)
            slope = _handle_slope(kind["right"], curve_slopes[index], "right")
            point.handle_right = (frame + reach, value + slope * reach)
    fcurve.update()
    return True


class _Curve:
    """A scratch F-curve used to ask Blender what a key set evaluates to.

    Fitting against a hand-written Bezier would mean re-deriving Blender's handle solver
    exactly, and any drift would show as a curve that fit cleanly here but looked wrong
    in the viewport; asking Blender cannot disagree with Blender. The curve is reused
    rather than rebuilt, because discarding the action and re-inserting every key for
    each of the hundreds of candidates a fit tries would dominate the runtime.
    """

    def __init__(self, samples):
        self.samples = samples
        self.slopes = slopes(samples)
        values = [value for _, value in samples]
        self.low, self.high = min(values), max(values)
        self.obj = bpy.data.objects.new("__espresso_fit", None)
        self.obj.animation_data_create()
        self.obj.location[0] = samples[0][1]
        self.obj.keyframe_insert("location", index=0, frame=samples[0][0])
        self.fcurve = fcurve_of(self.obj)
        self.keys = [0]

    def close(self):
        action = getattr(getattr(self.obj, "animation_data", None), "action", None)
        bpy.data.objects.remove(self.obj, do_unlink=True)
        if action is not None and action.users == 0:
            bpy.data.actions.remove(action)

    # -- key set ----------------------------------------------------------
    def add(self, index):
        frame, value = self.samples[index]
        self.fcurve.keyframe_points.insert(frame, value, options={"FAST"})
        self.keys.append(index)
        self.keys.sort()

    def drop(self, index):
        position = self.keys.index(index)
        self.fcurve.keyframe_points.remove(
            self.fcurve.keyframe_points[position], fast=True)
        self.keys.pop(position)

    def reset(self, indices):
        points = self.fcurve.keyframe_points
        while len(points) > 1:
            points.remove(points[-1], fast=True)
        first = self.samples[indices[0]]
        points[0].co = (first[0], first[1])
        self.keys = [indices[0]]
        for index in indices[1:]:
            self.add(index)
        self.fcurve.update()

    # -- types ------------------------------------------------------------
    def realise(self, spec):
        apply_plan(self.fcurve, self.samples, self.keys, spec, self.slopes)

    def uniform(self, candidate, interp="BEZIER"):
        spec = {i: {"interp": interp, "left": candidate, "right": candidate}
                for i in self.keys}
        self.realise(spec)
        return spec

    # -- measurement ------------------------------------------------------
    def worst(self):
        """Worst absolute error over every sample, and the sample it is at.

        Keyed samples are skipped: a key reproduces its own value exactly.
        """
        keyed = set(self.keys)
        evaluate = self.fcurve.evaluate
        worst_index, worst_error = -1, 0.0
        for i, (frame, value) in enumerate(self.samples):
            if i in keyed:
                continue
            error = abs(evaluate(frame) - value)
            if error > worst_error:
                worst_index, worst_error = i, error
        return worst_index, worst_error

    def excursion(self, step=0.25):
        """How far the curve travels outside the range the samples occupied.

        Error measured at sample frames cannot see this: a tangent handle bulges between
        frames, so a curve can pass through every sample within tolerance and still
        swing past the highest value the driver ever produced. A ball that dips below
        the floor between two keys, or a light brighter than its own maximum, is motion
        nobody asked for, and sub-frame steps are what a motion-blurred render reads.
        """
        evaluate = self.fcurve.evaluate
        low, high = self.low, self.high
        worst = 0.0
        frame, end = float(self.samples[0][0]), float(self.samples[-1][0])
        while frame <= end:
            value = evaluate(frame)
            if value > high:
                worst = max(worst, value - high)
            elif value < low:
                worst = max(worst, low - value)
            frame += step
        return worst

    def holds(self, tolerance, allowance):
        """The whole contract in one call: accurate AND inside the range."""
        _, worst = self.worst()
        if worst > tolerance:
            return False, worst
        if self.excursion() > allowance:
            return False, worst
        return True, worst

    def span_error(self, lo, hi):
        """Worst error between two SAMPLE indices, exclusive.

        Typing decisions are local, so scoring them against the whole curve
        would let unrelated error elsewhere pick the winner. Scanning only the
        span is also what makes stage 2 affordable.
        """
        evaluate = self.fcurve.evaluate
        worst = 0.0
        for i in range(lo + 1, hi):
            frame, value = self.samples[i]
            error = abs(evaluate(frame) - value)
            if error > worst:
                worst = error
        return worst


def seed_indices(samples, eps=_FLAT_EPSILON):
    """Sample indices that must be keyed regardless of what the error says.

    Ends, true turns, and the corners where motion meets a flat. The last of
    these is what collapses a plateau: without it the fitter discovers the two
    corners of a hold one key at a time, paying for each.
    """
    count = len(samples)
    keep = {0, count - 1}
    for i in range(1, count - 1):
        before = samples[i][1] - samples[i - 1][1]
        after = samples[i + 1][1] - samples[i][1]
        turns = (before > eps and after < -eps) or (before < -eps and after > eps)
        meets_flat = (abs(before) > eps) != (abs(after) > eps)
        if turns or meets_flat:
            keep.add(i)
    return keep


def _fit(curve, tolerance, max_keys, candidate):
    """Stage 1. Insert the worst-fitting frame until nothing exceeds tolerance."""
    curve.reset(sorted(seed_indices(curve.samples)))
    curve.uniform(candidate)
    for _ in range(len(curve.samples)):
        worst_index, worst_error = curve.worst()
        if worst_index < 0 or worst_error <= tolerance:
            return list(curve.keys)
        curve.add(worst_index)
        curve.uniform(candidate)
        # Once the fit needs nearly every frame there is nothing left to win,
        # and continuing only burns time to arrive at the dense bake anyway.
        if len(curve.keys) >= max_keys:
            return None
    _, error = curve.worst()
    return list(curve.keys) if error <= tolerance else None


def _optimise(curve, spec, tolerance, allowance):
    """Stage 2. Per-segment interpolation and per-side handles, by measurement."""
    keys = curve.keys
    changed = False

    def commit(index, field, value):
        nonlocal changed
        previous = spec[index][field]
        spec[index][field] = value
        curve.realise(spec)
        if previous == value:
            return
        ok, _worst = curve.holds(tolerance, allowance)
        if not ok:
            spec[index][field] = previous
            curve.realise(spec)
            return
        changed = True

    # Interpolation governs the segment that FOLLOWS its key.
    for position in range(len(keys) - 1):
        lo, hi = keys[position], keys[position + 1]
        if hi - lo <= 1:
            continue  # adjacent keys: nothing in between to get wrong
        original = spec[lo]["interp"]
        best, best_error = None, None
        for mode in INTERPOLATIONS:
            spec[lo]["interp"] = mode
            curve.realise(spec)
            error = curve.span_error(lo, hi)
            # Ties go to the earlier, simpler mode: CONSTANT and LINEAR are
            # exact where they win and far easier to edit afterwards.
            if best_error is None or error < best_error - 1e-12:
                best, best_error = mode, error
        spec[lo]["interp"] = original
        commit(lo, "interp", best)

    # Handles only shape BEZIER segments, and each side is scored against the
    # segment it actually governs.
    for position, index in enumerate(keys):
        for field in ("right", "left"):
            if field == "right":
                if position >= len(keys) - 1 or spec[index]["interp"] != "BEZIER":
                    continue
                lo, hi = index, keys[position + 1]
            else:
                if position == 0:
                    continue
                other = keys[position - 1]
                if spec[other]["interp"] != "BEZIER":
                    continue
                lo, hi = other, index
            if hi - lo <= 1:
                continue
            original = spec[index][field]
            best, best_error = None, None
            for candidate in HANDLE_CANDIDATES:
                spec[index][field] = candidate
                curve.realise(spec)
                error = curve.span_error(lo, hi)
                if best_error is None or error < best_error - 1e-12:
                    best, best_error = candidate, error
            spec[index][field] = original
            commit(index, field, best)

    return changed


def _prune(curve, spec, tolerance, allowance):
    """Stage 3. Drop keys the corrected types have made unnecessary.

    Every removal is re-measured against the whole curve, so a key holding a
    peak or a one-frame flash simply fails the test and stays.
    """
    changed = False
    position = 1
    while position < len(curve.keys) - 1:
        index = curve.keys[position]
        removed = dict(spec[index])
        curve.drop(index)
        spec.pop(index, None)
        curve.realise(spec)
        ok, _error = curve.holds(tolerance, allowance)
        if ok:
            changed = True
            continue  # this position now holds the next key
        curve.add(index)
        spec[index] = removed
        curve.realise(spec)
        position += 1
    return changed


def plan(samples, *, tolerance_fraction=0.01, passes=DEFAULT_PASSES, min_saving=0.1,
         overshoot_fraction=0.02):
    """Choose which samples to key, and how each key should behave.

    ``samples``            [(frame, value)] as a dense bake would have written.
    ``tolerance_fraction`` Allowed worst-frame error as a fraction of the
                           channel's own peak-to-peak range, so one number means
                           the same on a rotation in radians and a light in
                           watts.
    ``passes``             How many optimise/prune cycles to run, 1 to 5. The
                           cycle exits early once nothing changes, so a high
                           value is never wasted, only sometimes unnecessary.
    ``min_saving``         Give up unless at least this fraction of keys can go.

    Returns ``(kept_indices, spec, worst_error)``, where ``spec`` maps each kept
    index to ``{"interp", "left", "right"}``; or ``(None, None, error)`` when
    thinning cannot be done faithfully, in which case the caller should write
    every sample. A returned plan is guaranteed to meet its tolerance.
    """
    if len(samples) < 3:
        return None, None, 0.0
    values = [value for _, value in samples]
    span = max(values) - min(values)
    if span <= _FLAT_EPSILON:
        # Nothing moves. Two keys hold it exactly; hundreds is pure waste.
        ends = [0, len(samples) - 1]
        return (ends,
                {i: {"interp": "BEZIER", "left": "AUTO_CLAMPED",
                     "right": "AUTO_CLAMPED"} for i in ends},
                0.0)

    tolerance = span * max(tolerance_fraction, 0.0)
    allowance = span * max(overshoot_fraction, 0.0)
    max_keys = int(len(samples) * (1.0 - min_saving))
    passes = max(MIN_PASSES, min(MAX_PASSES, int(passes)))

    curve = _Curve(samples)
    try:
        # A baseline qualifies only if its fit holds the whole contract. Stage 1
        # measures error at sample frames, which cannot see a tangent handle bulging
        # between them, so a baseline that fits but overshoots is rejected here.
        best_keys, baseline, best_error = None, None, 0.0
        for candidate in BASELINES:
            attempt = _fit(curve, tolerance, max_keys, candidate)
            if attempt is None:
                continue
            curve.reset(attempt)
            curve.uniform(candidate)
            ok, error = curve.holds(tolerance, allowance)
            if not ok:
                continue
            if best_keys is None or len(attempt) < len(best_keys):
                best_keys, baseline, best_error = attempt, candidate, error
        if best_keys is None:
            return None, None, 0.0

        curve.reset(best_keys)
        spec = curve.uniform(baseline)
        safe = (list(curve.keys), {i: dict(v) for i, v in spec.items()})
        safe_error = best_error

        for _ in range(passes):
            touched = _optimise(curve, spec, tolerance, allowance)
            touched = _prune(curve, spec, tolerance, allowance) or touched
            ok, error = curve.holds(tolerance, allowance)
            if ok:
                safe = (list(curve.keys), {i: dict(v) for i, v in spec.items()})
                safe_error = error
            if not touched:
                break  # settled: further passes would repeat identical work

        keys, spec = safe
        if len(keys) > max_keys or safe_error > tolerance:
            return None, None, safe_error
        return keys, spec, safe_error
    finally:
        curve.close()
