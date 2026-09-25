"""A stamp whose driver was deleted by other means becomes invalid -- and goes.

A record says WHAT was applied; the driver is the authority on whether it
still EXISTS. Nothing stops an artist deleting a driver by hand -- Blender's
own Delete Driver, Clear Drivers, or a script -- and when they do, the stamp
is left behind describing motion that is no longer there. Every read that
validates already ignores it; the reads that must not -- Clear has to see
leftovers -- would otherwise offer a phantom row: a host whose stamp names a
channel that no longer has a driver.

Blender raises no "driver removed" event. What it does do is update the ID
the driver lived on, so ``depsgraph_update_post`` sees it -- and only it, in
``depsgraph.updates``. That is a dictionary-key check per updated ID, never a
scene-wide walk. A file may already hold leftovers that no update will flag,
so a load queues every stamped host once.

The reconcile itself runs DEFERRED, on idle, through a one-shot timer, never
inside the depsgraph pass. That is the safety property. The add-on's own
rebuilds remove and re-add drivers with a ``view_layer.update()`` between,
and a purge landing in that window would eat a live motion. Idle means the
rebuild has finished and its stamps are consistent.

Two rules that are easy to get wrong:

* EXISTENCE, never validity. A driver flagged invalid still exists -- a rig's
  drivers can read as invalid while the motion is intact -- and purging one
  would destroy a working record.
* One dead channel of a multi-channel motion trims that channel; only a
  record with no live channel left is purged -- and purged with its helpers,
  through the same teardown a group-level Remove uses, supplied as a hook so
  this layer never imports the UI.

The mirror image is a GHOST: a driver with no record describing it, which no
list can see, edit, bake or remove. A driver carries no mark of ours, so a
ghost is only ever claimed on the add-on's own evidence -- target memory
remembers applying to that exact channel, or the driver wears the controller
layer. Anything else is somebody's hand-made driver and is left alone.
"""

from __future__ import annotations

import bpy

from . import applied_motion

#: Installed by the Bake/Clear module at register time. Kept as a hook so the
#: apply layer does not import the UI layer that owns the teardown.
_PURGE = None

#: Hosts touched since the last reconcile, keyed by pointer so a host queued
#: twice is reconciled once.
_PENDING = {}

#: How long to wait when a rig is mid-rebuild, before looking again.
_RETRY_SECONDS = 0.25

#: The controller layer's variable prefix. A driver wearing it is ours.
CONTROLLER_VARIABLE = "espctl_"

DEAD_RECORD = "DEAD_RECORD"
DEAD_CHANNEL = "DEAD_CHANNEL"
GHOST_DRIVER = "GHOST_DRIVER"


def set_purge_hook(function):
    """``function(host, record)`` purges one record with its helpers."""
    global _PURGE
    _PURGE = function


def record_is_live(host, record):
    """A record with a driver still behind at least one of its paths.

    Path-less records describe setups made of objects and node groups rather
    than drivers, and are live by definition here -- their own liveness is a
    manifest question that belongs to the generated layer.
    """
    paths = applied_motion.paths_of(record)
    if not paths:
        return True
    return any(applied_motion._has_live_driver(host, path, index)
               for path, index in paths)


def _identity(record):
    return (str(record.get("code") or ""), applied_motion.record_slot(record))


def _classify(host, records):
    """(surviving, dead, trimmed_count) for one host's records."""
    surviving, dead = [], []
    trimmed = 0
    for record in records:
        if applied_motion.resolve_template(record) is None:
            surviving.append(record)
            continue
        paths = applied_motion.paths_of(record)
        if not paths:
            surviving.append(record)
            continue
        live = [(path, index) for path, index in paths
                if applied_motion._has_live_driver(host, path, index)]
        if len(live) == len(paths):
            surviving.append(record)
        elif not live:
            dead.append(record)
        else:
            trimmed += 1
            surviving.append(dict(record, paths=[[path, index] for path, index in live]))
    return surviving, dead, trimmed


def reconcile_host(host, only=None):
    """Trim dead channels and purge dead records on one host.

    ``only`` restricts the work to records with those identities (code, slot)
    -- what the artist ticked -- and leaves the rest exactly as found. Returns
    ``(trimmed, purged)``. Safe to call on anything: a host with no stamp, or
    one whose every record is intact, returns ``(0, 0)`` untouched.
    """
    try:
        records = applied_motion.read(host)
    except ReferenceError:
        return 0, 0
    if not records:
        return 0, 0

    # Classify first, act second. It cannot act mid-loop: the purge hook
    # rewrites the stamp list itself, so re-reading the list after a purge
    # would hand back the untrimmed copy of a record trimmed a moment earlier
    # in the same pass.
    surviving, dead, trimmed = _classify(host, records)
    if only is not None:
        chosen = set(only)
        # Anything not chosen goes back exactly as it was read.
        original = {_identity(record): record for record in records}
        surviving = [record if _identity(record) in chosen
                     else original[_identity(record)] for record in surviving]
        trimmed = sum(1 for record in surviving
                      if _identity(record) in chosen
                      and record is not original[_identity(record)])
        kept_dead = [record for record in dead if _identity(record) not in chosen]
        dead = [record for record in dead if _identity(record) in chosen]
        surviving = surviving + kept_dead
    if not dead and not trimmed:
        return 0, 0

    for record in dead:
        if _PURGE is not None:
            _PURGE(host, record)          # helpers, generated data, stamp
    # One write of the complete surviving set. The hook has already removed
    # the dead stamps; this settles the trimmed ones and cannot resurrect a
    # purged record, which is not in `surviving`.
    try:
        applied_motion.write(host, surviving)
    except ReferenceError:
        pass
    return trimmed, len(dead)


# ------------------------------------------------------------------ findings


def _remembered_channels(props):
    """Every (id_type, id_name, owner_path, data_path, index) the add-on
    remembers applying to. Bounded by the memory's history depth."""
    from ..core import target_memory

    out = set()
    if props is None:
        return out
    for entry in target_memory._candidate_entries(props):
        for target in entry.get("targets") or ():
            out.add((str(target.get("id_type", "")), str(target.get("id_name", "")),
                     str(target.get("owner_path", "")), str(target.get("data_path", "")),
                     int(target.get("index", -1))))
    return out


def _wears_controller(fcurve):
    driver = getattr(fcurve, "driver", None)
    return any(str(getattr(variable, "name", "")).startswith(CONTROLLER_VARIABLE)
               for variable in (driver.variables if driver else ()))


def _covered(records, data_path, index):
    for record in records:
        for path, recorded in applied_motion.paths_of(record):
            if path == data_path and (recorded < 0 or recorded == index):
                return True
    return False


def _channel_text(host, data_path, index):
    name = applied_motion._channel_name(host, data_path)
    if not name:
        name = str(data_path)
    if index >= 0:
        name = "%s[%d]" % (name, index)
    return name


def find_invalid(props, hosts):
    """What Clear Invalid would offer for these hosts, without touching any.

    Each finding is a dict with ``kind``, ``label``, a ``target`` descriptor
    (the add-on's own serialised owner/path/index, resolvable later), and for
    records the ``code`` and ``slot`` that identify them.
    """
    from ..core import target_memory

    remembered = _remembered_channels(props)
    findings = []
    seen_hosts = set()
    for host in hosts or ():
        try:
            pointer = host.as_pointer()
            host_name = host.name
        except (AttributeError, ReferenceError):
            continue
        if pointer in seen_hosts:
            continue
        seen_hosts.add(pointer)
        records = applied_motion.read(host)
        # Dead records and dead channels.
        for record in records:
            if applied_motion.resolve_template(record) is None:
                continue
            paths = applied_motion.paths_of(record)
            if not paths:
                continue
            live = [(path, index) for path, index in paths
                    if applied_motion._has_live_driver(host, path, index)]
            if len(live) == len(paths):
                continue
            first_path, first_index = paths[0]
            code, slot = _identity(record)
            label = applied_motion.describe(record)
            if not live:
                findings.append({
                    "kind": DEAD_RECORD, "code": code, "slot": slot,
                    "label": "%s — %s  (driver missing)" % (label, host_name),
                    "target": target_memory.serialize_target(host, first_path, first_index),
                })
            else:
                gone = [(path, index) for path, index in paths if (path, index) not in live]
                channels = ", ".join(_channel_text(host, path, index) for path, index in gone)
                findings.append({
                    "kind": DEAD_CHANNEL, "code": code, "slot": slot,
                    "label": "%s — %s  (%s missing)" % (label, host_name, channels),
                    "target": target_memory.serialize_target(host, gone[0][0], gone[0][1]),
                })
        # Ghosts: live drivers no record covers, that are provably ours.
        animation = getattr(host, "animation_data", None)
        for fcurve in (animation.drivers if animation else ()):
            if not getattr(fcurve, "driver", None):
                continue
            if _covered(records, fcurve.data_path, fcurve.array_index):
                continue
            target = target_memory.serialize_target(host, fcurve.data_path, fcurve.array_index)
            if not target:
                continue
            key = (str(target.get("id_type", "")), str(target.get("id_name", "")),
                   str(target.get("owner_path", "")), str(target.get("data_path", "")),
                   int(target.get("index", -1)))
            # ONLY target memory proves the driver is ours to delete.
            #
            # A controller variable is not proof on its own: attaching a
            # controller to a driver the artist wrote by hand is supported, and
            # counting it would classify their driver as an orphan of ours.
            # Accepting the finding restored their original expression and then
            # deleted the whole driver. A controller says Espresso TOUCHED this
            # driver, not that Espresso MADE it, and only the second justifies
            # removal.
            if key not in remembered:
                continue
            findings.append({
                "kind": GHOST_DRIVER, "code": "", "slot": "",
                "label": "Driver on %s › %s  (no record; remembered apply)" % (
                    host_name, _channel_text(host, fcurve.data_path, fcurve.array_index)),
                "target": target,
            })
    return findings


def remove_ghost(scene, props, host, data_path, index):
    """Delete one ghost driver, forgetting any controller binding first."""
    for record in applied_motion.read(host):
        if applied_motion.resolve_template(record) is not None:
            continue
        if any(path == data_path and (recorded < 0 or index < 0 or recorded == index)
               for path, recorded in applied_motion.paths_of(record)):
            return 0
    from ...ui.state import live_controls   # deliberate: the one UI reach,
                                            # for the binding rule it owns
    animation = getattr(host, "animation_data", None)
    fcurves = [fcurve for fcurve in (animation.drivers if animation else ())
               if fcurve.data_path == data_path
               and (index < 0 or fcurve.array_index == index)]
    if not fcurves:
        return 0
    try:
        live_controls.remove_fcurves(scene, props, fcurves)
    except Exception:
        pass
    removed = 0
    for fcurve in fcurves:
        try:
            host.driver_remove(fcurve.data_path, fcurve.array_index)
            removed += 1
        except (RuntimeError, TypeError, ReferenceError):
            pass
    return removed


# ------------------------------------------------------------- the idle pass


def note_host(host):
    """Queue one host for the next idle reconcile."""
    try:
        _PENDING[host.as_pointer()] = host
    except (AttributeError, ReferenceError):
        return
    _schedule()


def _schedule():
    if bpy.app.background:
        return                      # timers never tick under -b; tests flush
    if not bpy.app.timers.is_registered(_drain):
        bpy.app.timers.register(_drain, first_interval=0.0)


def _drain():
    """The idle pass. Returns a retry interval while a rig is being built."""
    # Nothing here builds a rig, so there is never a mid-rebuild state to stand
    # down for and the idle pass can always proceed.
    pending = list(_PENDING.values())
    _PENDING.clear()
    for host in pending:
        try:
            host.name                       # freed IDs raise here
        except ReferenceError:
            continue
        reconcile_host(host)
    return None


def flush_pending():
    """Run the idle pass now. For tests, and for anything that must not wait."""
    if bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    if not _PENDING:
        return 0
    count = len(_PENDING)
    _drain()
    return count


def stamped_hosts_in_file():
    """Every datablock carrying a stamp. Bounded by the file; used once per
    load, never per draw."""
    def stamped(host):
        # scene_hosts includes the view-settings curve mapping, which is not
        # an ID and raises "this type doesn't support IDProperties" on get().
        # load_post fires on every file load INCLUDING a factory reset, so an
        # unguarded raise here printed a traceback on every test's setup.
        try:
            return host.get(applied_motion.PROPERTY) is not None
        except (AttributeError, ReferenceError, TypeError):
            return False

    found = []
    for obj in bpy.data.objects:
        for host in applied_motion.hosts_for_object(obj):
            if stamped(host):
                found.append(host)
    for scene in bpy.data.scenes:
        for host in applied_motion.scene_hosts(scene):
            if stamped(host):
                found.append(host)
    return found


@bpy.app.handlers.persistent
def _on_depsgraph_update(_scene, depsgraph):
    try:
        updates = list(depsgraph.updates)
    except (AttributeError, ReferenceError):
        return
    for update in updates:
        identity = getattr(update, "id", None)
        original = getattr(identity, "original", identity)
        if original is None:
            continue
        try:
            stamped = original.get(applied_motion.PROPERTY) is not None
        except (AttributeError, ReferenceError, TypeError):
            continue
        if stamped:
            note_host(original)


@bpy.app.handlers.persistent
def _on_load(*_args):
    # Files saved before the reconciler existed carry leftovers that no
    # depsgraph update will ever flag, because nothing touches those IDs.
    _PENDING.clear()
    for host in stamped_hosts_in_file():
        note_host(host)


def register():
    for bucket, function in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
                             (bpy.app.handlers.load_post, _on_load)):
        # By name, not identity: after a reload the old function object is
        # gone and an identity check would append a second handler beside
        # its ghost.
        for existing in list(bucket):
            if getattr(existing, "__name__", "") == function.__name__:
                bucket.remove(existing)
        bucket.append(function)


def unregister():
    for bucket, function in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
                             (bpy.app.handlers.load_post, _on_load)):
        for existing in list(bucket):
            if getattr(existing, "__name__", "") == function.__name__:
                bucket.remove(existing)
    if bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    _PENDING.clear()


__all__ = ("record_is_live", "reconcile_host", "find_invalid", "remove_ghost",
           "note_host", "flush_pending", "stamped_hosts_in_file",
           "set_purge_hook", "register", "unregister",
           "DEAD_RECORD", "DEAD_CHANNEL", "GHOST_DRIVER")
