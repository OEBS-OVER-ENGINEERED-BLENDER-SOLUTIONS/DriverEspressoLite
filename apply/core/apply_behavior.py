"""Helpers for apply-time driver behaviors such as rest-start modes."""

from __future__ import annotations

from ...engine import utils


def read_target_current_value(owner, data_path, index):
    try:
        value = owner.path_resolve(data_path)
    except Exception:
        if data_path.startswith('["') and data_path.endswith('"]'):
            value = owner[data_path[2:-2]]
        else:
            value = getattr(owner, data_path)
    if index >= 0:
        try:
            value = value[index]
        except TypeError:
            # Blender records scalar node-socket FCurves with array_index 0,
            # while path_resolve() correctly returns the scalar itself.
            pass
    elif not isinstance(value, (int, float)):
        # index -1 means "the whole property". For an array that is a real
        # case, not an error: clicking a colour SWATCH (rather than one of its
        # component sliders) reports index -1, and path_resolve then hands back
        # a bpy_prop_array. driver_add(path, -1) goes on to drive every
        # component from the one expression, so a single representative rest
        # value is all this can carry - take the first component. Calling
        # float() on the array instead raised TypeError and killed every apply
        # onto a colour swatch, even with Rest Start switched off, because this
        # value is read before the mode is consulted.
        try:
            value = value[0]
        except (TypeError, IndexError, KeyError):
            return 0.0
    return float(value)


def capture_target_rest_state(
    target, expression, template, scene, mode, output_baseline=None,
    additive_profile=None, snapshot_frame=None,
):
    """Capture the rest state, optionally at a given frame. ``snapshot_frame`` lets a
    re-apply sample where the driver was originally applied instead of at the current
    playhead, so Update Last Target gives the same result at any frame.
    """
    rest_value = read_target_current_value(target.owner, target.data_path, target.index)
    return utils.capture_rest_start_state(
        expression,
        template,
        scene,
        mode,
        rest_value,
        snapshot_frame=(
            snapshot_frame if snapshot_frame is not None
            else getattr(scene, "frame_current", getattr(scene, "frame_start", 1))
        ),
        output_baseline=output_baseline,
        additive_profile=additive_profile,
        clamp_range=_clamp_range(template, scene, mode),
    )


def _clamp_range(template, scene, mode):
    """The artist's Min/Max limits, or None when the clamp does not apply.

    Every rest-state caller funnels through capture_target_rest_state, so resolving it
    once here keeps the toggle out of four separate call sites. The
    has_ordered_output_range test is repeated from the panel on purpose: the toggle is
    hidden for templates that carry their own Minimum/Maximum, but the flag is a scene
    property and survives switching to one of them, so a stale True must not clamp a
    template whose own range is already the answer.
    """
    if mode != utils.REST_START_ADDITIVE:
        return None
    props = getattr(scene, "espresso_props", None)
    if not getattr(props, "clamp_additive", False):
        return None
    from ...catalogue import catalogue_contracts

    if catalogue_contracts.has_ordered_output_range(template):
        # Nothing to add: the template's own Minimum/Maximum already bound the value, so
        # a clamp here would never fire. A kernel that exceeds its declared range should
        # be fixed in the catalogue, not hidden behind a toggle.
        return None
    # No declared range, so the artist supplies one - the Kick Drum case.
    return (float(props.clamp_min), float(props.clamp_max))
