"""Bake driven properties to plain keyframes.

A Driver Espresso template produces a live driver - an expression re-evaluated
every frame. Baking freezes that: it samples the driven value across a frame
range, writes those samples as ordinary keyframes, and (by default) removes the
driver, so the property becomes hand-editable animation that survives without the
add-on installed.

The sample is taken from the EVALUATED property, not from re-running the built
expression. That matters: a driver may read an input controller, another object,
or a scene value, and only the evaluated result reflects all of it. Stepping the
frame and reading the property is the one method that is always correct.
"""

from __future__ import annotations

import bpy
import math


def preflight_targets(targets):
    """Refuse read-only owners and Actions before sampling or rollback copies."""
    for target in targets:
        owner, path = _resolve_owner_and_path(target.owner, target.data_path)
        action = getattr(getattr(owner, "animation_data", None), "action", None)
        for block in (owner, action):
            if block is not None and not getattr(block, "is_editable", True):
                return f"Cannot bake {path}: {block.name} is linked or read-only."
    return ""


def _verified_key(id_block, path, index, frame, value):
    curve = _find_action_fcurve(id_block, path, index)
    animation = id_block.animation_data
    frame = animation.nla_tweak_strip_time_to_scene(frame, invert=True)
    return curve is not None and any(
        abs(point.co[0] - frame) < 1e-4
        and math.isclose(point.co[1], value, rel_tol=1e-5, abs_tol=1e-5)
        for point in curve.keyframe_points
    )


def _resolve_owner_and_path(owner, data_path):
    """Split an owner+path into the object that holds keyframes and a local path.

    A driver target's owner may be the datablock (an Object) or a nested struct
    (a node socket, a bone). keyframe_insert lives on the ID-bearing struct, so
    resolve to ``id_data`` and rebase the path onto it.
    """
    id_data = getattr(owner, "id_data", None)
    if id_data is None or id_data is owner:
        return owner, data_path
    try:
        prefix = owner.path_from_id()
    except Exception:
        return owner, data_path
    full = f"{prefix}.{data_path}" if prefix else data_path
    return id_data, full


def _read(id_block, depsgraph, data_path, index):
    """Read one evaluated scalar from a datablock at the current frame."""
    value = id_block.evaluated_get(depsgraph).path_resolve(data_path)
    return float(value[index] if index >= 0 else value)


def frame_list(start, end, step):
    """Frames to sample: every ``step`` from start to end, always including end.

    The endpoint is forced in so a bake never stops short of the last frame just
    because the range length is not a whole multiple of the step.
    """
    start, end = int(start), int(end)
    if end < start:
        start, end = end, start
    step = max(1, int(step))
    frames = list(range(start, end + 1, step))
    if frames and frames[-1] != end:
        frames.append(end)
    return frames


def _find_action_fcurve(id_block, data_path, index):
    """The keyframe F-curve just written for one target (not its driver)."""
    action = getattr(getattr(id_block, "animation_data", None), "action", None)
    if action is None:
        return None
    direct = getattr(action, "fcurves", None)
    slot = getattr(id_block.animation_data, "action_slot", None)
    bags = [direct] if direct else [
        bag.fcurves
        for layer in getattr(action, "layers", ()) or ()
        for strip in getattr(layer, "strips", ()) or ()
        for bag in getattr(strip, "channelbags", ()) or ()
        if slot is not None and bag.slot_handle == slot.handle
    ]
    for fcurves in bags:
        for fcurve in fcurves or ():
            if fcurve.data_path == data_path and (index < 0 or fcurve.array_index == index):
                return fcurve
    return None


def action_is_shared(owner):
    """Count artist users, excluding transaction holding copies and fake users."""
    from .transaction import holding_user_pointers

    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return False
    users = bpy.data.user_map(subset={action}).get(action, set())
    holding = holding_user_pointers()
    if any(user != owner and user.as_pointer() not in holding
           for user in users):
        return True
    return any(strip.action == action for track in animation.nla_tracks for strip in track.strips)


def _isolate_shared_action(owner):
    if not action_is_shared(owner):
        return
    animation = owner.animation_data
    slot = getattr(animation, "action_slot", None)
    identifier = slot.identifier if slot is not None else ""
    copied = animation.action.copy()
    animation.action = copied
    if identifier:
        animation.action_slot = next(item for item in copied.slots if item.identifier == identifier)


def bake_targets(scene, targets, *, start, end, step=1, remove_driver=True, progress=None,
                 smart=False, smart_tolerance=0.01, smart_passes=2):
    """Bake atomically, joining the caller's cleanup transaction when present."""
    from .transaction import bake_transaction
    targets = list(targets)
    reason = preflight_targets(targets)
    if reason:
        return 0, 0, reason
    with bake_transaction(bpy.context, targets=targets):
        return _bake_targets(
            scene, targets, start=start, end=end, step=step,
            remove_driver=remove_driver, progress=progress, smart=smart,
            smart_tolerance=smart_tolerance, smart_passes=smart_passes,
        )


def _bake_targets(scene, targets, *, start, end, step=1, remove_driver=True, progress=None,
                 smart=False, smart_tolerance=0.01, smart_passes=2):
    """Bake a list of (owner, data_path, index) targets to keyframes.

    Returns ``(baked_count, keyframe_count, message)``. Every value is sampled
    BEFORE any driver is removed, so removing one target's driver cannot change
    another target's samples midway.

    ``progress`` is an optional callable taking ``(done, total)`` frame counts,
    so an operator can drive a Blender progress bar during a long bake.

    Sampling is frame-OUTER: the frame is set once and every target read at that
    frame, rather than stepping the whole timeline once per target. Setting the
    frame re-evaluates the entire depsgraph, so a per-target loop would repeat
    that work once for each of a colour plan's three channels.
    """
    targets = list(targets)
    reason = preflight_targets(targets)
    if reason:
        return 0, 0, reason
    frames = frame_list(start, end, step)
    if not frames:
        return 0, 0, "Nothing to bake: the frame range is empty."

    # Resolve every target to a keyframable (id_block, path) up front.
    plans = []
    for target in targets:
        owner = getattr(target, "owner", None)
        data_path = getattr(target, "data_path", None)
        index = int(getattr(target, "index", -1))
        if owner is None or not data_path:
            continue
        id_block, resolved_path = _resolve_owner_and_path(owner, data_path)
        if not isinstance(id_block, bpy.types.ID):
            continue
        plans.append([owner, id_block, data_path, resolved_path, index, []])

    if not plans:
        return 0, 0, "None of the targets could be sampled."

    # Phase 1: sample. One frame_set per frame, all targets read while every
    # driver is still live.
    original = scene.frame_current
    try:
        for done, frame in enumerate(frames):
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            depsgraph.update()
            for plan in plans:
                _owner, id_block, _dp, resolved_path, index, samples = plan
                try:
                    samples.append((frame, _read(id_block, depsgraph, resolved_path, index)))
                except Exception as exc:  # noqa: BLE001 - report the property, not a traceback
                    scene.frame_set(original)
                    return 0, 0, f"Could not sample {resolved_path}: {exc}"
            if progress is not None:
                progress(done + 1, len(frames))
    finally:
        scene.frame_set(original)

    # Capture private dependencies while the public drivers still exist. The
    # driver variables are the authoritative ownership map; merely checking
    # whether an object has any driver confuses unrelated animation with a
    # surviving Espresso effect.
    helper_resources = []
    if remove_driver:
        from ...apply import internal_helpers
        for _owner, id_block, _data_path, resolved_path, index, _samples in plans:
            fcurve = _find_driver(id_block, resolved_path, index)
            if fcurve is not None:
                helper_resources.extend(
                    internal_helpers.resources_from_driver(fcurve.driver)
                )

    for id_block in dict.fromkeys(plan[1] for plan in plans):
        _isolate_shared_action(id_block)

    # Keep every driver until all replacement keys have been verified.
    keyframes = 0
    dense_total = 0
    for owner, id_block, data_path, resolved_path, index, samples in plans:
        # Smart bake reduces WHICH samples get written; it never changes the
        # samples themselves, so a curve it declines to thin bakes exactly as
        # it always did.
        dense_total += len(samples)
        write_samples, smart_plan = samples, None
        if smart:
            from . import smart_bake
            kept, spec, _worst = smart_bake.plan(
                samples, tolerance_fraction=smart_tolerance, passes=smart_passes)
            if kept is not None:
                write_samples = [samples[i] for i in kept]
                smart_plan = (kept, spec)

        for frame, value in write_samples:
            # Write the value, then key it. Setting the property first means the
            # keyframe records the sampled number even for a property Blender
            # would otherwise read as its rest value.
            _assign(id_block, resolved_path, index, value)
            if index >= 0:
                inserted = id_block.keyframe_insert(resolved_path, index=index, frame=frame)
            else:
                inserted = id_block.keyframe_insert(resolved_path, frame=frame)
            if not inserted or not _verified_key(id_block, resolved_path, index, frame, value):
                raise RuntimeError(f"Could not write a replacement key for {resolved_path} at frame {frame}.")
            keyframes += 1

        # Interpolation and handles are part of the RESULT, not decoration. The
        # fit was measured against these exact types - a bounce apex is
        # auto-clamped so it cannot overshoot, its impact is vector because the
        # curve really does corner there, and a square hold is CONSTANT because
        # that is exact. Writing the keys without them produces a curve that
        # misses the tolerance it was fitted to.
        if smart_plan is not None:
            from . import smart_bake
            fcurve = _find_action_fcurve(id_block, resolved_path, index)
            if fcurve is not None:
                kept, spec = smart_plan
                smart_bake.apply_plan(fcurve, samples, kept, spec)

    if remove_driver:
        for owner, id_block, data_path, resolved_path, index, _samples in plans:
            if index >= 0:
                owner.driver_remove(data_path, index)
            else:
                owner.driver_remove(data_path)
            if _find_driver(id_block, resolved_path, index) is not None:
                raise RuntimeError(f"Could not remove the driver on {resolved_path}.")

    # Phase 3: the private helpers those drivers fed are now dead.
    #
    removed_helpers = 0

    noun = "driver" if len(plans) == 1 else "drivers"
    verb = "Baked and removed" if remove_driver else "Baked"
    message = f"{verb} {len(plans)} {noun} to {keyframes} keyframes."
    if smart and dense_total:
        saved = dense_total - keyframes
        if saved > 0:
            message += (" Smart bake saved %d of %d keys (%.0f%% fewer)."
                        % (saved, dense_total, 100.0 * saved / dense_total))
        else:
            # Silence here would read as "smart bake did nothing wrong"; the
            # artist should know the motion genuinely needed every frame.
            message += " Smart bake kept every frame: this motion changes too fast to thin."
    if removed_helpers:
        message += (" Cleared %d helper propert%s."
                    % (removed_helpers, "y" if removed_helpers == 1 else "ies"))
    return len(plans), keyframes, message


def _find_driver(id_block, data_path, index):
    """Return the exact driver F-curve for one resolved bake target."""
    animation = getattr(id_block, "animation_data", None)
    for fcurve in getattr(animation, "drivers", None) or ():
        if fcurve.data_path != data_path:
            continue
        if index < 0 or fcurve.array_index == index:
            return fcurve
    return None


def _assign(id_block, data_path, index, value):
    """Write ``value`` into an evaluated-path property on the real datablock."""
    container_path, _, attr = data_path.rpartition(".")
    container = id_block.path_resolve(container_path) if container_path else id_block
    if index >= 0:
        current = getattr(container, attr)
        current[index] = value
    else:
        setattr(container, attr, value)


# ---------------------------------------------------------------------------
# Standalone bake: finding drivers that Driver Espresso did not necessarily
# create. bake_targets above needs a list of targets; this is what produces one
# from a selection.
# ---------------------------------------------------------------------------

class DiscoveredTarget:
    """One driven channel, shaped for ``bake_targets``.

    ``owner`` is the ID that actually carries the driver, which is frequently
    NOT the object: a shape-key driver lives on the Key datablock, a colour
    driver on the material's node tree, a light's energy on the Light data.
    """

    __slots__ = ("owner", "data_path", "index", "label")

    def __init__(self, owner, data_path, index, label):
        self.owner = owner
        self.data_path = data_path
        self.index = index
        self.label = label


def _is_array_property(owner, data_path):
    """True when the driven property is a vector/colour rather than a scalar."""
    try:
        value = owner.path_resolve(data_path)
    except Exception:
        return None  # unresolvable - the caller reports it rather than guessing
    return hasattr(value, "__len__") and not isinstance(value, str)


def _driver_blocks_for_object(obj):
    """Every datablock reachable from one object that can carry a driver.

    Walking only ``obj.animation_data`` is the single biggest gap in a naive
    bake: measured on a scene with drivers on an object, a shape key, a
    material node socket, a light's energy and a pose bone, the object-only
    walk finds two of the five. The three it misses are precisely the ones a
    template add-on creates.
    """
    yield obj, "object"
    data = getattr(obj, "data", None)
    if data is not None:
        yield data, "data"
        if getattr(data, "node_tree", None) is not None:
            yield data.node_tree, "data nodes"
        keys = getattr(data, "shape_keys", None)
        if keys is not None:
            yield keys, "shape keys"
        # A mesh's materials hang off the data as well as the object slots;
        # both are visited and de-duplicated by identity in the caller.
        for material in getattr(data, "materials", None) or ():
            if material is not None:
                yield material, "material"
                if getattr(material, "node_tree", None) is not None:
                    yield material.node_tree, "material nodes"
    for slot in getattr(obj, "material_slots", None) or ():
        material = getattr(slot, "material", None)
        if material is not None:
            yield material, "material"
            if getattr(material, "node_tree", None) is not None:
                yield material.node_tree, "material nodes"
    for system in getattr(obj, "particle_systems", None) or ():
        settings = getattr(system, "settings", None)
        if settings is not None:
            yield settings, "particles"


def discover_driver_targets(objects, *, scene=None, include_muted=True):
    """Collect every driven channel on ``objects`` (and optionally the scene).

    Returns ``(targets, unresolved)``. ``unresolved`` counts drivers whose data
    path could not be resolved - a stale driver left behind by a deleted node
    or bone. They are reported rather than silently dropped, because a bake
    that quietly skips channels looks identical to one that worked.
    """
    seen_blocks = set()
    seen_channels = set()
    targets = []
    unresolved = 0

    blocks = []
    for obj in objects or ():
        for block, label in _driver_blocks_for_object(obj):
            blocks.append((block, label))
    if scene is not None:
        blocks.append((scene, "scene"))
        if getattr(scene, "world", None) is not None:
            blocks.append((scene.world, "world"))
            if getattr(scene.world, "node_tree", None) is not None:
                blocks.append((scene.world.node_tree, "world nodes"))
        compositing = getattr(scene, "compositing_node_group", None)
        if compositing is not None:
            blocks.append((compositing, "compositor"))

    for block, label in blocks:
        if block is None or id(block) in seen_blocks:
            continue
        seen_blocks.add(id(block))
        anim = getattr(block, "animation_data", None)
        if anim is None:
            continue
        for fcurve in getattr(anim, "drivers", None) or ():
            if not include_muted and fcurve.mute:
                continue
            data_path = fcurve.data_path
            is_array = _is_array_property(block, data_path)
            if is_array is None:
                unresolved += 1
                continue
            index = fcurve.array_index if is_array else -1
            key = (id(block), data_path, index)
            if key in seen_channels:
                continue
            seen_channels.add(key)
            targets.append(DiscoveredTarget(block, data_path, index, label))
    return targets, unresolved


def set_driver_mute(targets, mute):
    """Mute or unmute the drivers behind ``targets``; returns how many changed.

    Muting is the non-destructive alternative to removal, and it is not
    optional decoration: a LIVE driver overrides the keyframes on its own
    channel (measured), so a bake that leaves drivers running is invisible.
    Either the driver goes or it is silenced - keeping it live is the one
    outcome that cannot work.
    """
    changed = 0
    for target in targets:
        anim = getattr(target.owner, "animation_data", None)
        for fcurve in getattr(anim, "drivers", None) or ():
            same_index = target.index < 0 or fcurve.array_index == target.index
            if fcurve.data_path == target.data_path and same_index:
                if fcurve.mute != mute:
                    fcurve.mute = mute
                    changed += 1
    return changed
