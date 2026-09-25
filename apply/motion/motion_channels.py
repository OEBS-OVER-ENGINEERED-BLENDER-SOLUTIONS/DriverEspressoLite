"""Application helpers for templates that own explicit Object channels."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid

import bpy

from . import applied_motion, stack_records, stack_runtime
from ..core import apply_behavior, driver_manager, source_binding
from ..setups import internal_helpers
from ...catalogue import templates
from ...engine import quaternion_channels as _qc
from ...engine.targeting import conflicts
from ...engine.motion_stack.stack import BlendMode, MotionStack, StackLayer
from ...engine import utils


@dataclass(frozen=True)
class MotionTarget:
    owner: object
    data_path: str
    index: int


@dataclass
class MotionApplyResult:
    ok: bool
    message: str
    applied_count: int = 0
    targets: list = field(default_factory=list)
    target_states: list = field(default_factory=list)
    prepared_expressions: list = field(default_factory=list)
    template_data: object = None
    scene_data: object = None
    helper_plans: list = field(default_factory=list)
    helper_resources: list = field(default_factory=list)
    source_entry_data: object = None
    conflict_decision: object = None


def resolve_motion_object(owner):
    """Resolve the Object that should own a multi-channel motion plan."""
    if isinstance(owner, bpy.types.Object):
        return owner
    id_data = getattr(owner, "id_data", None)
    return id_data if isinstance(id_data, bpy.types.Object) else None


def motion_targets_for_object(obj, template, channels=None):
    if not isinstance(obj, bpy.types.Object):
        return []
    if not templates.has_motion_plan(template):
        return []
    source = channels if channels is not None else templates.template_channels(template)
    return [MotionTarget(obj, channel["data_path"], channel["index"]) for channel in source]


# Euler rotation orders Blender accepts on a bone; anything else (Quaternion,
# Axis-Angle) has no rotation_euler channel that actually drives the pose, so a
# rotation_euler driver on it is silently inert.
_EULER_ROTATION_MODES = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


# A pose bone has no delta transform channels -- delta is an Object-level idea.
# A disturbance recipe declares delta_location so it can layer under a travel
# that owns the camera's location; on a bone there is nothing to layer under, so
# it drives the ordinary channel instead. Without this, applying one to a bone
# raised PoseBone.path_from_id("delta_location") not found.
_BONE_PATH_FALLBACK = {
    "delta_location": "location",
    "delta_rotation_euler": "rotation_euler",
    "delta_rotation_quaternion": "rotation_quaternion",
    "delta_scale": "scale",
}


def bone_data_path(channel):
    """The channel's data path as a bone can actually receive it."""
    path = str(channel.get("data_path", ""))
    return _BONE_PATH_FALLBACK.get(path, path)


def motion_targets_for_bone(obj, pose_bone, template, channels=None):
    """Targets that drive a pose bone's own transform channels.

    The driver still lives on the armature Object (bone transforms are stored in
    the Object's animation data), but each data path is prefixed
    ``pose.bones["Name"].`` — produced via ``path_from_id`` so bone names with
    quotes or dots are escaped correctly.

    ``channels`` overrides which of the template's declared channels get a
    target — this is how a disabled channel is left with no target (and so no
    driver) rather than every declared channel always producing one.
    """
    if not isinstance(obj, bpy.types.Object) or pose_bone is None:
        return []
    if not templates.has_motion_plan(template):
        return []
    targets = []
    for channel in (channels if channels is not None else templates.template_channels(template)):
        data_path = pose_bone.path_from_id(bone_data_path(channel))
        targets.append(MotionTarget(obj, data_path, channel["index"]))
    return targets


def effective_channels(template, pose_bone):
    """The channel plan that will actually move a target, given its rotation mode.

    For Object targets (pose_bone=None) and Euler-mode bones this is the
    template's declared Euler plan; those map directly to the property the bone
    reads.

    For Quaternion/Axis-Angle bones a rotation_euler driver is silently inert.
    When the template has Euler rotation channels this function converts them to
    the quaternion form via compose_quaternion_expressions, provided every
    composed expression fits Blender's 255-character driver limit. If the limit
    is exceeded the Euler plan is returned unchanged (bone_euler_block_message
    will then block the apply with an honest explanation).

    props.py always does). At that point axis_role channels are bound to
    concrete rotation_euler indices with any sign flip applied inline. The
    quaternion composition reads those concrete indices, so the resulting
    quaternion represents the same orientation as the resolved Euler angles. No
    special case is needed.

    Additive rest-start: the decision to strip that mode for the quaternion path
    lives in prepare_motion_template_application, not here. This function only
    decides the channel structure.
    """
    euler_chs = templates.template_channels(template)
    if pose_bone is None or pose_bone.rotation_mode in _EULER_ROTATION_MODES:
        # Objects always use Euler. Euler-mode bones drive rotation_euler directly.
        return euler_chs
    # Non-Euler bone: attempt the quaternion plan.
    quat_chs = _qc.quaternion_channels_for(euler_chs)
    if quat_chs is None:
        # Template has no Euler rotation to convert (e.g. location-only).
        return euler_chs
    # Reject if any composed expression exceeds Blender's hard limit.
    if any(
        len(ch["expression"]) > utils.MAX_DRIVER_EXPRESSION_LENGTH
        for ch in quat_chs
        if ch.get("data_path") == "rotation_quaternion"
    ):
        return euler_chs  # too long; bone_euler_block_message will explain
    return quat_chs


def bone_euler_block_message(pose_bone, template):
    """Return a user-facing block message when this bone cannot receive the
    template's Euler rotation, else an empty string.

    A Quaternion/Axis-Angle bone whose template can be converted at build time
    returns an empty string — the apply path routes drivers to
    ``rotation_quaternion`` automatically and no block is needed.  Only bones
    whose expressions exceed the quaternion conversion limit are still blocked,
    and even then ``Set Bone to XYZ Euler`` remains the escape hatch.
    """
    if pose_bone is None:
        return ""
    has_euler = any(
        channel["data_path"] == "rotation_euler"
        for channel in templates.template_channels(template)
    )
    if not has_euler:
        return ""
    if pose_bone.rotation_mode not in _EULER_ROTATION_MODES:
        # Attempt conversion first; only block when it is genuinely impossible.
        # This gate reads the raw template expressions, which is a ROUGH estimate
        # only - measured, building can shrink an expression a lot (a camera
        # template goes 1568 -> 1012) or grow it (a slower template 248 ->
        # 302, because the % rewrite expands). The authoritative check is on the
        # built expressions in prepare_motion_template_application; this one just
        # keeps the panel from promising a conversion that is obviously hopeless.
        converted = _qc.quaternion_channels_for(
            templates.template_channels(template), allow_approximate=True,
        )
        if converted is not None and all(
            len(ch["expression"]) <= utils.MAX_DRIVER_EXPRESSION_LENGTH
            for ch in converted
            if ch.get("data_path") == "rotation_quaternion"
        ):
            return ""
        mode_label = pose_bone.rotation_mode.replace("_", " ").title()
        return (
            f'Bone "{pose_bone.name}" uses {mode_label} rotation. '
            "This template's rotation expressions are too complex to compose into "
            "quaternion form within Blender's 255-character driver limit. "
            'Use "Set Bone to XYZ Euler" to drive it with Euler drivers instead.'
        )
    return ""


def bone_will_use_quaternion_drivers(pose_bone, template):
    """True when applying this template to the bone would create quaternion drivers.

    A Quaternion/Axis-Angle bone with a convertible rotation template gets four
    ``rotation_quaternion`` drivers instead of three ``rotation_euler`` ones.
    Mirrors the budget check in ``bone_euler_block_message`` so UI and apply agree.
    """
    if pose_bone is None or pose_bone.rotation_mode in _EULER_ROTATION_MODES:
        return False
    converted = _qc.quaternion_channels_for(
        templates.template_channels(template), allow_approximate=True,
    )
    if converted is None:
        return False
    return all(
        len(ch["expression"]) <= utils.MAX_DRIVER_EXPRESSION_LENGTH
        for ch in converted
        if ch.get("data_path") == "rotation_quaternion"
    )


def _validate_plan(template, built_channels, targets, channels=None):
    channels = channels if channels is not None else templates.template_channels(template)
    if not templates.has_motion_plan(template):
        return False, "The selected template does not declare a motion channel plan."
    if len(channels) != len(built_channels) or len(channels) != len(targets):
        return False, "The built expression count does not match the template channel plan."
    seen = set()
    for channel, built, target in zip(channels, built_channels, targets):
        if channel["id"] != built.get("id"):
            return False, f"Channel order mismatch at {channel['label']}."
        target_key = (target.data_path, target.index)
        if target_key in seen:
            return False, f"Duplicate motion destination: {target_key}."
        seen.add(target_key)
        supported_paths = {
            "location": {0, 1, 2},
            "rotation_euler": {0, 1, 2},
            "rotation_quaternion": {0, 1, 2, 3},
            "scale": {0, 1, 2},
            "color": {0, 1, 2, 3},
            # Blender's delta channels are a second transform added on top of
            # the first. A recipe that is a DISTURBANCE rather than a move --
            # the operator's hands, a vehicle's ride -- writes here, which is
            # what lets a dolly own ``location`` while a handheld shakes the
            # same camera. Rotation deltas compose OUTSIDE the object's own
            # rotation, so they are supported but no camera recipe uses them:
            # a "pitch response" written there tilts about a world axis.
            "delta_location": {0, 1, 2},
            "delta_rotation_euler": {0, 1, 2},
            "delta_scale": {0, 1, 2},
        }
        if channel["data_path"] not in supported_paths:
            return False, f"Unsupported motion path: {channel['data_path']}."
        if channel["index"] not in supported_paths[channel["data_path"]]:
            return False, f"Unsupported motion index: {channel['index']}."
        valid, message = utils.validate_driver_expression(
            built.get("driver_expression") or built.get("expression", ""), template,
        )
        if not valid:
            return False, f"Invalid {channel['label']} expression: {message}"
        try:
            value = target.owner.path_resolve(target.data_path)
            value[target.index]
        except Exception as exc:
            return False, f"Target cannot receive {channel['label']}: {exc}"
    return True, ""


def _validate_quaternion_plan(template, built_channels, targets):
    """Validate a quaternion-converted motion plan.

    Called after ``quaternion_channels_for`` has rewritten the channel list.
    Replaces ``_validate_plan`` for the converted path because the channel
    ids, count, and paths all differ from the original template's euler spec.
    """
    if len(built_channels) != len(targets):
        return False, "Channel count mismatch after quaternion conversion."
    seen = set()
    for built, target in zip(built_channels, targets):
        dp = built.get("data_path", "")
        idx = int(built.get("index", -1))
        # rotation_quaternion uses indices 0-3 (W X Y Z); passthrough channels
        # keep their own paths and the original supported index ranges.
        if dp == "rotation_quaternion" and idx not in {0, 1, 2, 3}:
            return False, f"Invalid quaternion component index: {idx}."
        target_key = (target.data_path, target.index)
        if target_key in seen:
            return False, f"Duplicate quaternion destination: {target_key}."
        seen.add(target_key)
        valid, message = utils.validate_driver_expression(built.get("expression", ""), template)
        if not valid:
            return False, f"Invalid quaternion expression: {message}"
        try:
            value = target.owner.path_resolve(target.data_path)
            value[target.index]
        except Exception as exc:  # noqa: BLE001
            return False, f"Quaternion target is inaccessible: {exc}"
    return True, ""


def _fits(converted):
    return all(
        len(ch["expression"]) <= utils.MAX_DRIVER_EXPRESSION_LENGTH
        for ch in converted
        if ch.get("data_path") == "rotation_quaternion"
    )


# tan(angle/2) diverges at 180 degrees; 150 leaves the tangent form a wide
# safety margin while covering every swing the catalogue actually produces.
_TANGENT_PEAK_LIMIT = 2.6  # radians


def _peak_rotation_angle(built_channels, template, scene):
    """Largest |angle| any built Euler rotation channel produces, sampled.

    The tangent quaternion form is exact but diverges as an angle nears 180
    degrees, and whether that can happen depends on the artist's parameter
    values - not knowable statically. Sampling ~50 frames across the scene
    range is how the preview graph already characterises a motion, and a
    driver angle that only exceeds the limit BETWEEN samples would need a
    spike faster than anything the catalogue's periodic kernels produce.
    """
    peak = 0.0
    start = int(getattr(scene, "frame_start", 1))
    end = int(getattr(scene, "frame_end", start + 240))
    step = max(1, (end - start) // 48)
    for channel in built_channels:
        if channel.get("data_path") != "rotation_euler":
            continue
        for frame in range(start, end + 1, step):
            try:
                value = utils.evaluate_expression_at_frame(
                    channel["expression"], template, frame,
                )
            except Exception:  # noqa: BLE001 - unevaluable means ungateable
                return float("inf")
            peak = max(peak, abs(float(value)))
    return peak


def _select_quaternion_channels(built_channels, template, scene):
    """The best quaternion form for these built channels, most exact first.

    1. Exact half-angle composition - correct at any angle, but each axis
       appears six times, so long expressions overflow the driver limit.
    2. Tangent composition - the SAME rotation (Blender normalises, so the
       common cos product divides out), each axis appearing once. Gated on the
       sampled peak angle because tan(angle/2) diverges at 180 degrees.
    3. Small-angle approximation - always fits; the only rung that trades
       accuracy, so it is the only one that returns a user-facing note.

    Returns (converted, note) - note is None for the two exact rungs.
    """
    exact = _qc.quaternion_channels_for(built_channels)
    if exact is None:
        return None, None
    if _fits(exact):
        return exact, None

    tangent = _qc.quaternion_channels_for(built_channels, form="tangent")
    if _fits(tangent) and _peak_rotation_angle(
        built_channels, template, scene,
    ) < _TANGENT_PEAK_LIMIT:
        return tangent, None

    small = _qc.quaternion_channels_for(built_channels, form="small")
    note = (
        "Rotation uses a small-angle quaternion approximation "
        "(exact form exceeds Blender's driver length limit). "
        "Accurate for subtle motion; large swings drift from the Euler result."
    )
    return small, note


def _channel_tail(data_path):
    """The channel-level path of a stored target, bone prefix stripped:
    ``pose.bones["Suspension"].location`` reads as ``location``."""
    text = str(data_path or "")
    if text.startswith("pose.bones[") and "]." in text:
        return text.rsplit("].", 1)[-1]
    return text


def _target_keys(targets):
    return [
        (_channel_tail(target.get("data_path")), int(target.get("index", -1)))
        for target in (targets or ())
    ]


def stored_targets_follow_plan(template, targets):
    """Whether every stored target is a channel this plan can rebuild.

    Cheap and structural -- it runs from panel draw. Allows the quaternion
    form (four ``rotation_quaternion`` components standing in for the three
    Euler channels of a Quaternion or Axis-Angle bone) and a subset of the
    plan (a channel disabled before Apply has no target). Counting targets
    against channels, which is what this replaces, called both of those a
    mismatch and greyed every Live control on a perfectly healthy motion.
    """
    keys = set()
    has_euler = False
    for channel in templates.template_channels(template):
        path = str(channel.get("data_path", ""))
        index = int(channel.get("index", -1))
        keys.add((path, index))
        keys.add((_BONE_PATH_FALLBACK.get(path, path), index))
        if _BONE_PATH_FALLBACK.get(path, path) == "rotation_euler":
            has_euler = True
    if has_euler:
        keys.update(("rotation_quaternion", index) for index in range(4))
    wanted = _target_keys(targets)
    return bool(wanted) and all(key in keys for key in wanted)


def channels_for_stored_targets(built_channels, template, scene, targets, *,
                                values=None, enabled_channel_ids=None):
    """Match freshly built channels to the targets a remembered entry holds.

    Apply may have rewritten three Euler rotation channels into four quaternion
    components (a Quaternion or Axis-Angle bone) and may have left a disabled
    channel without a target. Live rebuilds from the template, so it has to
    repeat both decisions before it can hand one expression per stored target
    to ``apply_expression_to_entry``; otherwise the rebuilt channels no longer
    match the stored ones and the update is refused.

    A disabled channel on an Euler host simply has no target and drops out
    here. On a quaternion bone the four components look the same whichever
    axes fed them, so the caller passes ``enabled_channel_ids`` -- the same
    per-template toggle Apply read -- and ``values``, for channels gated by
    a parameter (``enabled_if``), exactly as Apply filtered them.

    Returns ``(channels, note)`` in target order, ``note`` being the
    small-angle approximation notice when that rung was needed, or
    ``(None, reason)`` when the stored targets are not this plan's.
    """
    wanted = _target_keys(targets)
    if not wanted:
        return None, "The remembered motion has no targets."
    channels = list(built_channels)
    if enabled_channel_ids is not None:
        enabled = set(enabled_channel_ids)
        channels = [ch for ch in channels if ch.get("id") in enabled]
    if values is not None:
        channels = [
            ch for ch in channels
            if not ch.get("enabled_if") or all(
                bool(values.get(token)) == bool(required)
                for token, required in ch["enabled_if"].items()
            )
        ]
    if not channels:
        return None, "Every motion channel is disabled."
    note = None
    if any(path == "rotation_quaternion" for path, _index in wanted):
        converted, note = _select_quaternion_channels(channels, template, scene)
        if converted is None:
            return None, (
                "The stored quaternion targets do not belong to this motion's rotation plan."
            )
        over = [
            ch for ch in converted
            if ch.get("data_path") == "rotation_quaternion"
            and len(ch["expression"]) > utils.MAX_DRIVER_EXPRESSION_LENGTH
        ]
        if over:
            return None, (
                "The composed quaternion expression is %d characters, exceeding "
                "Blender's %d-character limit."
                % (len(over[0]["expression"]), utils.MAX_DRIVER_EXPRESSION_LENGTH)
            )
        # Same contract as Apply: a quaternion component is a whole
        # orientation, never an offset on the bone's current value.
        for ch in converted:
            if ch.get("data_path") == "rotation_quaternion":
                ch["application_space"] = "ABSOLUTE"
                ch.setdefault("output_baseline", None)
                ch.setdefault("additive_profile", None)
                ch.setdefault("warnings", [])
        channels = converted
    by_key = {}
    for ch in channels:
        path = str(ch.get("data_path", ""))
        index = int(ch.get("index", -1))
        by_key.setdefault((path, index), ch)
        by_key.setdefault((_BONE_PATH_FALLBACK.get(path, path), index), ch)
    ordered = []
    for key in wanted:
        channel = by_key.get(key)
        if channel is None:
            return None, "The applied targets no longer match this motion's declared channels."
        ordered.append(channel)
    return ordered, note


def _conflict_policy():
    """Read the preference without making the apply layer depend on UI state."""
    try:
        prefs = bpy.context.preferences.addons[__package__.split(".apply")[0]].preferences
        return getattr(prefs, "conflict_policy", conflicts.ConflictPolicy.AUTO_REPLACE)
    except (AttributeError, KeyError, TypeError):
        return conflicts.ConflictPolicy.AUTO_REPLACE


def _target_channel_key(target):
    return "%s[%s]" % (target.data_path, int(target.index))


def _claims_for_targets(obj, targets):
    """Build managed/unmanaged claims for the target object, without mutation."""
    requested = {_target_channel_key(target) for target in targets}
    records = applied_motion.entries(obj, validate=True)
    claims = []
    managed_keys = set()
    for record in records:
        effect_id = str(record.get("code") or "")
        label = str(record.get("label") or effect_id)
        for path in record.get("paths") or ():
            if not path:
                continue
            key = "%s[%s]" % (str(path[0]), int(path[1]) if len(path) > 1 else -1)
            if key not in requested:
                continue
            managed_keys.add(key)
            animation = getattr(obj, "animation_data", None)
            curve = animation.drivers.find(str(path[0]), index=int(path[1])) if animation else None
            layer_supported = bool(
                curve is not None
                and not record.get("extras", {}).get("resource_ids")
            )
            claims.append(conflicts.ChannelClaim(
                channel=key,
                effect_id=effect_id,
                label=label,
                managed=True,
                layer_supported=layer_supported,
                resource_ids=tuple(record.get("extras", {}).get("resource_ids", ())),
            ))
    animation_data = getattr(obj, "animation_data", None)
    for fcurve in list(getattr(animation_data, "drivers", ()) or ()):
        key = "%s[%s]" % (fcurve.data_path, int(fcurve.array_index))
        if key in requested and key not in managed_keys:
            claims.append(conflicts.ChannelClaim(
                channel=key,
                effect_id="",
                label="Unmanaged driver",
                managed=False,
                layer_supported=False,
            ))
    return claims


def _conflict_preflight(obj, targets, template):
    effect_id = str((template or {}).get("effect_id") or "")
    label = str((template or {}).get("name") or effect_id or "Espresso effect")
    incoming = tuple(_target_channel_key(target) for target in targets)
    report = conflicts.inspect_target(
        target_label=getattr(obj, "name", "Target"),
        incoming_effect_id=effect_id,
        incoming_label=label,
        incoming_channels=incoming,
        existing_claims=_claims_for_targets(obj, targets),
    )
    decision = conflicts.resolve(report, _conflict_policy())
    return decision


def _foreign_motion_conflict(targets):
    for target in targets:
        foreign = driver_manager.foreign_live_motion_for_channel(
            target.owner, target.data_path, target.index,
        )
        if foreign is not None:
            return (
                "%s[%d] has motion from an unavailable edition (%s); "
                "it was left unchanged."
                % (target.data_path, target.index, foreign["label"])
            )
    return ""


def prepare_motion_template_application(
    obj,
    template,
    built_channels,
    *,
    scene=None,
    rest_start_mode=utils.REST_START_OFF,
    pose_bone=None,
    enabled_channel_ids=None,
    template_values=None,
    source_entry=None,
):
    """Preflight a complete motion plan without writing any drivers.

    When ``pose_bone`` is given, every channel is routed to that bone's own
    transform (``pose.bones["Name"].…``) on the armature ``obj`` instead of the
    Object's transform.

    ``enabled_channel_ids`` is the artist's per-template channel toggle (see
    ui.props.enabled_channel_ids_for_template). ``None`` means "no toggles are
    in play": every declared channel applies. A disabled channel gets no target
    and so no driver at all - not a driver forced to a constant - so whatever
    the artist already has on that property (a hand-keyed value, or nothing) is
    left alone.

    Filtering happens HERE, once, before quaternion routing: quaternion
    composition already treats a channel missing from ``built_channels`` as
    "this axis was never authored" (quaternion_channels.quaternion_channels_for
    keys off dict.get returning None for an absent index), so a disabled Euler
    axis composes correctly into the quaternion with no special-casing.
    """
    scene = scene or bpy.context.scene
    if source_binding.needs_input_source(template):
        if not source_entry:
            return MotionApplyResult(
                False,
                "Choose an Espresso input source before applying this motion set.",
            )
        descriptor, reason = source_binding.resolve_source(source_entry)
        if descriptor is None:
            return MotionApplyResult(False, reason or "The Espresso input source is invalid.")
    # Anything the quaternion path silently changed about what the artist
    # asked for, surfaced on the success message rather than swallowed.
    quaternion_notes = []
    helper_values = dict(template_values or {})
    if template.get("internal_helpers"):
        if rest_start_mode == utils.REST_START_ADDITIVE and any(
            item.get("token") == "START" for item in template.get("params", [])
        ):
            helper_values["START"] = int(getattr(scene, "frame_current", 1))
            # A helper-backed motion has one clock shared by its private DAG
            # and public channels. Rebuild both from the same START value;
            # retiming only the helpers leaves the public drop/launch formula
            # on scene time and causes an immediate transform jump.
            built_channels = utils.build_template_expressions(
                template, helper_values, scene,
            )
            quaternion_notes.append(
                "Rest Start restarted the native motion helpers at the current frame."
            )
        # Native helper graphs already implement restart/scene-phase semantics;
        # wrapping only the public expression would re-time one layer while the
        # helpers kept another clock.
        rest_start_mode = utils.REST_START_OFF

    filtered_channels = None
    if enabled_channel_ids is not None:
        enabled = set(enabled_channel_ids)
        built_channels = [ch for ch in built_channels if ch.get("id") in enabled]
        filtered_channels = [
            ch for ch in templates.template_channels(template) if ch.get("id") in enabled
        ]
        if not built_channels:
            return MotionApplyResult(
                False,
                "Every motion channel is disabled. Enable at least one in the "
                "Motion Channels list before applying.",
            )

    if pose_bone is not None and pose_bone.rotation_mode not in _EULER_ROTATION_MODES:
        # Bone uses Quaternion or Axis-Angle. Attempt build-time conversion so
        # drivers land on rotation_quaternion instead of the inert rotation_euler.
        converted, approximation_note = _select_quaternion_channels(
            built_channels, template, scene,
        )
        if converted is not None:
            if approximation_note:
                # Only the small-angle rung carries a note - the exact and
                # tangent rungs are the same rotation as the Euler form. A
                # quiet approximation is a quiet lie, so it reaches the
                # status message.
                quaternion_notes.append(approximation_note)
            # Check the budget on the BUILT expressions (tokens already resolved).
            # bone_euler_block_message does the same gate on raw template expressions
            # but built ones may be slightly longer; validate here to be sure.
            over = [
                ch for ch in converted
                if ch.get("data_path") == "rotation_quaternion"
                and len(ch["expression"]) > utils.MAX_DRIVER_EXPRESSION_LENGTH
            ]
            if over:
                return MotionApplyResult(
                    False,
                    f'The composed quaternion expression for "{pose_bone.name}" is '
                    f"{len(over[0]['expression'])} characters, exceeding Blender's "
                    f"{utils.MAX_DRIVER_EXPRESSION_LENGTH}-char limit. "
                    'Use "Set Bone to XYZ Euler" instead.',
                )
            # Euler rotation channels rewritten to four quaternion components.
            # Passthrough channels (location, scale) are preserved unchanged.
            #
            # application_space = "ABSOLUTE": quaternion components are complete
            # orientations, not offsets. Adding the motion_anchor (the bone's
            # current quaternion component value) to each independently would
            # violate the unit-quaternion constraint W²+X²+Y²+Z²=1 and corrupt
            # the rotation. ABSOLUTE skips the anchor-offset step entirely.
            for ch in converted:
                if ch.get("data_path") == "rotation_quaternion":
                    ch["application_space"] = "ABSOLUTE"
                    ch.setdefault("output_baseline", None)
                    ch.setdefault("additive_profile", None)
                    ch.setdefault("warnings", [])
            built_channels = converted
            # Additive and Offset-Only rest-start modes independently offset each
            # quaternion component value, which breaks W²+X²+Y²+Z²=1 and produces
            # a bogus pose. The composed expressions are absolute orientations, so
            # REST_START_OFF applies the authored motion correctly. Strip the mode
            # for this apply; the artist can re-apply with rest-start on an Euler
            # bone if that behaviour is essential.
            #
            # This MUST be reported. Silently downgrading the artist's Rest Start
            # setting is the same class of bug as the inert rotation_euler driver
            # this whole path exists to fix: the apply succeeds, and the result is
            # quietly not what was asked for.
            if rest_start_mode != utils.REST_START_OFF:
                quaternion_notes.append(
                    "Rest Start was turned off for this apply: a quaternion is a "
                    "whole orientation, so offsetting its four components "
                    "separately would produce an invalid rotation. Set the bone "
                    "to XYZ Euler if you need Rest Start on this template."
                )
            rest_start_mode = utils.REST_START_OFF
            targets = [
                MotionTarget(obj, pose_bone.path_from_id(bone_data_path(ch)), ch["index"])
                for ch in built_channels
            ]
            valid, reason = _validate_quaternion_plan(template, built_channels, targets)
        else:
            # No Euler rotation in this template (e.g. location-only) — the
            # channels are valid as-is on any rotation mode.
            targets = motion_targets_for_bone(obj, pose_bone, template, channels=filtered_channels)
            valid, reason = _validate_plan(template, built_channels, targets, channels=filtered_channels)
        if not valid:
            return MotionApplyResult(False, reason)
    else:
        # Euler bone, or object-level apply (pose_bone is None).
        if pose_bone is not None:
            block = bone_euler_block_message(pose_bone, template)
            if block:
                return MotionApplyResult(False, block)
            targets = motion_targets_for_bone(obj, pose_bone, template, channels=filtered_channels)
        else:
            targets = motion_targets_for_object(obj, template, channels=filtered_channels)
        valid, reason = _validate_plan(template, built_channels, targets, channels=filtered_channels)
        if not valid:
            return MotionApplyResult(False, reason)
    foreign_conflict = _foreign_motion_conflict(targets)
    if foreign_conflict:
        return MotionApplyResult(False, foreign_conflict, targets=targets)
    conflict_decision = _conflict_preflight(obj, targets, template)
    if not conflict_decision.allowed:
        return MotionApplyResult(
            False,
            conflicts.format_conflict_message(conflict_decision),
            targets=targets,
            template_data=template,
            scene_data=scene,
            source_entry_data=source_entry,
            conflict_decision=conflict_decision,
        )
    states = []
    for target, built in zip(targets, built_channels):
        anchor = apply_behavior.read_target_current_value(
            target.owner, target.data_path, target.index,
        )
        state = apply_behavior.capture_target_rest_state(
            target,
            built["expression"],
            template,
            scene,
            rest_start_mode,
            output_baseline=built.get("output_baseline"),
            additive_profile=built.get("additive_profile"),
        )
        state = dict(state)
        state["motion_anchor"] = anchor
        state["application_space"] = built.get("application_space", "RELATIVE")
        state["motion_data_path"] = target.data_path
        states.append(state)

    prepared = []
    for target, built, state in zip(targets, built_channels, states):
        applied_expression = built.get("driver_expression") or built["expression"]
        wrapped = utils.wrap_expression_with_rest_state(applied_expression, template, state)
        if (
            state.get("application_space") == "RELATIVE"
            and state.get("mode", utils.REST_START_OFF) == utils.REST_START_OFF
        ):
            anchor = float(state.get("motion_anchor", 0.0))
            if target.data_path == "scale":
                if anchor != 1.0:
                    wrapped = f"{utils._format_driver_literal(anchor)}*({wrapped})"
            elif anchor != 0.0:
                wrapped = f"{utils._format_driver_literal(anchor)}+({wrapped})"
        valid, message = utils.validate_driver_expression(wrapped, template, scene)
        if not valid:
            return MotionApplyResult(False, message, 0, targets, states)
        prepared.append(wrapped)

    if template.get("internal_helpers") or template.get("sample_internal_helpers"):
        return MotionApplyResult(False, "This edition does not support helper-based recipes.", 0, targets, states)
    helper_plans = []

    summary = f"Applied {template['name']} to {len(prepared)} motion channels."
    if quaternion_notes:
        summary = "%s %s" % (summary, " ".join(quaternion_notes))
    return MotionApplyResult(
        True,
        summary,
        0,
        targets,
        states,
        prepared,
        template,
        scene,
        helper_plans,
        [],
        source_entry,
        conflict_decision,
    )


def _stamp_applied_motion(template, stamped):
    """Record what was just applied, so it can be found again by name.

    Stamping here rather than in the operators means every caller is covered by
    one hook - there are thirteen places that finish an apply, and a record kept
    by twelve of them is worse than none, because the bake list would look
    complete while silently omitting whatever the thirteenth wrote.

    Never fatal. A motion that is applied but unstamped still works; it just
    will not appear in the bake list. Failing the apply over a bookkeeping
    problem would be the worse trade.
    """
    code = (template or {}).get("effect_id") or ""
    if not code or not stamped:
        return
    label = (template or {}).get("name") or code
    metadata_keys = (
        "id", "application_mode_id", "application_mode_label", "master_id",
        "legacy_id", "compatibility_origin", "route", "delivery",
        "motion_class", "public_visibility", "parameter_mapping",
        "profile_schema", "channel_roles", "camera_stage", "camera_modes",
        "camera_control_groups", "causal_controls", "release_stage",
    )
    extras = {
        ("template_id" if key == "id" else key): template[key]
        for key in metadata_keys
        if (template or {}).get(key) not in (None, "", (), [], {})
    }
    by_host = {}
    for host, data_path, index in stamped:
        if host is None:
            continue
        by_host.setdefault(id(host), (host, []))[1].append([data_path, int(index)])
    for host, paths in by_host.values():
        try:
            applied_motion.remember(host, code, label, paths, extras=extras)
        except Exception:
            pass


def commit_prepared_motion(prepared_result):
    """Write a previously preflighted motion plan."""
    if not prepared_result.ok:
        return prepared_result

    foreign_conflict = _foreign_motion_conflict(prepared_result.targets)
    if foreign_conflict:
        return MotionApplyResult(False, foreign_conflict, targets=prepared_result.targets)

    if prepared_result.helper_plans:
        return MotionApplyResult(False, "This edition cannot install motion helpers.")

    applied = 0
    template = prepared_result.template_data
    scene = prepared_result.scene_data or bpy.context.scene
    decision = prepared_result.conflict_decision
    if decision is not None and decision.action is conflicts.DecisionAction.LAYER:
        return _commit_layered_motion(prepared_result)
    if decision is not None and decision.action in {
        conflicts.DecisionAction.UPDATE,
        conflicts.DecisionAction.REPLACE,
    }:
        # driver_add cannot safely overwrite an existing F-curve in every RNA
        # owner. Remove only channels already claimed by Espresso; unmanaged
        # data would have been blocked during preflight.
        for target in prepared_result.targets:
            try:
                target.owner.driver_remove(target.data_path, target.index)
            except (AttributeError, RuntimeError):
                pass
    helper_resources = []
    created_targets = []
    stamped = []
    for target, wrapped in zip(
        prepared_result.targets,
        prepared_result.prepared_expressions,
    ):
        try:
            result = target.owner.driver_add(target.data_path, target.index)
        except Exception as exc:
            internal_helpers.cleanup(helper_resources)
            return MotionApplyResult(
                False,
                f"Could not add {target.data_path}[{target.index}] driver: {exc}",
                applied,
                prepared_result.targets,
                prepared_result.target_states,
                prepared_result.prepared_expressions,
                template,
                scene,
                prepared_result.helper_plans,
            )
        fcurves = result if isinstance(result, list) else [result]
        for fcurve in fcurves:
            if fcurve is None:
                continue
            ok, message = source_binding.bind_required_variables(
                fcurve.driver,
                template,
                prepared_result.source_entry_data,
                owner_object=resolve_motion_object(target.owner),
            )
            if not ok:
                try:
                    target.owner.driver_remove(target.data_path, target.index)
                except Exception:
                    pass
                for created in created_targets:
                    try:
                        created.owner.driver_remove(created.data_path, created.index)
                    except Exception:
                        pass
                internal_helpers.cleanup(helper_resources)
                return MotionApplyResult(False, message, applied)
            ok, message = utils.assign_driver_expression(
                fcurve.driver, wrapped, template, scene,
            )
            if not ok:
                for created in created_targets:
                    try:
                        created.owner.driver_remove(created.data_path, created.index)
                    except Exception:
                        pass
                internal_helpers.cleanup(helper_resources)
                return MotionApplyResult(
                    False,
                    message,
                    applied,
                    prepared_result.targets,
                    prepared_result.target_states,
                    prepared_result.prepared_expressions,
                    template,
                    scene,
                    prepared_result.helper_plans,
                )
            applied += 1
            created_targets.append(target)
            # Record against the F-curve rather than the target: a driver on a
            # node socket lands on the material's node tree with a rewritten
            # path, so target.data_path would not find it again.
            stamped.append(
                (
                    getattr(target.owner, "id_data", target.owner),
                    fcurve.data_path,
                    fcurve.array_index,
                )
            )

    if not applied:
        return MotionApplyResult(False, "No motion channels were driven.")

    _stamp_applied_motion(template, stamped)
    return MotionApplyResult(
        True,
        prepared_result.message.replace(
            f"{len(prepared_result.prepared_expressions)} motion channels",
            f"{applied} motion channels",
        ),
        applied,
        prepared_result.targets,
        prepared_result.target_states,
        prepared_result.prepared_expressions,
        template,
        scene,
        prepared_result.helper_plans,
        helper_resources,
        prepared_result.source_entry_data,
    )


def _existing_record_for_path(owner, data_path, index):
    for record in applied_motion.entries(owner, validate=True):
        if (data_path, int(index)) in applied_motion.paths_of(record):
            return record
    return None


def _motion_stack_for_target(target, incoming_expression, template, incoming_variables=()):
    owner = target.owner
    animation = getattr(owner, "animation_data", None)
    curve = animation.drivers.find(target.data_path, index=target.index) if animation else None
    if curve is None:
        raise ValueError("This target no longer has a driver to layer.")
    record = _existing_record_for_path(owner, target.data_path, target.index)
    if record is None:
        raise ValueError("The existing Espresso effect has no live ownership record.")
    payload = stack_records.stack_from_extras(record.get("extras", {}))
    channel = _target_channel_key(target)
    if payload:
        motion = MotionStack.from_dict(payload)
        layers = list(motion.layers)
        stack_id = motion.stack_id
    else:
        layers = [StackLayer(
            layer_id="layer.%s" % uuid.uuid4().hex[:12],
            effect_id=str(record.get("code") or "existing"),
            label=str(record.get("label") or "Existing motion"),
            channel=channel,
            expression=str(curve.driver.expression),
            blend=BlendMode.REPLACE,
            variables=stack_runtime.serialize_driver_variables(curve.driver),
        )]
        stack_id = "stack.%s" % uuid.uuid4().hex[:16]
    blend = BlendMode.MULTIPLY if target.data_path == "scale" else BlendMode.ADD
    layers.append(StackLayer(
        layer_id="layer.%s" % uuid.uuid4().hex[:12],
        effect_id=str((template or {}).get("effect_id") or "incoming"),
        label=str((template or {}).get("name") or "Incoming motion"),
        channel=channel,
        expression=str(incoming_expression),
        blend=blend,
        variables=tuple(incoming_variables or ()),
    ))
    return MotionStack(stack_id=stack_id, layers=tuple(layers))


def _incoming_stack_variables(target, prepared_result):
    """Resolve an incoming source binding without altering the real target driver."""
    template = prepared_result.template_data or {}
    if not template.get("requires_driver_variables"):
        return ()
    key = "__espresso_stack_probe_%s" % uuid.uuid4().hex[:10]
    owner = target.owner
    owner[key] = 0.0
    curve = None
    try:
        curve = owner.driver_add('["%s"]' % key)
        ok, message = source_binding.bind_required_variables(
            curve.driver, template, prepared_result.source_entry_data,
            owner_object=resolve_motion_object(owner),
        )
        if not ok:
            raise ValueError(message)
        return stack_runtime.serialize_driver_variables(curve.driver)
    finally:
        if curve is not None:
            try:
                owner.driver_remove('["%s"]' % key)
            except (AttributeError, RuntimeError):
                pass
        if key in owner:
            del owner[key]


def _commit_layered_motion(prepared_result):
    """Commit only the exact native-driver stack slice supported today."""
    if prepared_result.helper_plans:
        return MotionApplyResult(
            False,
            "This motion uses an internal helper graph and cannot yet be auto-layered exactly.",
        )
    snapshots = []
    stacks = []
    try:
        for target, expression in zip(
            prepared_result.targets, prepared_result.prepared_expressions,
        ):
            animation = getattr(target.owner, "animation_data", None)
            curve = animation.drivers.find(target.data_path, index=target.index) if animation else None
            snapshots.append((
                target, stack_runtime._copy_driver_state(curve),
                applied_motion.read(target.owner),
            ))
            stacks.append((target, _motion_stack_for_target(
                target, expression, prepared_result.template_data,
                _incoming_stack_variables(target, prepared_result),
            )))
        applied = []
        for target, motion in stacks:
            ok, message = stack_runtime.apply(
                target.owner, target.data_path, target.index, motion,
                expression_limit=utils.MAX_DRIVER_EXPRESSION_LENGTH,
            )
            if not ok:
                raise ValueError(message)
            applied.append((target, motion))
    except Exception as exc:
        for target, motion in locals().get("applied", ()):
            stack_runtime.clear(target.owner, motion.stack_id)
        for target, state, records in snapshots:
            try:
                target.owner.driver_remove(target.data_path, target.index)
            except (AttributeError, RuntimeError):
                pass
            stack_runtime._restore_driver(target.owner, target.data_path, target.index, state)
            applied_motion.write(target.owner, records)
        return MotionApplyResult(False, "Motion Stack apply rolled back: %s" % exc)
    return MotionApplyResult(
        True,
        "Layered %s over the existing motion on %d channel(s)." % (
            prepared_result.template_data.get("name", "motion"), len(applied),
        ),
        len(applied), prepared_result.targets, prepared_result.target_states,
        prepared_result.prepared_expressions, prepared_result.template_data,
        prepared_result.scene_data, source_entry_data=prepared_result.source_entry_data,
    )


def apply_motion_template_to_object(
    obj,
    template,
    built_channels,
    *,
    scene=None,
    rest_start_mode=utils.REST_START_OFF,
    pose_bone=None,
    enabled_channel_ids=None,
    template_values=None,
    source_entry=None,
):
    """Preflight and apply a complete template plan to one object or bone."""
    prepared = prepare_motion_template_application(
        obj,
        template,
        built_channels,
        scene=scene,
        rest_start_mode=rest_start_mode,
        pose_bone=pose_bone,
        enabled_channel_ids=enabled_channel_ids,
        template_values=template_values,
        source_entry=source_entry,
    )
    return commit_prepared_motion(prepared)
