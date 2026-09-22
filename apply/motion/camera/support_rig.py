"""Support rig teardown and lookup.

Driver Espresso Lite ships no camera recipes, so it never builds a camera rig.
These are the teardown calls the shared clear paths make unconditionally; with
nothing to tear down, each one correctly does nothing.
"""

from __future__ import annotations

def resolve_support_rig(_camera=None):
    """No camera in this product carries a support rig."""
    return None


def clear_support_rig(_camera=None, _effect_id=""):
    """Nothing to stand back down."""
    return 0
