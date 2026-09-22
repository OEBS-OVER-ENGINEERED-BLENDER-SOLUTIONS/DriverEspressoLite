"""Persistent config stored outside the addon directory so it survives reinstalls."""

from __future__ import annotations

import json
import pathlib

import bpy

from .product import identity

_CONFIG_VERSION = 1
# One folder per product: this file stores preset-usage rankings, and two
# products sharing a folder would rank each other's clicks.
_DIR_NAME = identity.EXTENSION_ID
_FILE_NAME = "config.json"

_cache: dict | None = None
_cache_mtime: float = 0.0


def _config_path() -> pathlib.Path:
    return pathlib.Path(bpy.utils.user_resource('CONFIG')) / _DIR_NAME / _FILE_NAME


def _default_config() -> dict:
    return {
        "version": _CONFIG_VERSION,
        "master_preset_usage": {},
        "param_preset_usage": {},
    }


def load() -> dict:
    """Return the full config dict, using a file-mtime cache to avoid redundant reads."""
    global _cache, _cache_mtime
    path = _config_path()
    try:
        exists = path.exists()
    except OSError:
        return _cache if _cache is not None else _default_config()
    if not exists:
        _cache = _default_config()
        _cache_mtime = 0.0
        return _cache
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = _default_config()
    except Exception:
        data = _default_config()
    data.setdefault("version", _CONFIG_VERSION)
    data.setdefault("master_preset_usage", {})
    data.setdefault("param_preset_usage", {})
    _cache = data
    _cache_mtime = mtime
    return _cache


def save(data: dict) -> None:
    """Write the config dict to disk and update the cache."""
    global _cache, _cache_mtime
    path = _config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Usage ranking is optional; an unavailable Blender config mount must
        # never cancel the artist's actual preset operation.
        _cache = data
        _cache_mtime = 0.0
        return
    _cache = data
    try:
        _cache_mtime = path.stat().st_mtime
    except OSError:
        _cache_mtime = 0.0


def record_preset_click(template_id: str, preset_label: str) -> None:
    """Increment the click count for a master preset, keyed by its label.

    Keyed by label rather than by position, matching how parameter presets are
    already stored. A position is meaningless on its own - a file full of
    ``{"4": 25}`` cannot be read by a human - and worse, it silently rots: adding,
    removing or reordering a template's presets re-points every stored index at a
    different preset, so the "most used" shelf starts ranking by counts that were
    earned by something else.
    """
    data = load()
    usage = data.setdefault("master_preset_usage", {})
    bucket = usage.setdefault(template_id, {})
    bucket[preset_label] = bucket.get(preset_label, 0) + 1
    save(data)


def get_preset_usage(template_id: str) -> dict:
    """Return {preset_label: click_count} for one template."""
    return load().get("master_preset_usage", {}).get(template_id, {})


def record_param_preset_click(template_id: str, slot_index: int, preset_label: str) -> None:
    """Increment the click count for an individual parameter preset."""
    data = load()
    usage = data.setdefault("param_preset_usage", {})
    key = f"{template_id}:{slot_index}"
    bucket = usage.setdefault(key, {})
    bucket[preset_label] = bucket.get(preset_label, 0) + 1
    save(data)


def get_param_preset_usage(template_id: str, slot_index: int) -> dict:
    """Return {preset_label: click_count} for one parameter slot."""
    key = f"{template_id}:{slot_index}"
    return load().get("param_preset_usage", {}).get(key, {})
