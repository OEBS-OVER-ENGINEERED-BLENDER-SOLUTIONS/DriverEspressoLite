"""Live bindings — not part of Driver Espresso Lite.

The implementation is not part of this build. This module still exists, and
still answers, because a few call sites ask a subsystem directly instead of
going through `capabilities` / `generated_routes`, which is where the rest
of the boundary is handled.

Every value below is the one the real module returned for a template that was
not its own. Driver Espresso Lite ships no recipe this subsystem owns, so that was already
the only answer it ever gave here.
"""

from __future__ import annotations

def binding_key(*args, **kwargs):
    return ""


def read_values(*args, **kwargs):
    return {}


def resolve(*args, **kwargs):
    return None


def supports(*args, **kwargs):
    return False


def write_values(*args, **kwargs):
    return (False, "Not available in this product.")


__all__ = ("binding_key", "read_values", "resolve", "supports", "write_values")
