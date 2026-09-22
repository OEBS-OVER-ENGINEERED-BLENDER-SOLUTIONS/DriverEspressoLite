"""Blender-free helpers for expanding right-click button targets."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ButtonDriverTarget:
    owner: object
    data_path: str
    index: int = -1
    # The Object this driver is FOR, when the caller knows it.
    #
    # A driver on a material's node tree reports the shader tree, which is two
    # steps from any object - and a material shared by ten objects has ten
    # equally valid owners, so guessing is refused rather than done. But code
    # applying across a selection is looping over the objects and knows exactly
    # which one it is on, so it should say so instead of leaving the binder to
    # work it out and fail.
    owner_object: object = None


def _normalized_array_length(array_length):
    try:
        length = int(array_length or 0)
    except Exception:
        return 0
    return length if length > 1 else 0


def multi_target_count(array_length):
    """Return the eligible array size for multi-apply, or 0 when scalar."""
    return _normalized_array_length(array_length)


def expand_multi_targets(target, array_length):
    """Return all sibling index targets for an arrayed property click.

    Scalar properties, single-item arrays, or non-indexed clicks return ``[]``
    so callers can cleanly hide the multi-apply action.
    """
    length = _normalized_array_length(array_length)
    if length == 0 or target is None or int(getattr(target, "index", -1)) < 0:
        return []
    return [
        ButtonDriverTarget(target.owner, target.data_path, index)
        for index in range(length)
    ]
