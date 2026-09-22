"""Lens helper teardown.

Driver Espresso Lite ships no camera recipes, so it never builds a camera rig.
These are the teardown calls the shared clear paths make unconditionally; with
nothing to tear down, each one correctly does nothing.
"""

from __future__ import annotations

def clear_lens_helpers(_camera=None, _effect_id=""):
    """No focus or lens helpers were generated."""
    return 0
