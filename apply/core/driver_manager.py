"""Metadata enrichment for the real-driver-backed Active target manager.

Driver discovery remains in :mod:`driver_targets`.  This module only joins
optional Espresso records onto those real rows and never invents a target.
"""

from __future__ import annotations

from collections import OrderedDict

import bpy

from ..motion import applied_motion, applied_motion_manager, stack_records
from . import driver_targets, target_memory


def descriptor_key(value):
    """Stable key for one real driver descriptor within the current file."""
    return "\x1f".join((
        str(value.get("id_type", "")),
        str(value.get("id_name", "")),
        str(value.get("owner_path", "")),
        str(value.get("data_path", "")),
        str(int(value.get("index", -1))),
    ))


BUCKET_STRUCTURES = "bucket:structures"
BUCKET_MOTIONS = "bucket:motions"
_HOST_SCOPED = frozenset({"SCENE", "LAST"})

_list_search_filter = ""


def set_list_search_filter(name):
    """Remember the Applied Effects UIList search box for selection operators."""
    global _list_search_filter
    _list_search_filter = str(name or "")


def list_search_filter():
    return _list_search_filter


def matches_list_search(item, filter_name=None):
    """True when this row would remain listed under the current name filter."""
    text = str(
        list_search_filter() if filter_name is None else filter_name
    ).strip().lower()
    if not text:
        return True
    label = item["label"] if isinstance(item, dict) else getattr(item, "label", "")
    return text in str(label or "").lower()

_ROUTE_COMPACT = {
    "OBJECT_SET_NATIVE": "Driver",
    "PREPARED_LAYOUT_GN": "GN",
    "GENERATED_GRAPHIC_NATIVE": "GN",
    "SHOWCASE_HYBRID": "Hybrid",
    "TRAVEL_REVEAL_HYBRID": "Hybrid",
    "EVENT_TRACK_NATIVE": "Action",
}


def _bucket_key(effect_kind):
    if effect_kind == "STRUCTURAL_EFFECT":
        return BUCKET_STRUCTURES
    return BUCKET_MOTIONS


def _bucket_key_for_group(group):
    metadata = group.get("metadata")
    if not metadata:
        return BUCKET_MOTIONS
    return _bucket_key(metadata.get("effect_kind", "SINGLE_PROPERTY"))


def _category_row_key(bucket_key, category_name):
    return "category:%s:%s" % (bucket_key.split(":", 1)[-1], category_name)


def catalogue_category_for_group(group):
    """Artist-facing catalogue category used for adaptive motion folders."""
    metadata = group.get("metadata")
    if not metadata:
        key = str(group.get("key", ""))
        if key.startswith("category:"):
            return key.split(":", 1)[1]
        return "Other"
    effect_kind = metadata.get("effect_kind", "SINGLE_PROPERTY")
    if effect_kind == "STRUCTURAL_EFFECT":
        return ""
    template_id = str(metadata.get("template_id") or "")
    if not template_id:
        template = metadata.get("template")
        template_id = str((template or {}).get("id") or "")
    if template_id:
        from ...catalogue.core import taxonomy
        entry = taxonomy.TEMPLATE_TAXONOMY.get(template_id)
        if entry:
            return entry[0]
    return "Other"


def compact_route_label(metadata):
    """Compact delivery-route badge for one applied effect."""
    if not metadata:
        return "Driver"
    if metadata.get("child_effect_kind") == "SPATIAL_EFFECTOR":
        return "Effector"
    if metadata.get("child_effect_kind") == "SHAPE_ADAPTATION":
        return "Layout · GN"
    # An authoring rig is neither Geometry Nodes nor a layout. It builds objects, an
    # aim constraint and drivers -- there is no node tree anywhere in it.
    # Every GENERATED_SETUP was badged "GN" and every STRUCTURAL_EFFECT
    # "Layout · GN", so filing the rig as a structure moved it from claiming a
    # node tree to claiming a node tree AND a layout. "GN" means Geometry
    # Nodes; this has none.
    effect_kind = metadata.get("effect_kind", "SINGLE_PROPERTY")
    if effect_kind == "STRUCTURAL_EFFECT":
        return "Layout · GN"
    route_name = str(metadata.get("route") or "")
    if route_name == "Prepared Layout":
        return "Layout · GN"
    template_id = str(metadata.get("template_id") or "")
    if not template_id:
        template = metadata.get("template")
        template_id = str((template or {}).get("id") or "")
    if template_id:
        from ...engine.motion_stack import capabilities
        item = capabilities.get(template_id)
        if item:
            return _ROUTE_COMPACT.get(item.route, "GN")
    if effect_kind == "MOTION_SET":
        return "Driver"
    if effect_kind == "GENERATED_SETUP":
        return "GN"
    return "Driver"


def _plain_host_name(text):
    value = str(text or "").strip()
    for prefix in ("Object:", "OBJ:"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value


def _host_key_and_label(group):
    """Stable object/datablock identity for Last and Scene host folders."""
    metadata = group.get("metadata") or {}
    host = metadata.get("host")
    if host is not None and not isinstance(host, dict):
        try:
            name = str(host.name)
            kind = "Object" if hasattr(host, "type") else host.__class__.__name__
            if name:
                return "host:%s:%s" % (kind, name), name
        except (ReferenceError, AttributeError, TypeError):
            pass
    targets = group.get("targets") or ()
    if targets:
        first = targets[0]
        name = str(first.get("id_name") or "")
        kind = str(first.get("id_type") or "Object")
        if name:
            return "host:%s:%s" % (kind, name), name
    host_label = str(metadata.get("host_label") or "")
    label = _plain_host_name(host_label)
    if not label:
        return "host:label:Unknown", "Unknown"
    if host_label.startswith("Object:") or host_label.startswith("OBJ:"):
        return "host:Object:%s" % label, label
    return "host:label:%s" % label, label


def _child_destination_label(label, host_name):
    """Drop owner identity from a child row once the host folder already names it."""
    text = str(label or "")
    name = str(host_name or "")
    if not text or not name:
        return text
    prefixes = (
        "OBJ: %s > " % name,
        "OBJ:%s > " % name,
        "%s > " % name,
        "Last Applied  |  %s > " % name,
        "Last Applied | %s > " % name,
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _container_row(row_kind, group_key, label, icon, nest_depth=0, visible=True,
                   badge_text="", member_count=0):
    return {
        "row_kind": row_kind, "group_key": group_key,
        "descriptor_key": group_key, "label": label,
        "icon": icon, "category": label,
        "id_type": "", "id_name": "", "owner_path": "",
        "data_path": "", "index": -1,
        "effect_code": "", "template_id": "",
        "editable": False, "record_token": "",
        "effect_kind": row_kind,
        "batch_eligible": False,
        "bakeable": False, "removable": False,
        "visible": visible,
        "badge_text": badge_text, "member_count": member_count,
        "nest_depth": nest_depth,
    }


def _host_row(host_key, label, nest_depth=0, visible=True, badge_text=""):
    return _container_row(
        "HOST", host_key, label, "OBJECT_DATA", nest_depth, visible,
        badge_text=badge_text,
    )


def host_visibility_badge(host):
    """Name viewport/collection state. Never call a hidden host unsupported."""
    if host is None:
        return ""
    try:
        view_layer = getattr(bpy.context, "view_layer", None)
        objects = getattr(view_layer, "objects", None)
        if objects is not None and getattr(host, "name", "") not in objects:
            return "Excluded"
        if bool(host.hide_get()) or bool(getattr(host, "hide_viewport", False)):
            return "Hidden"
    except (ReferenceError, AttributeError, TypeError):
        return "Missing"
    return ""


def _bucket_row(bucket_key, nest_depth=0, visible=True):
    if bucket_key == BUCKET_STRUCTURES or bucket_key.startswith(BUCKET_STRUCTURES + ":"):
        label, icon = "Structures", "MOD_ARRAY"
    else:
        label, icon = "Motions", "DRIVER"
    return _container_row(
        "BUCKET", bucket_key, label, icon, nest_depth, visible,
    )


def _category_row(category_key, label, member_count, visible, nest_depth=1):
    count_label = "%d motion%s" % (
        member_count, "" if member_count == 1 else "s",
    )
    return _container_row(
        "CATEGORY", category_key, label, "OUTLINER_COLLECTION",
        nest_depth, visible, count_label, member_count,
    )


def descendants_in_bucket(items, bucket_key):
    """Return rows nested under one Structures or Motions parent."""
    return descendants_under_row(items, bucket_key)


def descendants_under_row(items, anchor_key):
    """Return rows nested under a host, bucket, or category parent."""
    anchor_index = -1
    anchor_depth = 0
    for index, item in enumerate(items):
        key = item["group_key"] if isinstance(item, dict) else getattr(item, "group_key", "")
        if key == anchor_key:
            anchor_index = index
            raw = (
                item.get("nest_depth", 0) if isinstance(item, dict)
                else getattr(item, "nest_depth", 0)
            )
            anchor_depth = int(raw or 0)
            break
    if anchor_index < 0:
        return []
    result = []
    for item in items[anchor_index + 1:]:
        raw = (
            item.get("nest_depth", 0) if isinstance(item, dict)
            else getattr(item, "nest_depth", 0)
        )
        if int(raw or 0) <= anchor_depth:
            break
        result.append(item)
    return result


def _record_for_target(target):
    resolved, _reason = target_memory.resolve_target_record(target)
    if resolved is None:
        return None, None
    host = resolved.get("owner")
    path = str(resolved.get("data_path", ""))
    index = int(resolved.get("index", -1))
    for record in reversed(applied_motion.entries(host, validate=False)):
        for record_path, record_index in applied_motion.paths_of(record):
            if record_path != path:
                continue
            # -1 means "whatever index this property has". That is already the
            # convention `applied_motion._has_live_driver` validates records
            # with, and matching it strictly here meant a record stamped -1
            # never recognised its own driver at array_index 0 -- so an
            # effect's targets filed themselves under a plain category instead
            # of under the effect, and the row never carried its name.
            if record_index < 0 or index < 0 or record_index == index:
                return host, record
    return host, None


def metadata_for_descriptor(target):
    """Return trusted Espresso metadata for a real target, when available."""
    host, record = _record_for_target(target)
    if record is None:
        return None
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    template = applied_motion.resolve_template(record)
    template_id = str(extras.get("template_id") or ((template or {}).get("id") or ""))
    values = extras.get("parameter_values")
    entry = dict(extras.get("target_entry") or {})
    stack_payload = stack_records.stack_from_extras(extras)
    editable = bool(
        stack_payload is not None
        or (template_id and isinstance(values, dict) and template is not None and entry)
    )
    effect_kind = applied_motion_manager.effect_kind(record)
    return {
        "host": host,
        "record": record,
        "effect_code": str(record.get("code", "")),
        "label": applied_motion.describe(record),
        "template_id": template_id,
        "parameter_values": dict(values or {}),
        "entry": entry,
        "editable": editable,
        "removable": template is not None,
        "effect_kind": effect_kind,
        "record_token": applied_motion.entry_token(host, record),
    }


def foreign_live_motion_for_channel(owner, data_path, index):
    """Return a live channel's unavailable applied-motion metadata, if any."""
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return None
    curve = (
        animation.drivers.find(data_path, index=index)
        if index >= 0 else animation.drivers.find(data_path)
    )
    if curve is None:
        return None
    descriptor = target_memory.serialize_target(owner, data_path, index)
    metadata = metadata_for_descriptor(descriptor) if descriptor else None
    if metadata and applied_motion.resolve_template(metadata["record"]) is None:
        return metadata
    return None


def rows_for_targets(targets, group_state=None, effects=None, source="ACTIVE"):
    """Flatten grouped presentation rows over an authoritative target list."""
    group_state = dict(group_state or {})
    groups = OrderedDict()
    effects = list(effects or ())
    for target in targets:
        target = dict(target)
        metadata = metadata_for_descriptor(target)
        if metadata:
            group_key = "effect:%s" % metadata["record_token"]
            group_label = metadata["label"]
            group_icon = "DRIVER"
        else:
            group_key = "category:%s" % target.get("category", "Other")
            group_label = target.get("category", "Other")
            group_icon = target.get("icon", "DRIVER")
        group = groups.setdefault(group_key, {
            "key": group_key, "label": group_label, "icon": group_icon,
            "metadata": metadata, "targets": [],
        })
        enriched = dict(target)
        enriched.update({
            "row_kind": "TARGET",
            "group_key": group_key,
            "descriptor_key": descriptor_key(target),
            "effect_code": metadata["effect_code"] if metadata else "",
            "template_id": metadata["template_id"] if metadata else "",
            "editable": bool(metadata and metadata["editable"]),
            "record_token": metadata["record_token"] if metadata else "",
            "effect_kind": metadata["effect_kind"] if metadata else "SINGLE_PROPERTY",
            "batch_eligible": bool(
                not metadata or (metadata["removable"]
                                 and metadata["effect_kind"] == "SINGLE_PROPERTY")
            ),
            "badge_text": "", "member_count": 0,
            "nest_depth": 0,
        })
        group["targets"].append(enriched)

    for effect in effects:
        if (effect["effect_kind"] not in {"GENERATED_SETUP", "STRUCTURAL_EFFECT"}
                and not effect.get("child_effect_kind")):
            # A child of a setup earns a row on its parent's account. Without
            # this an authoring rig's Flight Motion vanished the moment it was
            # correctly typed as a motion rather than a generated setup.
            continue
        key = effect["group_key"]
        icon = "GEOMETRY_NODES"
        group = groups.setdefault(key, {
            "key": key, "label": effect["label"], "icon": icon,
            "metadata": effect, "targets": [],
        })
        group["label"] = effect["label"]
        group["icon"] = icon
        group["metadata"] = {**(group.get("metadata") or {}), **effect}

    child_groups = OrderedDict()
    root_groups = []
    for group in groups.values():
        metadata = group.get("metadata") or {}
        parent_token = str(metadata.get("parent_record_token") or "")
        if parent_token:
            child_groups.setdefault("effect:%s" % parent_token, []).append(group)
        else:
            root_groups.append(group)

    structure_groups = []
    motion_groups = []
    for group in root_groups:
        bucket = _bucket_key_for_group(group)
        if bucket == BUCKET_STRUCTURES:
            structure_groups.append(group)
        else:
            motion_groups.append(group)

    rows = []

    def append_group(group, ancestors_open, show_category_in_badge, nest_depth):
        metadata = group["metadata"]
        effect_kind = (
            metadata.get("effect_kind", "SINGLE_PROPERTY")
            if metadata else "SINGLE_PROPERTY"
        )
        group_batch = effect_kind in {
            "GENERATED_SETUP", "STRUCTURAL_EFFECT", "MOTION_SET",
        } and not bool(metadata.get("child_effect_kind"))
        label = group["label"]
        _host_key, host_name = _host_key_and_label(group)
        omit_host = source in _HOST_SCOPED
        group_open = group_state.get(group["key"], True)
        group_visible = ancestors_open
        child_visible = ancestors_open and group_open
        catalogue_category = catalogue_category_for_group(group)
        route_badge = compact_route_label(metadata)
        if show_category_in_badge and catalogue_category:
            badge_text = "%s · %s" % (catalogue_category, route_badge)
        else:
            badge_text = route_badge
        rows.append({
            "row_kind": "GROUP", "group_key": group["key"],
            "descriptor_key": group["key"], "label": label,
            "icon": group["icon"], "category": group["label"],
            "id_type": "", "id_name": "", "owner_path": "",
            "data_path": "", "index": -1,
            "effect_code": metadata["effect_code"] if metadata else "",
            "template_id": metadata["template_id"] if metadata else "",
            "editable": bool(metadata and metadata["editable"]),
            "record_token": metadata.get("record_token", "") if metadata else "",
            "effect_kind": effect_kind,
            "batch_eligible": bool(group_batch and metadata.get("removable", True)),
            "bakeable": bool(metadata and metadata.get("bakeable", True)),
            "removable": bool(metadata and metadata.get("removable", True)),
            "visible": group_visible,
            "badge_text": badge_text,
            "member_count": 0,
            "nest_depth": nest_depth,
        })
        for target in group["targets"]:
            child = dict(target)
            child["visible"] = child_visible
            child["nest_depth"] = nest_depth + 1
            if omit_host:
                child["label"] = _child_destination_label(child.get("label"), host_name)
            rows.append(child)
        for child_group in sorted(
                child_groups.get(group["key"], ()),
                key=lambda item: item["label"].lower()):
            append_group(
                child_group, child_visible,
                show_category_in_badge=False,
                nest_depth=nest_depth + 1,
            )

    def append_motion_groups(bucket_groups, bucket_open, bucket_key, group_depth):
        by_category = OrderedDict()
        for group in bucket_groups:
            category = catalogue_category_for_group(group) or "Other"
            by_category.setdefault(category, []).append(group)
        for category_name, category_groups in by_category.items():
            use_category_parent = len(category_groups) >= 2
            category_key = _category_row_key(bucket_key, category_name)
            category_open = group_state.get(category_key, True)
            if use_category_parent:
                rows.append(_category_row(
                    category_key, category_name, len(category_groups), bucket_open,
                    nest_depth=group_depth,
                ))
            parent_open = bucket_open and (category_open if use_category_parent else True)
            for group in sorted(category_groups, key=lambda item: item["label"].lower()):
                append_group(
                    group, parent_open,
                    show_category_in_badge=not use_category_parent,
                    nest_depth=group_depth + (1 if use_category_parent else 0),
                )

    def emit_type_buckets(structure_groups, motion_groups, ancestors_open, base_depth, key_suffix=""):
        if structure_groups:
            bucket_key = (
                "%s:%s" % (BUCKET_STRUCTURES, key_suffix) if key_suffix else BUCKET_STRUCTURES
            )
            rows.append(_bucket_row(
                bucket_key, nest_depth=base_depth, visible=ancestors_open,
            ))
            bucket_open = ancestors_open and group_state.get(bucket_key, True)
            for group in sorted(structure_groups, key=lambda item: item["label"].lower()):
                append_group(
                    group, bucket_open, show_category_in_badge=False,
                    nest_depth=base_depth + 1,
                )
        if motion_groups:
            bucket_key = (
                "%s:%s" % (BUCKET_MOTIONS, key_suffix) if key_suffix else BUCKET_MOTIONS
            )
            rows.append(_bucket_row(
                bucket_key, nest_depth=base_depth, visible=ancestors_open,
            ))
            bucket_open = ancestors_open and group_state.get(bucket_key, True)
            append_motion_groups(
                motion_groups, bucket_open, bucket_key, base_depth + 1,
            )

    if source in _HOST_SCOPED:
        by_host = OrderedDict()
        for group in root_groups:
            host_key, host_label = _host_key_and_label(group)
            slot = by_host.setdefault(host_key, {
                "label": host_label, "structures": [], "motions": [], "host": None,
            })
            if slot["host"] is None:
                slot["host"] = (group.get("metadata") or {}).get("host")
            if slot["host"] is None:
                for target in group.get("targets") or ():
                    name = str(target.get("id_name") or "")
                    if name and name in bpy.data.objects:
                        slot["host"] = bpy.data.objects[name]
                        break
            if _bucket_key_for_group(group) == BUCKET_STRUCTURES:
                slot["structures"].append(group)
            else:
                slot["motions"].append(group)
        for host_key, slot in sorted(
                by_host.items(), key=lambda item: item[1]["label"].lower()):
            rows.append(_host_row(
                host_key, slot["label"],
                badge_text=host_visibility_badge(slot.get("host")),
            ))
            host_open = group_state.get(host_key, True)
            emit_type_buckets(
                slot["structures"], slot["motions"],
                ancestors_open=host_open, base_depth=1, key_suffix=host_key,
            )
    else:
        emit_type_buckets(structure_groups, motion_groups, True, 0, "")

    return rows


def rows_signature(rows):
    return "|".join(
        "%s:%s:%s:%s:%s:%s:%s:%s:%s" % (
            row.get("row_kind", "TARGET"), row.get("group_key", ""),
            row.get("descriptor_key", descriptor_key(row)),
            int(bool(row.get("editable", False))),
            row.get("effect_kind", "SINGLE_PROPERTY"),
            int(bool(row.get("batch_eligible", False))),
            int(bool(row.get("visible", True))),
            row.get("label", ""),
            row.get("badge_text", ""),
        )
        for row in rows
    )


def descriptors_from_items(items, selected_only=True):
    result = []
    for item in items:
        if getattr(item, "row_kind", "TARGET") != "TARGET":
            continue
        if not getattr(item, "batch_eligible", True):
            continue
        if selected_only and not getattr(item, "batch_selected", False):
            continue
        result.append(driver_targets.descriptor_from_item(item))
    return result


def effect_tokens_from_items(items, selected_only=True):
    """Selected atomic effects represented by generated or structural rows."""
    result = []
    seen = set()
    for item in items:
        kind = getattr(item, "row_kind", "TARGET")
        if kind not in {"SETUP", "GROUP"}:
            continue
        if not getattr(item, "batch_eligible", False):
            continue
        if selected_only and not getattr(item, "batch_selected", False):
            continue
        token = str(getattr(item, "record_token", "") or "")
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def selected_removal_counts(items, selected_only=True):
    """Count selected effect groups and driver channels for destructive UI labels."""
    effect_count = 0
    driver_count = 0
    counted_tokens = set()
    for item in items:
        if selected_only and not getattr(item, "batch_selected", False):
            continue
        if not getattr(item, "batch_eligible", False):
            continue
        kind = getattr(item, "row_kind", "TARGET")
        if kind in {"GROUP", "SETUP"}:
            token = str(getattr(item, "record_token", "") or "")
            if token:
                counted_tokens.add(token)
            effect_count += 1
        elif kind == "TARGET":
            token = str(getattr(item, "record_token", "") or "")
            effect_kind = str(getattr(item, "effect_kind", "") or "")
            if token and effect_kind == "SINGLE_PROPERTY":
                if token not in counted_tokens:
                    counted_tokens.add(token)
                    effect_count += 1
            else:
                driver_count += 1
    return effect_count, driver_count


def visible_resource_label(effect_count, driver_count):
    """Heading count using the same effect/driver units as Remove Selected."""
    parts = []
    if effect_count:
        parts.append(
            "%d Effect%s" % (effect_count, "" if effect_count == 1 else "s")
        )
    if driver_count:
        parts.append(
            "%d Driver%s" % (driver_count, "" if driver_count == 1 else "s")
        )
    if not parts:
        return "0"
    return " + ".join(parts)


def remove_selected_label(effect_count, driver_count):
    """Human-readable Remove Selected label with distinct effect/driver units."""
    label = visible_resource_label(effect_count, driver_count)
    if label == "0":
        return "Remove Selected"
    return "Remove " + label
