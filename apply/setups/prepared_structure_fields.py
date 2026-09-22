"""Import compatibility for unavailable prepared-structure fields."""

from __future__ import annotations

_ABSENT = "prepared_structure_fields is not part of Driver Espresso Lite"


def __getattr__(name):
    # Dunder lookups belong to the import machinery, not to callers. Answering
    # `__path__` with a RuntimeError makes Python think this is a broken
    # package rather than a plain module, and the whole add-on fails to import.
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    raise RuntimeError("%s (attribute %r)" % (_ABSENT, name))


__all__ = ()
