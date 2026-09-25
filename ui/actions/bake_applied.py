"""Bake and clear motion that is already applied, chosen by name.

The existing bake entry points work on the LAST apply or on whatever drivers a
scope happens to contain. Neither can answer "bake the Ball Bounce on this
object but leave the flicker alone", because until the effect-ID registry there
was nothing on disk saying which driver belonged to which template.

Now there is. Each application stamps its host (see apply/applied_motion.py), so
these operators read stamps rather than guessing from expressions, and the
artist picks by the template's own name.

Scene-level motion gets its own operator rather than sharing one list. Camera
flash drives scene exposure and belongs to nothing in the outliner, so folding
it into the object list would mean an artist who selected one object still had
to read past entries that have nothing to do with their selection.
"""

from __future__ import annotations

from types import SimpleNamespace

import bpy
from bpy.props import EnumProperty

from ...apply import (
    applied_motion,
    target_memory,
)
from ...catalogue import templates
from ...generated import helpers as generated_helpers
from ...generated import manifest as generated_manifest
from ...generated import nodes as generated_nodes
from ...generated import properties as generated_properties
from ...generated.core.contracts import ResourceKind
from ...apply.motion import authoring_routes
from ...apply.setups import layout_preparation, generated_routes
from .operators import BakeOptionsMixin

ALL = "ALL"

# Blender does not copy the strings a dynamic enum callback returns, so a list
# built fresh on every call is freed while the UI still points at it - which
# crashes inside button_rna_enum_item_get rather than raising. Holding the last
# returned list at module scope is what keeps those strings alive.
_ENUM_CACHE = {"object": [], "scene": []}


# ------------------------------------------------------------------ resolving


def _host_token(host):
    return applied_motion.host_token(host)


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


def _template_id(record):
    template = applied_motion.resolve_template(record)
    if template is not None:
        return str(template.get("id") or "")
    return str((record.get("extras") or {}).get("template_id") or "")


def _route_for(host, record):
    """Resolve the setup family that owns this applied-motion lifecycle."""
    template_id = _template_id(record)
    if not template_id:
        return None
    adapter = generated_routes.adapter_for(template_id)
    if adapter is None:
        # A recipe that builds a rig rather than writing drivers owns its own
        # teardown; sweeping its drivers one by one is the wrong answer.
        adapter = authoring_routes.adapter_for(template_id)
    if adapter is None:
        return None
    target = adapter.resolve(SimpleNamespace(active_object=host), template_id, host)
    return (adapter, template_id, target) if target is not None else None


def route_for_record(host, record):
    """The lifecycle route that owns this record, or None for a plain one."""
    return _route_for(host, record)


def purge_emptied_records(context, host):
    """Purge every record on ``host`` whose drivers are all gone, dependencies too.

    The Organize tab removes drivers one row at a time as well as by effect. A
    driver belongs to whatever applied it, and forgetting the record once its
    last driver went -- which is all ``prune`` did -- left the controller,
    constraint, node group or support rig it served standing in the file. This
    runs the group-level Remove's own purge for a record that was emptied a row
    at a time. A record that still has a live driver is left alone (one channel
    of a multi-channel motion was removed, the rest still needs its helpers),
    and a path-less record describes objects rather than drivers and is not a
    row's to empty. Returns how many records were purged.
    """
    # Keyed by record IDENTITY, not by code. Since record slots, two
    # applications of one recipe to different channels of one host are two
    # records sharing a code; keyed by code, a dead one was kept for as long
    # as its live sibling existed.
    live = {_record_identity(item) for item in applied_motion.entries(host)}
    purged = 0
    for record in list(applied_motion.read(host)):
        if applied_motion.resolve_template(record) is None:
            continue
        if not applied_motion.paths_of(record):
            continue
        if _record_identity(record) in live:
            continue
        _purge(host, record, remove_generated=True)
        purged += 1
    return purged


def _record_identity(record):
    return (str(record.get("code") or ""), applied_motion.record_slot(record))


def purge_record(host, record):
    """Purge ONE record's leftovers -- helpers, generated data, stamp.

    The per-record entry point the stale-stamp reconciler uses when a driver
    was deleted by other means. Same teardown as a group-level Remove, so a
    hand-deleted driver leaves no controller layer, support rig or node group
    standing behind it.
    """
    if applied_motion.resolve_template(record) is not None:
        _purge(host, record, remove_generated=True)


def expand_clear_snapshot_hosts(chosen):
    """Include every host a route CLEAR can mutate.

    Checking one member still runs ``adapter.clear()`` across the controller
    and every sibling. Snapshotting only the checked host leaves an earlier
    member unrestorable after a later-member failure.
    """
    expanded = []
    seen = set()

    def add(host, record):
        if host is None:
            return
        try:
            key = id(host)
        except ReferenceError:
            return
        if key in seen:
            return
        seen.add(key)
        expanded.append((host, record))

    for host, record in chosen:
        add(host, record)
        route = _route_for(host, record)
        if route is None:
            continue
        adapter, template_id, target = route
        if target is not None and target is not host:
            entries = list(applied_motion.entries(target) or ())
            add(target, entries[0] if entries else {})
        snapshot_hosts = getattr(adapter, "snapshot_hosts", None)
        if callable(snapshot_hosts):
            for owner in snapshot_hosts(target, template_id):
                entries = list(applied_motion.entries(owner) or ())
                add(owner, entries[0] if entries else {})
        module_fn = getattr(adapter, "_module", None)
        module = module_fn(target, template_id) if callable(module_fn) else None
        members_fn = getattr(module, "members", None) if module is not None else None
        if members_fn is None:
            continue
        try:
            siblings = list(members_fn(target) or ())
        except (AttributeError, ReferenceError, TypeError):
            continue
        for member in siblings:
            entries = list(applied_motion.entries(member) or ())
            add(member, entries[0] if entries else {})
    return expanded


def capture_clear_snapshot(chosen, *, extra_fcurve_owners=(), defer_helper_cleanup=True):
    """Capture every CLEAR resource before holding copies can enter discovery.

    Holding clones intentionally keep Espresso identity so they can restore it.
    Resolving generated resources after the first clone exists can therefore
    discover and recursively snapshot the rollback data itself.
    """
    from ...generated.resources import clear_snapshot

    snapshot_hosts = expand_clear_snapshot_hosts(chosen)
    objects = []
    groups = []
    collections = []
    for _host, record in snapshot_hosts:
        route = _route_for(_host, record)
        if route is not None:
            adapter, template_id, target = route
            collect = getattr(adapter, "snapshot_collections", None)
            if callable(collect):
                for collection in collect(target, template_id):
                    if collection not in collections:
                        collections.append(collection)
        found_objects, found_groups = generated_resources_for(record)
        objects.extend(obj for obj in found_objects if obj not in objects)
        groups.extend(group for group in found_groups if group not in groups)
    snapshot = clear_snapshot.capture(
        snapshot_hosts,
        extra_objects=objects,
        extra_fcurve_owners=extra_fcurve_owners,
    )
    try:
        captured_routes = set()
        for host, record in snapshot_hosts:
            route = _route_for(host, record)
            if route is None:
                continue
            adapter, template_id, target = route
            capture_route = getattr(adapter, 'snapshot_clear', None)
            if not callable(capture_route) or _route_key(route) in captured_routes:
                continue
            captured_routes.add(_route_key(route))
            state = capture_route(target, template_id)
            snapshot.before_restore(state.restore)
            snapshot.on_discard(state.discard)
            if getattr(state, 'tree', None) is not None:
                snapshot.capture_id(state.tree)
        for collection in collections:
            snapshot.capture_collection(collection)
        for group in groups:
            snapshot.capture_node_group(group)
        snapshot.isolate_node_group_copies()
        from ...apply.setups import internal_helpers

        helper_resources = []
        for host, _record in snapshot_hosts:
            animation = getattr(host, 'animation_data', None)
            for curve in getattr(animation, 'drivers', ()) or ():
                helper_resources.extend(internal_helpers.resources_from_driver(curve.driver))
        if helper_resources and defer_helper_cleanup:
            # Rollback copies temporarily read these helpers. Retry their scoped
            # cleanup only after those copies go; restored artist drivers protect
            # the same resources when discard follows a rollback instead.
            snapshot.on_discard(lambda: internal_helpers.cleanup(helper_resources))
    except Exception:
        snapshot.discard()
        raise
    return snapshot_hosts, snapshot


def _run_route(context, route, action, *, start=None, end=None, step=1):
    adapter, template_id, target = route
    if action == "CLEAR":
        removed = adapter.clear(target, template_id)
        if int(removed or 0) <= 0:
            return False, (
                "Could not remove the %s setup; no owned resources were cleared."
                % template_id.replace("_", " ").title()
            )
        return True, "Removed %s setup (%d resource%s)." % (
            template_id.replace("_", " ").title(), removed,
            "" if removed == 1 else "s",
        )
    result = adapter.bake(
        context, target, template_id, start=start, end=end, step=step,
    )
    return result.ok, result.message


def _host_alive(host):
    return target_memory.host_is_alive(host)


def _route_key(route):
    adapter, template_id, target = route
    return adapter.stable_key(target, template_id)


def _classify_routes(chosen):
    """Resolve route identity before any lifecycle call can remove its owner."""
    routed = []
    ordinary = []
    for host, record in chosen:
        route = _route_for(host, record)
        if route is None:
            ordinary.append((host, record))
        else:
            routed.append((_route_key(route), route))
    return routed, ordinary


def _bake_records_body(context, chosen, *, start, end, step, smart,
                       smart_tolerance, smart_passes):
    """Bake chosen records. Raises on the first failed route or driver bake."""
    from ...engine import bake

    routed_motion, ordinary_motion = _classify_routes(chosen)
    messages = []
    handled_routes = set()
    for setup_key, route in routed_motion:
        if setup_key in handled_routes:
            continue
        handled_routes.add(setup_key)
        ok, message = _run_route(
            context, route, "BAKE", start=start, end=end, step=step,
        )
        if not ok:
            raise RuntimeError(message)
        messages.append(message)

    if ordinary_motion:
        targets = []
        for host, record in ordinary_motion:
            targets.extend(_targets_for(host, record))
        if not targets:
            if not messages:
                raise RuntimeError(
                    "The selected effect has no drivers to bake; use Clear "
                    "Applied Motion to remove it instead."
                )
        else:
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
            for host, record in ordinary_motion:
                _purge(host, record, remove_generated=False)
                _remove_unreferenced_generated(record)
            messages.append(message)
    return messages


def bake_records(context, chosen, *, start, end, step=1, smart=False,
                 smart_tolerance=0.01, smart_passes=2):
    """Bake explicit records without re-resolving them from UI selection."""
    from ..views import guided_apply
    from ...engine.baking.transaction import bake_transaction

    guided_apply.clear_preflight_cache()

    if not chosen:
        return False, "Choose at least one applied motion."

    supported = [(host, record) for host, record in chosen
                 if applied_motion.resolve_template(record) is not None]
    skipped = len(chosen) - len(supported)
    if not supported:
        return False, "Selected motion is not available in this edition; it was left unchanged."
    chosen = supported
    skipped_message = (
        " Skipped %d effect%s not available in this edition; left unchanged."
        % (skipped, "" if skipped == 1 else "s")
    ) if skipped else ""

    routed_motion, ordinary_motion = _classify_routes(chosen)
    if not routed_motion:
        targets = []
        for host, record in ordinary_motion:
            targets.extend(_targets_for(host, record))
        if not targets:
            return False, (
                "The selected effect has no drivers to bake; use Clear "
                "Applied Motion to remove it instead."
            )

    from ...engine import bake
    targets = [target for host, record in ordinary_motion
               for target in _targets_for(host, record)]
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
    return True, " ".join(messages) + skipped_message


def target_has_foreign_motion(target):
    """Whether a driven channel belongs to a stamped, unavailable recipe."""
    from ...apply.core import driver_manager

    descriptor = target_memory.serialize_target(
        target.owner, target.data_path, target.index,
    )
    metadata = driver_manager.metadata_for_descriptor(descriptor) if descriptor else None
    return bool(metadata and applied_motion.resolve_template(metadata["record"]) is None)


def _clear_records_body(context, chosen):
    """Delete chosen records. Raises on the first failed driver or route clear."""
    removed_drivers = 0
    removed_effects = 0
    handled_routes = set()
    routed_motion, ordinary_motion = _classify_routes(chosen)
    for setup_key, route in routed_motion:
        if setup_key in handled_routes:
            continue
        handled_routes.add(setup_key)
        ok, message = _run_route(context, route, "CLEAR")
        if not ok:
            raise RuntimeError(message)
        removed_effects += 1
    for host, record in ordinary_motion:
        if not _host_alive(host):
            continue
        extras = record.get("extras") or {}
        if extras.get("structural_layout"):
            removed = layout_preparation.clear(host)
            if removed <= 0:
                raise RuntimeError("Could not remove the Prepared Layout.")
            removed_effects += 1
            continue
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
        target_memory.cleanup_entry_node_groups(entry)
        _purge(host, record, remove_generated=True)
        removed_effects += 1
    return removed_effects, removed_drivers


def clear_records(context, chosen, *, transaction=None):
    """Clear explicit records without changing the artist's selection.

    When ``transaction`` is supplied, this function mutates inside that
    existing transaction and does not take its own snapshot. The caller
    owns rollback.
    """
    from ..views import guided_apply
    from ...generated.core.transaction import GeneratedTransaction
    guided_apply.clear_preflight_cache()
    for host, _record in chosen:
        if host is None:
            return False, "A checked applied effect no longer exists; nothing was removed."

    # A foreign stamp may describe helper data this edition cannot clean up.
    # Exclude the whole record before touching its drivers or taking a snapshot.
    supported = [(host, record) for host, record in chosen
                 if applied_motion.resolve_template(record) is not None]
    skipped = len(chosen) - len(supported)
    if skipped and not supported:
        return False, "Selected motion is not available in this edition; it was left unchanged."
    chosen = supported
    skipped_message = (
        " Skipped %d effect%s not available in this edition; left unchanged."
        % (skipped, "" if skipped == 1 else "s")
    ) if skipped else ""

    if transaction is not None:
        removed_effects, removed_drivers = _clear_records_body(context, chosen)
        return True, "Removed %d applied effect%s (%d driver%s)." % (
            removed_effects, "" if removed_effects == 1 else "s",
            removed_drivers, "" if removed_drivers == 1 else "s",
        ) + skipped_message

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
    ) + skipped_message




def generated_resources_for(record, host=None):
    """Controller objects and node groups this effect created.

    Baking or clearing has to take these with it, or the artist is left with an
    orphan Espresso empty in the outliner and a node group that no longer drives
    anything - the parts of an application that are not drivers at all.

    """
    objects = []
    groups = []
    value = generated_manifest.from_extras(record.get("extras"))
    if value is not None:
        for resource in value.resources:
            if resource.kind is ResourceKind.OBJECT:
                found = bpy.data.objects.get(resource.name)
                if found is not None:
                    objects.append(found)
            elif resource.kind is ResourceKind.NODE_GROUP:
                found = bpy.data.node_groups.get(resource.name)
                if found is not None:
                    groups.append(found)

    return objects, groups


def _remove_attachments(host, code):
    """Release camera attachments owned by the removed motion."""
    if getattr(host, "type", "") == "CAMERA":
        from ...apply.motion.camera import aim_rig, trajectory

        aim_rig.clear_tracking(host, code)
        trajectory.clear_route(host, code)
        release_support_rig(host)
    # Lite creates no orbit pivots; ordinary Empty hosts need no rig teardown.
    # Lens drivers live on the Camera DATABLOCK, and the focus rig or dolly
    # reference they measure to is owned by the camera object using it.
    if isinstance(host, bpy.types.Camera):
        from ...apply.motion.camera import optics as camera_optics

        for camera in (obj for obj in bpy.data.objects if obj.data is host):
            camera_optics.clear_lens_helpers(camera, code)


def support_rig_still_needed(camera):
    """Whether a camera's support empty is still carrying anything.

    An empty parent that holds no constraint, no driver and no applied-motion
    stamp is doing nothing but sitting between the camera and its real parent.
    Anything at all on it means another motion is still using it, so it stays.
    """
    from ...apply.motion.camera import support_rig

    rig = support_rig.resolve_support_rig(camera)
    if rig is None:
        return False
    if list(getattr(rig, "constraints", ()) or ()):
        return True
    animation = getattr(rig, "animation_data", None)
    if animation is not None and (list(animation.drivers) or animation.action):
        return True
    if applied_motion.read(rig):
        return True
    return False


def release_support_rig(camera):
    """Give the camera back its own parent once nothing needs the rig.

    A motion that built a support empty has to take it away again, or every
    cleared recipe leaves an Espresso empty in the outliner with the camera
    still hanging off it. Kept only while something is still on it.
    """
    from ...apply.motion.camera import support_rig

    rig = support_rig.resolve_support_rig(camera)
    if rig is None or support_rig_still_needed(camera):
        return False
    return bool(support_rig.clear_support_rig(camera))


def _is_referenced(resource):
    """Return whether the generated datablock still has references.

    Preserve objects used by parents, constraints, modifiers or drivers.
    For other datablocks, use Blender's user count excluding the fake user.
    """
    if isinstance(resource, bpy.types.Object):
        for obj in bpy.data.objects:
            if obj is resource:
                continue
            if obj.parent is resource:
                return True
            for constraint in getattr(obj, "constraints", ()) or ():
                if getattr(constraint, "target", None) is resource:
                    return True
            for modifier in getattr(obj, "modifiers", ()) or ():
                if getattr(modifier, "object", None) is resource:
                    return True
            animation = getattr(obj, "animation_data", None)
            for curve in (getattr(animation, "drivers", ()) or ()):
                driver = getattr(curve, "driver", None)
                for variable in (getattr(driver, "variables", ()) or ()):
                    for target in (getattr(variable, "targets", ()) or ()):
                        if getattr(target, "id", None) is resource:
                            return True
        return False
    # Node groups: Blender already counts users, and a group still sitting in a
    # modifier or a material keeps a user even after its drivers are baked - so
    # it is still doing something and stays.
    users = int(getattr(resource, "users", 0) or 0)
    if getattr(resource, "use_fake_user", False):
        users -= 1
    return users > 0


def _remove_unreferenced_generated(record):
    """After a bake, drop generated data nothing points at any more."""
    removed = 0
    objects, groups = generated_resources_for(record)
    for obj in objects:
        if not _is_referenced(obj):
            if generated_helpers.remove_empty(obj):
                removed += 1
    for group in groups:
        if not _is_referenced(group):
            if generated_nodes.remove_group(group, require_unused=False):
                removed += 1
    return removed


def _discard_controller_bindings(host, record):
    """Forget any controller binding left pointing at this record's channels.

    A binding is keyed by CHANNEL and stores the expression to put back when
    the controller comes off. Purging a motion takes its binding with it. Left
    behind, the next motion applied to the same channel would inherit it, and a
    re-applied driver would come back carrying the previous motion's
    expression. The stamp, helpers and generated data were all torn down; only
    this was missed, though ``purge_record`` already promised otherwise.

    Discarded, not restored: the driver is gone by the time a purge runs, so
    there is nothing left to restore onto. Channels that DO still have a live
    driver are skipped -- their controller is somebody's working setup, not a
    leftover of what we are purging.
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


def _purge(host, record, remove_generated=True):
    """Remove one application's leftovers: helpers, generated data, stamp."""
    if applied_motion.resolve_template(record) is None:
        return
    _discard_controller_bindings(host, record)
    for key in applied_motion.helper_property_names(host, record):
        try:
            generated_properties.remove(host, key)
        except (KeyError, TypeError):
            pass
    if remove_generated:
        code = str(record.get("code") or "")
        _remove_attachments(host, code)
        objects, groups = generated_resources_for(record, host)
        for obj in objects:
            try:
                generated_helpers.remove_empty(obj)
            except Exception:
                pass
        for group in groups:
            try:
                generated_nodes.remove_group(group, require_unused=False)
            except Exception:
                pass
    try:
        # By record identity, not code alone. Two applications of one recipe
        # to different channels of one host share a code; forgetting by code
        # erased the live sibling's stamp along with the dead one's.
        applied_motion.forget(host, record.get("code", ""),
                              slot=applied_motion.record_slot(record))
    except ReferenceError:
        # A generated route may own and remove its carrier. Its motion stamp
        # disappears with that datablock, so no separate forget remains.
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
            level = "WARNING" if message.startswith((
                "The selected effect has no drivers to bake",
                "Selected motion is not available in this edition",
            )) else "ERROR"
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
        "Freeze motion applied to the scene itself - exposure, world and "
        "compositor effects that belong to no object in the outliner"
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
        objects, groups = [], []
        for _host, record in chosen:
            found_objects, found_groups = generated_resources_for(record)
            objects.extend(found_objects)
            groups.extend(found_groups)
        if objects or groups:
            box.label(text="plus %d object(s) and %d node group(s)"
                           % (len(set(objects)), len(set(groups))),
                      icon="OUTLINER")

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
        "Remove applied motion and everything it created - drivers, helper "
        "properties, generated node groups and controller objects"
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
        "Remove scene-owned motion and its generated resources without "
        "touching motion applied to selected objects"
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
