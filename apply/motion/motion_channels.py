"""Application helpers for templates that own explicit Object channels."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import uuid

import bpy

from . import applied_motion, stack_records, stack_runtime
from ..core import apply_behavior, target_memory
from ...catalogue import templates
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
        supported_paths = {"color": {0, 1, 2, 3}}
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


def _target_keys(targets):
    return [
        (str(target.get("data_path") or ""), int(target.get("index", -1)))
        for target in (targets or ())
    ]


def stored_targets_follow_plan(template, targets):
    """Whether every stored target is a channel this plan can rebuild.

    Cheap and structural -- it runs from panel draw. Allows a subset of the plan
    (a channel disabled before Apply has no target). Counting targets against
    channels, which is what this replaces, called that a mismatch and greyed
    every Live control on a perfectly healthy motion.
    """
    keys = {
        (str(channel.get("data_path", "")), int(channel.get("index", -1)))
        for channel in templates.template_channels(template)
    }
    wanted = _target_keys(targets)
    return bool(wanted) and all(key in keys for key in wanted)


def channels_for_stored_targets(built_channels, template, scene, targets, *,
                                enabled_channel_ids=None):
    """Match freshly built channels to the targets a remembered entry holds.

    Apply may have left a disabled channel without a target. Live rebuilds from the
    template, so it repeats that decision before handing one expression per stored
    target to ``apply_expression_to_entry``; otherwise the rebuilt channels would not
    match the stored ones and the update would be refused. Returns ``(channels, note)``
    in target order, or ``(None, reason)`` when the stored targets are not this plan's.
    """
    wanted = _target_keys(targets)
    if not wanted:
        return None, "The remembered motion has no targets."
    channels = list(built_channels)
    if enabled_channel_ids is not None:
        enabled = set(enabled_channel_ids)
        channels = [ch for ch in channels if ch.get("id") in enabled]
    if not channels:
        return None, "Every motion channel is disabled."
    by_key = {}
    for ch in channels:
        by_key.setdefault((str(ch.get("data_path", "")), int(ch.get("index", -1))), ch)
    ordered = []
    for key in wanted:
        channel = by_key.get(key)
        if channel is None:
            return None, "The applied targets no longer match this motion's declared channels."
        ordered.append(channel)
    return ordered, None


def _conflict_policy():
    """Read the preference without making the apply layer depend on UI state."""
    try:
        prefs = bpy.context.preferences.addons[__package__.split(".apply")[0]].preferences
        return getattr(prefs, "conflict_policy", conflicts.ConflictPolicy.AUTO_REPLACE)
    except (AttributeError, KeyError, TypeError):
        return conflicts.ConflictPolicy.AUTO_REPLACE


def _newest_layer(record):
    """The top layer of a Motion Stack record, or None for any other record."""
    payload = stack_records.stack_from_extras((record or {}).get("extras", {}))
    layers = MotionStack.from_dict(payload).layers if payload else ()
    return layers[-1] if layers else None


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
        newest = _newest_layer(record)
        if newest is not None:
            # A Motion Stack answers to the recipe layered on top, so applying
            # that recipe again updates its layer instead of piling on a copy.
            effect_id, label = newest.effect_id, newest.label
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


def prepare_motion_template_application(
    obj,
    template,
    built_channels,
    *,
    scene=None,
    rest_start_mode=utils.REST_START_OFF,
    enabled_channel_ids=None,
):
    """Preflight a complete motion plan without writing any drivers.

    ``enabled_channel_ids`` is the artist's per-template channel toggle (see
    ui.props.enabled_channel_ids_for_template). ``None`` means "no toggles are
    in play": every declared channel applies. A disabled channel gets no target
    and so no driver at all - not a driver forced to a constant - so whatever
    the artist already has on that property (a hand-keyed value, or nothing) is
    left alone.

    Filtering happens HERE, once, before the targets are built.
    """
    scene = scene or bpy.context.scene
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

    targets = motion_targets_for_object(obj, template, channels=filtered_channels)
    valid, reason = _validate_plan(template, built_channels, targets, channels=filtered_channels)
    if not valid:
        return MotionApplyResult(False, reason)
    conflict_decision = _conflict_preflight(obj, targets, template)
    if not conflict_decision.allowed:
        return MotionApplyResult(
            False,
            conflicts.format_conflict_message(conflict_decision),
            targets=targets,
            template_data=template,
            scene_data=scene,
            conflict_decision=conflict_decision,
        )
    props = getattr(scene, "espresso_props", None)
    if (
        conflict_decision.action is conflicts.DecisionAction.LAYER
        or (
            conflict_decision.action is conflicts.DecisionAction.UPDATE
            and all(_newest_layer(_existing_record_for_path(t.owner, t.data_path, t.index)) is not None
                    for t in targets)
        )
    ) and target_memory.controller_bound_targets(targets, props):
        return MotionApplyResult(
            False,
            "A Controller is attached to this motion. Remove it from the driver "
            "before layering another motion over it.",
            targets=targets,
            template_data=template,
            scene_data=scene,
            conflict_decision=conflict_decision,
        )
    states = []
    for target, built in zip(targets, built_channels):
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
        states.append(state)

    prepared = []
    for target, built, state in zip(targets, built_channels, states):
        applied_expression = built.get("driver_expression") or built["expression"]
        wrapped = utils.wrap_expression_with_rest_state(applied_expression, template, state)
        valid, message = utils.validate_driver_expression(wrapped, template, scene)
        if not valid:
            return MotionApplyResult(False, message, 0, targets, states)
        prepared.append(wrapped)

    summary = f"Applied {template['name']} to {len(prepared)} motion channels."
    return MotionApplyResult(
        True,
        summary,
        0,
        targets,
        states,
        prepared,
        template,
        scene,
        conflict_decision,
    )


def _stamp_applied_motion(template, stamped):
    """Record what was just applied, so it can be found again by name. Stamping here
    rather than in the operators covers every caller with one hook; an apply that
    finishes without a record would be missing from the bake list. Never fatal: a
    motion that is applied but unstamped still works, it only does not appear in the
    bake list, and failing the apply over bookkeeping would be the worse trade.
    """
    code = (template or {}).get("effect_id") or ""
    if not code or not stamped:
        return
    label = (template or {}).get("name") or code
    extras = {}
    if (template or {}).get("id"):
        extras["template_id"] = template["id"]
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

    applied = 0
    template = prepared_result.template_data
    scene = prepared_result.scene_data or bpy.context.scene
    decision = prepared_result.conflict_decision
    replaced = None
    if decision is not None and decision.action is conflicts.DecisionAction.LAYER:
        return _commit_layered_motion(prepared_result)
    if (
        decision is not None
        and decision.action is conflicts.DecisionAction.UPDATE
        and prepared_result.targets
        and all(
            _newest_layer(_existing_record_for_path(t.owner, t.data_path, t.index)) is not None
            for t in prepared_result.targets
        )
    ):
        return _commit_layered_motion(prepared_result, update_newest=True)
    if decision is not None and decision.action in {
        conflicts.DecisionAction.UPDATE,
        conflicts.DecisionAction.REPLACE,
    }:
        # driver_add cannot safely overwrite an existing F-curve in every RNA
        # owner. Remove only channels already claimed by Espresso; unmanaged
        # data would have been blocked during preflight.
        replaced = target_memory.capture_cleanup_for_targets(
            prepared_result.targets, getattr(scene, "espresso_props", None),
        )
        for target in prepared_result.targets:
            _discard_stack_on_channel(target.owner, target.data_path, target.index)
            try:
                target.owner.driver_remove(target.data_path, target.index)
            except (AttributeError, RuntimeError):
                pass
    created_targets = []
    stamped = []
    for target, wrapped in zip(
        prepared_result.targets,
        prepared_result.prepared_expressions,
    ):
        try:
            result = target.owner.driver_add(target.data_path, target.index)
        except Exception as exc:
            return MotionApplyResult(
                False,
                f"Could not add {target.data_path}[{target.index}] driver: {exc}",
                applied,
                prepared_result.targets,
                prepared_result.target_states,
                prepared_result.prepared_expressions,
                template,
                scene,
            )
        fcurves = result if isinstance(result, list) else [result]
        for fcurve in fcurves:
            if fcurve is None:
                continue
            ok, message = utils.assign_driver_expression(
                fcurve.driver, wrapped, template, scene,
            )
            if not ok:
                for created in created_targets:
                    try:
                        created.owner.driver_remove(created.data_path, created.index)
                    except Exception:
                        pass
                return MotionApplyResult(
                    False,
                    message,
                    applied,
                    prepared_result.targets,
                    prepared_result.target_states,
                    prepared_result.prepared_expressions,
                    template,
                    scene,
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

    if replaced:
        # A Controller wrapped the driver that was just replaced; it has nothing left to wrap.
        target_memory.cleanup_captured_resources(
            replaced, scene, getattr(scene, "espresso_props", None),
        )
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
    )


def _existing_record_for_path(owner, data_path, index):
    for record in applied_motion.entries(owner, validate=True):
        if (data_path, int(index)) in applied_motion.paths_of(record):
            return record
    return None


def _discard_stack_on_channel(owner, data_path, index):
    """Retire the Motion Stack that drives a channel about to be replaced.

    Its record, helper object and helper properties go with it; leaving them
    would strand a helper object that nothing refers to any more.
    """
    record = _existing_record_for_path(owner, data_path, index)
    payload = stack_records.stack_from_extras((record or {}).get("extras", {}))
    if payload:
        stack_runtime.clear(owner, MotionStack.from_dict(payload).stack_id)


def _motion_stack_for_target(target, incoming_expression, template, *, update_newest=False):
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
    blend = BlendMode.ADD
    if update_newest and payload and layers:
        # Same recipe again: new settings for its layer, same place in the stack.
        newest = layers.pop()
        layers.append(replace(newest, channel=channel, expression=str(incoming_expression)))
        return MotionStack(stack_id=stack_id, layers=tuple(layers))
    layers.append(StackLayer(
        layer_id="layer.%s" % uuid.uuid4().hex[:12],
        effect_id=str((template or {}).get("effect_id") or "incoming"),
        label=str((template or {}).get("name") or "Incoming motion"),
        channel=channel,
        expression=str(incoming_expression),
        blend=blend,
    ))
    return MotionStack(stack_id=stack_id, layers=tuple(layers))


def _commit_layered_motion(prepared_result, *, update_newest=False):
    """Commit only the exact native-driver stack slice supported today."""
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
                update_newest=update_newest,
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
    verb = "Updated" if update_newest else "Layered"
    return MotionApplyResult(
        True,
        "%s %s %s the existing motion on %d channel(s)." % (
            verb, prepared_result.template_data.get("name", "motion"),
            "in" if update_newest else "over", len(applied),
        ),
        len(applied), prepared_result.targets, prepared_result.target_states,
        prepared_result.prepared_expressions, prepared_result.template_data,
        prepared_result.scene_data,
    )


def apply_motion_template_to_object(
    obj,
    template,
    built_channels,
    *,
    scene=None,
    rest_start_mode=utils.REST_START_OFF,
    enabled_channel_ids=None,
):
    """Preflight and apply a complete template plan to one object."""
    prepared = prepare_motion_template_application(
        obj,
        template,
        built_channels,
        scene=scene,
        rest_start_mode=rest_start_mode,
        enabled_channel_ids=enabled_channel_ids,
    )
    return commit_prepared_motion(prepared)
