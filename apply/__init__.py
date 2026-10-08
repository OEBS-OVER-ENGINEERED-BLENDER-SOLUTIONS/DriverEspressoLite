"""Putting driver expressions onto real Blender properties.

Implementation modules live in responsibility subpackages.  The lazy facade
keeps the established ``from <package>.apply import module`` contract.
"""

from importlib import import_module


_MODULES = {
    "apply_behavior": ".core.apply_behavior",
    "apply_target": ".core.apply_target",
    "driver_targets": ".core.driver_targets",
    "driver_manager": ".core.driver_manager",
    "source_binding": ".core.source_binding",
    "target_memory": ".core.target_memory",
    "application_plan": ".core.application_plan",
    "applied_motion": ".motion.applied_motion",
    "applied_motion_manager": ".motion.applied_motion_manager",
    "motion_channels": ".motion.motion_channels",
}

__all__ = tuple(_MODULES)


def __getattr__(name):
    module_path = _MODULES.get(name)
    if module_path is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    module = import_module(module_path, __name__)
    globals()[name] = module
    return module
