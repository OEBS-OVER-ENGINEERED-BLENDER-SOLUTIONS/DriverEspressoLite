"""What this build calls itself.

The name appears in the manifest, the add-on preferences, the panel header
and the right-click menu. It is defined here once so those cannot disagree;
nothing else in the package hard-codes it.

`EXTENSION_ID` is a separate value. The name is display text; the id is what
Blender writes into a saved scene's add-on reference, so changing it would
orphan scenes that refer to the old one.
"""

from __future__ import annotations

#: Display name: the manifest, the panel header and the menu heading.
NAME = "Driver Espresso Lite"

#: One line, for the manifest and the extension listing.
TAGLINE = "A starter set of native Blender driver recipes"

#: The Blender extension id. NOT renamed with the product -- see the module
#: docstring. Saved scenes reference add-ons by this.
EXTENSION_ID = "driver_espresso_lite"


#: Whether this build draws the illustrated Visual Preview: pictures that
#: perform the motion rather than chart it. The graph preview, which reports
#: what the driver actually does, is always available.
HAS_VISUAL_PREVIEW = False

#: Whether this build offers the Favorites & Recent section. Read from this
#: flag rather than by comparing EXTENSION_ID, so identity lives in one file.
HAS_FAVORITES = False

__all__ = ("EXTENSION_ID", "HAS_FAVORITES", "NAME", "TAGLINE")
