"""Independent constraint state for destructive generated-resource rollback.

An Object.copy() loses constraint target pointers when their IDs are deleted.
These descriptors retain original identities and plain RNA values separately,
then resolve targets only after all captured object identities exist again.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix

from .attachments import ATTACHMENTS_PROPERTY


def _plain(value):
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return tuple(_plain(item) for item in value)


def _reference(value):
    if value is None:
        return None
    if not isinstance(value, bpy.types.ID):
        raise TypeError("unsupported constraint pointer %s" % type(value).__name__)
    return {"block": value, "pointer": value.as_pointer(), "name": value.name_full,
            "type": value.bl_rna.identifier}


def _resolve(reference, restored):
    if reference is None:
        return None
    original = reference["block"]
    try:
        if original.as_pointer() == reference["pointer"]:
            return original
    except ReferenceError:
        pass
    replacement = restored.get(reference["pointer"])
    if replacement is not None:
        return replacement
    raise ValueError("missing %s target %r" % (reference["type"], reference["name"]))


def _rna_state(value, label):
    scalars, pointers, collections = {}, {}, {}
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name", "type"}:
            continue
        current = getattr(value, name)
        if prop.type == 'COLLECTION':
            if name == 'targets' and getattr(value, 'type', '') == 'ARMATURE':
                collections[name] = [_rna_state(item, label + " target") for item in current]
            elif len(current):
                raise ValueError("%s has unsupported constraint collection %s" % (label, name))
            continue
        if prop.is_readonly:
            continue
        if prop.type == 'POINTER':
            pointers[name] = _reference(current)
        else:
            scalars[name] = _plain(current)
    return {"values": scalars, "pointers": pointers, "collections": collections}


def capture(obj):
    """Capture object and pose stacks without retaining mutable constraint RNA."""
    stacks = []
    owners = [("", obj)]
    if obj.pose is not None:
        owners.extend((bone.name, bone) for bone in obj.pose.bones)
    for bone_name, owner in owners:
        records = []
        for constraint in owner.constraints:
            label = "%s/%s constraint %s" % (obj.name, bone_name or 'object', constraint.name)
            record = _rna_state(constraint, label)
            record.update(name=constraint.name, type=constraint.type)
            records.append(record)
        stacks.append({"bone": bone_name, "records": records})
    return {"stacks": stacks, "has_attachments": ATTACHMENTS_PROPERTY in obj,
            "attachments": obj.get(ATTACHMENTS_PROPERTY)}


def _owner(obj, bone_name):
    if not bone_name:
        return obj
    bone = obj.pose.bones.get(bone_name) if obj.pose is not None else None
    if bone is None:
        raise ValueError("missing captured pose bone %s on %s" % (bone_name, obj.name))
    return bone


def _write_rna(value, record, restored, label, errors):
    # Target assignment can change constraints' dependent settings; assign
    # pointers first, then the captured scalar/matrix values.
    for name, reference in record["pointers"].items():
        try:
            setattr(value, name, _resolve(reference, restored))
        except Exception as exc:
            errors.append("%s.%s: %s" % (label, name, exc))
    for name, scalar in record["values"].items():
        try:
            # Nested tuples take the RNA column-major assignment path; a
            # Matrix preserves the row-major values captured by iteration.
            if isinstance(getattr(value, name), Matrix):
                scalar = Matrix(scalar)
            setattr(value, name, scalar)
        except Exception as exc:
            errors.append("%s.%s: %s" % (label, name, exc))
    for name, entries in record["collections"].items():
        collection = getattr(value, name)
        for item in list(collection):
            collection.remove(item)
        for index, entry in enumerate(entries):
            child = collection.new()
            _write_rna(child, entry, restored, "%s.%s[%d]" % (label, name, index), errors)


def restore(obj, snapshot, restored, errors):
    """Recreate exact ordered stacks before constraint driver paths are added."""
    for stack in snapshot["stacks"]:
        label = "%s/%s" % (obj.name, stack["bone"] or 'object')
        try:
            owner = _owner(obj, stack["bone"])
            for constraint in list(owner.constraints):
                owner.constraints.remove(constraint)
            for record in stack["records"]:
                constraint = owner.constraints.new(record["type"])
                constraint.name = record["name"]
                _write_rna(constraint, record, restored,
                           label + " constraint " + record["name"], errors)
        except Exception as exc:
            errors.append("%s constraint restore: %s" % (label, exc))
    if snapshot["has_attachments"]:
        obj[ATTACHMENTS_PROPERTY] = snapshot["attachments"]
    elif ATTACHMENTS_PROPERTY in obj:
        del obj[ATTACHMENTS_PROPERTY]


def _verify_rna(value, record, restored, label, errors):
    for name, expected in record["values"].items():
        if _plain(getattr(value, name)) != expected:
            errors.append("%s.%s constraint value mismatch: %r != %r" % (
                label, name, _plain(getattr(value, name)), expected))
    for name, reference in record["pointers"].items():
        try:
            expected = _resolve(reference, restored)
            if getattr(value, name) != expected:
                errors.append("%s.%s constraint target mismatch" % (label, name))
        except Exception as exc:
            errors.append("%s.%s: %s" % (label, name, exc))
    for name, entries in record["collections"].items():
        actual = getattr(value, name)
        if len(actual) != len(entries):
            errors.append("%s.%s constraint collection count mismatch" % (label, name))
        for index, (child, entry) in enumerate(zip(actual, entries)):
            _verify_rna(child, entry, restored, "%s.%s[%d]" % (label, name, index), errors)


def verify(obj, snapshot, restored):
    """Check real RNA after holding copies have been discarded, not clone data."""
    errors = []
    for stack in snapshot["stacks"]:
        label = "%s/%s" % (obj.name, stack["bone"] or 'object')
        try:
            constraints = _owner(obj, stack["bone"]).constraints
            expected_order = [(record["name"], record["type"]) for record in stack["records"]]
            if [(item.name, item.type) for item in constraints] != expected_order:
                errors.append("%s constraint order/type/name mismatch" % label)
                continue
            for constraint, record in zip(constraints, stack["records"]):
                _verify_rna(constraint, record, restored,
                            label + " constraint " + record["name"], errors)
        except Exception as exc:
            errors.append("%s constraint verification: %s" % (label, exc))
    if ((ATTACHMENTS_PROPERTY in obj) != snapshot["has_attachments"]
            or obj.get(ATTACHMENTS_PROPERTY) != snapshot["attachments"]):
        errors.append("%s generated attachment identity mismatch" % obj.name)
    return errors
