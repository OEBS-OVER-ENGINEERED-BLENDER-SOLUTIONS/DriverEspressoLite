"""generated systems route dispatch — no route is registered in Driver Espresso Lite.

A dispatcher would resolve a template id to a route adapter through
`capabilities.get()`. That registry is empty in this build, so `adapter_for()`
answers None for every recipe.

Every caller handles a None adapter; that seam is what the whole boundary
rests on.
"""

from __future__ import annotations


def adapter_for(template_id):
    """No route adapter: this product registers no generated systems capability."""
    return None


def state_fingerprint(context, template_id):
    """Constant, because there is no route state to invalidate a card against."""
    active = getattr(context, "active_object", None)
    try:
        pointer = int(active.as_pointer()) if active is not None else 0
    except (AttributeError, ReferenceError, TypeError, ValueError):
        pointer = 0
    return (str(template_id or ""), pointer)


__all__ = ("adapter_for", "state_fingerprint")
