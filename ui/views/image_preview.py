"""Blender-side preview-image wiring for the Driver Espresso image graph.

Turns an RGBA pixel buffer (produced by ``visualizer.render_image_pixels``) into
a ``bpy.utils.previews`` icon that a panel can show inline via
``layout.template_icon``. Everything here is best-effort: callers must treat a
``None`` icon id as "image mode unavailable" and fall back to the text graph.
"""

from __future__ import annotations

import bpy
# Not optional and not redundant: bpy.utils.previews is a submodule that
# `import bpy` does not pull in. Without this line register()'s
# bpy.utils.previews.new() raises AttributeError, the bare except below
# swallows it, _pcoll stays None, available() is False forever, and every
# illustration silently becomes "Illustrations need Pixel Preview."
import bpy.utils.previews


_pcoll = None
_MAX_ENTRIES = 24

# Where the collection is remembered between add-on reloads.
#
# A preview collection must be handed back to bpy.utils.previews.remove(), and
# Blender tracks the outstanding ones in its own module-level registry. Our
# _pcoll reference does NOT survive a reload: reloading rebuilds this module
# with _pcoll = None, so unregister() has nothing to hand back, the old
# collection is orphaned, and Blender reports
# "ResourceWarning: ImagePreviewCollection ... left open" when it is collected.
#
# bpy.app.driver_namespace is a plain dict owned by Blender, so it outlives this
# module being rebuilt. Stashing the collection there lets a later load find and
# reuse it instead of leaking one collection per reload.
_STASH_KEY = "driver_espresso_preview_collection"


def _stashed():
    return bpy.app.driver_namespace.get(_STASH_KEY)


def register():
    global _pcoll
    _pcoll = _stashed()
    if _pcoll is not None:
        return
    try:
        _pcoll = bpy.utils.previews.new()
        bpy.app.driver_namespace[_STASH_KEY] = _pcoll
    except Exception:
        _pcoll = None


def unregister():
    global _pcoll
    collection = _pcoll if _pcoll is not None else _stashed()
    if collection is not None:
        try:
            bpy.utils.previews.remove(collection)
        except Exception:
            pass
    bpy.app.driver_namespace.pop(_STASH_KEY, None)
    _pcoll = None


def available():
    return _pcoll is not None


def cached_icon_id(name):
    """Return the icon id for an already-rendered graph, or None on a miss.

    Lets callers skip regenerating the pixel buffer entirely when the same graph
    (same expression / style / frame) was drawn on a previous redraw.
    """
    if _pcoll is None:
        return None
    preview = _pcoll.get(name)
    return preview.icon_id if preview is not None else None


def _upload(preview, pixels):
    # foreach_set is dramatically faster than assignment, especially with a
    # numpy buffer; fall back to assignment if the buffer type is unsupported.
    try:
        preview.image_pixels_float.foreach_set(pixels)
    except (TypeError, ValueError, RuntimeError):
        preview.image_pixels_float = pixels if isinstance(pixels, list) else list(pixels)


def get_graph_icon_id(name, pixels, size):
    """Create/update a preview for ``name`` and return its icon id, or None."""
    if _pcoll is None:
        return None
    try:
        # Keep the collection bounded; previews cannot be removed individually.
        if name not in _pcoll and len(_pcoll) >= _MAX_ENTRIES:
            _pcoll.clear()
        preview = _pcoll.get(name)
        if preview is None:
            preview = _pcoll.new(name)
            preview.image_size = (int(size[0]), int(size[1]))
            _upload(preview, pixels)
        return preview.icon_id
    except Exception:
        return None


def refresh_graph_icon(name, pixels, size):
    """Create ``name`` or RE-UPLOAD pixels into it, returning its icon id.

    get_graph_icon_id deliberately skips the upload when the preview already
    exists, because for a keyed graph identical name means identical pixels.
    The playback cursor is the one case where that is false: the name is held
    stable on purpose so a whole playback occupies ONE cache entry, and the
    pixels change every frame as the playhead moves.

    Reusing the entry is not a micro-optimisation. A 224x224 RGBA float preview
    is 784 KB, so keying per frame would allocate about 191 MB across a
    250-frame playback, and this collection cannot evict individually - at its
    entry cap it clears wholesale, which would throw away every cached graph
    mid-playback. Measured: re-uploading keeps the icon id stable, so the panel
    keeps drawing the same icon while its contents change underneath.
    """
    if _pcoll is None:
        return None
    try:
        preview = _pcoll.get(name)
        if preview is None:
            if len(_pcoll) >= _MAX_ENTRIES:
                _pcoll.clear()
            preview = _pcoll.new(name)
            preview.image_size = (int(size[0]), int(size[1]))
        _upload(preview, pixels)
        return preview.icon_id
    except Exception:
        return None
