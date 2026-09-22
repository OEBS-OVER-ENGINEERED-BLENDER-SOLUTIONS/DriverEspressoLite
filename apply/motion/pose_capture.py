"""Use a bone's current pose as the amplitude of a motion, instead of a number.

Every other template asks the artist for an amount - degrees, metres, a 0-to-1
scalar. That works when the motion is one axis. It does not work for an eyelid,
where "closed" is a specific compound pose the rigger already knows how to make
by hand and could not describe in numbers if asked.

So: the artist poses the bone into the extreme, and the pose itself becomes the
amplitude. What gets recorded is the DELTA from rest, baked into the driver as
constants. The pose is then cleared, which is what makes the result read
correctly - the bone sits at rest (lids open) and the driver takes it to the
recorded pose and back on the template's rhythm.

Rest is the identity transform, so "clear the pose" and "the motion returns to
zero" are the same statement. That is deliberate: it means the driven result at
envelope 0 is exactly the bone's rest pose, with no accumulated offset.
"""

from __future__ import annotations

import re

import bpy

from ...engine import driver_literals

# What an unposed bone reads on each channel. Anything else is the artist's pose.
_REST = {
    "location": (0.0, 0.0, 0.0),
    "rotation_euler": (0.0, 0.0, 0.0),
    "rotation_quaternion": (1.0, 0.0, 0.0, 0.0),
    "scale": (1.0, 1.0, 1.0),
}

# Below this a channel is treated as untouched. Dragging a bone in the viewport
# leaves float32 dust on axes the artist never meant to move, and a driver per
# speck of dust is noise on their rig.
_EPSILON = 1e-5


def _rotation_path(pose_bone):
    """The rotation channel this bone actually reads, given its rotation mode."""
    mode = pose_bone.rotation_mode
    if mode == "QUATERNION":
        return "rotation_quaternion"
    if mode == "AXIS_ANGLE":
        return None      # four numbers that are not a linear blend - unsupported
    return "rotation_euler"


def capture_pose_deltas(pose_bone):
    """Channels this bone is posed away from rest, as (path, index, rest, posed).

    Rotation is all-or-nothing for a quaternion bone: if any component moved,
    all four are returned. Blending a quaternion means moving every component
    together - driving X while leaving W at its rest value is not a rotation
    half-way to the pose, it is a different rotation.
    """
    captured = []
    rotation_path = _rotation_path(pose_bone)

    for path in ("location", "rotation_euler", "rotation_quaternion", "scale"):
        if path in ("rotation_euler", "rotation_quaternion") and path != rotation_path:
            continue
        rest = _REST[path]
        posed = tuple(getattr(pose_bone, path))
        moved = [i for i, (r, p) in enumerate(zip(rest, posed)) if abs(p - r) > _EPSILON]
        if not moved:
            continue
        indices = range(len(rest)) if path == "rotation_quaternion" else moved
        for index in indices:
            captured.append((path, index, rest[index], posed[index]))
    return captured


def clear_pose(pose_bone):
    """Return one bone to rest, leaving the rest of the rig alone.

    Deliberately not bpy.ops.pose.transforms_clear(), which acts on the whole
    selection and would wipe posing the artist wants to keep on other bones.
    """
    pose_bone.location = _REST["location"]
    pose_bone.scale = _REST["scale"]
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.rotation_quaternion = _REST["rotation_quaternion"]
    elif pose_bone.rotation_mode != "AXIS_ANGLE":
        pose_bone.rotation_euler = _REST["rotation_euler"]


def restore_pose(pose_bone, captured):
    """Put a captured pose back on a bone after a failed apply.

    The counterpart to clear_pose. ``captured`` carries the posed value for
    every component that had moved away from rest, so writing those back
    reproduces exactly what the artist had -- the components that were already
    at rest are untouched rather than being re-written from a stale snapshot.
    """
    for path, index, _rest, posed in captured or ():
        try:
            getattr(pose_bone, path)[index] = posed
        except (AttributeError, IndexError, TypeError):
            continue


def channels_from_pose(pose_bone, envelope, captured=None):
    """Driver channels blending rest -> the captured pose, driven by ``envelope``.

    ``envelope`` is the template's own expression, expected to run 0 at rest and
    1 at the extreme. Each channel becomes ``rest + delta*(envelope)``, so the
    envelope keeps its authored shape and only its amplitude is per-channel.

    For a quaternion bone this is an nlerp - the four components blended
    linearly, with Blender normalising the result. Measured against a true
    slerp: 0.07 deg of timing difference on a 15 deg lid, 0.14 deg on 45 deg,
    and the endpoints are exact either way. A slerp in a driver expression would
    cost an acos and a pair of sins per component and buy nothing visible.
    """
    if captured is None:
        captured = capture_pose_deltas(pose_bone)

    channels = []
    for path, index, rest, posed in captured:
        delta = driver_literals.format_computed_literal(posed - rest)
        if abs(rest) < 1e-12:
            expression = "%s*(%s)" % (delta, envelope)
        else:
            expression = "%s+%s*(%s)" % (
                driver_literals.format_computed_literal(rest), delta, envelope,
            )
        channels.append({
            # Kept as numbers as well as baked into the expression, so the
            # memory can rebuild this channel later without the pose.
            "pose_rest": float(rest),
            "pose_delta": float(posed - rest),
            "id": "%s_%d" % (path, index),
            "label": "%s %s" % (path.replace("_", " ").title(), "XYZW"[index] if path == "rotation_quaternion" else "XYZ"[index]),
            "data_path": path,
            "index": index,
            "expression": expression,
        })
    return channels


def describe_capture(captured):
    """Short human summary of what was recorded, for the operator report."""
    paths = []
    for path, _index, _rest, _posed in captured:
        label = {
            "location": "location",
            "rotation_euler": "rotation",
            "rotation_quaternion": "rotation",
            "scale": "scale",
        }[path]
        if label not in paths:
            paths.append(label)
    return " + ".join(paths) if paths else "nothing"


def expression_from_stored_pose(envelope, pose_rest, pose_delta):
    """Rebuild one channel from an amplitude the memory kept.

    The mirror of channels_from_pose, for a re-apply: the bone is at rest by
    then (that is the whole design), so the amplitude has to come from the
    remembered entry instead of from the bone.
    """
    delta = driver_literals.format_computed_literal(pose_delta)
    if abs(float(pose_rest)) < 1e-12:
        return "%s*(%s)" % (delta, envelope)
    return "%s+%s*(%s)" % (
        driver_literals.format_computed_literal(pose_rest), delta, envelope,
    )


# Numeric duplicate suffixes Blender appends: Lid.T.R.004, Lid_L_001.
_NUMERIC_SUFFIX = re.compile(r"[._-]\d+$")
_SIDE_WORDS = {
    "l": "L", "left": "L", "lft": "L",
    "r": "R", "right": "R", "rgt": "R",
}


def bone_side(name):
    """Return the final side token as L or R, or None for an unsided bone.

    Strip numeric duplicate suffixes before scanning tokens from the end, so
    lid.T.R.004 and eyelid_left_upper resolve correctly. Callers must not
    assign a default side when this returns None.
    """
    stem = _NUMERIC_SUFFIX.sub("", str(name or ""))
    for token in reversed(re.split(r"[._\- ]+", stem)):
        side = _SIDE_WORDS.get(token.lower())
        if side:
            return side
    return None


def side_time_offsets(lead_frames):
    """Per-side frame offsets for an eye-lead of ``lead_frames``.

    Positive leads with the LEFT lid, negative with the right; the magnitude is
    the gap between the two, in whole frames.

    Only the leading side is moved, and always backwards in time. The
    alternative - splitting the gap and shifting each side half of it - would
    keep the pair centred on the blink point, but half of an odd gap is half a
    frame, and the blink envelope is sampled at whole frames: a half-frame peak
    closes the lid to 0.924 instead of 1. Whole frames only.
    """
    lead = int(lead_frames or 0)
    if lead > 0:
        return {"L": -lead, "R": 0}
    if lead < 0:
        return {"L": 0, "R": lead}
    return {"L": 0, "R": 0}
