"""Generated systems workflow routes, none of which this build includes.

Not the generic "absent" stub, because `catalogue/core/catalogue_contracts.py`
walks `WORKFLOWS` at import time to build its contract table and compares each
row's route against the three constants below. Raising on attribute access
would stop the catalogue loading at all.

An empty `WORKFLOWS` is the honest answer: the loop runs zero times, so the
contract table gains no entry for a generated systems recipe, which is correct —
Driver Espresso Lite ships none. The route constants keep their original string
values so any comparison against them still behaves.

The workflow definitions themselves are not part of this build.
"""

from __future__ import annotations

OBJECT_SET = "OBJECT_SET"
PREPARED_FIELD = "PREPARED_FIELD"
GENERATED_GRAPHIC = "GENERATED_GRAPHIC"

#: Empty by design. See the module docstring.
WORKFLOWS = ()

ROUTES = (OBJECT_SET, PREPARED_FIELD, GENERATED_GRAPHIC)

def ids(route=""):
    """No workflow ids: WORKFLOWS is empty in this product."""
    return ()


__all__ = ("ids", "OBJECT_SET", "PREPARED_FIELD", "GENERATED_GRAPHIC", "ROUTES",
           "WORKFLOWS")
