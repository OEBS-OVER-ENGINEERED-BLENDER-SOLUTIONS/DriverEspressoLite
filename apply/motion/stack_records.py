"""Store Motion Stack data inside the canonical applied-motion extras."""

from __future__ import annotations

from typing import Mapping


STACK_KEY = "motion_stack"


def with_stack(extras, value):
    updated = dict(extras or {})
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise TypeError("Motion Stack payload must be a mapping.")
    updated[STACK_KEY] = dict(value)
    return updated


def stack_from_extras(extras):
    if not isinstance(extras, Mapping):
        return None
    value = extras.get(STACK_KEY)
    return dict(value) if isinstance(value, Mapping) else None
