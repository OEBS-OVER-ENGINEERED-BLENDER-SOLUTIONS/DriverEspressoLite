"""Audio reactivity — no recipe in Driver Espresso Lite uses it.

No recipe here is audio-driven, so the analysis engine is not part of this
build. These functions answer rather than raise, because they sit on the
GENERIC apply path: every template consults them, not only audio ones.

`compatible()` is false for everything this build ships, so the values below
are the ones the apply path expects for a non-audio recipe.
"""

from __future__ import annotations


def compatible(template):
    """No recipe here declares audio."""
    return False


def application_block_message(template, scene):
    """Nothing to block: no recipe here needs an audio analysis first."""
    return ""


def template_config(template, scene):
    return None


def prepare_expression(template, expression, scene, output_baseline=0.0):
    """No audio layer to add, so the expression passes through untouched."""
    return expression


def bind_driver(driver, template, scene):
    """Nothing to bind."""
    return False


def clear_variables(driver, template):
    """No Espresso-owned audio variable can exist, so none is removed."""
    return 0


def prepare_driver(driver, template, scene):
    """A dry route, which is the only kind here, always succeeds."""
    return True, ""


def validation_template(template, scene):
    return template


def has_analysis(scene):
    return False


def analysis_matches(scene, props):
    return False


def controller_for_scene(scene):
    return None


def has_owned_binding(driver, template=None):
    return False


__all__ = (
    "analysis_matches", "application_block_message", "bind_driver",
    "clear_variables", "compatible", "controller_for_scene", "has_analysis",
    "has_owned_binding", "prepare_driver", "prepare_expression",
    "template_config", "validation_template",
)
