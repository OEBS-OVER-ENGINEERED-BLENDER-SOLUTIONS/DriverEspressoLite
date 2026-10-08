"""Bake and clear motion that is already applied, chosen by name.

The other bake entry points work on the last apply or on whatever drivers a scope
contains, and cannot answer "bake the Sine Oscillation on this object but leave the
flicker alone". Each application stamps its host (see apply/applied_motion.py), so these
operators read stamps rather than guessing from expressions, and the artist picks by the
template's own name.

Scene-level motion gets its own operator rather than sharing one list. Motion on the
scene itself belongs to nothing in the outliner, so folding it into the object list
would make an artist who selected one object read past entries that have nothing to do
with their selection.
"""

from __future__ import annotations

import bpy
from bpy.props import EnumProperty

from ...apply import (
    applied_motion,
    target_memory,
)
from ...apply.motion import stack_runtime
from .operators import BakeOptionsMixin

ALL = "ALL"

# Blender does not copy the strings a dynamic enum callback returns, so a list
# built fresh on every call is freed while the UI still points at it - which
# crashes inside button_rna_enum_item_get rather than raising. Holding the last
# returned list at module scope is what keeps those strings alive.
_ENUM_CACHE = {"object": [], "scene": []}


# ------------------------------------------------------------------ resolving


def _entry_token(host, record):
    return applied_motion.entry_token(host, record)


def remembered_hosts(context):
    """Objects the panel is already naming as carrying applied motion.

    Clear reaches what the panel names, not only what is selected: a camera
    holding motion the artist has not selected must still be clearable while
    the panel reports it three rows above. What the panel names, the panel can
    clear.
    """
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None:
        return []
    found, seen = [], set()
    for entry in (target_memory.latest_entry(props),):
        for record in (entry or {}).get("targets", ()):
            resolved, _reason = target_memory.resolve_target_record(record)
            if resolved is None:
                continue
            owner = resolved.get("owner")
            root = getattr(owner, "id_data", None) or owner
            # A driver on camera DATA belongs to the camera OBJECT an artist
            # would select, so resolve back to it.
            for obj in bpy.data.objects:
                if obj.data is root:
                    root = obj
                    break
            key = (root.__class__.__name__, getattr(root, "name", ""))
            if root is not None and key not in seen:
                seen.add(key)
                found.append(root)
    return found


def scope_objects(context):
    """Everything Clear / Bake Motion may act on, nearest first.

    The selection comes first, then whatever the panel is naming as carrying
    motion. If none of those carry any, the scene follows: an artist looking
    at a panel that says motion exists should not have to guess which object
    to select before the button will appear. Ordering matters -- the operator
    lists these in this order, so the selection stays the obvious answer
    whenever it has one.
    """
    objects = list(getattr(context, "selected_objects", ()) or ())
    active = getattr(context, "active_object", None)
    if active is not None and active not in objects:
        objects.append(active)
    for host in remembered_hosts(context):
        if host not in objects:
            objects.append(host)
    if applied_motion.any_applied(objects):
        return objects
    scene = getattr(context, "scene", None)
    for obj in getattr(scene, "objects", ()) or ():
        if obj not in objects:
            objects.append(obj)
    return objects


def _object_scope(context):
    return applied_motion.collect_for_objects(scope_objects(context))


def _scene_scope(context):
    return applied_motion.collect_for_scene(getattr(context, "scene", None))


def _build_items(collected, cache_key, noun):
    items = []
    total = sum(len(records) for _host, _label, records in collected)
    if total > 1:
        items.append((ALL, "All applied motion (%d)" % total,
                      "Bake every effect found on %s" % noun))
    for host, host_label, records in collected:
        for record in records:
            label = applied_motion.describe(record)
            code = record.get("code", "")
            items.append((
                _entry_token(host, record),
                "%s  [%s]" % (label, code),
                "%s on %s" % (label, host_label),
            ))
    if not items:
        items = [("NONE", "Nothing applied", "No Driver Espresso motion was found")]
    _ENUM_CACHE[cache_key] = items
    return _ENUM_CACHE[cache_key]


def _object_items(self, context):
    return _build_items(_object_scope(context), "object", "the selected object(s)")


def _scene_items(self, context):
    return _build_items(_scene_scope(context), "scene", "this scene")


def _chosen(collected, token):
    """[(host, record)] for the chosen token, or everything when it is ALL."""
    out = []
    for host, _label, records in collected:
        for record in records:
            if token == ALL or _entry_token(host, record) == token:
                out.append((host, record))
    return out


def _targets_for(host, record):
    from ...engine.targeting.button_targeting import ButtonDriverTarget

    targets = []
    for entry in record.get("paths") or ():
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        data_path = str(entry[0])
        index = int(entry[1]) if len(entry) > 1 else -1
        targets.append(ButtonDriverTarget(host, data_path, index))
    return targets


def purge_emptied_records(context, host):
    """Purge every record on ``host`` whose drivers are all gone, dependencies too.

    The Organize tab removes drivers one row at a time as well as by effect. A driver
    belongs to whatever applied it, so forgetting the record when its last driver goes
    (which is all ``prune`` does) would leave the controller it served standing in the
    file. This runs the group-level Remove's own purge for a record that was emptied a
    row at a time. A record that still has a live driver is left alone (one channel of a
    multi-channel motion was removed, and the rest still needs its helpers), and a
    path-less record describes objects rather than drivers and is not a row's to empty.
    Returns how many records were purged.
    """
    # Keyed by record identity, not by code: two applications of one recipe to different
    # channels of one host are two records sharing a code, and keyed by code a dead one
    # would be kept for as long as its live sibling existed.
    live = {_record_identity(item) for item in applied_motion.entries(host)}
    purged = 0
    for record in list(applied_motion.read(host)):
        if not applied_motion.paths_of(record):
            continue
        if _record_identity(record) in live:
            continue
        _purge(host, record)
        purged += 1
    return purged


def _record_identity(record):
    return (str(record.get("code") or ""), applied_motion.record_slot(record))


def purge_record(host, record):
    """Purge ONE record's leftovers: its controller bindings and its stamp.

    The per-record entry point the stale-stamp reconciler uses when a driver
    was deleted by other means.
    """
    _purge(host, record)


def expand_clear_snapshot_hosts(chosen):
    """The hosts a clear can change: each chosen host once."""
    expanded = []
    seen = set()
    for host, record in chosen:
        if host is None:
            continue
        try:
            key = id(host)
        except ReferenceError:
            continue
        if key in seen:
            continue
        seen.add(key)
        expanded.append((host, record))
    return expanded


def capture_clear_snapshot(chosen, *, extra_fcurve_owners=()):
    """Capture every driver a clear can remove, so a failure can put them back."""
    from ...generated.resources import clear_snapshot

    snapshot_hosts = expand_clear_snapshot_hosts(chosen)
    # A Motion Stack's helper object is deleted with its record, so a rollback
    # has to be able to bring it back.
    helpers = [helper for helper in (stack_runtime.helper_object_of(record) for _host, record in chosen)
               if helper is not None]
    snapshot = clear_snapshot.capture(
        snapshot_hosts,
        extra_objects=helpers,
        extra_fcurve_owners=extra_fcurve_owners,
    )
    return snapshot_hosts, snapshot


def _host_alive(host):
    return target_memory.host_is_alive(host)


def _bake_records_body(context, chosen, *, start, end, step, smart,
                       smart_tolerance, smart_passes):
    """Bake chosen records. Raises on the first failed driver bake."""
    from ...engine import bake

    targets = []
    for host, record in chosen:
        targets.extend(_targets_for(host, record))
    if not targets:
        raise RuntimeError(
            "The selected effect has no drivers to bake; use Clear "
            "Applied Motion to remove it instead."
        )
    wm = context.window_manager
    wm.progress_begin(0, 1)
    try:
        baked, _keys, message = bake.bake_targets(
            context.scene, targets, start=start, end=end, step=step,
            remove_driver=True, smart=smart,
            smart_tolerance=smart_tolerance,
            smart_passes=smart_passes,
            progress=lambda done, total: wm.progress_update(
                done / max(1, total)
            ),
        )
    finally:
        wm.progress_end()
    if not baked:
        raise RuntimeError(message)
    for host, record in chosen:
        _purge(host, record)
    return [message]


def bake_records(context, chosen, *, start, end, step=1, smart=False,
                 smart_tolerance=0.01, smart_passes=2):
    """Bake explicit records without re-resolving them from UI selection."""
    from ...engine.baking.transaction import bake_transaction

    if not chosen:
        return False, "Choose at least one applied motion."

    from ...engine import bake
    targets = [target for host, record in chosen
               for target in _targets_for(host, record)]
    if not targets:
        return False, (
            "The selected effect has no drivers to bake; use Clear "
            "Applied Motion to remove it instead."
        )
    reason = bake.preflight_targets(targets)
    if reason:
        return False, reason
    try:
        with bake_transaction(context, targets=targets, chosen=chosen):
            messages = _bake_records_body(
                context, chosen, start=start, end=end, step=step,
                smart=smart, smart_tolerance=smart_tolerance,
                smart_passes=smart_passes,
            )
    except Exception as exc:
        return False, "Bake cancelled: %s" % exc
    return True, " ".join(messages)


def _clear_records_body(context, chosen):
    """Delete chosen records. Raises on the first failed driver removal."""
    removed_drivers = 0
    removed_effects = 0
    for host, record in chosen:
        if not _host_alive(host):
            continue
        extras = record.get("extras") or {}
        targets = _targets_for(host, record)
        props = getattr(context.scene, "espresso_props", None)
        captured = target_memory.capture_cleanup_for_targets(targets, props)
        for target in targets:
            if target.index >= 0:
                host.driver_remove(target.data_path, target.index)
            else:
                host.driver_remove(target.data_path)
            removed_drivers += 1
        target_memory.cleanup_captured_resources(captured, context.scene, props)
        entry = extras.get("target_entry")
        if not isinstance(entry, dict):
            entry = {
                "targets": [
                    target_memory.serialize_target(
                        target.owner, target.data_path, target.index,
                    )
                    for target in targets
                ],
            }
        _purge(host, record)
        removed_effects += 1
    return removed_effects, removed_drivers


def clear_records(context, chosen, *, transaction=None):
    """Clear explicit records without changing the artist's selection.

    When ``transaction`` is supplied, this function mutates inside that
    existing transaction and does not take its own snapshot. The caller
    owns rollback.
    """
    from ...generated.core.transaction import GeneratedTransaction
    for host, _record in chosen:
        if host is None:
            return False, "A checked applied effect no longer exists; nothing was removed."

    if transaction is not None:
        removed_effects, removed_drivers = _clear_records_body(context, chosen)
        return True, "Removed %d applied effect%s (%d driver%s)." % (
            removed_effects, "" if removed_effects == 1 else "s",
            removed_drivers, "" if removed_drivers == 1 else "s",
        )

    try:
        _snapshot_hosts, snapshot = capture_clear_snapshot(chosen)
    except Exception as exc:
        return False, "Clear cancelled before changes: snapshot could not be captured (%s)." % exc

    try:
        with GeneratedTransaction("clear-records") as owned:
            owned.on_rollback(snapshot.restore)
            removed_effects, removed_drivers = _clear_records_body(context, chosen)
            owned.commit()
        snapshot.discard()
    except Exception as exc:
        snapshot.discard()
        report = snapshot.verify()
        if report.get("ok"):
            return False, "Clear was rolled back: %s" % exc
        detail = "; ".join(report.get("errors") or ())
        if detail:
            return False, "Clear failed and could not fully restore: %s (%s)" % (exc, detail)
        return False, "Clear failed and could not fully restore: %s" % exc
    return True, "Removed %d applied effect%s (%d driver%s)." % (
        removed_effects, "" if removed_effects == 1 else "s",
        removed_drivers, "" if removed_drivers == 1 else "s",
    )


def _discard_controller_bindings(host, record):
    """Forget any controller binding left pointing at this record's channels.

    A binding is keyed by channel and stores the expression to put back when the
    controller comes off. Purging a motion takes its binding with it; otherwise the next
    motion applied to the same channel would inherit it, and a re-applied driver would
    come back carrying the previous motion's expression. It is discarded, not restored,
    because the driver is gone by the time a purge runs. Channels that still have a live
    driver are skipped, since their controller is a working setup rather than a
    leftover.
    """
    from ...ui.state import live_controls
    from ...apply.core import target_memory

    scene = getattr(bpy.context, "scene", None)
    props = getattr(scene, "espresso_props", None) if scene is not None else None
    if props is None:
        return
    targets = []
    for path, index in applied_motion.paths_of(record):
        if applied_motion._has_live_driver(host, path, index):
            continue
        descriptor = target_memory.serialize_target(host, path, index)
        if descriptor:
            targets.append(descriptor)
    if not targets:
        return
    live_controls.discard_bindings_for_targets(scene, props, targets)


def _purge(host, record):
    """Remove one application's leftovers: controller bindings and the stamp."""
    _discard_controller_bindings(host, record)
    stack_runtime.discard_resources(host, record)
    try:
        # By record identity, not code alone. Two applications of one recipe
        # to different channels of one host share a code; forgetting by code
        # erased the live sibling's stamp along with the dead one's.
        applied_motion.forget(host, record.get("code", ""),
                              slot=applied_motion.record_slot(record))
    except ReferenceError:
        pass


# ----------------------------------------------------------------- operators


class _BakeAppliedBase(BakeOptionsMixin):
    """Shared body. The only difference between the two is which hosts are
    searched, so the selection, baking and cleanup live here once."""

    scope_key = "object"

    def _collect(self, context):
        raise NotImplementedError

    @classmethod
    def poll(cls, context):
        return bool(cls._poll_collect(context))

    def invoke(self, context, event):
        self._seed_range(context)
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "target", text="Effect")
        BakeOptionsMixin.draw(self, context)

        from ...engine import bake

        chosen = _chosen(self._collect(context), self.target)
        if not chosen:
            return
        frames = len(bake.frame_list(self.bake_start, self.bake_end, self.bake_step))
        channels = sum(len(_targets_for(host, record)) for host, record in chosen)
        box = layout.box()
        for host, record in chosen[:6]:
            box.label(text=applied_motion.describe(record), icon="DRIVER")
        if len(chosen) > 6:
            box.label(text="...and %d more" % (len(chosen) - 6))
        box.label(text="%d driver(s) x %d frames = %d keyframes"
                       % (channels, frames, channels * frames),
                  icon="KEYFRAME_HLT")

    def execute(self, context):
        self._commit_range(context)
        collected = self._collect(context)
        chosen = _chosen(collected, self.target)
        if not chosen:
            self.report({"WARNING"}, "No applied motion was selected.")
            return {"CANCELLED"}

        ok, message = bake_records(
            context, chosen, start=self.bake_start, end=self.bake_end,
            step=self.bake_step, smart=self.bake_smart,
            smart_tolerance=self.bake_smart_tolerance / 100.0,
            smart_passes=self.bake_smart_passes,
        )
        if not ok:
            level = "WARNING" if message.startswith(
                "The selected effect has no drivers to bake",
            ) else "ERROR"
            self.report({level}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_bake_applied_motion(_BakeAppliedBase, bpy.types.Operator):
    """Bake motion applied to the selected object(s), chosen by name."""

    bl_idname = "espresso.bake_applied_motion"
    bl_label = "Bake Motion"
    bl_description = (
        "Freeze motion already applied to the selected object(s) into plain "
        "keyframes. Pick one effect by name or bake them all"
    )
    bl_options = {"REGISTER", "UNDO"}
    scope_key = "object"

    target: EnumProperty(
        name="Effect",
        description="Which applied effect to bake",
        items=_object_items,
    )

    def _collect(self, context):
        return _object_scope(context)

    @staticmethod
    def _poll_collect(context):
        # any_applied short-circuits on the first hit and bounds the miss, so
        # poll stays cheap even when the scope has widened to the scene.
        return applied_motion.any_applied(scope_objects(context))


class ESPRESSO_OT_bake_scene_motion(_BakeAppliedBase, bpy.types.Operator):
    """Bake motion applied to the scene rather than to any object."""

    bl_idname = "espresso.bake_scene_motion"
    bl_label = "Bake Scene Motion"
    bl_description = (
        "Freeze motion applied to the scene itself - world and compositor "
        "effects that belong to no object in the outliner"
    )
    bl_options = {"REGISTER", "UNDO"}
    scope_key = "scene"

    target: EnumProperty(
        name="Effect",
        description="Which applied scene effect to bake",
        items=_scene_items,
    )

    def _collect(self, context):
        return _scene_scope(context)

    @staticmethod
    def _poll_collect(context):
        return _scene_scope(context)


class _ClearAppliedBase:
    """Shared confirmation and lifecycle dispatch for a single clear scope."""

    def _collect(self, context):
        raise NotImplementedError

    @classmethod
    def poll(cls, context):
        return bool(cls._poll_collect(context))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "target", text="Effect")
        chosen = _chosen(self._collect(context), self.target)
        if not chosen:
            return
        box = layout.box()
        box.label(text="Will remove:", icon="TRASH")
        for host, record in chosen[:6]:
            box.label(text=applied_motion.describe(record))

    def execute(self, context):
        chosen = _chosen(self._collect(context), self.target)
        if not chosen:
            self.report({"WARNING"}, "No applied motion was selected.")
            return {"CANCELLED"}

        ok, message = clear_records(context, chosen)
        if not ok:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_clear_applied_motion(_ClearAppliedBase, bpy.types.Operator):
    """Remove applied motion from the selected object scope."""

    bl_idname = "espresso.clear_applied_motion"
    bl_label = "Clear Applied Motion"
    bl_description = (
        "Remove applied motion and the drivers it created"
    )
    bl_options = {"REGISTER", "UNDO"}

    target: EnumProperty(
        name="Effect",
        description="Which applied effect to remove",
        items=_object_items,
    )

    def _collect(self, context):
        return _object_scope(context)

    @staticmethod
    def _poll_collect(context):
        return _object_scope(context)


class ESPRESSO_OT_clear_scene_motion(_ClearAppliedBase, bpy.types.Operator):
    """Remove scene-owned exposure, World, and Compositor motion."""

    bl_idname = "espresso.clear_scene_motion"
    bl_label = "Clear Scene Motion"
    bl_description = (
        "Remove scene-owned motion without touching motion applied to "
        "selected objects"
    )
    bl_options = {"REGISTER", "UNDO"}

    target: EnumProperty(
        name="Effect",
        description="Which applied scene effect to remove",
        items=_scene_items,
    )

    def _collect(self, context):
        return _scene_scope(context)

    @staticmethod
    def _poll_collect(context):
        return _scene_scope(context)


CLASSES = (
    ESPRESSO_OT_bake_applied_motion,
    ESPRESSO_OT_bake_scene_motion,
    ESPRESSO_OT_clear_applied_motion,
    ESPRESSO_OT_clear_scene_motion,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    # The stale-stamp reconciler lives in the apply layer and must not import
    # this one; it purges through this hook, with the same teardown a Remove
    # uses, so a hand-deleted driver leaves no helpers behind.
    from ...apply.motion import applied_reconcile

    applied_reconcile.set_purge_hook(purge_record)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for key in _ENUM_CACHE:
        _ENUM_CACHE[key] = []
