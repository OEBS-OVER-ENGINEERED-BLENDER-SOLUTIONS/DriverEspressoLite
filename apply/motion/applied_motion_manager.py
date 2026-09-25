"""Typed applied-effect discovery for the Applied Effects UI.

This joins persistent effect records to their real ownership route.  It does
not discover F-curves (``driver_targets`` owns that job) and it does not mutate
setups.  Keeping the join read-only lets the panel show drivers, motion sets,
and generated setups without creating a second source of truth.
"""

from __future__ import annotations

import time

import bpy

from ...catalogue import templates
from ...generated import attachments as generated_attachments
from ...generated import manifest as generated_manifest
from ...generated.core import registry as generated_registry
from ..setups import layout_preparation
from ..core import target_memory
from . import applied_motion, stack_records


SINGLE_PROPERTY = "SINGLE_PROPERTY"
MOTION_SET = "MOTION_SET"
GENERATED_SETUP = "GENERATED_SETUP"
STRUCTURAL_EFFECT = "STRUCTURAL_EFFECT"


def _template(record):
    return applied_motion.resolve_template(record)


def effect_kind(record):
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    if stack_records.stack_from_extras(extras) is not None:
        return MOTION_SET
    # Generated workflows also describe themselves as motion sets because
    # they own several animated channels.  Their ownership manifest is the
    # stronger route contract: they must remain one generated setup for Live,
    # Bake and Clear dispatch.  Driver-backed object workflows have no such
    # manifest and use the explicit motion_set marker below.
    if generated_manifest.from_extras(extras) is not None:
        return GENERATED_SETUP
    if extras.get("motion_set") is True:
        return MOTION_SET
    template = _template(record)
    route = (template or {}).get("apply_route", "")
    if route in {"generated_setup", "spatial_field"}:
        return GENERATED_SETUP
    if any(key in extras for key in (
        "object_set", "event_controller", "generated_manifest",
    )):
        return GENERATED_SETUP
    if not applied_motion.paths_of(record):
        return GENERATED_SETUP
    return MOTION_SET if template is not None and templates.has_motion_plan(template) else SINGLE_PROPERTY


def _iter_stamped_resources():
    """Yield generated ID datablocks once per discovery pass."""
    for collection in (
        bpy.data.objects, bpy.data.meshes, bpy.data.curves, bpy.data.materials,
        bpy.data.node_groups, bpy.data.collections, bpy.data.actions,
    ):
        for value in collection:
            yield value
def _build_resource_index():
    """Build one stable-ID lookup for an entire Applied Effects collection."""
    index = {}
    for value in _iter_stamped_resources():
        identity = generated_registry.resource_identity(value)
        resource_id = identity.get("resource_id")
        if resource_id:
            index[str(resource_id)] = value
    for obj in bpy.data.objects:
        for value in tuple(getattr(obj, "modifiers", ())) + tuple(getattr(obj, "constraints", ())):
            resource_id = generated_attachments.identity(obj, value).get("resource_id")
            if resource_id:
                index[str(resource_id)] = value
    return index


def _resource_resolver(resource, resource_index):
    return resource_index.get(str(resource.resource_id))


def _generated_is_live(record, resource_cache):
    manifest = generated_manifest.from_extras(
        record.get("extras") if isinstance(record.get("extras"), dict) else None,
    )
    if manifest is None:
        # Older pathless setup records have no portable resource manifest. Do
        # not fabricate health from the stamp alone; route adapters may add a
        # precise fallback later.
        return False, None
    if resource_cache[0] is None:
        resource_cache[0] = _build_resource_index()
    return bool(manifest.resources) and all(
        _resource_resolver(resource, resource_cache[0]) is not None
        for resource in manifest.resources
    ), manifest


def _modifier_matches_manifest(host, modifier, manifest):
    """Role-only hits are not enough; ownership must match this effect."""
    if host is None or modifier is None or manifest is None:
        return False
    ident = generated_attachments.identity(host, modifier)
    if not ident:
        return False
    setup_id = str(getattr(manifest, "setup_id", "") or "")
    effect_id = str(getattr(manifest, "effect_id", "") or "")
    if setup_id and str(ident.get("setup_id") or "") != setup_id:
        return False
    if effect_id and str(ident.get("effect_id") or "") != effect_id:
        return False
    return True


def _route_modifier_still_live(host, manifest):
    """Fallback when a manifest resource id is stale but the owned route still exists."""
    if host is None or manifest is None:
        return False
    roles = {
        str(getattr(resource, "role", "") or "")
        for resource in manifest.resources
    }
    # No generated modifier to match: nothing this product applies builds one.
    modifier = None
    return _modifier_matches_manifest(host, modifier, manifest)


def _record_is_live(host, record):
    from . import applied_reconcile

    try:
        return applied_reconcile.record_is_live(host, record)
    except (AttributeError, ReferenceError, TypeError):
        return True


def _effect(host, host_label, record, resource_cache):
    kind = effect_kind(record)
    manifest = None
    if kind == GENERATED_SETUP:
        live, manifest = _generated_is_live(record, resource_cache)
        # Driver-backed generated routes remain honest through their F-curves;
        # pathless routes require a healthy ownership manifest.
        if not live and not applied_motion.paths_of(record):
            if not _route_modifier_still_live(host, manifest):
                return None
    template = _template(record)
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    template_id = str(extras.get("template_id") or ((template or {}).get("id") or ""))
    token = applied_motion.entry_token(host, record)
    label = applied_motion.describe(record)
    route = ""
    if manifest is not None:
        route = "Geometry Nodes" if any(
            resource.kind.value in {"NODE_GROUP", "MODIFIER"}
            for resource in manifest.resources
        ) else "Generated Setup"
    child_kind = str(extras.get("child_effect_kind") or "")
    if child_kind == "SPATIAL_EFFECTOR" and template is not None:
        template = None
    return {
        "host": host,
        "host_label": host_label,
        "record": record,
        "record_token": token,
        "group_key": "effect:%s" % token,
        "effect_code": str(record.get("code", "")),
        "label": label,
        "template_id": template_id,
        "template": template,
        "effect_kind": kind,
        "route": route,
        # Derived, never stored: the driver is the authority on whether the
        # motion still exists. A picker offering something to edit or attach
        # to skips a dead one; Clear still sees it, which is why the
        # collectors themselves do not validate.
        "alive": _record_is_live(host, record),
        "setup_id": manifest.setup_id if manifest is not None else "",
        "layer_id": str(extras.get("layer_id") or ""),
        "child_effect_kind": child_kind,
        "parent_effect_code": str(extras.get("parent_effect_code") or ""),
        "parent_record_token": "",
        "editable": bool(
            stack_records.stack_from_extras(extras) is not None
            or (template_id and template is not None)
        ),
        # A spatial child can be edited or removed independently. Baking its
        # parent remains the only honest way to bake the composed field.
        "bakeable": not bool(child_kind),
        "removable": template is not None,
    }


def _active_hosts(context):
    active = getattr(context, "active_object", None)
    hosts = list(applied_motion.hosts_for_object(active))
    carrier = layout_preparation.resolve_carrier(active)
    if carrier is not None and carrier not in hosts:
        hosts.extend(applied_motion.hosts_for_object(carrier))
    return hosts


def _layout_effect(carrier):
    template = layout_preparation.parameter_template(carrier)
    if template is None:
        return None
    setup_id = str(carrier.get(layout_preparation.LAYOUT_ID_TAG, ""))
    token = "layout:%s" % (setup_id or carrier.name_full)
    label = layout_preparation.layout_label(carrier)
    is_shape = str(carrier.get(layout_preparation.LAYOUT_KIND_TAG, "")) in {
        "SHAPE_SURFACE", "SHAPE_VOLUME",
    }
    return {
        "host": carrier,
        "host_label": "Object: %s" % carrier.name,
        "record": {
            "code": layout_preparation.EFFECT_ID,
            "label": label,
            "paths": [],
            "extras": {"structural_layout": True, "layout_id": setup_id},
        },
        "record_token": token,
        "group_key": "effect:%s" % token,
        "effect_code": layout_preparation.EFFECT_ID,
        "label": label,
        "template_id": template["id"],
        "template": template,
        "effect_kind": STRUCTURAL_EFFECT,
        "route": "Prepared Layout",
        "setup_id": setup_id,
        "editable": not is_shape,
        "bakeable": True,
        "removable": True,
    }


#: The one authoring rig that is a STRUCTURE with a MOTION on it.


def _shape_adaptation_effect(carrier, parent):
    """Expose Shape Adaptation as settings owned by its Prepared Layout."""
    kind = str(carrier.get(layout_preparation.LAYOUT_KIND_TAG, ""))
    if kind not in {"SHAPE_SURFACE", "SHAPE_VOLUME"}:
        return None
    template = layout_preparation.parameter_template(carrier)
    if template is None:
        return None
    setup_id = str(carrier.get(layout_preparation.LAYOUT_ID_TAG, ""))
    token = "layout-shape:%s" % (setup_id or carrier.name_full)
    return {
        "host": carrier,
        "host_label": "Object: %s" % carrier.name,
        "record": {
            "code": "LAY00.SHAPE",
            "label": "Adapt to Shape",
            "paths": [],
            "extras": {
                "structural_layout": True,
                "layout_id": setup_id,
                "child_effect_kind": "SHAPE_ADAPTATION",
                "parent_effect_code": layout_preparation.EFFECT_ID,
            },
        },
        "record_token": token,
        "group_key": "effect:%s" % token,
        "effect_code": "LAY00.SHAPE",
        "label": "Adapt to Shape",
        "template_id": template["id"],
        "template": template,
        "effect_kind": STRUCTURAL_EFFECT,
        "route": "Shape Adaptation",
        "setup_id": setup_id,
        "layer_id": "",
        "child_effect_kind": "SHAPE_ADAPTATION",
        "parent_effect_code": layout_preparation.EFFECT_ID,
        "parent_record_token": str(parent.get("record_token") or ""),
        "editable": True,
        "bakeable": False,
        "removable": False,
    }


def _layout_carriers(context, source):
    if source == "SCENE":
        scene = getattr(context, "scene", None)
        return [obj for obj in tuple(getattr(scene, "objects", ()) or ()) if layout_preparation.is_carrier(obj)]
    if source == "ACTIVE":
        carrier = layout_preparation.resolve_carrier(getattr(context, "active_object", None))
        return [carrier] if carrier is not None else []
    return []


def _scene_collected(context):
    scene = getattr(context, "scene", None)
    objects = tuple(getattr(scene, "objects", ()) or ())
    return (
        applied_motion.collect_for_objects(objects, validate=False)
        + applied_motion.collect_for_scene(scene, validate=False)
    )


def _last_collected(context):
    scene = getattr(context, "scene", None)
    props = getattr(scene, "espresso_props", None)
    token = str(getattr(props, "last_applied_effect_token", "") or "")
    if token:
        matched = []
        for host, host_label, records in _scene_collected(context):
            keep = [record for record in records
                    if applied_motion.entry_token(host, record) == token]
            if keep:
                matched.append((host, host_label, keep))
        if matched:
            return matched

    # Old driver-only applies predate the effect pointer. Their target-memory
    # entry still names the exact RNA owner, so Last remains independent of
    # whichever object happens to be active now.
    entry = target_memory.latest_entry(props) if props is not None else None
    hosts = []
    seen = set()
    for target in (entry or {}).get("targets") or ():
        resolved, _reason = target_memory.resolve_target_record(target)
        owner = (resolved or {}).get("owner")
        if owner is not None and id(owner) not in seen:
            seen.add(id(owner))
            hosts.append(owner)
    return applied_motion.collect(hosts, validate=False)


def _collected(context, source):
    if source == "SCENE":
        return _scene_collected(context)
    if source == "LAST":
        return _last_collected(context)
    return applied_motion.collect(_active_hosts(context), validate=False)


def collect_effects(context, source="ACTIVE"):
    """Return distinct, live applied effects for one manager scope."""
    result = []
    seen = set()
    resource_cache = [None]
    for host, host_label, records in _collected(context, source):
        for record in records:
            extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
            value = (
                _layout_effect(host)
                if extras.get("prepared_layout") is True
                and layout_preparation.is_carrier(host)
                else _effect(host, host_label, record, resource_cache)
            )
            if value is None:
                continue
            # One generated setup is intentionally stamped on its controller
            # and members so discovery works from any selected participant.
            # Scene scope must still show the setup once, not once per host.
            identity = (
                value["record_token"]
                if value.get("child_effect_kind")
                else
                "setup:%s:effect:%s" % (
                    value["setup_id"], value.get("effect_code", ""),
                )
                if value.get("setup_id")
                else value["record_token"]
            )
            if identity in seen:
                continue
            seen.add(identity)
            # ALSO by record token. The layout-carrier pass below dedupes on
            # `record_token in seen`, and once structural effects started
            # deduping by setup (so an authoring rig stops appearing once per
            # driven object) the token was no longer being registered here --
            # so a prepared layout was emitted twice, identically. Registering
            # both keys costs nothing and keeps that second pass honest.
            seen.add(value["record_token"])
            result.append(value)
    for carrier in _layout_carriers(context, source):
        value = _layout_effect(carrier)
        if value is None or value["record_token"] in seen:
            continue
        seen.add(value["record_token"])
        result.append(value)
        child = _shape_adaptation_effect(carrier, value)
        if child is not None and child["record_token"] not in seen:
            seen.add(child["record_token"])
            result.append(child)
    parent_tokens = {
        (id(item.get("host")), str(item.get("effect_code") or "")):
            str(item.get("record_token") or "")
        for item in result
        if not item.get("child_effect_kind")
    }
    for item in result:
        parent_code = str(item.get("parent_effect_code") or "")
        if parent_code:
            item["parent_record_token"] = parent_tokens.get(
                (id(item.get("host")), parent_code), "",
            )
    shape_children = []
    for item in tuple(result):
        if item.get("effect_kind") != STRUCTURAL_EFFECT:
            continue
        child = _shape_adaptation_effect(item.get("host"), item)
        if child is not None and child["record_token"] not in seen:
            seen.add(child["record_token"])
            shape_children.append(child)
    result.extend(shape_children)
    return result


def find_effect(context, record_token, source="ACTIVE"):
    return next((item for item in collect_effects(context, source)
                 if item["record_token"] == record_token), None)


_DRAW_CACHE = {}
_DRAW_CACHE_SECONDS = 0.15


def find_effect_for_draw(context, record_token, source="ACTIVE"):
    """Read-only UI lookup. Operators must continue to use find_effect."""
    return next((item for item in collect_effects_for_draw(context, source)
                 if item["record_token"] == record_token), None)


def collect_effects_for_draw(context, source="ACTIVE"):
    """Short-lived redraw cache; mutation and operator paths stay uncached."""
    scene = getattr(context, "scene", None)
    active = getattr(context, "active_object", None)
    props = getattr(scene, "espresso_props", None)
    key = (
        int(scene.as_pointer()) if scene is not None else 0,
        int(active.as_pointer()) if active is not None else 0,
        str(source),
        str(getattr(props, "last_applied_effect_token", "") or ""),
    )
    now = time.monotonic()
    cached = _DRAW_CACHE.get(key)
    if cached is not None and now - cached[0] <= _DRAW_CACHE_SECONDS:
        return cached[1]
    result = collect_effects(context, source)
    _DRAW_CACHE.clear()
    _DRAW_CACHE[key] = (now, result)
    return result
