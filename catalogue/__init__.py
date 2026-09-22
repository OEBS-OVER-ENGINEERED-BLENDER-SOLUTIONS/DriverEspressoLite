"""Template catalogue: what the templates ARE.

Blender-free by design so the whole catalogue can be validated outside Blender.
"""

from importlib import import_module


# The runtime reads the frozen records in catalogue/core/frozen_records.py.
# Nothing here imports template authoring sources, and no such module loads at
# registration; keep it that way when adding an entry.
_MODULES = {
    "browse_groups": ".core.browse_groups",
    "catalogue_contracts": ".core.catalogue_contracts",
    "effect_ids": ".core.effect_ids",
    "taxonomy": ".core.taxonomy",
    "templates": ".core.templates",
    "use_cases": ".core.use_cases",
    "palettes": ".presets.palettes",
    "preset_blocks": ".presets.preset_blocks",
}

__all__ = tuple(_MODULES)


def __getattr__(name):
    module_path = _MODULES.get(name)
    if module_path is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    module = import_module(module_path, __name__)
    globals()[name] = module
    return module
