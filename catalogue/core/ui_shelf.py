"""Additive catalogue-shelf grouping. Does not change template IDs."""

from __future__ import annotations

UI_GROUPS = ("essential", "timing", "spatial", "additional")
UI_GROUP_LABELS = {
    "essential": "Essential",
    "timing": "Timing",
    "spatial": "Spatial",
    "additional": "Additional",
}

#: Per-template overrides for how a recipe's parameters are shelved.
#:
#: Empty here. Every entry this table ever held keyed a generated-system recipe
#: -- helix distribution, the typography and chart builders, the travel reveal
#: and grid showcase -- and this product ships none of them. `ui_group()` reads
#: it with `.get(template_id, {})` and falls through to `infer_ui_group()`, which
#: is what shelved every recipe here anyway.
HEAVY_UI_GROUPS = {}


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
    template_id = (template or {}).get("id", "")
    overlay = HEAVY_UI_GROUPS.get(template_id, {}).get((param or {}).get("token"))
    if overlay in UI_GROUPS:
        return overlay
    if len((template or {}).get("params") or ()) < 10:
        return "essential"
    return infer_ui_group(param)


def ui_pair(param):
    value = (param or {}).get("ui_pair")
    return str(value) if value else ""
