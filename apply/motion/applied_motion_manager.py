"""Typed applied-effect discovery for the Applied Effects UI.

This joins persistent effect records to their real ownership route.  It does
not discover F-curves (``driver_targets`` owns that job) and it does not mutate
setups.  Keeping the join read-only lets the panel show drivers and motion sets
without creating a second source of truth.
"""

from __future__ import annotations

import time

from ...catalogue import templates
from ..core import target_memory
from . import applied_motion, stack_records


SINGLE_PROPERTY = "SINGLE_PROPERTY"
MOTION_SET = "MOTION_SET"


def _template(record):
    return applied_motion.resolve_template(record)


def effect_kind(record):
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    if stack_records.stack_from_extras(extras) is not None or extras.get("motion_set") is True:
        return MOTION_SET
    template = _template(record)
    return MOTION_SET if template is not None and templates.has_motion_plan(template) else SINGLE_PROPERTY


def _record_is_live(host, record):
    from . import applied_reconcile

    try:
        return applied_reconcile.record_is_live(host, record)
    except (AttributeError, ReferenceError, TypeError):
        return True


def _effect(host, host_label, record):
    # A record with no driven path owns nothing this add-on can show, edit or clear.
    if not applied_motion.paths_of(record):
        return None
    template = _template(record)
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    template_id = str(extras.get("template_id") or ((template or {}).get("id") or ""))
    token = applied_motion.entry_token(host, record)
    return {
        "host": host,
        "host_label": host_label,
        "record": record,
        "record_token": token,
        "group_key": "effect:%s" % token,
        "effect_code": str(record.get("code", "")),
        "label": applied_motion.describe(record),
        "template_id": template_id,
        "template": template,
        "effect_kind": effect_kind(record),
        "route": "",
        # Derived, never stored: the driver is the authority on whether the
        # motion still exists. A picker offering something to edit or attach
        # to skips a dead one; Clear still sees it, which is why the
        # collectors themselves do not validate.
        "alive": _record_is_live(host, record),
        "editable": bool(
            stack_records.stack_from_extras(extras) is not None
            or (template_id and template is not None)
        ),
        "bakeable": True,
        "removable": True,
    }


def _active_hosts(context):
    return list(applied_motion.hosts_for_object(getattr(context, "active_object", None)))


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
    for host, host_label, records in _collected(context, source):
        for record in records:
            value = _effect(host, host_label, record)
            if value is None or value["record_token"] in seen:
                continue
            seen.add(value["record_token"])
            result.append(value)
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


def _hosts_alive(effects):
    """False when a cached effect's host was deleted since it was collected."""
    for effect in effects:
        try:
            effect["host"].as_pointer()
        except ReferenceError:
            return False
    return True


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
    if (
        cached is not None
        and now - cached[0] <= _DRAW_CACHE_SECONDS
        and _hosts_alive(cached[1])
    ):
        return cached[1]
    result = collect_effects(context, source)
    _DRAW_CACHE.clear()
    _DRAW_CACHE[key] = (now, result)
    return result
