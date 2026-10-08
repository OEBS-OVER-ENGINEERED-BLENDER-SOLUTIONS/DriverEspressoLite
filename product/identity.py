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


__all__ = ("EXTENSION_ID", "NAME", "TAGLINE")
