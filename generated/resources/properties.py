"""Typed custom-property creation for generated carriers."""

from __future__ import annotations


def define(owner, key: str, value, *, description: str = "", minimum=None,
           maximum=None, soft_minimum=None, soft_maximum=None, subtype=None):
    owner[key] = value
    options = {}
    if description:
        options["description"] = description
    if minimum is not None:
        options["min"] = minimum
    if maximum is not None:
        options["max"] = maximum
    if soft_minimum is not None:
        options["soft_min"] = soft_minimum
    if soft_maximum is not None:
        options["soft_max"] = soft_maximum
    if subtype:
        options["subtype"] = subtype
    if options:
        owner.id_properties_ui(key).update(**options)
    return key


def remove(owner, key: str) -> bool:
    if owner is None or key not in owner.keys():
        return False
    del owner[key]
    return True
