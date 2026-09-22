"""Expression building, validation, and numeric formatting.

Blender-free by design; these modules never import bpy.
"""

from importlib import import_module


_MODULES = {
    "bake": ".baking.bake",
    "smart_bake": ".baking.smart_bake",
    "live_control_core": ".control.live_control_core",
    "aliasing": ".expression.aliasing",
    "driver_literals": ".expression.driver_literals",
    "duration": ".expression.duration",
    "utils": ".expression.utils",
    "button_targeting": ".targeting.button_targeting",
    "quaternion_channels": ".targeting.quaternion_channels",
    "stagger_apply": ".targeting.stagger_apply",
}

__all__ = tuple(_MODULES)


def __getattr__(name):
    module_path = _MODULES.get(name)
    if module_path is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    module = import_module(module_path, __name__)
    globals()[name] = module
    return module
