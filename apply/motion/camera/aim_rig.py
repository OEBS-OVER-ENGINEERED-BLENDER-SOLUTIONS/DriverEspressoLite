"""Aim rig teardown.

Driver Espresso Lite ships no camera recipes, so it never builds a camera rig.
These are the teardown calls the shared clear paths make unconditionally; with
nothing to tear down, each one correctly does nothing.
"""

from __future__ import annotations

def clear_tracking(_camera=None, _effect_id=""):
    """No aim constraints exist here, so none are removed."""
    return 0
