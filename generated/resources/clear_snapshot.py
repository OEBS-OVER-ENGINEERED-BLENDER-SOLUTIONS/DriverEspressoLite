"""Identity-preserving snapshots for generated CLEAR rollback.

Route `clear()` and layout teardown destroy Blender datablocks. Restoring only
applied-motion JSON leaves records pointing at missing modifiers, node groups,
helpers, and carriers. This module copies the owned graph before mutation so
rollback can put the same identities back.
"""

from __future__ import annotations

import copy
import uuid

import bpy

from ...apply import applied_motion
from ...apply.core import target_memory
from . import attachments as generated_attachments
from . import constraint_snapshot


_HOLDING_PREFIX = "__espresso_clear_rollback_"


def _alive(block, collection_name):
    if block is None:
        return False
    try:
        collection = getattr(bpy.data, collection_name)
        return block.name in collection
    except (ReferenceError, AttributeError, TypeError):
        return False


def _object_alive(obj):
    return _alive(obj, "objects")


def _id_alive(block):
    return target_memory.host_is_alive(block)


def _host_key(block):
    return "%s:%s" % (block.__class__.__name__, getattr(block, "name", ""))


def _remove_driver(owner, data_path, index):
    if index >= 0:
        try:
            owner.driver_remove(data_path, index)
        except Exception:
            return
        return
    try:
        owner.driver_remove(data_path)
    except Exception:
        pass


def _add_driver(owner, data_path, index):
    if index >= 0:
        try:
            return owner.driver_add(data_path, index)
        except TypeError:
            pass
    return owner.driver_add(data_path)


def _find_driver(owner, data_path, index):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return None
    for curve in getattr(animation, "drivers", ()) or ():
        if curve.data_path != data_path:
            continue
        if index < 0 or int(curve.array_index) == int(index):
            return curve
    return None


def _driver_snapshot_errors(expected, actual, prefix):
    errors = []
    for key in ("type", "expression", "use_self"):
        if expected.get(key) != actual.get(key):
            errors.append(
                "%s %s mismatch: %r != %r"
                % (prefix, key, actual.get(key), expected.get(key))
            )
    expected_vars = list(expected.get("variables") or ())
    actual_vars = list(actual.get("variables") or ())
    if len(expected_vars) != len(actual_vars):
        errors.append(
            "%s variable count mismatch: %r != %r"
            % (prefix, len(actual_vars), len(expected_vars))
        )
        return errors
    for index, (expected_var, actual_var) in enumerate(zip(expected_vars, actual_vars)):
        var_prefix = "%s variable %s" % (prefix, expected_var.get("name") or index)
        for key in ("name", "type"):
            if expected_var.get(key) != actual_var.get(key):
                errors.append(
                    "%s %s mismatch: %r != %r"
                    % (var_prefix, key, actual_var.get(key), expected_var.get(key))
                )
        expected_targets = list(expected_var.get("targets") or ())
        actual_targets = list(actual_var.get("targets") or ())
        if len(expected_targets) != len(actual_targets):
            errors.append(
                "%s target count mismatch: %r != %r"
                % (var_prefix, len(actual_targets), len(expected_targets))
            )
            continue
        for target_index, (expected_target, actual_target) in enumerate(
            zip(expected_targets, actual_targets)
        ):
            target_prefix = "%s target %s" % (var_prefix, target_index)
            expected_id = expected_target.get("id") or {}
            actual_id = actual_target.get("id") or {}
            expected_identity = (
                expected_id.get("type"),
                expected_id.get("name"),
                expected_id.get("library") or "",
            )
            actual_identity = (
                actual_id.get("type"),
                actual_id.get("name"),
                actual_id.get("library") or "",
            )
            if expected_identity != actual_identity:
                errors.append(
                    "%s id/library mismatch: %r != %r"
                    % (target_prefix, actual_identity, expected_identity)
                )
            for name in (
                "id_type",
                "data_path",
                "bone_target",
                "transform_type",
                "transform_space",
                "rotation_mode",
                "context_property",
            ):
                if name not in expected_target:
                    continue
                if expected_target.get(name) != actual_target.get(name):
                    errors.append(
                        "%s %s mismatch: %r != %r"
                        % (
                            target_prefix, name,
                            actual_target.get(name), expected_target.get(name),
                        )
                    )
    return errors


def _verify_drivers(owner, records, label, errors):
    from ...ui.state import live_controls

    for driver in records or ():
        data_path = driver["data_path"]
        curve = _find_driver(owner, data_path, int(driver["index"]))
        if curve is None:
            errors.append("missing driver %s on %s" % (data_path, label))
            continue
        expected = driver.get("snapshot") or {}
        actual = live_controls.snapshot_driver(curve.driver)
        errors.extend(
            _driver_snapshot_errors(expected, actual, "%s on %s" % (data_path, label))
        )


def _captured_modifiers(obj):
    records = []
    for modifier in list(getattr(obj, "modifiers", ()) or ()):
        group = getattr(modifier, "node_group", None)
        records.append({
            "name": modifier.name,
            "type": modifier.type,
            "node_group": group.name if group is not None else "",
            "id_properties": _id_property_map(modifier),
            "attachment": generated_attachments.identity(obj, modifier),
        })
    return records


def _id_property_map(block):
    result = {}
    try:
        keys = list(block.keys())
    except (AttributeError, TypeError):
        return result
    for key in keys:
        try:
            value = block[key]
        except (AttributeError, TypeError, KeyError, UnicodeDecodeError):
            continue
        if isinstance(value, (bytes, bytearray, memoryview)):
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        try:
            result[key] = value
        except (AttributeError, TypeError):
            continue
    return result


def _apply_id_properties(block, values):
    if block is None or not values:
        return
    for key, value in values.items():
        try:
            block[key] = value
        except (AttributeError, TypeError, KeyError, UnicodeDecodeError):
            continue


def _copy_id_properties(source, destination):
    if source is None or destination is None:
        return
    try:
        keys = list(source.keys())
    except (AttributeError, TypeError):
        return
    for key in keys:
        try:
            value = source[key]
        except (AttributeError, TypeError, KeyError, UnicodeDecodeError):
            continue
        if isinstance(value, (bytes, bytearray, memoryview)):
            continue
        try:
            destination[key] = value
        except (AttributeError, TypeError, KeyError, UnicodeDecodeError):
            continue


def _snapshot_drivers(obj):
    from ...ui.state import live_controls

    animation = getattr(obj, "animation_data", None)
    if animation is None:
        return []
    records = []
    for curve in list(getattr(animation, "drivers", ()) or ()):
        records.append({
            "data_path": curve.data_path,
            "index": curve.array_index,
            "snapshot": live_controls.snapshot_driver(curve.driver),
        })
    return records


def _restore_drivers(obj, records, errors=None):
    from ...ui.state import live_controls

    if errors is None:
        errors = []
    for item in records:
        data_path = item["data_path"]
        index = int(item["index"])
        try:
            _remove_driver(obj, data_path, index)
            curve = _add_driver(obj, data_path, index)
            if isinstance(curve, list):
                curve = curve[0] if curve else None
            if curve is None:
                errors.append(
                    "could not add driver %s on %s"
                    % (data_path, getattr(obj, "name", ""))
                )
                continue
            live_controls.restore_driver(curve.driver, item["snapshot"])
        except Exception as exc:
            errors.append(
                "%s %s: %s" % (getattr(obj, "name", ""), data_path, exc)
            )
    return errors


def _scene_links(obj):
    names = []
    for collection in bpy.data.collections:
        if obj.name in collection.objects:
            names.append(collection.name)
    scene = bpy.context.scene
    if scene is not None and obj.name in scene.collection.objects:
        names.append("")
    return tuple(names)


def _copy_rna(src, dst, skip=frozenset()):
    rna = getattr(src, "bl_rna", None)
    if rna is None:
        return
    skip = set(skip) | {"rna_type", "name", "type"}
    for prop in rna.properties:
        ident = prop.identifier
        if prop.is_readonly or ident in skip:
            continue
        try:
            setattr(dst, ident, getattr(src, ident))
        except Exception:
            continue


def _restore_object_from_clone(live, clone):
    """Copy ID properties and missing modifiers onto a host that survived CLEAR."""
    _copy_id_properties(clone, live)
    existing = {item.name: item for item in list(getattr(live, "modifiers", ()) or ())}
    for src in list(getattr(clone, "modifiers", ()) or ()):
        dst = existing.get(src.name)
        if dst is None:
            try:
                dst = live.modifiers.new(src.name, src.type)
            except Exception:
                continue
            existing[dst.name] = dst
        _copy_rna(src, dst)
        if hasattr(src, "node_group") and hasattr(dst, "node_group"):
            if dst.node_group is None and src.node_group is not None:
                dst.node_group = src.node_group
        _copy_id_properties(src, dst)


def _snapshot_transform(obj):
    """Capture local transform and parenting state changed by native baking."""
    return {
        "location": tuple(obj.location),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": tuple(obj.rotation_euler),
        "rotation_quaternion": tuple(obj.rotation_quaternion),
        "rotation_axis_angle": tuple(obj.rotation_axis_angle),
        "scale": tuple(obj.scale),
        "delta_location": tuple(obj.delta_location),
        "delta_rotation_euler": tuple(obj.delta_rotation_euler),
        "delta_rotation_quaternion": tuple(obj.delta_rotation_quaternion),
        "delta_scale": tuple(obj.delta_scale),
        "parent_name": obj.parent.name if obj.parent else "",
        "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone,
        "matrix_parent_inverse": obj.matrix_parent_inverse.copy(),
    }


def _restore_transform(obj, state):
    parent_name = state.get("parent_name") or ""
    obj.parent = bpy.data.objects.get(parent_name) if parent_name else None
    obj.parent_type = state.get("parent_type") or "OBJECT"
    obj.parent_bone = state.get("parent_bone") or ""
    obj.matrix_parent_inverse = state["matrix_parent_inverse"]
    obj.rotation_mode = state["rotation_mode"]
    obj.location = state["location"]
    obj.rotation_euler = state["rotation_euler"]
    obj.rotation_quaternion = state["rotation_quaternion"]
    obj.rotation_axis_angle = state["rotation_axis_angle"]
    obj.scale = state["scale"]
    obj.delta_location = state["delta_location"]
    obj.delta_rotation_euler = state["delta_rotation_euler"]
    obj.delta_rotation_quaternion = state["delta_rotation_quaternion"]
    obj.delta_scale = state["delta_scale"]


def _transform_errors(obj, state, label):
    actual = _snapshot_transform(obj)
    errors = []
    for key in (
        "location", "rotation_mode", "rotation_euler", "rotation_quaternion",
        "rotation_axis_angle", "scale", "delta_location",
        "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale",
        "parent_name", "parent_type", "parent_bone",
    ):
        if actual.get(key) != state.get(key):
            errors.append("%s %s mismatch" % (label, key))
    if actual["matrix_parent_inverse"] != state["matrix_parent_inverse"]:
        errors.append("%s matrix_parent_inverse mismatch" % label)
    return errors


def _link_object(obj, collection_names):
    scene = bpy.context.scene
    for name in collection_names:
        if name == "":
            if scene is not None and obj.name not in scene.collection.objects:
                scene.collection.objects.link(obj)
            continue
        collection = bpy.data.collections.get(name)
        if collection is not None and obj.name not in collection.objects:
            collection.objects.link(obj)


class GeneratedClearSnapshot:
    """Copied generated graph plus motion records for one CLEAR transaction."""

    def __init__(self):
        self._token = _HOLDING_PREFIX + uuid.uuid4().hex[:12]
        self._objects = []
        self._ids = []
        self._collections = []
        self._node_groups = []
        self._data_copies = []
        self._records = {}
        self._source_state = []
        self._actions = {}
        self._known_actions = set(bpy.data.actions.keys())
        self._discarded = False
        self.restored = False
        self._last_verify = {"ok": False, "errors": ["not restored"]}
        self._restore_errors = []

    def _capture_action_state(self, owner):
        animation = getattr(owner, "animation_data", None)
        action = getattr(animation, "action", None) if animation is not None else None
        if action is None:
            return {"action_key": None, "slot_identifier": ""}
        key = int(action.as_pointer())
        if key not in self._actions:
            copied = action.copy()
            copied.use_fake_user = True
            copied.name = self._token + "_action_" + action.name
            self._actions[key] = {
                "original": action,
                "original_name": action.name,
                "copy": copied,
                "promoted": False,
            }
        slot = getattr(animation, "action_slot", None)
        return {
            "action_key": key,
            "slot_identifier": getattr(slot, "identifier", "") if slot else "",
        }

    def preserve_action_identity(self, owner):
        """For modifier-only clear routes that guarantee artist Actions are untouched."""
        state = getattr(owner, 'animation_data', None)
        action = getattr(state, 'action', None)
        if action is not None and action.as_pointer() in self._actions:
            self._actions[action.as_pointer()]['preserve_original'] = True

    def _restore_action_state(self, owner, state):
        animation = getattr(owner, "animation_data", None)
        key = state.get("action_key") if state else None
        if key is None:
            if animation is not None:
                animation.action = None
            return
        entry = self._actions.get(key)
        if entry is None:
            return
        restored = self._promote_action(entry)
        animation = owner.animation_data_create()
        animation.action = restored
        identifier = state.get("slot_identifier") or ""
        if identifier:
            slot = next(
                (item for item in getattr(restored, "slots", ()) if item.identifier == identifier),
                None,
            )
            if slot is not None:
                try:
                    animation.action_slot = slot
                except (AttributeError, RuntimeError, TypeError):
                    pass

    def _promote_action(self, entry):
        if entry.get('preserve_original') and _alive(entry['original'], 'actions'):
            return entry['original']
        if entry.get("promoted"):
            return entry["copy"]
        copied = entry["copy"]
        original = entry["original"]
        if _alive(original, "actions") and original != copied:
            try:
                original.user_remap(copied)
            except (ReferenceError, RuntimeError, TypeError):
                pass
            if _alive(original, "actions") and original.users == 0:
                bpy.data.actions.remove(original)
        copied.name = entry["original_name"]
        copied.use_fake_user = False
        entry["promoted"] = True
        return copied

    def capture_object(self, obj):
        if not _object_alive(obj):
            return
        if any(item["name"] == obj.name for item in self._objects):
            return
        # Validate/capture independently before allocating holding copies. A
        # shallow object clone cannot retain targets after their IDs are gone.
        constraints = constraint_snapshot.capture(obj)
        data_copy = None
        data_collection = ""
        if obj.data is not None:
            data_copy = obj.data.copy()
            data_copy.use_fake_user = True
            data_copy.name = self._token + "_data_" + obj.name
            data_collection = (
                target_memory.datablock_collection_name(data_copy)
                or target_memory.datablock_collection_name(obj.data)
            )
            self._data_copies.append({
                "collection": data_collection,
                "copy": data_copy,
            })
        clone = obj.copy()
        clone.data = data_copy
        clone.use_fake_user = True
        clone.name = self._token + "_obj_" + obj.name
        _copy_id_properties(obj, clone)
        original_modifiers = {
            item.name: item for item in list(getattr(obj, "modifiers", ()) or ())
        }
        group_map = {}
        for modifier in list(getattr(clone, "modifiers", ()) or ()):
            original = original_modifiers.get(modifier.name)
            if original is not None:
                _copy_id_properties(original, modifier)
            group = getattr(modifier, "node_group", None)
            if group is None:
                continue
            copied = group_map.get(group.name)
            if copied is None:
                copied = group.copy()
                copied.use_fake_user = True
                copied.name = self._token + "_ng_" + group.name
                group_map[group.name] = copied
                self._node_groups.append({
                    "original_name": group.name,
                    "copy": copied,
                })
            modifier.node_group = copied
            if original is not None:
                _copy_id_properties(original, modifier)
        palette = obj.get("__espresso_layout_source_collection")
        palette_name = ""
        source_names = []
        if isinstance(palette, bpy.types.Collection):
            palette_name = palette.name
            holding = bpy.data.collections.new(self._token + "_col_" + palette.name)
            holding.use_fake_user = True
            for child in list(palette.objects):
                holding.objects.link(child)
                source_names.append(child.name)
                if not any(item["name"] == child.name for item in self._source_state):
                    self._source_state.append({
                        "name": child.name,
                        "hide_render": bool(child.hide_render),
                        "hide_viewport": bool(child.hide_get()),
                    })
            clone["__espresso_layout_source_collection"] = holding
            self._collections.append({
                "original_name": palette_name,
                "copy": holding,
                "source_names": tuple(source_names),
            })
        self._objects.append({
            "name": obj.name,
            "source_pointer": obj.as_pointer(),
            "constraints": constraints,
            "data_name": obj.data.name if obj.data is not None else "",
            "copy": clone,
            "collection_names": _scene_links(obj),
            "parent_name": obj.parent.name if obj.parent else "",
            "transform": _snapshot_transform(obj),
            "action_state": self._capture_action_state(obj),
            "data_action_state": (
                self._capture_action_state(obj.data) if obj.data is not None else None
            ),
            "drivers": _snapshot_drivers(obj),
            "modifiers": _captured_modifiers(obj),
            "source_names": tuple(source_names),
            "palette_name": palette_name,
            "data_collection": data_collection,
        })
        key = _host_key(obj)
        if key not in self._records:
            self._records[key] = copy.deepcopy(applied_motion.read(obj))

    def capture_id(self, block):
        """Snapshot drivers and applied-motion records on a non-object ID."""
        if not _id_alive(block):
            return
        if isinstance(block, bpy.types.Object):
            self.capture_object(block)
            return
        key = _host_key(block)
        if any(item.get("id_key") == key for item in self._ids):
            return
        self._ids.append({
            "id_key": key,
            "id_type": block.__class__.__name__,
            "name": getattr(block, "name", ""),
            "collection": target_memory.datablock_collection_name(block),
            "drivers": _snapshot_drivers(block),
            "action_state": self._capture_action_state(block),
        })
        if key not in self._records:
            self._records[key] = copy.deepcopy(applied_motion.read(block))

    def capture_record(self, host, record=None):
        if host is None:
            return
        if isinstance(host, bpy.types.Object):
            self.capture_object(host)
            return
        self.capture_id(host)

    def capture_node_group(self, group):
        if group is None or not _alive(group, "node_groups"):
            return
        if any(item["original_name"] == group.name for item in self._node_groups):
            return
        copied = group.copy()
        copied.use_fake_user = True
        copied.name = self._token + "_ng_" + group.name
        self._node_groups.append({
            "original_name": group.name,
            "copy": copied,
        })

    def capture_collection(self, collection):
        """Retain an owned collection and its explicit scene/parent membership."""
        if collection is None or not _alive(collection, "collections"):
            return
        if any(item["original_name"] == collection.name for item in self._collections):
            return
        parents = tuple(parent.name for parent in bpy.data.collections
                        if not parent.name.startswith(self._token)
                        and collection.name in parent.children)
        scenes = tuple(scene.name for scene in bpy.data.scenes
                       if collection.name in scene.collection.children)
        copied = collection.copy()
        copied.use_fake_user = True
        copied.name = self._token + "_col_" + collection.name
        self._collections.append({
            "original_name": collection.name, "copy": copied,
            "source_names": tuple(obj.name for obj in collection.objects),
            "collection_state": {
                "parents": parents, "scenes": scenes,
                "children": tuple(child.name for child in collection.children),
                "values": {name: getattr(collection, name) for name in (
                    "hide_render", "hide_viewport", "hide_select", "color_tag")},
                "instance_offset": tuple(collection.instance_offset),
                "properties": _id_property_map(collection),
            },
        })

    def isolate_node_group_copies(self):
        """Make the rollback graph independent from every live nested group.

        Blender's node-group copy is shallow: copied group nodes still point at
        their live child groups.  That extra user prevents strict lifecycle
        clears from removing an otherwise fully owned live hierarchy.  Call
        this after all groups in the closure have been captured so holding
        parents reference holding children exclusively.
        """
        entries = tuple(self._node_groups)
        copies = {item["original_name"]: item["copy"] for item in entries}
        copy_names = {
            item["copy"].as_pointer(): item["original_name"] for item in entries
        }
        for item in entries:
            copied = item["copy"]
            if not _alive(copied, "node_groups"):
                continue
            for node in tuple(getattr(copied, "nodes", ()) or ()):
                child = getattr(node, "node_tree", None)
                if child is None:
                    continue
                original_name = copy_names.get(child.as_pointer(), child.name)
                replacement = copies.get(original_name)
                if replacement is not None and child is not replacement:
                    node.node_tree = replacement

    def restore(self):
        """Put copied identities back, then drop unpromoted holding copies."""
        if self._discarded:
            return self.restored
        ok = True
        try:
            for callback in getattr(self, '_before_restore_callbacks', ()):
                callback()
            ok = self._restore_identities()
        except ReferenceError:
            ok = False
        finally:
            self.discard()
            self._purge_unknown_actions()
        self._last_verify = self.verify()
        self.restored = bool(ok and self._last_verify.get("ok"))
        return self.restored

    def _restore_identities(self):
        ok = True
        driver_errors = self._restore_errors
        for item in self._node_groups:
            copied = item["copy"]
            if not _alive(copied, "node_groups"):
                ok = False
                continue
            if bpy.data.node_groups.get(item["original_name"]) is None:
                copied.name = item["original_name"]
                copied.use_fake_user = False
                item["promoted"] = True
        for item in self._collections:
            holding = item["copy"]
            if not _alive(holding, "collections"):
                ok = False
                continue
            original = bpy.data.collections.get(item["original_name"])
            if original is None:
                holding.name = item["original_name"]
                holding.use_fake_user = False
                original = holding
                item["promoted"] = True
            for source_name in item["source_names"]:
                source = bpy.data.objects.get(source_name)
                if source is not None and source.name not in original.objects:
                    original.objects.link(source)
        for item in self._objects:
            try:
                clone = item["copy"]
                if not _object_alive(clone):
                    ok = False
                    continue
                live = bpy.data.objects.get(item["name"])
                if live is None:
                    clone.name = item["name"]
                    clone.use_fake_user = False
                    data = clone.data
                    data_name = item.get("data_name") or ""
                    data_collection = item.get("data_collection") or ""
                    collection = (
                        getattr(bpy.data, data_collection, None)
                        if data_collection else None
                    )
                    if collection is None and data is not None:
                        coll_name = target_memory.datablock_collection_name(data)
                        collection = getattr(bpy.data, coll_name, None) if coll_name else None
                    if (
                        data is not None and data_name and collection is not None
                        and collection.get(data_name) is None
                    ):
                        data.name = data_name
                        data.use_fake_user = False
                    _link_object(clone, item["collection_names"])
                    live = clone
                    item["promoted"] = clone
                elif live != clone:
                    _restore_object_from_clone(live, clone)
            except ReferenceError as exc:
                driver_errors.append("%s identity restore: %s" % (item["name"], exc))
                ok = False
        # Every captured target must exist before any parent, constraint or
        # driver is rebound. Capture order is not dependency order.
        restored = self._restored_objects()
        self._restore_collection_links(driver_errors)
        for item in self._objects:
            try:
                live = bpy.data.objects.get(item["name"])
                if live is None:
                    ok = False
                    continue
                self._rebind_holding_node_groups(live)
                self._rebind_holding_object_data(live, item)
                self._restore_modifier_identity(live, item)
                self._restore_modifier_order(live, item)
                _restore_transform(live, item["transform"])
                constraint_snapshot.restore(live, item["constraints"], restored, driver_errors)
                self._restore_action_state(live, item.get("action_state"))
                if live.data is not None:
                    self._restore_action_state(
                        live.data, item.get("data_action_state")
                    )
                if item["palette_name"]:
                    palette = bpy.data.collections.get(item["palette_name"])
                    if palette is not None:
                        live["__espresso_layout_source_collection"] = palette
                _restore_drivers(live, item["drivers"], driver_errors)
                records = self._records.get(_host_key(live)) or self._records.get(item["name"])
                if records:
                    applied_motion.write(live, records)
                for source_name in item["source_names"]:
                    source = bpy.data.objects.get(source_name)
                    if source is None:
                        continue
                    source["__espresso_layout_carrier"] = live
            except ReferenceError:
                ok = False
                continue
        for item in self._ids:
            live = target_memory.find_id_block(item["id_type"], item["name"])
            if live is None:
                ok = False
                continue
            _restore_drivers(live, item["drivers"], driver_errors)
            self._restore_action_state(live, item.get("action_state"))
            records = self._records.get(item["id_key"])
            if records:
                applied_motion.write(live, records)
        self._restore_source_visibility()
        if driver_errors:
            ok = False
        return ok

    def _restored_objects(self):
        return {item["source_pointer"]: bpy.data.objects.get(item["name"])
                for item in self._objects}

    def _restore_collection_links(self, errors):
        for item in self._collections:
            state = item.get("collection_state")
            if state is None:
                continue
            collection = bpy.data.collections.get(item["original_name"])
            if collection is None:
                continue
            try:
                for name, value in state["values"].items():
                    setattr(collection, name, value)
                collection.instance_offset = state["instance_offset"]
                for key in _id_property_map(collection):
                    if key not in state["properties"]:
                        del collection[key]
                _apply_id_properties(collection, state["properties"])
                for obj in list(collection.objects):
                    if obj.name not in item["source_names"]:
                        collection.objects.unlink(obj)
                for name in item["source_names"]:
                    obj = bpy.data.objects.get(name)
                    if obj is None:
                        errors.append("collection %s missing object %s" % (collection.name, name))
                    elif name not in collection.objects:
                        collection.objects.link(obj)
                for child in list(collection.children):
                    if child.name not in state["children"]:
                        collection.children.unlink(child)
                for name in state["children"]:
                    child = bpy.data.collections.get(name)
                    if child is None:
                        errors.append("collection %s missing child %s" % (collection.name, name))
                    elif name not in collection.children:
                        collection.children.link(child)
                for parent in bpy.data.collections:
                    if parent.name.startswith(self._token):
                        continue
                    wanted = parent.name in state["parents"]
                    linked = collection.name in parent.children
                    if wanted and not linked:
                        parent.children.link(collection)
                    elif linked and not wanted:
                        parent.children.unlink(collection)
                for scene in bpy.data.scenes:
                    wanted = scene.name in state["scenes"]
                    linked = collection.name in scene.collection.children
                    if wanted and not linked:
                        scene.collection.children.link(collection)
                    elif linked and not wanted:
                        scene.collection.children.unlink(collection)
            except Exception as exc:
                errors.append("collection %s restore: %s" % (collection.name, exc))

    def _purge_unknown_actions(self):
        """Remove bake-created Actions after every captured owner was restored."""
        for action in list(bpy.data.actions):
            if action.name in self._known_actions or action.users:
                continue
            try:
                action.use_fake_user = False
                bpy.data.actions.remove(action)
            except (ReferenceError, RuntimeError):
                continue

    def _rebind_holding_node_groups(self, live):
        """Point restored modifiers at original groups before discard()."""
        for modifier in list(getattr(live, "modifiers", ()) or ()):
            group = getattr(modifier, "node_group", None)
            if group is None or not str(group.name).startswith(self._token):
                continue
            matched = None
            original_name = ""
            for item in self._node_groups:
                if item["copy"] == group:
                    matched = item
                    original_name = item["original_name"]
                    break
            original = bpy.data.node_groups.get(original_name) if original_name else None
            if original is not None and original != group:
                modifier.node_group = original
            elif original_name and bpy.data.node_groups.get(original_name) is None:
                group.name = original_name
                group.use_fake_user = False
                if matched is not None:
                    matched["promoted"] = True

    def _rebind_holding_object_data(self, live, item):
        data = getattr(live, "data", None)
        if data is None or not str(getattr(data, "name", "")).startswith(self._token):
            return
        data_name = item.get("data_name") or ""
        collection_name = item.get("data_collection") or target_memory.datablock_collection_name(data)
        collection = getattr(bpy.data, collection_name, None) if collection_name else None
        original = collection.get(data_name) if collection is not None and data_name else None
        if original is not None and original != data:
            live.data = original
            return
        if data_name:
            data.name = data_name
            data.use_fake_user = False

    def _restore_modifier_identity(self, live, item):
        existing = {entry.name: entry for entry in list(getattr(live, "modifiers", ()) or ())}
        for captured in item.get("modifiers") or ():
            destination = existing.get(captured.get("name"))
            if destination is None:
                continue
            _apply_id_properties(destination, captured.get("id_properties"))

    def _restore_modifier_order(self, live, item):
        """Restore the captured evaluation order after recreating modifiers."""
        desired = [entry.get("name") for entry in item.get("modifiers") or ()]
        modifiers = live.modifiers
        for target_index, name in enumerate(desired):
            current_index = next(
                (index for index, modifier in enumerate(modifiers)
                 if modifier.name == name),
                None,
            )
            if current_index is None or current_index == target_index:
                continue
            modifiers.move(current_index, target_index)

    def _restore_source_visibility(self):
        for state in self._source_state:
            source = bpy.data.objects.get(state["name"])
            if source is None:
                continue
            source.hide_render = bool(state.get("hide_render", False))
            source.hide_set(bool(state.get("hide_viewport", False)))

    def before_restore(self, callback):
        """Restore route-owned structures before their captured animation."""
        if not callable(callback):
            raise TypeError('Restore callback must be callable')
        if not hasattr(self, '_before_restore_callbacks'):
            self._before_restore_callbacks = []
        self._before_restore_callbacks.append(callback)

    def on_discard(self, callback):
        """Release route-owned zero-user resources after holding copies vanish."""
        if not callable(callback):
            raise TypeError('Discard callback must be callable')
        callbacks = getattr(self, '_discard_callbacks', None)
        if callbacks is None:
            self._discard_callbacks = callbacks = []
        callbacks.append(callback)

    def discard(self):
        """Drop holding copies after a committed CLEAR or a finished rollback."""
        if self._discarded:
            return
        self._discarded = True
        for item in self._objects:
            clone = item["copy"]
            if not _object_alive(clone) or not clone.name.startswith(self._token):
                continue
            data = clone.data
            try:
                bpy.data.objects.remove(clone, do_unlink=True)
            except Exception:
                pass
            self._remove_unused_data(data)
        for item in self._collections:
            holding = item["copy"]
            if not _alive(holding, "collections") or not holding.name.startswith(self._token):
                continue
            try:
                bpy.data.collections.remove(holding)
            except Exception:
                pass
        for item in self._node_groups:
            group = item["copy"]
            if not _alive(group, "node_groups") or not group.name.startswith(self._token):
                continue
            try:
                group.use_fake_user = False
            except Exception:
                pass
            try:
                bpy.data.node_groups.remove(group)
            except Exception:
                pass
        for item in self._actions.values():
            action = item["copy"]
            if item.get("promoted") or not _alive(action, "actions"):
                continue
            try:
                action.use_fake_user = False
                bpy.data.actions.remove(action)
            except (ReferenceError, RuntimeError):
                pass
        for item in self._data_copies:
            self._remove_unused_data(item.get("copy"), item.get("collection") or "")
        self._purge_token_prefixed()
        for callback in getattr(self, '_discard_callbacks', ()):
            callback()
        self._discard_callbacks = []

    def _remove_unused_data(self, data, collection_name=""):
        if data is None:
            return
        collection_name = collection_name or target_memory.datablock_collection_name(data)
        if not collection_name or not _alive(data, collection_name):
            return
        if not str(getattr(data, "name", "")).startswith(self._token):
            return
        try:
            data.use_fake_user = False
        except Exception:
            pass
        if data.users == 0:
            getattr(bpy.data, collection_name).remove(data)

    def _purge_token_prefixed(self):
        rank = {"objects": 0, "collections": 2, "node_groups": 3}
        leftovers = list(self.holding_leftovers())
        leftovers.sort(key=lambda item: rank.get(item.partition(":")[0], 1))
        for leftover in leftovers:
            attr, _, name = leftover.partition(":")
            collection = getattr(bpy.data, attr, None)
            if collection is None:
                continue
            block = collection.get(name)
            if block is None or not str(getattr(block, "name", "")).startswith(self._token):
                continue
            try:
                block.use_fake_user = False
            except Exception:
                pass
            try:
                if attr == "objects":
                    collection.remove(block, do_unlink=True)
                else:
                    collection.remove(block)
            except Exception:
                continue

    def holding_leftovers(self):
        leftovers = []
        prefix = self._token
        for attr in (
            "objects", "meshes", "curves", "metaballs", "lattices", "armatures",
            "grease_pencils", "node_groups", "collections", "materials", "lights",
            "cameras", "speakers", "volumes", "hair_curves", "pointclouds",
        ):
            collection = getattr(bpy.data, attr, None)
            if collection is None:
                continue
            leftovers.extend(
                "%s:%s" % (attr, item.name)
                for item in collection
                if str(getattr(item, "name", "")).startswith(prefix)
            )
        return leftovers

    def verify(self):
        """Structured check that every captured resource and driver is live."""
        errors = list(self._restore_errors)
        restored = self._restored_objects()
        for item in self._objects:
            live = bpy.data.objects.get(item["name"])
            if live is None:
                errors.append("missing object %s" % item["name"])
                continue
            errors.extend(constraint_snapshot.verify(live, item["constraints"], restored))
            for modifier in item.get("modifiers") or ():
                found = next(
                    (entry for entry in live.modifiers if entry.name == modifier["name"]),
                    None,
                )
                if found is None:
                    errors.append(
                        "missing modifier %s on %s" % (modifier["name"], item["name"])
                    )
                    continue
                expected_group = modifier.get("node_group") or ""
                if expected_group:
                    group = getattr(found, "node_group", None)
                    if group is None:
                        errors.append(
                            "missing node group on modifier %s" % modifier["name"]
                        )
                    elif group.name != expected_group:
                        errors.append(
                            "modifier %s node group is %s, expected %s"
                            % (modifier["name"], group.name, expected_group)
                        )
                expected_ident = modifier.get("attachment") or {}
                if expected_ident.get("resource_id"):
                    actual_ident = generated_attachments.identity(live, found)
                    if actual_ident.get("resource_id") != expected_ident.get("resource_id"):
                        errors.append(
                            "modifier %s identity mismatch" % modifier["name"]
                        )
                expected_resource = (modifier.get("id_properties") or {}).get(
                    "__espresso_resource_id"
                )
                if expected_resource:
                    from ...generated.core import registry as generated_registry
                    stamped = generated_registry.resource_identity(found)
                    if stamped.get("resource_id") != expected_resource:
                        errors.append(
                            "modifier %s resource_id mismatch" % modifier["name"]
                        )
            expected_order = tuple(
                modifier.get("name") for modifier in item.get("modifiers") or ()
            )
            live_order = tuple(modifier.name for modifier in live.modifiers)
            if live_order != expected_order:
                errors.append(
                    "modifier order mismatch on %s: %r != %r"
                    % (item["name"], live_order, expected_order)
                )
            _verify_drivers(live, item.get("drivers"), item["name"], errors)
            errors.extend(_transform_errors(live, item["transform"], item["name"]))
            expected_action = item.get("action_state") or {}
            actual_action = getattr(getattr(live, "animation_data", None), "action", None)
            if bool(expected_action.get("action_key")) != bool(actual_action):
                errors.append("%s object Action mismatch" % item["name"])
            expected_data_action = item.get("data_action_state") or {}
            actual_data_action = getattr(
                getattr(getattr(live, "data", None), "animation_data", None),
                "action", None,
            )
            if bool(expected_data_action.get("action_key")) != bool(actual_data_action):
                errors.append("%s data Action mismatch" % item["name"])
            expected = self._records.get(_host_key(live)) or self._records.get(item["name"])
            if expected:
                live_codes = {record.get("code") for record in applied_motion.read(live)}
                for record in expected:
                    code = record.get("code")
                    if code and code not in live_codes:
                        errors.append("missing record %s on %s" % (code, item["name"]))
        for item in self._ids:
            live = target_memory.find_id_block(item["id_type"], item["name"])
            if live is None:
                errors.append("missing %s %s" % (item["id_type"], item["name"]))
                continue
            _verify_drivers(
                live, item.get("drivers"),
                "%s %s" % (item["id_type"], item["name"]), errors,
            )
            expected_action = item.get("action_state") or {}
            actual_action = getattr(getattr(live, "animation_data", None), "action", None)
            if bool(expected_action.get("action_key")) != bool(actual_action):
                errors.append("%s %s Action mismatch" % (item["id_type"], item["name"]))
            expected = self._records.get(item["id_key"])
            if expected:
                live_codes = {record.get("code") for record in applied_motion.read(live)}
                for record in expected:
                    code = record.get("code")
                    if code and code not in live_codes:
                        errors.append(
                            "missing record %s on %s %s"
                            % (code, item["id_type"], item["name"])
                        )
        for item in self._node_groups:
            if bpy.data.node_groups.get(item["original_name"]) is None:
                errors.append("missing node group %s" % item["original_name"])
        for item in self._collections:
            collection = bpy.data.collections.get(item["original_name"])
            if collection is None:
                errors.append("missing collection %s" % item["original_name"])
                continue
            state = item.get("collection_state")
            if state is not None:
                checks = {
                    "objects": (set(obj.name for obj in collection.objects), set(item["source_names"])),
                    "parents": ({parent.name for parent in bpy.data.collections
                                 if not parent.name.startswith(self._token)
                                 and collection.name in parent.children}, set(state["parents"])),
                    "scenes": ({scene.name for scene in bpy.data.scenes
                                if collection.name in scene.collection.children}, set(state["scenes"])),
                    "children": ({child.name for child in collection.children}, set(state["children"])),
                    "properties": (_id_property_map(collection), state["properties"]),
                    "instance_offset": (tuple(collection.instance_offset), state["instance_offset"]),
                }
                checks.update({name: (getattr(collection, name), value)
                               for name, value in state["values"].items()})
                for name, (actual, expected) in checks.items():
                    if actual != expected:
                        errors.append("collection %s %s mismatch" % (collection.name, name))
        for state in self._source_state:
            source = bpy.data.objects.get(state["name"])
            if source is None:
                errors.append("missing source %s" % state["name"])
                continue
            if bool(source.hide_render) != bool(state.get("hide_render", False)):
                errors.append("source %s hide_render mismatch" % state["name"])
            if bool(source.hide_get()) != bool(state.get("hide_viewport", False)):
                errors.append("source %s hide_viewport mismatch" % state["name"])
        if self._discarded:
            leftovers = self.holding_leftovers()
            if leftovers:
                errors.append("holding leftovers: %s" % leftovers)
        return {"ok": not errors, "errors": errors}


def capture(hosts_and_records, extra_objects=(), extra_fcurve_owners=()):
    """Snapshot generated hosts, owned helpers, and related driver owners."""
    snapshot = GeneratedClearSnapshot()
    try:
        for host, record in hosts_and_records:
            snapshot.capture_record(host, record)
            if _id_alive(host):
                key = _host_key(host)
                if key not in snapshot._records:
                    snapshot._records[key] = copy.deepcopy(applied_motion.read(host))
        for obj in extra_objects:
            snapshot.capture_object(obj)
        for owner in extra_fcurve_owners:
            snapshot.capture_record(owner)
    except Exception:
        snapshot.discard()
        raise
    return snapshot

