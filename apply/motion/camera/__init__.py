"""Camera rig teardown for a product with no camera rigs.

Driver Espresso Lite ships no camera recipes, so it never builds a camera rig.
These are the teardown calls the shared clear paths make unconditionally; with
nothing to tear down, each one correctly does nothing.
"""

from __future__ import annotations

