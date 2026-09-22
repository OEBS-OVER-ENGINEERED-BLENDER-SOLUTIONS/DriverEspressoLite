"""Which sections of the Motion panel belong to which template.

The panel grew section by section, and each one decided for itself whether to
draw -- so a section added for one kind of recipe appeared for all of them
unless somebody remembered to exclude it. Measured across all 343 templates:
the TEMPLATE EXPRESSION box drew for 306, including both authoring recipes,
which have no driver expression at all. On an authoring rig that put a "0.0"
expression, a Duration, a Rest Start mode and an additive Clamp on screen,
none of which it reads -- it flies a rig.

So the rule lives here, as one table, rather than as a condition per section.
A recipe is exactly one KIND, and each section names the kinds it belongs to.

    DRIVER      an ordinary recipe: a driver expression, or motion channels,
                applied onto an object's transform or a property.
    GENERATED   owns a generated destination -- node groups and modifiers it
                builds and manages itself.
    AUTHORING   authors a rig with its own solve and its own controls: the
                authoring rig flies one, recorded-path camera drives one. No expression, no
                driver, no rest state.
"""

from __future__ import annotations

from ...engine.motion_stack import capabilities as capabilities_module

DRIVER = "DRIVER"
GENERATED = "GENERATED"
AUTHORING = "AUTHORING"

ALL_KINDS = (DRIVER, GENERATED, AUTHORING)


def template_kind(template):
    """Exactly one kind per template. Authoring wins: a recipe that authors a
    rig may also declare a placeholder expression, and the rig is what it is."""
    if not template:
        return DRIVER
    if template.get("authoring_kind"):
        return AUTHORING
    if capabilities_module.owns_destination(template.get("id")):
        return GENERATED
    # The definitive surface-motion systems own a generated destination too --
    # a Geometry Nodes modifier, or a compositor group for atmospheric. They
    # declare a placeholder "0.0" expression because a record needs one, and
    # drawing that as TEMPLATE EXPRESSION tells the artist the recipe computes
    # nothing. It is the same trap an authoring rig set, and the same answer.
    from ...apply.spatial import spatial_fields

    if template.get("id") in spatial_fields.SURFACE_IDS:
        return GENERATED
    return DRIVER


# section -> the kinds it belongs to. A section absent from a kind is not
# drawn for it at all: no header, no body, no explanation.
SECTION_OWNERS = {
    # The generated expression, its Duration, Rest Start and Clamp. Only a
    # driver recipe has any of these. GENERATED routes describe and manage
    # their setup in APPLY & MANAGE; AUTHORING recipes fly or drive a rig
    # whose controls are their own.
    "expression": (DRIVER,),
    # Catalogue parameters, target slots, and any recipe-owned control block.
    # Every kind has something to put here.
    "parameters": (DRIVER, GENERATED, AUTHORING),
    # "No parameters needed" -- honest for a recipe whose parameters ARE the
    # catalogue's, a contradiction under a rig with forty controls of its own.
    "empty_parameter_note": (DRIVER, GENERATED),
    # The apply action and what it applies to. Universal.
    "apply_manage": (DRIVER, GENERATED, AUTHORING),
}


def applies(section, template):
    """Whether ``section`` belongs to this template's kind."""
    return template_kind(template) in SECTION_OWNERS.get(section, ALL_KINDS)


def owns_its_apply(template):
    """True when the recipe manages its own setup instead of committing a
    driver -- the two kinds for which the expression half is meaningless."""
    return not applies("expression", template)


__all__ = ("DRIVER", "GENERATED", "AUTHORING", "ALL_KINDS", "SECTION_OWNERS",
           "template_kind", "applies", "owns_its_apply")
