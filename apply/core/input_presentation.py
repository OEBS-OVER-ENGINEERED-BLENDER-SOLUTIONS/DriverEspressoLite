"""Pure presentation contract for source-aware apply flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


PREVIEW_TRANSFER = "TRANSFER_CURVE"
PREVIEW_TIMELINE = "OVER_TIME"


@dataclass(frozen=True)
class InputFlow:
    kind: str
    source_label: str
    recipe_label: str
    target_labels: Tuple[str, ...]
    preview_mode: str
    can_apply: bool
    reason: str = ""
    source_value: float | None = None
    output_value: float | None = None

    @property
    def chain(self):
        source = self.source_label or "Timeline"
        target = ", ".join(self.target_labels) or "Choose target"
        return f"{source} -> {self.recipe_label or 'Recipe'} -> {target}"


def build_input_flow(*, recipe_label, target_labels=(), source_required=False,
                     source_active=False, source_label="", uses_timeline=True,
                     source_value=None, output_value=None, target_reason=""):
    kind = "HYBRID" if source_required and uses_timeline else (
        "SOURCE" if source_required else "TIMELINE"
    )
    preview = PREVIEW_TIMELINE if uses_timeline else PREVIEW_TRANSFER
    if source_required and not source_active:
        return InputFlow(
            kind, source_label, recipe_label, tuple(target_labels), preview,
            False, "Choose or relink the required Espresso Input before applying.",
            source_value, output_value,
        )
    if target_reason:
        return InputFlow(
            kind, source_label, recipe_label, tuple(target_labels), preview,
            False, target_reason, source_value, output_value,
        )
    return InputFlow(
        kind, source_label, recipe_label, tuple(target_labels), preview,
        True, "", source_value, output_value,
    )
