"""Lossless editable FCurve state for transaction rollback (Blender 4.2+).

Driver target identities remain handled by clear_snapshot/live_controls. RNA
introspection preserves version-specific editable fields; collection contents
need explicit reconstruction because RNA marks the collections read-only.
"""

from __future__ import annotations


def _values(block, skip=()):
    values = {}
    if block is None:
        return values
    for prop in block.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in skip or prop.type in {"POINTER", "COLLECTION"}:
            continue
        value = getattr(block, name)
        values[name] = tuple(value) if getattr(prop, "is_array", False) else value
    return values


def _assign(block, values, skip=()):
    for name, value in values.items():
        # Some RNA fields are dynamically read-only (e.g. TRANSFORMS id_type)
        # despite their static property metadata. Already correct values need
        # no setter; a genuinely differing unwriteable value must still fail.
        if name not in skip and getattr(block, name) != value:
            setattr(block, name, value)


def capture(curve):
    """Capture editable RNA, ordered modifiers, keys and samples independently."""
    modifiers = []
    for modifier in curve.modifiers:
        modifiers.append({
            "type": modifier.type,
            "values": _values(modifier),
            "control_points": [
                _values(point) for point in getattr(modifier, "control_points", ())
            ],
        })
    group = curve.group
    return {
        "values": _values(curve, {"data_path", "array_index"}),
        "group": group,
        "keys": [_values(key, {"co_ui"}) for key in curve.keyframe_points],
        "samples": [tuple(point.co) for point in curve.sampled_points],
        "modifiers": modifiers,
        "driver_values": _values(curve.driver),
        "target_values": [
            [_values(target) for target in variable.targets]
            for variable in getattr(curve.driver, "variables", ())
        ],
    }


def restore(curve, state):
    """Restore in place; propagate failures for transaction verification."""
    # Rebuild only changed curves. The caller leaves identical curves untouched.
    for modifier in list(curve.modifiers):
        curve.modifiers.remove(modifier)
    if curve.sampled_points:
        curve.convert_to_keyframes(0, 1)
    curve.keyframe_points.clear()
    keys = state["keys"]
    if keys:
        curve.keyframe_points.add(len(keys))
        for key, values in zip(curve.keyframe_points, keys):
            key.co = values["co"]
            _assign(key, values, {"co", "handle_left", "handle_right"})
        curve.update()
        # update() recalculates automatic handles. Preserve the captured artist
        # handle coordinates after sorting/evaluation preparation.
        for key, values in zip(curve.keyframe_points, keys):
            key.handle_left = values["handle_left"]
            key.handle_right = values["handle_right"]
    samples = state["samples"]
    if samples:
        # RNA exposes no sampled_points.add(). Allocate with the native converter
        # then assign every captured coordinate, including fractional sample X.
        curve.keyframe_points.insert(0, 0)
        curve.keyframe_points.insert(len(samples), 0)
        curve.convert_to_samples(0, len(samples))
        if len(curve.sampled_points) != len(samples):
            raise RuntimeError("Could not allocate captured FCurve samples")
        curve.sampled_points.foreach_set("co", [value for point in samples for value in point])
    for item in state["modifiers"]:
        modifier = curve.modifiers.new(item["type"])
        if modifier is None:
            raise RuntimeError("Could not restore FCurve modifier " + item["type"])
        values = item["values"]
        # Generator mode and degree resize its coefficient array.
        for name in ("mode", "poly_order"):
            if name in values:
                setattr(modifier, name, values[name])
        # Range setters clamp against the opposing endpoint. Establish the end
        # first, then replay the complete values after both bounds exist.
        for name in ("frame_end", "frame_start"):
            if name in values:
                setattr(modifier, name, values[name])
        _assign(modifier, values, {"active"})
        for point_state in item["control_points"]:
            point = modifier.control_points.add(point_state["frame"])
            _assign(point, point_state)
    # Creating a modifier makes it active; set the captured active item last.
    for modifier, item in zip(curve.modifiers, state["modifiers"]):
        if item["values"].get("active", False):
            modifier.active = True
    for variable, targets in zip(getattr(curve.driver, "variables", ()), state["target_values"]):
        for target, values in zip(variable.targets, targets):
            # The shared driver restore resolves ID pointers before this step.
            _assign(target, values)
    _assign(curve.driver, state["driver_values"])
    if curve.group != state["group"]:
        curve.group = state["group"]
    _assign(curve, state["values"])


def errors(curve, expected, label):
    """Detect incomplete restore in every captured FCurve component."""
    try:
        actual = capture(curve)
    except Exception as exc:
        return [f"could not verify FCurve {label}: {exc}"]
    return [
        f"FCurve {label} {name} mismatch"
        for name, value in expected.items()
        if actual.get(name) != value
    ]
