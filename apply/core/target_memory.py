"""Persistent last-target memory for Driver Espresso apply flows."""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import bpy

from ...engine import utils
from ..motion import applied_motion


MEMORY_MODE_ITEMS = (
    ("SINGLE", "Single Last Target", "Remember one newest successful apply target", 0),
    ("SEPARATE", "Separate by Apply Mode", "Keep separate newest targets for template apply and copied-driver apply", 1),
    ("HISTORY", "Recent History", "Keep a short newest-first list of successful apply targets", 2),
)


def memory_enabled(context):
    prefs = utils.addon_preferences(context)
    return True if prefs is None else bool(getattr(prefs, "remember_apply_targets", True))


def memory_mode(context):
    prefs = utils.addon_preferences(context)
    return "SINGLE" if prefs is None else getattr(prefs, "apply_target_memory_mode", "SINGLE")


def history_size(context):
    prefs = utils.addon_preferences(context)
    return 5 if prefs is None else int(getattr(prefs, "apply_target_history_size", 5))


def read_store(props):
    try:
        data = json.loads(props.apply_target_memory or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_store(props, data):
    props.apply_target_memory = json.dumps(data, sort_keys=True)


def _embedded_node_tree_parent(tree):
    """Return the owning ID and path for embedded shader/compositor trees."""
    if tree is None:
        return None, ""
    for collection_name in ("materials", "worlds", "scenes", "lights"):
        for candidate in getattr(bpy.data, collection_name, []):
            if getattr(candidate, "node_tree", None) is tree:
                return candidate, "node_tree"
    return None, ""


def host_is_alive(host):
    """Whether a stamped host can still be mutated.

    Embedded material, world, and light node trees are not in
    ``bpy.data.node_groups``. Treating a missing node-groups lookup as 'dead'
    made Clear Applied Motion skip Stack Fill / Position Wave and report a
    successful zero-item clear.
    """
    if host is None:
        return False
    try:
        name = host.name
    except ReferenceError:
        return False
    parent, path = _embedded_node_tree_parent(host)
    if parent is not None:
        return getattr(parent, path, None) is host
    collection = datablock_collection_name(host)
    if not collection:
        return True
    try:
        found = getattr(bpy.data, collection).get(name)
    except (ReferenceError, AttributeError, TypeError):
        return False
    return found is host


def _target_root(owner):
    # Walk up id_data until we reach a self-referential root ID block.
    # A single hop fails for embedded structs like shader node sockets whose
    # id_data is the NodeTree (not in bpy.data.node_groups), not the Material.
    current = getattr(owner, "id_data", None)
    if current is None:
        return owner
    while True:
        parent = getattr(current, "id_data", None)
        if parent is current:
            embedded_parent, _path = _embedded_node_tree_parent(current)
            return embedded_parent or current
        if parent is None:
            return current
        current = parent


def _owner_path(owner):
    # Chain path_from_id() segments up through id_data until the root.
    # For a NodeSocket in a material's shader tree this produces
    # "node_tree.nodes[\"Emission\"].inputs[0]" (from Material) rather than
    # just "nodes[\"Emission\"].inputs[0]" (from NodeTree) which can't be
    # resolved on the Material root block.
    try:
        segments = []
        current = owner
        while True:
            parent = getattr(current, "id_data", None)
            if parent is current:
                _embedded_parent, embedded_path = _embedded_node_tree_parent(current)
                if embedded_path:
                    segments.append(embedded_path)
                break
            if parent is None:
                break
            seg = current.path_from_id()
            if seg:
                segments.append(seg)
            current = parent
        segments.reverse()
        return ".".join(segments)
    except Exception:
        return ""


def serialize_owner(owner):
    """Serialize the stable portion shared by every target on one RNA owner."""
    root = _target_root(owner)
    if root is None:
        return {}
    return {
        "id_type": root.__class__.__name__, "id_name": getattr(root, "name", ""),
        "owner_path": _owner_path(owner),
    }


def serialize_target(owner, data_path, index=-1, owner_record=None):
    """Serialize one drivable owner/property without retaining RNA pointers.

    ``owner_record`` lets callers enumerating many F-curves on the same owner
    reuse the expensive root/path lookup. The returned record is still a fresh
    dictionary, so a caller can safely add display metadata without mutating
    the cached owner data.
    """
    item = dict(owner_record) if owner_record is not None else serialize_owner(owner)
    if not item:
        return {}
    item.update({"data_path": data_path, "index": int(index)})
    return item


def _target_fingerprint(target):
    return (
        target.get("id_type", ""),
        target.get("id_name", ""),
        target.get("owner_path", ""),
        target.get("data_path", ""),
        int(target.get("index", -1)),
    )


def _entry_fingerprint(entry):
    return tuple(_target_fingerprint(target) for target in entry.get("targets", []))


def _fallback_display_label(targets, display_label):
    if display_label:
        return display_label
    if not targets:
        return "Unknown target"
    first = targets[0]
    label = first.get("data_path", "Property")
    index = int(first.get("index", -1))
    if index >= 0:
        label += f"[{index}]"
    if len(targets) > 1:
        label += f" (+{len(targets) - 1})"
    return label


def _current_frame():
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    return int(getattr(scene, "frame_current", getattr(scene, "frame_start", 1)))


def json_safe_values(values):
    """Return JSON-safe friendly parameter values without RNA references."""
    safe = {}
    for key, value in dict(values or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
        elif isinstance(value, (list, tuple)):
            safe[str(key)] = list(value)
        else:
            try:
                safe[str(key)] = list(value)
            except TypeError:
                continue
    return safe


def _current_template_values(context, template_id):
    if not template_id:
        return {}
    try:
        from ...ui import props as espresso_props
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        if template and template.get("id") == template_id:
            return espresso_props.collect_values(props, template)
    except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError):
        pass
    return {}


def build_entry(targets, display_label, apply_kind, template_id="", template_name="", target_states=None, template_values=None):
    serialized_targets = []
    target_states = list(target_states or [])
    for index, target in enumerate(targets):
        owner = getattr(target, "owner", None)
        root = _target_root(owner)
        if root is None:
            continue
        item = serialize_target(
            owner, getattr(target, "data_path", ""), getattr(target, "index", -1),
        )
        if index < len(target_states) and target_states[index]:
            item["rest_state"] = dict(target_states[index])
        serialized_targets.append(item)

    if not serialized_targets:
        return {}

    entry = {
        "display_label": _fallback_display_label(serialized_targets, display_label),
        "apply_kind": apply_kind,
        # The frame the artist was standing on when they applied. Re-applying
        # has to sample there, not wherever the playhead has since wandered, or
        # the same click gives a different answer depending on the timeline.
        "applied_frame": _current_frame(),
        "template_id": template_id,
        "template_name": template_name,
        "timestamp": time.time(),
        "targets": serialized_targets,
    }
    safe_values = json_safe_values(template_values)
    if safe_values:
        entry["parameter_values"] = safe_values
    return entry


def remember_entry(context, entry):
    if not entry or not memory_enabled(context):
        return

    props = context.scene.espresso_props
    store = read_store(props)
    mode = memory_mode(context)
    store["latest"] = entry
    template_id = entry.get("template_id", "")
    if template_id:
        recent_templates = store.setdefault("recent_templates", {})
        recent_templates[template_id] = entry
        if len(recent_templates) > 32:
            oldest = sorted(
                recent_templates,
                key=lambda key: recent_templates[key].get("timestamp", 0),
            )[:-32]
            for key in oldest:
                recent_templates.pop(key, None)

    apply_kind = entry.get("apply_kind", "")
    if mode == "SEPARATE" and apply_kind:
        slots = store.setdefault("slots", {})
        slots[apply_kind] = entry

    if mode == "HISTORY":
        history = store.setdefault("history", [])
        entry_key = _entry_fingerprint(entry)
        history = [item for item in history if _entry_fingerprint(item) != entry_key]
        history.insert(0, entry)
        store["history"] = history[: max(1, history_size(context))]
    else:
        store["history"] = [entry]

    if mode != "SEPARATE":
        store.pop("slots", None)
    write_store(props, store)


def remember_live_entry(context, entry):
    """Persist a successful Live edit on its exact existing effect records.

    History alone is insufficient: the effect picker reads the host's embedded
    record, including after save/reopen or appending into a different scene.
    Never claim paths or recreate missing records while updating friendly values.
    """
    def identity(target):
        return tuple(target.get(key, '') for key in (
            'id_type', 'id_name', 'library', 'owner_path', 'data_path'
        )) + (int(target.get('index', -1)),)

    targets = entry.get('targets') or []
    keys = {identity(target) for target in targets}
    hosts = {}
    for target in targets:
        resolved, _reason = resolve_target_record(target)
        if resolved is not None:
            host = resolved['owner']
            hosts.setdefault(id(host), (host, set()))[1].add(
                (resolved['data_path'], int(resolved['index'])))
    for host, paths in hosts.values():
        records = applied_motion.read(host)
        changed = False
        for record in records:
            extras = record.get('extras') or {}
            stored = extras.get('target_entry') or {}
            if (extras.get('template_id') != entry.get('template_id')
                    or not keys
                    or {identity(t) for t in stored.get('targets', [])} != keys
                    or {(p, int(i)) for p, i in record.get('paths', [])} != paths):
                continue
            extras['target_entry'] = copy.deepcopy(entry)
            extras['parameter_values'] = json_safe_values(entry.get('parameter_values', {}))
            record['extras'] = extras
            changed = True
        if changed:
            applied_motion.write(host, records)
    remember_entry(context, entry)


def remember_targets(context, targets, display_label, apply_kind, template_id="", template_name="", target_states=None, template_values=None):
    if template_values is None:
        template_values = _current_template_values(context, template_id)
    entry = build_entry(
        targets, display_label, apply_kind, template_id, template_name,
        target_states=target_states, template_values=template_values,
    )
    if entry and template_id:
        props = getattr(getattr(context, "scene", None), "espresso_props", None)
        if props is not None and getattr(props, "last_template_id", "") == template_id:
            entry["application_mode"] = getattr(props, "application_mode", "SINGLE")
        try:
            from ...catalogue import templates
            from . import application_plan, source_binding

            template = templates.TEMPLATE_BY_ID.get(template_id) or {}
            channels = tuple(template.get("channels") or ())
            if len(channels) == len(entry.get("targets", ())):
                for record, channel in zip(entry["targets"], channels):
                    record["channel_id"] = str(
                        channel.get("id") or channel.get("channel_id") or ""
                    )
            source = source_binding.latest_source(props) if props is not None else None
            plan = application_plan.from_entry({
                **entry,
                "source_entry": source or {},
                "source_required": source_binding.needs_input_source(template),
            }, template)
            entry.update(application_plan.entry_metadata(plan))
        except (AttributeError, KeyError, TypeError, ValueError):
            # A successfully applied legacy/custom route still remains usable;
            # it will be inferred read-only when the plan is next inspected.
            pass
    # Persistent effect metadata enriches the real-driver Active manager.  The
    # F-curve remains the authority on whether a row exists; this record only
    # preserves the friendly template identity and editable values.
    if entry and template_id:
        try:
            from ...catalogue import templates
            from ..motion import applied_motion

            template = templates.TEMPLATE_BY_ID.get(template_id)
            code = (template or {}).get("effect_id", "")
            by_host = {}
            for target_record in entry.get("targets", []):
                resolved, _reason = resolve_target_record(target_record)
                if resolved is None:
                    continue
                host = resolved.get("owner")
                if host is None:
                    continue
                by_host.setdefault(id(host), (host, []))[1].append(
                    [resolved["data_path"], int(resolved["index"])]
                )
            extras = {
                "template_id": template_id,
                "parameter_values": json_safe_values(template_values),
                "target_entry": entry,
            }
            for host, paths in by_host.values():
                applied_motion.remember(host, code, template_name, paths, extras=extras)
        except Exception:
            # Bookkeeping must never invalidate a successfully assigned driver.
            pass
    remember_entry(context, entry)



def forget_latest(props):
    """Drop the remembered apply, leaving every driver in place.

    Separate from remove_last_target, which DELETES the drivers. This only
    forgets, which is what an artist needs when a stale multi-channel entry is
    blocking a single-channel template.
    """
    store = read_store(props)
    store.pop("latest", None)
    store["history"] = []
    write_store(props, store)



def bone_name_from_path(data_path):
    """The bone a driver path belongs to, or "" for an object-level one."""
    marker = 'pose.bones["'
    start = str(data_path or "").find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = data_path.find('"]', start)
    return data_path[start:end] if end > start else ""


def destination_count(entry):
    """How many places this apply landed, as the ARTIST counts them.

    Not the number of channels. A blink on one eyelid is four quaternion
    drivers, and calling that "4 targets" reads as four bones. What the artist
    picked was one bone, so that is what gets counted - distinct object-and-bone
    destinations, however many channels each carries.
    """
    seen = set()
    for target in (entry or {}).get("targets") or []:
        if not isinstance(target, dict):
            continue
        seen.add((target.get("id_name", ""), bone_name_from_path(target.get("data_path", ""))))
    return len(seen)


def latest_entry(props):
    store = read_store(props)
    latest = store.get("latest")
    return latest if isinstance(latest, dict) and latest.get("targets") else None


def recent_template_entry(props, template_id):
    store = read_store(props)
    entry = (store.get("recent_templates") or {}).get(template_id)
    return entry if isinstance(entry, dict) and entry.get("targets") else None


def _candidate_entries(props):
    """Newest unique remembered entries across every configured memory mode."""
    store = read_store(props) if props is not None else {}
    candidates = []
    candidates.extend(store.get("history") or ())
    candidates.extend((store.get("recent_templates") or {}).values())
    candidates.extend((store.get("slots") or {}).values())
    if isinstance(store.get("latest"), dict):
        candidates.append(store["latest"])
    unique = {}
    for entry in candidates:
        if not isinstance(entry, dict) or not entry.get("targets"):
            continue
        key = (entry.get("template_id", ""), _entry_fingerprint(entry))
        current = unique.get(key)
        if current is None or entry.get("timestamp", 0) > current.get("timestamp", 0):
            unique[key] = entry
    return sorted(unique.values(), key=lambda item: item.get("timestamp", 0), reverse=True)


def entry_for_active_object(props, active_object, template_id=""):
    """Newest remembered apply whose target is owned by the active object."""
    if props is None or active_object is None:
        return None
    host_keys = {
        (host.__class__.__name__, getattr(host, "name", ""))
        for host in applied_motion.hosts_for_object(active_object)
    }
    for entry in _candidate_entries(props):
        if template_id and entry.get("template_id") != template_id:
            continue
        if any(
            (target.get("id_type", ""), target.get("id_name", "")) in host_keys
            for target in entry.get("targets") or ()
        ):
            return entry
    return None


def clear_latest_entry(props):
    """Remove the latest remembered target — called when it can no longer be resolved."""
    store = read_store(props)
    store.pop("latest", None)
    write_store(props, store)


def latest_label(props):
    entry = latest_entry(props)
    if not entry:
        return ""
    return entry.get("display_label", "")


def _resolved_display_label(entry):
    """Build a fresh human label from the remembered target itself.

    Stored display captions are useful as historical context, but they can go
    stale when a generic apply route remembered a friendly batch caption like
    "1 lights > Constant Speed". The target list below the tooltip already
    resolves the real remembered target; the tooltip should describe that same
    destination instead of trusting the old caption.
    """
    targets = list((entry or {}).get("targets") or ())
    if not targets:
        return entry.get("display_label", "Unknown target") if isinstance(entry, dict) else "Unknown target"

    resolved, _reason = resolve_target_record(targets[0])
    if resolved is None:
        return entry.get("display_label", "Unknown target")

    owner = resolved.get("owner")
    root = _target_root(owner)
    owner_name = getattr(root, "name", "") or targets[0].get("id_name", "Unknown")
    path = resolved.get("data_path", "") or targets[0].get("data_path", "Property")
    index = int(resolved.get("index", targets[0].get("index", -1)))
    if index >= 0:
        path += f"[{index}]"
    label = f"{owner_name} > {path}"
    if len(targets) > 1:
        label += f" (+{len(targets) - 1})"
    return label


def latest_summary(props):
    entry = latest_entry(props)
    if not entry:
        return "No remembered target yet."
    mode = entry.get("apply_kind", "template")
    source = entry.get("template_name", "")
    if mode == "copied":
        prefix = "Copied driver"
    elif mode == "graph":
        prefix = "Graph driver"
    else:
        prefix = "Template apply"
    if source:
        return f"{prefix}: {source}"
    return prefix


def latest_info_lines(props):
    entry = latest_entry(props)
    if not entry:
        return ["No remembered target yet."]

    mode = entry.get("apply_kind", "template")
    mode_label = {
        "template": "Current template apply",
        "copied": "Copied driver apply",
        "graph": "Graph Editor apply",
    }.get(mode, mode.title())

    lines = [
        f"Remembered target: {_resolved_display_label(entry)}",
        f"Source: {latest_summary(props)}",
        f"Memory mode: {mode_label}",
    ]

    targets = entry.get("targets", [])
    if targets:
        first = targets[0]
        path = first.get("data_path", "")
        index = int(first.get("index", -1))
        if index >= 0:
            path += f"[{index}]"
        lines.append(f"Property path: {path}")
        if len(targets) > 1:
            lines.append(f"Applies to {len(targets)} remembered target channels.")
    return lines


# Fast path: map the most common ID type names straight to their bpy.data
# collection so we do not scan every attribute of bpy.data on each lookup.
_ID_TYPE_TO_COLLECTION = {
    "Object": "objects",
    "Mesh": "meshes",
    "Material": "materials",
    "Light": "lights",
    "Camera": "cameras",
    "Scene": "scenes",
    "Curve": "curves",
    "Armature": "armatures",
    "Collection": "collections",
    "World": "worlds",
    "Image": "images",
    "NodeTree": "node_groups",
    "ShaderNodeTree": "node_groups",
    "GeometryNodeTree": "node_groups",
    "Key": "shape_keys",
    "Texture": "textures",
    "ParticleSettings": "particles",
    "Speaker": "speakers",
    "GreasePencil": "grease_pencils",
    "Lattice": "lattices",
    "TextCurve": "curves",
    "SurfaceCurve": "curves",
    "MetaBall": "metaballs",
    "Volume": "volumes",
    "LightProbe": "lightprobes",
    "HairCurves": "hair_curves",
    "Curves": "hair_curves",
    "PointCloud": "pointclouds",
}


def library_filepath(id_block):
    """Stable library identity for a datablock, or empty for a local ID."""
    if id_block is None:
        return ""
    library = getattr(id_block, "library", None)
    if library is None:
        return ""
    filepath = str(getattr(library, "filepath", "") or "")
    if filepath:
        try:
            return str(bpy.path.abspath(filepath) or filepath)
        except Exception:
            return filepath
    return str(getattr(library, "name", "") or "")


def find_id_block(id_type, id_name, library=""):
    """Resolve a datablock from its RNA type name, datablock name, and library."""
    return _find_id_block(id_type, id_name, library=library)


def datablock_collection_name(block):
    """Return the bpy.data collection attribute that holds this ID."""
    if block is None:
        return ""
    mapped = _ID_TYPE_TO_COLLECTION.get(block.__class__.__name__, "")
    if mapped:
        return mapped
    identifier = getattr(getattr(block, "bl_rna", None), "identifier", "") or ""
    if "NodeTree" in identifier:
        return "node_groups"
    mapped = _ID_TYPE_TO_COLLECTION.get(identifier, "")
    if mapped:
        return mapped
    name = getattr(block, "name", "")
    if not name:
        return ""
    for attr in dir(bpy.data):
        collection = getattr(bpy.data, attr, None)
        getter = getattr(collection, "get", None)
        if getter is None:
            continue
        try:
            found = getter(name)
        except Exception:
            continue
        if found is block:
            return attr
    return ""


def _find_id_block(id_type, id_name, library=""):
    if not id_name:
        return None
    library = "" if library is None else str(library)
    matches = []
    collections = []
    attr = _ID_TYPE_TO_COLLECTION.get(id_type)
    if attr:
        collections.append(getattr(bpy.data, attr, None))
    else:
        for name in dir(bpy.data):
            collection = getattr(bpy.data, name, None)
            if getattr(collection, "get", None) is not None:
                collections.append(collection)
    seen = set()
    for collection in collections:
        if collection is None:
            continue
        marker = id(collection)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            items = list(collection)
        except Exception:
            continue
        for item in items:
            if getattr(item, "name", None) != id_name:
                continue
            if item.__class__.__name__ != id_type:
                continue
            matches.append(item)
    for item in matches:
        if library_filepath(item) == library:
            return item
    if library:
        return None
    return matches[0] if matches else None


def resolve_target_record(target):
    """Resolve one serialized target record to its live RNA owner."""
    id_block = _find_id_block(
        target.get("id_type", ""),
        target.get("id_name", ""),
        library=target.get("library", ""),
    )
    if id_block is None:
        return None, "Target no longer exists or is not drivable."
    owner_path = target.get("owner_path", "")
    try:
        owner = id_block.path_resolve(owner_path) if owner_path else id_block
    except Exception:
        return None, "Target no longer exists or is not drivable."
    if owner is None or not target.get("data_path"):
        return None, "Target no longer exists or is not drivable."
    return {
        "owner": owner, "data_path": target["data_path"],
        "index": int(target.get("index", -1)),
        "rest_state": dict(target.get("rest_state") or {}),
    }, ""


def resolve_entry(entry):
    if not entry:
        return None, "No remembered target yet."

    resolved = []
    for target in entry.get("targets", []):
        item, _reason = resolve_target_record(target)
        if item is None:
            return None, "Last target no longer exists or is not drivable."
        resolved.append(item)

    if not resolved:
        return None, "Last target no longer exists or is not drivable."
    return resolved, ""


def canonical_driver_channel(target):
    """Locate the actual ID-owned F-Curve for a remembered RNA target."""
    owner = target["owner"]
    path = target["data_path"]
    index = int(target["index"])
    driver_owner = getattr(owner, "id_data", None) or owner
    if driver_owner is not owner:
        try:
            path = owner.path_from_id(path)
        except (AttributeError, TypeError, ValueError):
            return None, "The remembered target's driver path is unavailable."
    animation = getattr(driver_owner, "animation_data", None)
    drivers = getattr(animation, "drivers", ()) if animation else ()
    if index < 0:
        try:
            property_length = len(driver_owner.path_resolve(path))
        except (AttributeError, KeyError, TypeError, ValueError):
            property_length = 0
        if property_length > 1:
            return None, (
                "The remembered whole-property target is an array. "
                "Reapply a specific component before updating."
            )
        matches = [curve for curve in drivers if curve.data_path == path]
        if len(matches) != 1:
            return None, (
                "The remembered whole-property target does not identify exactly "
                "one live driver channel. Reapply a specific component instead."
            )
        index = int(matches[0].array_index)
    return {"owner": driver_owner, "data_path": path, "index": index}, ""


def capture_cleanup_for_fcurves(fcurves, props=None):
    """Inventory owned helpers before public F-Curves disappear."""
    from ..setups import internal_helpers

    descriptors = []
    helper_resources = []
    bindings = {}
    if props is not None:
        from ...ui import live_controls

        bindings = live_controls.read_bindings(props)
    for fcurve in fcurves or ():
        descriptor = serialize_target(
            getattr(fcurve, "id_data", None), fcurve.data_path, fcurve.array_index,
        )
        if descriptor:
            descriptors.append(descriptor)
        helper_resources.extend(internal_helpers.resources_from_driver(fcurve.driver))
        if props is not None and descriptor:
            from ...ui import live_controls

            binding = bindings.get(live_controls._target_key(descriptor))
            if binding:
                helper_resources.extend(internal_helpers.resources_from_snapshot(
                    binding.get("original"), _find_id_block,
                ))
    return {"targets": descriptors, "helpers": helper_resources}


def capture_cleanup_for_entry(entry, props=None):
    from . import driver_targets

    fcurves = [driver_targets.resolve_driver(item) for item in (entry or {}).get("targets", [])]
    return capture_cleanup_for_fcurves(
        [item for item in fcurves if item is not None], props,
    )


def capture_cleanup_for_targets(targets, props=None):
    """Inventory resources for ordinary owner/path/index target objects."""
    fcurves = []
    for target in targets or ():
        owner = getattr(target, "owner", None)
        data_path = getattr(target, "data_path", "")
        index = int(getattr(target, "index", -1))
        animation = getattr(owner, "animation_data", None)
        for fcurve in getattr(animation, "drivers", None) or ():
            if fcurve.data_path == data_path and (
                index < 0 or fcurve.array_index == index
            ):
                fcurves.append(fcurve)
                break
    return capture_cleanup_for_fcurves(fcurves, props)


def entry_overlaps_targets(entry, targets):
    """Whether remembered live state refers to any explicitly removed target."""
    remembered = {
        _target_fingerprint(target)
        for target in (entry or {}).get("targets", [])
    }
    return bool(remembered.intersection(
        _target_fingerprint(target) for target in targets or () if target
    ))


def cleanup_captured_resources(captured, scene=None, props=None):
    from ..setups import internal_helpers

    helper_count = internal_helpers.cleanup((captured or {}).get("helpers", []))
    controller_count = 0
    if scene is not None and props is not None:
        from ...ui import live_controls

        controller_count = live_controls.discard_bindings_for_targets(
            scene, props, (captured or {}).get("targets", []),
        )
    return helper_count, controller_count


def cleanup_entry_node_groups(entry):
    """Remove generated Espresso groups connected to remembered node targets."""
    from ..spatial import shared_material_sweep

    removed = 0
    for target in (entry or {}).get("targets", []):
        resolved, _reason = resolve_target_record(target)
        if not resolved:
            continue
        owner = resolved["owner"]
        data_path = resolved["data_path"]
        if shared_material_sweep.remove_group_for_driver_path(owner, data_path):
            removed += 1
        elif shared_material_sweep.remove_group_feeding_target_path(
            owner, data_path,
        ):
            removed += 1
    return removed


def apply_expression_to_entry(
    entry, expression, template, source_entry=None, scene=None,
    rest_start_mode=None, output_baseline=None, validation_template=None,
    prepare_driver=None,
):
    original_target_records = copy.deepcopy((entry or {}).get("targets", []))
    if (entry or {}).get("apply_kind") == "node_route":
        return False, (
            "Apply with Shared Material again to rebuild this Espresso node setup."
        )
    resolved, reason = resolve_entry(entry)
    if not resolved:
        return False, reason
    canonical_targets = []
    for target in resolved:
        channel, reason = canonical_driver_channel(target)
        if channel is None:
            return False, reason
        canonical_targets.append(channel)
    scene = scene or bpy.context.scene
    registered_props = getattr(scene, "espresso_props", None)
    if registered_props is None:
        if len(canonical_targets) != 1:
            return False, "Register Driver Espresso before updating multiple targets."
        from ...ui.state import live_controls

        channel = canonical_targets[0]
        curve = _find_owner_driver(
            channel["owner"], channel["data_path"], channel["index"],
        )
        controller_names = {
            live_controls.CONTROLLER_VARIABLE,
            live_controls.BASE_VARIABLE,
        }
        has_controller_marker = bool(
            curve and any(
                variable.name in controller_names
                for variable in curve.driver.variables
            )
        )
        has_carrier = any(
            str(key).startswith("__espresso_carrier_") for key in scene.keys()
        )
        if has_controller_marker or has_carrier:
            return False, (
                "Register Driver Espresso before updating a possible "
                "Controller-bound target."
            )
    expressions = list(expression) if isinstance(expression, (list, tuple)) else [expression] * len(resolved)
    baselines = (
        list(output_baseline) if isinstance(output_baseline, (list, tuple))
        else [output_baseline] * len(resolved)
    )
    if len(expressions) != len(resolved) or len(baselines) != len(resolved):
        return False, "The rebuilt motion channels no longer match the applied targets."
    pose_driven = any(
        (target.get("rest_state") or {}).get("pose_delta") is not None
        for target in resolved
    )
    from . import application_plan

    scalar_broadcast = (
        application_plan.infer_scope(entry) == application_plan.BROADCAST
        and not template.get("channels")
    )
    if (
        len(resolved) > 1 and not template.get("channels")
        and not pose_driven and not scalar_broadcast
    ):
        label = entry.get("display_label", "the remembered target")
        return False, (
            f"{label} was applied across several channels, so a single-value "
            "template cannot go to the same place. Press Forget Last Target "
            "(the X beside \"Last applied\") to stop remembering it, then apply "
            "again. Its drivers are not affected."
        )

    prepared = []
    for target_index, target in enumerate(resolved):
        owner = target["owner"]
        data_path = target["data_path"]
        index = int(target["index"])
        existing_state = target.get("rest_state")
        channel_expression = expressions[target_index]
        channel_baseline = baselines[target_index]

        # A pose-driven channel stores its own amplitude. Re-applying has to
        # rebuild rest + delta*(envelope); pushing the bare envelope would drive
        # every channel with the same value and flatten the artist's pose into
        # nothing. The bones are at rest by now, so the entry is the only place
        # this amplitude still exists.
        pose_target = bool(existing_state and "pose_delta" in existing_state)
        if pose_target:
            from ..motion import pose_capture

            expression_for_target = pose_capture.expression_from_stored_pose(
                channel_expression,
                existing_state.get("pose_rest", 0.0),
                existing_state.get("pose_delta", 0.0),
            )
        else:
            expression_for_target = channel_expression
        # Reuse the rest_state captured on the original apply rather than
        # resampling the target's current value: it's already driven by then, so
        # recapturing here would treat the driven output as the new rest position
        # and compound further with every Update Last Target click. Only capture
        # fresh when there is no stored state yet, or the rest mode changed.
        if (
            scene is not None and rest_start_mode is not None
            and not pose_target
            and (not existing_state or existing_state.get("mode") != rest_start_mode)
        ):
            from . import apply_behavior
            graph_target = type(
                "RememberedTarget", (),
                {"owner": owner, "data_path": data_path, "index": index},
            )()
            target["rest_state"] = apply_behavior.capture_target_rest_state(
                graph_target, expression_for_target, template, scene, rest_start_mode,
                output_baseline=channel_baseline,
                # Sample where this was applied, not where the playhead is.
                snapshot_frame=entry.get("applied_frame"),
            )
            serialized = entry.get("targets", [])[target_index]
            if target["rest_state"]:
                serialized["rest_state"] = dict(target["rest_state"])
            else:
                serialized.pop("rest_state", None)
        elif (
            scene is not None
            and rest_start_mode == "ADDITIVE"
            and not pose_target
            and existing_state
            and existing_state.get("mode") == rest_start_mode
        ):
            # Keep the original captured value/application frame (the
            # anti-compounding invariant) while rebuilding phase and bounded
            # normalization metadata from the newly generated expression.
            # Those derived values legitimately change when a user edits
            # Minimum, Maximum, Speed, or another kernel parameter.
            target["rest_state"] = utils.capture_rest_start_state(
                expression_for_target,
                template,
                scene,
                rest_start_mode,
                existing_state.get("rest_value", 0.0),
                snapshot_frame=existing_state.get("snapshot_frame"),
                output_baseline=channel_baseline,
                additive_profile=existing_state.get("additive_profile"),
            )
            entry.get("targets", [])[target_index]["rest_state"] = dict(target["rest_state"])
        # A pose channel carries a whole orientation, not an offset. Rest Start
        # re-wraps its four quaternion components independently, which both
        # breaks the unit-length constraint and re-phases the envelope - the
        # measured symptom was a lid that stopped closing at all after one
        # Update Last Target. The stored state pins it OFF; honour that over
        # whatever the panel currently shows.
        wrapped_expression = utils.wrap_expression_with_rest_state(
            expression_for_target,
            template,
            target.get("rest_state"),
        )
        state = target.get('rest_state') or {}
        if (not pose_target and state.get('application_space') == 'RELATIVE'
                and state.get('mode', utils.REST_START_OFF) == utils.REST_START_OFF
                and 'motion_anchor' in state):
            # Mirror Motion Apply's original relative placement; reading the
            # currently driven value would accumulate an offset on each edit.
            anchor = float(state['motion_anchor'])
            if state.get('motion_data_path', data_path) == 'scale':
                if anchor != 1.0:
                    wrapped_expression = f'{utils._format_driver_literal(anchor)}*({wrapped_expression})'
            elif anchor != 0.0:
                wrapped_expression = f'{utils._format_driver_literal(anchor)}+({wrapped_expression})'
        valid, message = utils.validate_driver_expression(
            wrapped_expression, validation_template or template, scene,
        )
        if not valid:
            entry["targets"] = original_target_records
            return False, message
        prepared.append(wrapped_expression)

    # Build every replacement on a private driver first, then commit the whole
    # remembered set with its controller bindings in one transaction.
    from ...ui.state import live_controls
    from . import driver_manager, source_binding

    props = (
        registered_props if registered_props is not None
        else SimpleNamespace(live_control_bindings="{}")
    )
    bindings = live_controls.read_bindings(props)
    required = [var["name"] for var in template.get("requires_driver_variables", [])]
    replacements = []
    for target, wrapped_expression in zip(canonical_targets, prepared):
        owner, data_path, index = (
            target["owner"], target["data_path"], int(target["index"])
        )
        foreign = getattr(
            driver_manager, "foreign_live_motion_for_channel", lambda *_: None,
        )(owner, data_path, index)
        if foreign:
            entry["targets"] = original_target_records
            return False, f"An unavailable edition owns {data_path}[{index}]."
        descriptor = serialize_target(owner, data_path, index)
        binding = bindings.get(live_controls._target_key(descriptor))
        fcurve = _find_owner_driver(owner, data_path, index)
        if _controller_binding_issue(
            fcurve, binding, binding_present=live_controls._target_key(descriptor) in bindings,
        ):
            entry["targets"] = original_target_records
            return False, (
                f"The Controller binding at {data_path}[{index}] is missing or damaged; "
                "the driver was left unchanged."
            )
        base = (
            binding.get("original") if isinstance(binding, dict) else None
        ) or (
            live_controls.snapshot_driver(fcurve.driver) if fcurve else
            {"type": "SCRIPTED", "expression": "0", "use_self": False, "variables": []}
        )

        def configure(driver, expression=wrapped_expression):
            if required:
                existing = {item.name for item in driver.variables}
                missing = [name for name in required if name not in existing]
                if missing or source_entry:
                    ok, message = source_binding.bind_required_variables(
                        driver, template, source_entry,
                    )
                    if not ok:
                        raise ValueError(
                            message or "Add required driver variable(s) first: "
                            + ", ".join(missing)
                        )
            if prepare_driver is not None:
                result = prepare_driver(driver)
                if isinstance(result, tuple) and not result[0]:
                    raise ValueError(result[1])
                if result is False:
                    raise ValueError("Could not prepare the remembered driver route.")
            ok, message = utils.assign_driver_expression(
                driver, expression, validation_template or template, scene,
            )
            if not ok:
                raise ValueError(message)

        try:
            new_base = live_controls.draft_base_snapshot(owner, base, configure)
        except Exception as exc:
            entry["targets"] = original_target_records
            return False, f"Could not update {data_path}[{index}]: {exc}"
        replacements.append({
            "owner": owner, "data_path": data_path, "index": index, "base": new_base,
        })

    ok, message = live_controls.replace_base_snapshots(
        scene, props, replacements, template=template,
    )
    if not ok:
        entry["targets"] = original_target_records
        return False, message
    count = len(replacements)
    message = "Updated last target." if count == 1 else f"Updated {count} target channels."
    return True, message


def _find_owner_driver(owner, data_path, index):
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return None
    if index >= 0:
        return animation.drivers.find(data_path, index=index)
    return animation.drivers.find(data_path)


def _controller_binding_issue(fcurve, binding, *, binding_present=False):
    """Whether a public driver and its Controller record disagree."""
    from ...ui.state import live_controls

    if fcurve is None:
        return binding_present
    names = {variable.name for variable in fcurve.driver.variables}
    controller = live_controls.CONTROLLER_VARIABLE
    base = live_controls.BASE_VARIABLE
    if not binding_present:
        return bool(names & {controller, base})
    if not isinstance(binding, dict) or not binding:
        return True
    if controller not in names:
        return True
    if binding.get("disabled"):
        enabled = binding.get("enabled_driver")
        if not isinstance(enabled, dict):
            return True
        expression = enabled.get("expression")
        variables = enabled.get("variables")
        if not isinstance(expression, str) or not isinstance(variables, list):
            return True
        if any(
            not isinstance(variable, dict)
            or not isinstance(variable.get("name"), str)
            for variable in variables
        ):
            return True
        return controller not in expression or not any(
            variable["name"] == controller for variable in variables
        )
    return controller not in fcurve.driver.expression


def _snapshot_resolved_drivers(resolved):
    from ...ui.state import live_controls

    snapshots = []
    for target in resolved:
        owner = target["owner"]
        data_path = target["data_path"]
        index = int(target["index"])
        curve = _find_owner_driver(owner, data_path, index)
        snapshots.append({
            "owner": owner,
            "data_path": data_path,
            "index": index,
            "existed": curve is not None,
            "snapshot": (
                live_controls.snapshot_driver(curve.driver)
                if curve is not None else None
            ),
        })
    return snapshots


def _restore_resolved_drivers(snapshots):
    from ...ui.state import live_controls

    for item in snapshots:
        if not item["existed"]:
            continue
        owner = item["owner"]
        data_path = item["data_path"]
        index = item["index"]
        curve = _find_owner_driver(owner, data_path, index)
        if curve is None:
            try:
                curve = (
                    owner.driver_add(data_path, index)
                    if index >= 0 else owner.driver_add(data_path)
                )
            except (RuntimeError, TypeError):
                curve = None
        if curve is not None and item["snapshot"] is not None:
            live_controls.restore_driver(curve.driver, item["snapshot"])


def remove_driver_from_entry(entry, context=None):
    resolved, reason = resolve_entry(entry)
    if not resolved:
        return False, reason

    for target in resolved:
        owner = target["owner"]
        data_path = target["data_path"]
        index = int(target["index"])
        for record in applied_motion.read(owner):
            if applied_motion.resolve_template(record) is not None:
                continue
            if any(path == data_path and (recorded < 0 or index < 0 or recorded == index)
                   for path, recorded in applied_motion.paths_of(record)):
                return False, "Selected motion is not available in this edition; it was left unchanged."

    context = context or bpy.context
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    captured = capture_cleanup_for_entry(entry, props)
    snapshots = _snapshot_resolved_drivers(resolved)
    removed = 0
    removed_groups = 0
    try:
        for item in snapshots:
            if not item["existed"]:
                continue
            owner = item["owner"]
            data_path = item["data_path"]
            index = item["index"]
            if index >= 0:
                owner.driver_remove(data_path, index)
            else:
                owner.driver_remove(data_path)
            removed += 1
    except Exception as exc:
        _restore_resolved_drivers(snapshots)
        return False, "Could not remove a remembered driver: %s" % exc
    # Those drivers are gone, so the stamps describing them are dead. entries()
    # already hides them, but leaving __espresso_applied behind would make a
    # cleared object still look like it carries Driver Espresso motion.
    hosts = {}
    for target in resolved:
        owner = target["owner"]
        host = getattr(owner, "id_data", owner)
        if host is not None:
            hosts.setdefault(id(host), host)
    for host in hosts.values():
        try:
            applied_motion.prune(host)
        except Exception:
            pass

    removed_groups = cleanup_entry_node_groups(entry)
    helper_count, controller_count = cleanup_captured_resources(
        captured, getattr(context, "scene", None), props,
    )

    if removed == 0 and removed_groups == 0 and helper_count == 0 and controller_count == 0:
        return False, "No driver was present on the remembered target."
    if removed_groups:
        group_label = (
            "Espresso node group" if removed_groups == 1
            else f"{removed_groups} Espresso node groups"
        )
        message = f"Removed {group_label} and its connected driver(s)."
    else:
        message = "Removed driver from last target." if removed == 1 else f"Removed {removed} target drivers."
    cleaned = helper_count + controller_count
    if cleaned:
        message += f" Cleaned {cleaned} Espresso helper resource{'' if cleaned == 1 else 's'}."
    return True, message


def convert_motion_entry_to_single(
    entry, target_index, expression, template, *, source_entry=None, scene=None,
    rest_start_mode=None, output_baseline=None, validation_template=None,
    clear_other_channels=True,
):
    """Atomically convert one remembered Motion Set channel to a single recipe.

    Every original driver is snapshotted before the chosen channel is rewritten.
    If removal of any sibling channel fails, all channels are restored exactly.
    """
    resolved, reason = resolve_entry(entry)
    if not resolved:
        return False, reason, None
    if not 0 <= int(target_index) < len(resolved):
        return False, "Choose a valid remembered motion channel.", None

    snapshots = _snapshot_resolved_drivers(resolved)
    converted = copy.deepcopy(entry)
    converted["targets"] = [copy.deepcopy(entry["targets"][int(target_index)])]
    converted["application_scope"] = "SINGLE"
    converted["application_mode"] = "SINGLE"
    converted["template_id"] = str((template or {}).get("id") or "")
    converted["template_name"] = str((template or {}).get("name") or "")
    converted["apply_kind"] = "template"

    ok, message = apply_expression_to_entry(
        converted, expression, template, source_entry=source_entry, scene=scene,
        rest_start_mode=rest_start_mode, output_baseline=output_baseline,
        validation_template=validation_template,
    )
    if not ok:
        _restore_resolved_drivers(snapshots)
        return False, message, None

    if clear_other_channels:
        try:
            for index, target in enumerate(resolved):
                if index == int(target_index):
                    continue
                owner = target["owner"]
                path = target["data_path"]
                array_index = int(target["index"])
                removed = (
                    owner.driver_remove(path, array_index)
                    if array_index >= 0 else owner.driver_remove(path)
                )
                if removed is False:
                    raise RuntimeError("Blender refused to remove a remembered channel")
        except Exception as exc:
            _restore_resolved_drivers(snapshots)
            return False, "Could not convert the Motion Set; every channel was restored: %s" % exc, None
    return True, "Converted the remembered Motion Set to one property.", converted
