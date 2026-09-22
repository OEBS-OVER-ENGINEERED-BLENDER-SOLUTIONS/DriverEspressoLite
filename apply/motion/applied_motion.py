"""What motion is applied where - the record the bake and clear tools read.

Most templates write custom properties, so their presence is already evidence.
Many do not: a plain rotation driver leaves nothing behind but an F-curve whose
expression could have been typed by hand. Without a record there is no way to
answer "which of these drivers is Ball Bounce: Bowling Ball", and a bake list
that cannot name what it is about to bake is not usable.

So each application stamps the datablock that hosts its drivers:

    obj["__espresso_applied"] = '[{"code": "BB01", "paths": [["location", 2]], ...}]'

The host, not the object, because a driver lives on whichever ID owns the
animation data - the object for a transform, the material's node tree for a
shader value, the Scene for camera flash exposure. One code path covers all
three, which is why scene-level motion needs no special case here.

Two properties make this survive contact with real files:

* It travels with the data. Duplicating an object copies its drivers and its
  stamp together; appending into another file brings both.
* It survives uninstalling the add-on, because it is plain ID property data.

And one thing it cannot promise: a stamp can outlive the driver it describes,
because nothing stops an artist deleting a driver by hand. So the driver is the
authority on whether motion EXISTS and the stamp is the authority on WHAT it
is - ``entries`` validates every record against a live F-curve and drops the
ones that no longer resolve. That keeps every validating read honest with no
repair step. The reads that must NOT validate (Clear needs to see leftovers)
are made honest the other way: ``applied_reconcile`` notices the ID a driver
was deleted from and, on idle, trims the dead channel or purges the record
with its helpers. A leftover is therefore a state that lasts until Blender is
next idle, not one that lives in the file.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
import re

import bpy

PROPERTY = "__espresso_applied"

# Custom properties whose names begin with this belong to an application and
# must be removed along with it.
HELPER_PREFIX = "__espresso_"
LAST_EFFECT_ATTR = "last_applied_effect_token"


def _remember_latest_effect(host, record):
    """Point the Last manager scope at the effect that was actually applied."""
    scene = getattr(bpy.context, "scene", None)
    props = getattr(scene, "espresso_props", None)
    if props is not None:
        props.last_applied_effect_token = entry_token(host, record)


def _forget_latest_effect(host, record):
    token = entry_token(host, record)
    for scene in bpy.data.scenes:
        props = getattr(scene, "espresso_props", None)
        if props is not None and props.last_applied_effect_token == token:
            props.last_applied_effect_token = ""


def _drivers(host):
    animation_data = getattr(host, "animation_data", None)
    return list(getattr(animation_data, "drivers", ()) or ())


@lru_cache(maxsize=128)
def _decode_stamp(raw):
    """Cache text only: never retain Blender IDs across file loads or undo."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("code")]


def read(host):
    """Every stamp on this datablock, whether or not it still resolves.

    Never raises. A corrupt or hand-edited value yields an empty list rather
    than breaking the panel that is trying to draw it - a malformed stamp is a
    reason to show nothing, not a reason to fail to open the file.
    """
    if host is None:
        return []
    try:
        raw = host[PROPERTY]
    except (KeyError, TypeError):
        return []
    if not isinstance(raw, str):
        return []
    # Every caller owns its result; edits must never poison subsequent reads.
    return deepcopy(_decode_stamp(raw))


def write(host, records):
    """Replace the stamp, removing the property entirely when nothing is left.

    Leaving an empty list behind would make a clean object look like it still
    carries Driver Espresso data in the custom-properties panel.
    """
    if host is None:
        return
    if not records:
        try:
            del host[PROPERTY]
        except (KeyError, TypeError):
            pass
        return
    host[PROPERTY] = json.dumps(records, separators=(",", ":"), sort_keys=True)


def _paths_of(record):
    out = []
    for entry in record.get("paths") or ():
        if isinstance(entry, (list, tuple)) and entry:
            data_path = str(entry[0])
            index = int(entry[1]) if len(entry) > 1 else -1
            out.append((data_path, index))
    return out


def paths_of(record):
    """Public, normalized target paths for manager/bake consumers."""
    return _paths_of(record)


def _has_live_driver(host, data_path, index):
    for fcurve in _drivers(host):
        if fcurve.data_path != data_path:
            continue
        if index < 0 or fcurve.array_index == index:
            return True
    return False


def entries(host, validate=True):
    """Stamps that still describe real drivers, newest first.

    With ``validate`` off this is just ``read`` - useful for a clear operation
    that wants to tidy up leftovers precisely because their drivers are gone.
    """
    records = read(host)
    if not validate:
        return records
    alive = []
    for record in records:
        paths = _paths_of(record)
        if not paths:
            # A record with no paths describes a setup made of objects and node
            # groups rather than drivers, so there is no F-curve to check.
            alive.append(record)
            continue
        if any(_has_live_driver(host, path, index) for path, index in paths):
            alive.append(record)
    return alive


def remember(host, code, label, targets, extras=None):
    """Stamp one application. Re-applying the same code replaces its record.

    Replacement rather than appending, because applying Ball Bounce twice to
    one object is one motion with new settings, not two motions - and a list
    that grew every time would make the bake menu fill with duplicates.
    """
    if host is None or not code:
        return None
    paths = []
    for target in targets or ():
        if isinstance(target, (list, tuple)) and target:
            paths.append([str(target[0]), int(target[1]) if len(target) > 1 else -1])
    record = {"code": str(code), "label": str(label or code), "paths": paths}
    slot = slot_for(paths)
    if slot:
        record["slot"] = slot
    if extras:
        record["extras"] = extras

    # An earlier effect that drove these same channels no longer does - this
    # one took them. Its record has to give them up, and if that leaves it with
    # nothing it is gone entirely.
    #
    # Without this the old record stayed "valid", because validation only asks
    # whether a driver exists at that path and one does - the new one. Switching
    # between Ball Bounce channels left the bake list offering Bowling, Golf and
    # Tennis on an object that was only bouncing one way.
    claimed = {(str(p[0]), int(p[1])) for p in paths}
    records = []
    for item in read(host):
        # Same code AND the same channels is the same application with new
        # settings -- replaced, which is what "applying Ball Bounce twice to
        # one object is one motion" has always meant. Same code on DIFFERENT
        # channels is a second application, and it now survives instead of
        # being deleted with its driver left running. It still gives up any
        # channel this apply just took, by the same rule as every other code.
        if item.get("code") == str(code) and record_slot(item) == slot:
            continue
        if not claimed:
            records.append(item)
            continue
        kept = [p for p in (item.get("paths") or ())
                if (str(p[0]), int(p[1]) if len(p) > 1 else -1) not in claimed]
        if kept or not (item.get("paths") or ()):
            # Path-less records describe object/node-group setups rather than
            # drivers, so nothing here can claim their channels away.
            records.append(dict(item, paths=kept) if item.get("paths") else item)
    records.append(record)
    write(host, records)
    _remember_latest_effect(host, record)
    return record


def prune(host):
    """Drop stamps whose drivers are all gone. Returns how many were removed.

    ``entries`` already hides these, so this is hygiene rather than
    correctness: without it, clearing every driver from an object leaves
    ``__espresso_applied`` sitting in the custom-properties panel describing
    motion that no longer exists.

    Records with no recorded paths are kept - those describe setups built from
    objects, constraints and node groups rather than drivers on this host, so
    there is no F-curve whose absence means anything.
    """
    records = read(host)
    if not records:
        return 0
    keep = []
    for record in records:
        paths = _paths_of(record)
        if not paths or any(_has_live_driver(host, path, i) for path, i in paths):
            keep.append(record)
    if len(keep) == len(records):
        return 0
    write(host, keep)
    return len(records) - len(keep)


def forget(host, code, slot=None):
    """Drop one stamp. Returns the record removed, or None.

    ``slot`` names ONE application when a recipe has been applied more than
    once to the same host on different channels. Left out, every application
    of that code goes -- which is what every existing caller means by "remove
    this effect from this host", and what a setup teardown has to do.
    """
    records = read(host)

    def matches(item):
        if item.get("code") != str(code):
            return False
        return slot is None or record_slot(item) == str(slot)

    keep = [item for item in records if not matches(item)]
    if len(keep) == len(records):
        return None
    removed = next(item for item in records if matches(item))
    write(host, keep)
    _forget_latest_effect(host, removed)
    return removed


# --------------------------------------------------------------- enumeration


def nested_node_trees(tree, _seen=None):
    """``tree`` and every node group reachable inside it.

    An effect can be applied to a group NESTED inside a material rather than to
    the material's own tree, which is exactly where a shipped Fire recipe puts
    its controls. A walk that stops at ``material.node_tree`` never sees it.
    Without it, a node group can carry a perfectly good ``__espresso_applied``
    record while both the ACTIVE and the SCENE scope return nothing and Live
    reports that the effect no longer resolves, with the motion still running
    in the viewport.

    Depth-guarded by identity rather than by a depth count, so a group that
    somehow reaches itself is visited once instead of recursing for ever.
    """
    if tree is None:
        return []
    seen = set() if _seen is None else _seen
    key = id(tree)
    if key in seen:
        return []
    seen.add(key)
    found = [tree]
    for node in getattr(tree, "nodes", ()) or ():
        inner = getattr(node, "node_tree", None)
        if inner is not None:
            found.extend(nested_node_trees(inner, seen))
    return found


def hosts_for_object(obj):
    """Every datablock that could hold motion applied *through* this object.

    An artist who applied a flicker to a lamp's material and a bounce to its
    transform thinks of both as "on that object", so a bake list keyed only on
    the object would silently omit half of it.
    """
    if obj is None:
        return []
    found = []
    seen = set()

    def add(candidate):
        if candidate is None:
            return
        key = (candidate.__class__.__name__, getattr(candidate, "name", ""), id(candidate))
        if key in seen:
            return
        seen.add(key)
        found.append(candidate)

    def add_tree(tree):
        # Nested groups too: a recipe's controls often live in a group inside
        # the material rather than in the material's own tree.
        for nested in nested_node_trees(tree):
            add(nested)

    add(obj)
    data = getattr(obj, "data", None)
    add(data)
    add_tree(getattr(data, "node_tree", None))
    if getattr(obj, "type", "") == "CAMERA":
        for scene in getattr(obj, "users_scene", ()) or ():
            add(scene)
        add(getattr(bpy.context, "scene", None))
        # A camera's owned support empty carries motion applied THROUGH the
        # camera: an orbit drives the pivot the camera hangs from, and an
        # artist who selects the camera means that motion too.
        from .camera import support_rig as camera_support_rig

        add(camera_support_rig.resolve_support_rig(obj))
    shape_keys = getattr(data, "shape_keys", None)
    add(shape_keys)
    for slot in getattr(obj, "material_slots", ()) or ():
        material = getattr(slot, "material", None)
        add(material)
        add_tree(getattr(material, "node_tree", None))
    for modifier in getattr(obj, "modifiers", ()) or ():
        add_tree(getattr(modifier, "node_group", None))
    animation = getattr(obj, "animation_data", None)
    add(getattr(animation, "action", None))
    return found


def objects_using_node_tree(tree):
    """Objects that carry this embedded shader tree through a material or lamp."""
    if tree is None:
        return []
    found = []
    seen = set()

    def add(obj):
        if obj is None:
            return
        marker = id(obj)
        if marker in seen:
            return
        seen.add(marker)
        found.append(obj)

    for material in getattr(bpy.data, "materials", []) or []:
        # `is not tree` missed a group nested inside the material, which is
        # the same blind spot hosts_for_object had.
        if tree not in nested_node_trees(getattr(material, "node_tree", None)):
            continue
        for obj in bpy.data.objects:
            slots = getattr(getattr(obj, "data", None), "materials", None) or ()
            if any(slot is material for slot in slots):
                add(obj)
    for light in getattr(bpy.data, "lights", []) or []:
        if tree not in nested_node_trees(getattr(light, "node_tree", None)):
            continue
        for obj in bpy.data.objects:
            if getattr(obj, "data", None) is light:
                add(obj)
    return found


def scene_hosts(scene):
    """Datablocks holding motion that is not pegged to any object.

    Camera flash drives scene exposure; a compositor effect drives a node in
    the scene's compositing tree. Neither belongs to anything in the outliner,
    which is why these get their own bake entry point.
    """
    if scene is None:
        return []
    found = [scene]
    world = getattr(scene, "world", None)
    if world is not None:
        found.append(world)
        found.extend(nested_node_trees(getattr(world, "node_tree", None)))
    # Blender 5.x moved the compositor off Scene.node_tree.
    found.extend(nested_node_trees(getattr(scene, "compositing_node_group", None)))
    view_settings = getattr(scene, "view_settings", None)
    curve_mapping = getattr(view_settings, "curve_mapping", None)
    if curve_mapping is not None:
        found.append(curve_mapping)
    return found


def _label_for(host):
    return "%s: %s" % (host.__class__.__name__, getattr(host, "name", "?"))


def host_token(host):
    """A session-local key for a stamped host in the active object's scope.

    This intentionally mirrors the bake picker rather than storing another
    identifier on the blend.  The manifest owns persistent identity; the token
    only lets a manager button point at one host while the current UI is open.
    """
    return "%s\x1f%s" % (host.__class__.__name__, getattr(host, "name", ""))


def slot_for(paths):
    """A short, stable id for the exact channels one application drives.

    Two applications of the SAME recipe to DIFFERENT channels of the same host
    are two effects, not one. Nothing in the record said so: a record was
    identified by host plus effect code alone, so the second apply had no way
    to be told apart from the first -- and `remember` resolved that by deleting
    the older one while its driver went on running. Found in the wild: one
    node group with two live Campfire Flicker drivers on it, on `inputs[3]`
    and `inputs[0]`, and a single record naming only `inputs[0]`. The first
    effect still drove the shot and could not be seen, edited, baked or
    removed.

    Derived from the channels rather than stored as a counter, so it is the
    same id every time the same channels are claimed -- which is what keeps
    re-applying to the same socket a REPLACEMENT, exactly as before. Empty for
    a record with no paths (a setup made of objects and node groups), so those
    keep the token they have always had.
    """
    claimed = sorted(
        "%s\x1e%d" % (str(entry[0]), int(entry[1]) if len(entry) > 1 else -1)
        for entry in paths or ()
        if isinstance(entry, (list, tuple)) and entry
    )
    if not claimed:
        return ""
    digest = 0
    for text in claimed:
        for character in text:
            digest = (digest * 131 + ord(character)) & 0xFFFFFFFF
    return "%08x" % digest


def record_slot(record):
    """This record's channel slot, blank for anything written before slots."""
    return str((record or {}).get("slot") or "")


def entry_token(host, record):
    """A manager/bake picker key for one effect record on one host.

    The slot is appended only when the record carries one, so every record
    written before slots existed keeps exactly the token it had.
    """
    slot = record_slot(record)
    base = "%s\x1f%s" % (host_token(host), record.get("code", ""))
    return "%s\x1f%s" % (base, slot) if slot else base


def collect(hosts, validate=True):
    """[(host, label, [records])] for every host that carries motion.

    Hosts with nothing applied are dropped, so a caller can use the length of
    this directly to decide whether to draw a list at all.
    """
    out = []
    for host in hosts or ():
        records = entries(host, validate=validate)
        if records:
            out.append((host, _label_for(host), records))
    return out


def any_applied(objects, scan_limit=64):
    """Cheap "is there anything to bake?" for panel draw code.

    ``collect_for_objects`` parses JSON for every host it finds; this stops at
    the first hit, so the common case - something IS applied - costs one
    dictionary lookup. Panel draw runs on every redraw, which is why this
    exists rather than calling collect and taking its length.

    ``scan_limit`` bounds the miss case, where nothing is applied and there is
    nothing to short-circuit on. Selecting several hundred objects would
    otherwise walk every material and modifier on all of them each redraw. The
    cost of the bound is that a button can stay hidden when only object 65+
    carries motion; the operators remain reachable from search and the
    right-click menu, so nothing becomes unreachable.
    """
    scanned = 0
    for obj in objects or ():
        for host in hosts_for_object(obj):
            if read(host):
                return True
        scanned += 1
        if scanned >= scan_limit:
            break
    return False


def collect_for_objects(objects, validate=True):
    hosts = []
    seen = set()
    for obj in objects or ():
        for host in hosts_for_object(obj):
            key = id(host)
            if key not in seen:
                seen.add(key)
                hosts.append(host)
    return collect(hosts, validate=validate)


def collect_for_scene(scene, validate=True):
    return collect(scene_hosts(scene), validate=validate)


_SOCKET_PATH = re.compile(
    r'^nodes\["(?P<node>(?:[^"\\]|\\.)*)"\]'
    r'\.(?P<side>inputs|outputs)\[(?P<index>\d+)\]\.default_value$'
)
_CUSTOM_PROPERTY = re.compile(r'\["([^"]+)"\]')


def _channel_name(host, data_path):
    """The name an artist would recognise for one driven channel.

    A node socket's own label ("Top Brightness") rather than its index, and a
    custom property's name rather than its bracketed path. Empty when the
    channel has no better name than the path it already shows.
    """
    text = str(data_path or "")
    match = _SOCKET_PATH.match(text)
    if match:
        try:
            node = host.nodes[match.group("node").replace('\\"', '"')]
            sockets = (node.inputs if match.group("side") == "inputs"
                       else node.outputs)
            return str(getattr(sockets[int(match.group("index"))], "name", "")
                       or "")
        except (AttributeError, KeyError, IndexError, TypeError, ReferenceError):
            return ""
    # A bone path also carries a quoted name, so the custom-property rule must
    # not claim it: pose.bones["Arm"].location names a channel on a bone, not a
    # custom property called "Arm". Name it "Arm · Location" so two applies on
    # two bones are told apart by the bone AND a bone's channel is not mistaken
    # for a property.
    bone = _BONE_PATH.search(text)
    if bone:
        tail = text.rsplit(".", 1)[-1]
        if tail and "[" not in tail:
            return "%s · %s" % (bone.group(1), tail.replace("_", " ").title())
    found = _CUSTOM_PROPERTY.findall(text)
    return found[-1] if found and text.endswith('"]') else ""


_BONE_PATH = re.compile(r'pose\.bones\["([^"]+)"\]')
_AXIS_NAMES = ("X", "Y", "Z", "W")


def channel_name(host, data_path, index=-1):
    """One driven channel's name for a menu row -- never blank.

    ``_channel_name`` for a socket or custom property, otherwise the last
    path segment as words plus the axis letter: "Emission Strength",
    "Location Z", "Arm · Rotation Euler X". The raw path is the last resort.
    """
    text = str(data_path or "")
    name = _channel_name(host, data_path)
    if name:
        if _BONE_PATH.search(text) and int(index or -1) >= 0:
            axis = _AXIS_NAMES[index] if index < len(_AXIS_NAMES) else str(index)
            return "%s %s" % (name, axis)
        return name
    tail = text.rsplit(".", 1)[-1]
    words = tail.replace("_", " ").title() if tail and "[" not in tail else (tail or text)
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = -1
    if index >= 0:
        axis = _AXIS_NAMES[index] if index < len(_AXIS_NAMES) else str(index)
        return "%s %s" % (words, axis)
    return words


def channel_label(host, record):
    """Where this record drives, in words. Empty when it has nothing to add.

    Two applications of one recipe on one host are told apart by WHERE they
    drive, so anything listing them has to say where. "Campfire Flicker"
    twice is not a choice an artist can make; "Campfire Flicker \u00b7 Top
    Brightness" and "\u00b7 Master Height" is.
    """
    names = []
    for data_path, _index in paths_of(record):
        name = _channel_name(host, data_path)
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    if len(names) > 3:
        return "%s +%d more" % (", ".join(names[:3]), len(names) - 3)
    return ", ".join(names)


def describe_with_channel(host, record):
    """``describe`` plus the channel, when there is a channel worth naming."""
    label = describe(record)
    channel = channel_label(host, record)
    return "%s \u00b7 %s" % (label, channel) if channel else label


def describe(record):
    """The line a user reads in the bake list.

    Falls back to the raw code when the stored label is missing, so an entry
    written by an older build is still selectable rather than blank.
    """
    template = resolve_template(record)
    if template is not None and template.get("name"):
        return str(template["name"])
    label = str(record.get("label") or "").strip()
    code = str(record.get("code") or "")
    if not label:
        return code or "Unknown effect"
    return label


def resolve_template(record):
    """The catalogue template a stamp refers to, or None if it is unknown."""
    from ...catalogue import templates

    return templates.template_for_effect_id(record.get("code"))


def helper_property_names(host, record):
    """Custom properties this application owns, for removal.

    Matched by the code embedded in the name (``__espresso_BB01_a``) rather
    than by the bare prefix, so clearing one effect cannot take another's
    properties with it when two are applied to the same object.
    """
    code = str(record.get("code") or "")
    if not code or host is None:
        return []
    prefix = "%s%s_" % (HELPER_PREFIX, code)
    try:
        keys = list(host.keys())
    except (AttributeError, TypeError):
        return []
    return [key for key in keys if key.startswith(prefix)]
