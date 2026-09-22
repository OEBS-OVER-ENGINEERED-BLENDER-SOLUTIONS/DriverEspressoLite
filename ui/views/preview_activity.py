"""Pointer-gated previews; never start or stop the scene's animation."""

import bpy
from bpy.app.handlers import persistent

_enabled = False
_hover = None
_frames = {}
_requested_windows = set()
_running = {}
_timers = {}
_serial = 0


def sidebar_at(window, x, y):
    """Identify an open sidebar from window-relative mouse coordinates."""
    for area in window.screen.areas:
        for region in area.regions:
            if (region.type == 'UI' and region.width > 1 and region.height > 1
                    and region.x <= x < region.x + region.width
                    and region.y <= y < region.y + region.height):
                return window.as_pointer(), area.as_pointer()
    return None


def _set_hover(value):
    global _hover
    previous, _hover = _hover, value
    if previous == value:
        return
    # Only the previous/new sidebar needs a status refresh on pointer crossing.
    for window in bpy.context.window_manager.windows:
        for target in (previous, value):
            if target is not None and target[0] == window.as_pointer():
                for area in window.screen.areas:
                    if area.as_pointer() == target[1]:
                        area.tag_redraw()


@persistent
def clear(_unused=None):
    global _hover
    _hover = None
    _frames.clear()


class ESPRESSO_OT_preview_pointer_watch(bpy.types.Operator):
    """Observe sidebar hover without consuming clicks or keyboard shortcuts"""
    bl_idname = 'espresso.preview_pointer_watch'
    bl_label = 'Preview Pointer Watch'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _serial
        if not _enabled or context.window is None:
            return {'CANCELLED'}
        key = context.window.as_pointer()
        if key in _running:
            return {'CANCELLED'}
        _serial += 1
        self._key, self._token = key, _serial
        _running[key] = self._token
        _timers[key] = context.window_manager.event_timer_add(.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        _set_hover(sidebar_at(context.window, event.mouse_x, event.mouse_y))
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not _enabled or _running.get(self._key) != self._token:
            return {'CANCELLED'}
        if event.type == 'WINDOW_DEACTIVATE':
            _set_hover(None)
        elif event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            _set_hover(sidebar_at(context.window, event.mouse_x, event.mouse_y))
        return {'PASS_THROUGH'}

    def cancel(self, context):
        if _running.get(self._key) == self._token:
            _running.pop(self._key, None)
            timer = _timers.pop(self._key, None)
            if timer is not None:
                context.window_manager.event_timer_remove(timer)
            if _hover is not None and _hover[0] == self._key:
                _set_hover(None)


def _start_pending():
    if not _enabled:
        return None
    requests = set(_requested_windows)
    _requested_windows.clear()
    for window in bpy.context.window_manager.windows:
        if window.as_pointer() in requests and window.as_pointer() not in _running:
            with bpy.context.temp_override(window=window):
                bpy.ops.espresso.preview_pointer_watch('INVOKE_DEFAULT')
    return None


def is_hovered(context):
    """Headless evaluations have no pointer; interactive draws require hover."""
    if bpy.app.background:
        return True
    window = getattr(context, 'window', None)
    if not _enabled or window is None:
        return False
    key = window.as_pointer()
    if key not in _running:
        _requested_windows.add(key)
        if not bpy.app.timers.is_registered(_start_pending):
            bpy.app.timers.register(_start_pending, first_interval=0)
    area = getattr(context, 'area', None)
    return (_hover is not None and _hover[0] == key
            and (area is None or _hover[1] == area.as_pointer()))


def context_owner(context):
    """Detached window/editor identity for an asynchronous preview request."""
    window, area = getattr(context, 'window', None), getattr(context, 'area', None)
    return (window.as_pointer() if window else 0, area.as_pointer() if area else 0)


def is_owner_hovered(owner):
    """Do not transfer an old request to another sidebar merely on hover."""
    return bpy.app.background or (_enabled and _hover == owner)


def frame(context):
    """Freeze preview time outside the sidebar; leave scene time untouched."""
    scene = context.scene
    window, area = getattr(context, 'window', None), getattr(context, 'area', None)
    key = (scene.as_pointer(), window.as_pointer() if window else 0,
           area.as_pointer() if area else 0)
    if key not in _frames or is_hovered(context):
        if key not in _frames and len(_frames) >= 32:
            _frames.pop(next(iter(_frames)))
        _frames[key] = float(scene.frame_current_final)
    return _frames[key]


def register():
    global _enabled
    _enabled = True
    clear()
    bpy.utils.register_class(ESPRESSO_OT_preview_pointer_watch)
    if clear not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(clear)


def unregister():
    global _enabled
    _enabled = False
    if bpy.app.timers.is_registered(_start_pending):
        bpy.app.timers.unregister(_start_pending)
    for timer in tuple(_timers.values()):
        try:
            bpy.context.window_manager.event_timer_remove(timer)
        except (ReferenceError, RuntimeError):
            pass  # A closed window can already have removed its event timer.
    _timers.clear()
    _running.clear()
    _requested_windows.clear()
    clear()
    if clear in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(clear)
    bpy.utils.unregister_class(ESPRESSO_OT_preview_pointer_watch)
