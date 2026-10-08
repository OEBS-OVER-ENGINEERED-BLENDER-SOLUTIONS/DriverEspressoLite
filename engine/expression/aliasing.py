"""Telling the artist when Speed has gone past what frames can show.

Past that point a recipe reads as going slower when its Speed is raised. The motion is
faster than the frame rate can represent, so what stays visible is the small leftover.
At 20 frames, Speed 1.0 turns 0.05 per frame as seen; Speed 10.0 is 0.5 per frame but is
seen as -0.5 (backwards); Speed 20.0 is 1.0 but is seen as 0 (frozen); and Speed 100.7
is seen as 0.034 (slower than Speed 1).

Half a cycle per frame is the ceiling. Past it the sampled result folds back: it
reverses, then stands still, then creeps forward again (the wagon-wheel effect). Nothing
is broken, but the slider stops meaning what it says, and the line moves with the cycle
length. This computes the line and the panel states it. It does not clamp, because a
deliberate strobe is legitimate.

Scope: this describes smooth oscillators. A twinkle that re-rolls a hash every few
frames is not a wave being undersampled, so it is excluded.
"""

from __future__ import annotations

# Frames-per-cycle lives under different names across the catalogue. ``N`` is
# ambiguous: it is a frame hold in some templates, but a loop/step count in
# others, so cycle_length() checks its artist-facing semantics before using it.
CYCLE_LENGTH_TOKENS = ("PERIOD", "FRAMES", "N")

# Above this, one frame advances more than half a cycle and the sampled result
# no longer follows the real motion.
NYQUIST_CYCLES_PER_FRAME = 0.5

# The pseudo-random hash constant these templates share. Its presence means the
# value is re-rolled on a beat rather than swept continuously, so undersampling
# analysis does not describe it.
_HASH_MARKER = "43758"


def _number(values, token, fallback=0.0):
    try:
        return float(values.get(token, fallback))
    except (TypeError, ValueError):
        return fallback


def is_continuous(template):
    """Whether this template is a smooth wave rather than a random gate."""
    expression = template.get("expression") or ""
    if _HASH_MARKER in expression:
        return False
    for channel in template.get("channels") or ():
        if _HASH_MARKER in (channel.get("expression") or ""):
            return False
    return True


def cycle_length(template, values):
    """Frames per cycle, or None when the template does not state one."""
    params = {param["token"]: param for param in template.get("params", [])}
    for token in CYCLE_LENGTH_TOKENS:
        parameter = params.get(token)
        if parameter is not None:
            if token == "N":
                unit = str(parameter.get("unit") or "").casefold()
                label = str(parameter.get("label") or "").casefold()
                if unit not in {"frame", "frames"} and "frame" not in label:
                    continue
            length = _number(values, token)
            if length > 0:
                return length
    return None


def cycles_per_frame(template, values):
    """How much of a cycle one frame advances, or None if not determinable."""
    if "SPEED" not in {p["token"] for p in template.get("params", [])}:
        return None
    length = cycle_length(template, values)
    if not length:
        return None
    return abs(_number(values, "SPEED", 1.0)) / length


def usable_speed_limit(template, values):
    """The highest Speed whose result the frames can still show."""
    length = cycle_length(template, values)
    return length * NYQUIST_CYCLES_PER_FRAME if length else None


def advice(template, values):
    """One sentence for the panel, or "" when Speed is in a sane range."""
    if not is_continuous(template):
        return ""
    rate = cycles_per_frame(template, values)
    if rate is None or rate <= NYQUIST_CYCLES_PER_FRAME:
        return ""

    limit = usable_speed_limit(template, values)
    speed = _number(values, "SPEED", 1.0)

    if abs(rate - round(rate)) < 0.01:
        # A whole number of cycles per frame lands on the same phase every
        # frame, so it does not merely look slow - it stops dead.
        return (
            "Speed %g advances %.2f cycles per frame, which lands on the same "
            "point every frame - this will look frozen. Below %g stays "
            "visible." % (speed, rate, limit)
        )
    return (
        "Speed %g advances %.2f cycles per frame. Above %.1f cycles per frame "
        "the frames cannot follow it, so it reads slower or backwards rather "
        "than faster. Below Speed %g behaves as expected."
        % (speed, rate, NYQUIST_CYCLES_PER_FRAME, limit)
    )
