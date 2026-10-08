"""Additive catalogue-shelf grouping. Does not change template IDs."""

from __future__ import annotations

UI_GROUPS = ("essential", "timing", "spatial", "additional")
UI_GROUP_LABELS = {
    "essential": "Essential",
    "timing": "Timing",
    "spatial": "Spatial",
    "additional": "Additional",
}

def infer_ui_group(param):
    token = str((param or {}).get("token") or "").upper()
    unit = str((param or {}).get("unit") or "").lower()
    if any(part in token for part in ("FRAME", "DURATION", "PERIOD", "STAGGER", "PHASE", "SPEED", "EASE")):
        return "timing"
    if unit in {"frame", "frames"}:
        return "timing"
    if any(part in token for part in ("CENTRE", "CENTER", "RADIUS", "WIDTH", "HEIGHT", "DEPTH", "DISTANCE", "SPACING")):
        return "spatial"
    if unit in {"m", "degree", "°"}:
        return "spatial"
    return "essential"


def ui_group(template, param):
    explicit = (param or {}).get("ui_group")
    if explicit in UI_GROUPS:
        return explicit
    if len((template or {}).get("params") or ()) < 10:
        return "essential"
    return infer_ui_group(param)

