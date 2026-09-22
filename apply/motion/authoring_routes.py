"""Lifecycle adapters for recipes that own their own apply action.

An authoring recipe (``authoring_kind`` on the template) builds a rig rather
than writing an expression, so the Applied Effects list cannot remove or bake
it by walking its drivers. Each one that appears in the list registers an
adapter here, keyed by template id, answering the same ``resolve`` / ``clear``
/ ``bake`` / ``stable_key`` contract the motion-graphics routes do. Explicit
rather than discovered: a rig that is not listed here is not routed, and its
drivers would be swept one by one, which for a rig is the wrong answer.
"""

from __future__ import annotations

#: Empty in this build: it has no `authoring_kind` records, so no recipe
#: registers an adapter here.
ADAPTERS = {}


def adapter_for(template_id):
    return ADAPTERS.get(str(template_id or ""))


__all__ = ("ADAPTERS", "adapter_for")
