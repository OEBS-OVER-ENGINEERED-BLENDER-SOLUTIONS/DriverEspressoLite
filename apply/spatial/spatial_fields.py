"""Spatial field identifiers — no recipe in Driver Espresso Lite builds one.

This answers rather than raises: the panel consults these constants for every
template to decide whether a spatial section belongs on screen, so raising
would break drawing for recipes that have nothing to do with spatial fields.

The tag and prefix strings keep recognisable values, so a scene that already
carries a spatial modifier can still be identified and cleaned up. The id sets
are empty because no recipe in this build creates a spatial field, which is
what `supports()` reports.
"""

from __future__ import annotations

TEMPLATE_TAG = "__espresso_template_id"
MODIFIER_PREFIX = "Espresso Spatial "
GROUP_PREFIX = "Driver Espresso Spatial - "

#: Empty by design -- see the module docstring.
GEOMETRY_IDS = frozenset()
SHADER_IDS = frozenset()
SURFACE_IDS = frozenset()

HAZE_ID = ""


def supports(template_id):
    """Always False here: none of the spatial recipes ship in this product."""
    return False


__all__ = ("GEOMETRY_IDS", "GROUP_PREFIX", "HAZE_ID", "MODIFIER_PREFIX",
           "SHADER_IDS", "SURFACE_IDS", "TEMPLATE_TAG", "supports")
