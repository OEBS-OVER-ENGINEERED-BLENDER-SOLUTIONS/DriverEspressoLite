"""Typed custom-property creation for generated carriers."""

from __future__ import annotations


def define(owner, key: str, value, *, description: str = ""):
    owner[key] = value
    if description:
        owner.id_properties_ui(key).update(description=description)
    return key
