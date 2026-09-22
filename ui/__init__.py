"""Blender UI: panels, operators, properties, and previews."""

from importlib import import_module


_MODULES = {
    "bake_applied": ".actions.bake_applied",
    "driver_manager": ".actions.driver_manager",
    "operators": ".actions.operators",
    "button_menu_map": ".menus.button_menu_map",
    "context_menu": ".menus.context_menu",
    "live_controls": ".state.live_controls",
    "props": ".state.props",
    "image_preview": ".views.image_preview",
    "panels": ".views.panels",
    "source_display": ".views.source_display",
    "visualizer": ".views.visualizer",
}

__all__ = tuple(_MODULES)


def __getattr__(name):
    module_path = _MODULES.get(name)
    if module_path is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    module = import_module(module_path, __name__)
    globals()[name] = module
    return module
