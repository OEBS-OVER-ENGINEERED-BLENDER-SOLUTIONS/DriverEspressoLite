"""Batch and popup actions for the real-driver Active target manager."""

from __future__ import annotations

import bpy

from ...apply import (
    applied_motion, applied_motion_manager, driver_manager, driver_targets, target_memory,
)
from ...apply.motion import stack_records, stack_runtime
from ...apply.setups import (
    parameter_bindings,
)
from ...apply.setups import layout_preparation
from ...catalogue import templates
from ...engine import utils
from ...engine.motion_stack.stack import MotionStack, StackLayer
from ..state import props as espresso_props
from . import bake_applied
from .operators import BakeOptionsMixin, apply_expression_to_driver, missing_required_variables


def _selected_descriptors(context):
    props = context.scene.espresso_props
    return driver_manager.descriptors_from_items(props.driver_target_items)


def _selected_effect_tokens(context):
    props = context.scene.espresso_props
    return driver_manager.effect_tokens_from_items(props.driver_target_items)


def _dedupe_descriptors_for_effects(descriptors, effect_tokens):
    """Drop driver channels already covered by a selected effect group."""
    if not effect_tokens:
        return list(descriptors)
    selected = set(effect_tokens)
    filtered = []
    for descriptor in descriptors:
        metadata = driver_manager.metadata_for_descriptor(descriptor)
        if metadata and metadata.get("record_token") in selected:
            continue
        filtered.append(descriptor)
    return filtered


def _fcurves_for_recorded_effects(recorded_effects):
    fcurves = []
    seen = set()
    for effect in recorded_effects:
        host = effect["host"]
        animation = getattr(host, "animation_data", None)
        if animation is None:
            continue
        for data_path, index in applied_motion.paths_of(effect["record"]):
            curve = (
                animation.drivers.find(data_path, index=index)
                if index >= 0 else animation.drivers.find(data_path)
            )
            if curve is None:
                continue
            key = (curve.id_data, curve.data_path, curve.array_index)
            if key in seen:
                continue
            seen.add(key)
            fcurves.append(curve)
    return fcurves


def _snapshot_driver_fcurve(fcurve):
    from ..state import live_controls
    from ...apply.core import target_memory

    owner = fcurve.id_data
    record = target_memory.serialize_owner(owner) if owner is not None else {}
    return {
        "owner": owner,
        "owner_name": record.get("id_name") or getattr(owner, "name", ""),
        "id_type": record.get("id_type") or "",
        "data_path": fcurve.data_path,
        "index": fcurve.array_index,
        "snapshot": live_controls.snapshot_driver(fcurve.driver),
    }


def _driver_owner_alive(owner):
    from ...apply.core import target_memory

    return target_memory.host_is_alive(owner)


def _remove_owner_driver(owner, data_path, index):
    if index >= 0:
        try:
            owner.driver_remove(data_path, index)
            return
        except Exception:
            pass
    try:
        owner.driver_remove(data_path)
    except Exception:
        pass


def _add_owner_driver(owner, data_path, index):
    if index >= 0:
        try:
            return owner.driver_add(data_path, index)
        except TypeError:
            pass
    return owner.driver_add(data_path)


def _restore_driver_snapshots(snapshots):
    from ..state import live_controls
    from ...apply.core import target_memory

    for item in snapshots:
        owner = item.get("owner")
        if not _driver_owner_alive(owner):
            owner = target_memory.find_id_block(
                item.get("id_type") or "Object",
                item.get("owner_name", ""),
            )
        if owner is None:
            item["restore_error"] = "missing owner %s" % (item.get("owner_name") or "")
            continue
        data_path = item["data_path"]
        index = item["index"]
        try:
            _remove_owner_driver(owner, data_path, index)
            curve = _add_owner_driver(owner, data_path, index)
            if isinstance(curve, list):
                curve = curve[0] if curve else None
            if curve is not None:
                live_controls.restore_driver(curve.driver, item["snapshot"])
        except Exception as exc:
            item["restore_error"] = str(exc)
            continue
    return tuple(item.get("restore_error") for item in snapshots if item.get("restore_error"))


def _preflight_structural_effects(structural_effects):
    for effect in structural_effects:
        host = effect.get("host")
        if host is None:
            return False
        carriers = (
            [host] if layout_preparation.is_carrier(host)
            else layout_preparation.carriers_for_source(host)
        )
        if not carriers:
            return False
    return True


def _preflight_recorded_effects(recorded_effects):
    for effect in recorded_effects:
        host = effect.get("host")
        record = effect.get("record") or {}
        if host is None or not record:
            return False
        try:
            if not _driver_owner_alive(host):
                return False
        except (ReferenceError, AttributeError):
            return False
    return True


def remove_selected_with_rollback(context, descriptors, effect_tokens, props):
    """Preflight and remove mixed selections with rollback on failure."""
    from ...generated.core.transaction import GeneratedTransaction

    # A checked row that belongs to a rig with its own teardown IS the rig:
    # there is no such thing as an authoring rig minus its mount driver. The row
    # escalates to its effect and the route's clear runs for it.
    effect_tokens = list(effect_tokens)
    plain = []
    for descriptor in descriptors:
        metadata = driver_manager.metadata_for_descriptor(descriptor)
        route = (
            bake_applied.route_for_record(metadata["host"], metadata["record"])
            if metadata else None
        )
        if route is not None:
            if metadata["record_token"] not in effect_tokens:
                effect_tokens.append(metadata["record_token"])
            continue
        plain.append(descriptor)
    descriptors = _dedupe_descriptors_for_effects(plain, effect_tokens)
    fcurves = []
    for descriptor in descriptors:
        fcurve = driver_targets.resolve_driver(descriptor)
        if fcurve is None:
            return False, "A checked driver no longer exists; nothing was removed.", {}
        fcurves.append(fcurve)

    source = getattr(props, "driver_target_source", "ACTIVE")
    effects = [
        applied_motion_manager.find_effect(context, token, source)
        for token in effect_tokens
    ]
    if any(effect is None for effect in effects):
        return False, "A checked applied effect no longer exists; nothing was removed.", {}

    # STRUCTURAL_EFFECT alone is no longer enough to mean "prepared layout".
    # An authoring rig is filed as a structure too -- correctly, it is the thing
    # its flight attaches to -- and this branch hands every structural effect
    # to layout_preparation.clear(), which knows nothing about an authoring rig and
    # answered "A checked Prepared Layout could not be removed."
    #
    # The honest test is the one this branch actually depends on: it clears
    # layout CARRIERS, so it may only claim hosts that are carriers. Anything
    # else structural falls through to clear_records, which is the path that
    # tore an authoring rig down correctly before it was reclassified.
    structural_effects = [
        effect for effect in effects
        if effect["effect_kind"] == applied_motion_manager.STRUCTURAL_EFFECT
        and effect.get("host") is not None
        and layout_preparation.is_carrier(effect["host"])
    ]
    recorded_effects = [
        effect for effect in effects if effect not in structural_effects
    ]
    structural_hosts = {
        effect["host"] for effect in structural_effects if effect.get("host") is not None
    }
    recorded_effects = [
        effect for effect in recorded_effects
        if effect.get("host") not in structural_hosts
    ]
    if not _preflight_structural_effects(structural_effects):
        return False, "A checked Prepared Layout could not be removed.", {}
    if not _preflight_recorded_effects(recorded_effects):
        return False, "A checked applied effect could not be removed.", {}

    snapshot_curves = list(fcurves)
    for curve in _fcurves_for_recorded_effects(recorded_effects):
        if curve not in snapshot_curves:
            snapshot_curves.append(curve)
    driver_snapshots = [_snapshot_driver_fcurve(curve) for curve in snapshot_curves]
    extra_owners = []
    for item in driver_snapshots:
        owner = item.get("owner")
        if owner is not None:
            extra_owners.append(owner)
    chosen = [(effect["host"], effect["record"]) for effect in recorded_effects]
    for effect in structural_effects:
        host = effect["host"]
        carriers = (
            [host] if layout_preparation.is_carrier(host)
            else layout_preparation.carriers_for_source(host)
        )
        for carrier in carriers:
            chosen.append((carrier, effect.get("record") or {}))
    _snapshot_hosts, snapshot = bake_applied.capture_clear_snapshot(
        chosen, extra_fcurve_owners=extra_owners,
    )
    pin_snapshot = _snapshot_pinned_effect_settings(props)

    removed_drivers = 0
    try:
        with GeneratedTransaction("batch-remove") as transaction:
            transaction.on_rollback(
                lambda: _restore_pinned_effect_settings(props, pin_snapshot)
            )
            transaction.on_rollback(snapshot.restore)
            transaction.on_rollback(lambda: _restore_driver_snapshots(driver_snapshots))
            for effect in structural_effects:
                host = effect["host"]
                try:
                    alive = host is not None and target_memory.host_is_alive(host)
                except (ReferenceError, AttributeError):
                    alive = False
                if not alive:
                    raise RuntimeError("A checked Prepared Layout could not be removed.")
                if layout_preparation.clear(host) <= 0:
                    raise RuntimeError("A checked Prepared Layout could not be removed.")
            if recorded_effects:
                ok, message = bake_applied.clear_records(
                    context,
                    [(effect["host"], effect["record"]) for effect in recorded_effects],
                    transaction=transaction,
                )
                if not ok:
                    raise RuntimeError(message)
            if props.pinned_applied_effect_token in effect_tokens:
                clear_pinned_effect_settings(props)

            hosts = []
            for descriptor in descriptors:
                metadata = driver_manager.metadata_for_descriptor(descriptor)
                host = metadata.get("host") if metadata else None
                if host is not None and host not in hosts:
                    hosts.append(host)
            captured = target_memory.capture_cleanup_for_fcurves(fcurves, props)
            for curve in list(fcurves):
                owner = curve.id_data
                owner.driver_remove(curve.data_path, curve.array_index)
                removed_drivers += 1
            target_memory.cleanup_captured_resources(captured, context.scene, props)
            target_memory.cleanup_entry_node_groups({"targets": captured.get("targets", [])})
            # A record emptied a row at a time is purged with its dependencies,
            # exactly as if its effect had been checked.
            purged_effects = 0
            for host in hosts:
                purged_effects += bake_applied.purge_emptied_records(context, host)
            transaction.commit()
        snapshot.discard()
    except Exception as exc:
        snapshot.discard()
        report = snapshot.verify()
        if report.get("ok"):
            return False, "Batch removal was rolled back: %s" % exc, {}
        detail = "; ".join(report.get("errors") or ())
        if detail:
            return False, "Batch removal failed and could not fully restore: %s (%s)" % (exc, detail), {}
        return False, "Batch removal failed and could not fully restore: %s" % exc, {}

    props.driver_target_items_signature = ""
    return True, "", {
        "effects": len(effects) + purged_effects,
        "drivers": removed_drivers,
    }


def prepare_effect_settings(context, effect):
    """Resolve a settings model without changing the template browser state."""
    template = effect.get("template")
    if template is None:
        return None, None, {}, "The applied template is no longer available."
    binding = parameter_bindings.binding_for_effect(context, effect)
    if not binding.available:
        return None, template, {}, binding.reason
    if not binding.editable:
        return binding, template, {}, binding.reason or "This applied effect is read-only."
    values = parameter_bindings.read_values(binding)
    return binding, template, values, ""


def load_effect_settings(context, effect):
    """Compatibility helper; settings now open in their own editor."""
    binding, template, values, message = prepare_effect_settings(context, effect)
    return bool(binding and template and values), message or (
        "Settings are ready for %s." % (effect.get("label") or "the applied effect")
    )


def _applied_effect_identity(effect):
    if not effect:
        return ""
    host = effect.get("host")
    try:
        host_name = str(getattr(host, "name", "") or "")
    except (ReferenceError, AttributeError):
        host_name = str(effect.get("host_label") or "")
    label = str(effect.get("label") or "applied effect")
    if host_name and label:
        return "%s > %s" % (host_name, label)
    return host_name or label


def _record_manage_status(props, effect, message):
    identity = _applied_effect_identity(effect)
    text = str(message or "").strip()
    if identity and identity not in text:
        text = "%s — %s." % (text.rstrip("."), identity)
    espresso_props.set_last_apply_status(props, text)


def _selected_batch_identities(props):
    """Host and effect names for checked rows, captured before mutation."""
    identities = []
    seen = set()
    for item in getattr(props, "driver_target_items", ()):
        if not getattr(item, "batch_selected", False):
            continue
        if item.row_kind != "GROUP":
            continue
        host = str(item.owner_id_name or "").strip()
        label = str(item.label or "").strip()
        identity = "%s > %s" % (host, label) if host and label else (host or label)
        if identity and identity not in seen:
            seen.add(identity)
            identities.append(identity)
    if identities:
        return identities
    for item in getattr(props, "driver_target_items", ()):
        if not getattr(item, "batch_selected", False):
            continue
        if item.row_kind not in {"TARGET", "SETUP"}:
            continue
        host = str(item.owner_id_name or "").strip()
        label = str(item.label or "").strip()
        identity = host or label
        if identity and identity not in seen:
            seen.add(identity)
            identities.append(identity)
    return identities


def action_label(effect_kind, action):
    noun = "Effect" if effect_kind == applied_motion_manager.STRUCTURAL_EFFECT else "Motion"
    return {
        "SETTINGS": "%s Settings" % noun,
        "BAKE": "Bake %s" % noun,
        "CLEAR": "Remove Applied %s" % noun,
    }.get(action, "Manage Applied %s" % noun)


def _populate_parameter_items(collection, binding, template, values):
    collection.clear()
    visible = parameter_bindings.visible_tokens(binding, template, values)
    for spec in template.get("params", ()):
        if spec.get("token") not in visible:
            continue
        popup = collection.add()
        popup.token = spec["token"]
        popup.label = spec.get("label", popup.token)
        popup.value_type = spec.get("type", "FLOAT")
        popup.unit = spec.get("unit", "")
        value = values.get(popup.token, spec.get("default", 0))
        if popup.value_type == "BOOL":
            popup.bool_value = bool(value)
        elif popup.value_type == "INT":
            popup.int_value = int(round(value))
        elif popup.value_type == "COLOR":
            rgba = tuple(value)
            popup.color_value = rgba if len(rgba) == 4 else (*rgba[:3], 1.0)
        elif popup.value_type in {"STRING", "ENUM"}:
            popup.string_value = str(value)
        else:
            popup.float_value = float(value)


def _parameter_values(items):
    values = {}
    for item in items:
        if item.value_type == "BOOL":
            value = bool(item.bool_value)
        elif item.value_type == "INT":
            value = int(item.int_value)
        elif item.value_type == "COLOR":
            value = tuple(item.color_value)
        elif item.value_type in {"STRING", "ENUM"}:
            value = str(item.string_value)
        else:
            value = float(item.float_value)
        values[item.token] = value
    return values


def _snapshot_pinned_effect_settings(props):
    parameters = []
    for item in getattr(props, "pinned_effect_parameters", ()) or ():
        parameters.append({
            "token": item.token,
            "label": item.label,
            "value_type": item.value_type,
            "unit": item.unit,
            "bool_value": bool(item.bool_value),
            "int_value": int(item.int_value),
            "float_value": float(item.float_value),
            "string_value": str(item.string_value),
            "color_value": tuple(item.color_value),
        })
    return {
        "pinned": bool(getattr(props, "applied_effect_settings_pinned", False)),
        "token": str(getattr(props, "pinned_applied_effect_token", "") or ""),
        "source": str(getattr(props, "pinned_applied_effect_source", "ACTIVE") or "ACTIVE"),
        "label": str(getattr(props, "pinned_applied_effect_label", "") or ""),
        "parameters": parameters,
    }


def _restore_pinned_effect_settings(props, snapshot):
    if props is None or not snapshot:
        return
    previous = bool(getattr(props, "suspend_pinned_effect_updates", False))
    props.suspend_pinned_effect_updates = True
    try:
        props.applied_effect_settings_pinned = bool(snapshot.get("pinned"))
        props.pinned_applied_effect_token = str(snapshot.get("token") or "")
        props.pinned_applied_effect_source = str(snapshot.get("source") or "ACTIVE")
        props.pinned_applied_effect_label = str(snapshot.get("label") or "")
        props.pinned_effect_parameters.clear()
        for record in snapshot.get("parameters") or ():
            item = props.pinned_effect_parameters.add()
            item.token = record.get("token") or ""
            item.label = record.get("label") or ""
            item.value_type = record.get("value_type") or "FLOAT"
            item.unit = record.get("unit") or ""
            item.bool_value = bool(record.get("bool_value"))
            item.int_value = int(record.get("int_value") or 0)
            item.float_value = float(record.get("float_value") or 0.0)
            item.string_value = str(record.get("string_value") or "")
            color = record.get("color_value") or (1.0, 1.0, 1.0, 1.0)
            try:
                item.color_value = color
            except (TypeError, ValueError):
                pass
    finally:
        props.suspend_pinned_effect_updates = previous


def apply_preset_to_parameter_items(items, preset_values):
    by_token = {item.token: item for item in items}
    for token, value in (preset_values or {}).items():
        item = by_token.get(token)
        if item is None:
            continue
        if item.value_type == "BOOL":
            item.bool_value = bool(value)
        elif item.value_type == "INT":
            item.int_value = int(round(value))
        elif item.value_type == "COLOR":
            rgba = tuple(value)
            item.color_value = rgba if len(rgba) == 4 else (*rgba[:3], 1.0)
        elif item.value_type in {"STRING", "ENUM"}:
            item.string_value = str(value)
        else:
            item.float_value = float(value)


def _dialog_parameter_items(context, fallback=None):
    items = getattr(context.window_manager, "espresso_applied_dialog_parameters", None)
    if items is not None and len(items) > 0:
        return items
    return fallback


def _populate_dialog_parameters(context, binding, template, values, fallback=None):
    items = getattr(context.window_manager, "espresso_applied_dialog_parameters", None)
    if items is not None:
        _populate_parameter_items(items, binding, template, values)
        context.window_manager.espresso_applied_dialog_template_id = template.get("id", "")
    if fallback is not None:
        _populate_parameter_items(fallback, binding, template, values)


def _active_effect_item(props):
    items = props.driver_target_items
    index = int(getattr(props, "driver_target_active_index", -1))
    if not (0 <= index < len(items)):
        return None
    item = items[index]
    if item.record_token:
        return item
    return next(
        (value for value in items if value.group_key == item.group_key and value.record_token),
        None,
    )


def populate_pinned_effect_settings(context, effect):
    props = context.scene.espresso_props
    binding, template, values, message = prepare_effect_settings(context, effect)
    if binding is None or template is None or not binding.editable:
        return False, message
    props.pinned_applied_effect_token = effect.get("record_token", "")
    props.pinned_applied_effect_source = getattr(props, "driver_target_source", "ACTIVE")
    props.pinned_applied_effect_label = effect.get("label") or template.get("name", "Applied Effect")
    previous = bool(getattr(props, "suspend_pinned_effect_updates", False))
    props.suspend_pinned_effect_updates = True
    try:
        _populate_parameter_items(props.pinned_effect_parameters, binding, template, values)
    finally:
        props.suspend_pinned_effect_updates = previous
    return True, "Pinned settings for %s." % props.pinned_applied_effect_label


def clear_pinned_effect_settings(props, record_token=""):
    """Forget the pinned editor when its effect is removed or the pin is disabled."""
    if record_token and props.pinned_applied_effect_token != record_token:
        return False
    previous = bool(getattr(props, "suspend_pinned_effect_updates", False))
    props.suspend_pinned_effect_updates = True
    try:
        props.applied_effect_settings_pinned = False
        props.pinned_applied_effect_token = ""
        props.pinned_applied_effect_source = "ACTIVE"
        props.pinned_applied_effect_label = ""
        props.pinned_effect_parameters.clear()
    finally:
        props.suspend_pinned_effect_updates = previous
    return True


def sync_pinned_effect_settings(context):
    """Write the visible pinned editor to the effect it was populated from."""
    props = context.scene.espresso_props
    effect = applied_motion_manager.find_effect(
        context,
        str(getattr(props, "pinned_applied_effect_token", "") or ""),
        str(getattr(props, "pinned_applied_effect_source", "ACTIVE") or "ACTIVE"),
    )
    if effect is None:
        return False, "The pinned applied effect no longer resolves."
    binding, template, _values, message = prepare_effect_settings(context, effect)
    if binding is None or template is None or not binding.editable:
        return False, message or "This applied effect is read-only."
    ok, message = parameter_bindings.write_values(
        binding,
        _parameter_values(props.pinned_effect_parameters),
        context.scene,
        template=template,
    )
    props.driver_target_items_signature = ""
    return ok, message


class ESPRESSO_OT_toggle_applied_effect_settings_pin(bpy.types.Operator):
    bl_idname = "espresso.toggle_applied_effect_settings_pin"
    bl_label = "Show/Hide Settings"
    bl_description = (
        "Show or hide settings below the list. Select a layout or motion to edit it."
    )
    bl_options = {"INTERNAL"}

    def execute(self, context):
        props = context.scene.espresso_props
        if props.applied_effect_settings_pinned:
            clear_pinned_effect_settings(props)
            return {"FINISHED"}
        item = _active_effect_item(props)
        if item is not None and item.record_token:
            effect = applied_motion_manager.find_effect(
                context, item.record_token, getattr(props, "driver_target_source", "ACTIVE"),
            )
            if effect is not None:
                ok, message = populate_pinned_effect_settings(context, effect)
                if not ok:
                    self.report({"INFO"}, message)
        props.applied_effect_settings_pinned = True
        props.pinned_effect_settings_open = True
        return {"FINISHED"}


class ESPRESSO_OT_refresh_pinned_effect_settings(bpy.types.Operator):
    bl_idname = "espresso.refresh_pinned_effect_settings"
    bl_label = "Refresh Pinned Effect Settings"
    bl_description = "Reload the current native values for the pinned applied effect"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        props = context.scene.espresso_props
        if not props.applied_effect_settings_pinned:
            self.report({"WARNING"}, "Show settings first.")
            return {"CANCELLED"}
        item = _active_effect_item(props)
        if item is not None and item.record_token:
            effect = applied_motion_manager.find_effect(
                context, item.record_token, props.driver_target_source,
            )
        else:
            effect = applied_motion_manager.find_effect(
                context, props.pinned_applied_effect_token, props.pinned_applied_effect_source,
            )
        if effect is None:
            self.report({"WARNING"}, "The pinned applied effect no longer resolves.")
            return {"CANCELLED"}
        ok, message = populate_pinned_effect_settings(context, effect)
        self.report({"INFO" if ok else "WARNING"}, message)
        return {"FINISHED" if ok else "CANCELLED"}


class ESPRESSO_OT_update_pinned_effect_settings(bpy.types.Operator):
    bl_idname = "espresso.update_pinned_effect_settings"
    bl_label = "Update Applied Effect"
    bl_description = "Write these values to the pinned applied effect"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        ok, message = sync_pinned_effect_settings(context)
        self.report({"INFO" if ok else "WARNING"}, message)
        return {"FINISHED" if ok else "CANCELLED"}


class ESPRESSO_StackLayerEditItem(bpy.types.PropertyGroup):
    layer_id: bpy.props.StringProperty(default="")
    label: bpy.props.StringProperty(default="Layer")
    weight: bpy.props.FloatProperty(name="Weight", min=0.0, default=1.0)
    muted: bpy.props.BoolProperty(name="Mute", default=False)
    solo: bpy.props.BoolProperty(name="Solo", default=False)
    order: bpy.props.IntProperty(name="Order", min=1, default=1)


class ESPRESSO_OT_edit_motion_stack(bpy.types.Operator):
    bl_idname = "espresso.edit_motion_stack"
    bl_label = "Motion Stack Settings"
    bl_description = "Edit native Motion Stack order, weight, mute and solo controls"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    record_token: bpy.props.StringProperty(default="")
    source: bpy.props.StringProperty(default="ACTIVE")
    layers: bpy.props.CollectionProperty(type=ESPRESSO_StackLayerEditItem)

    def _effect(self, context):
        return applied_motion_manager.find_effect(context, self.record_token, self.source)

    def invoke(self, context, event):
        self.source = getattr(context.scene.espresso_props, "driver_target_source", "ACTIVE")
        effect = self._effect(context)
        if effect is None:
            self.report({"WARNING"}, "The Motion Stack no longer resolves.")
            return {"CANCELLED"}
        payload = stack_records.stack_from_extras(effect["record"].get("extras", {}))
        if payload is None:
            self.report({"WARNING"}, "This effect is not a Motion Stack.")
            return {"CANCELLED"}
        self.layers.clear()
        for index, layer in enumerate(MotionStack.from_dict(payload).layers, 1):
            item = self.layers.add()
            item.layer_id = layer.layer_id
            item.label = layer.label
            item.weight = layer.weight
            item.muted = layer.muted
            item.solo = layer.solo
            item.order = index
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Layers resolve from top to bottom.", icon="SEQ_STRIP_DUPLICATE")
        for item in self.layers:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=item.label, icon="ACTION")
            row.prop(item, "order", text="Order")
            controls = box.row(align=True)
            controls.prop(item, "weight")
            controls.prop(item, "muted", toggle=True)
            controls.prop(item, "solo", toggle=True)

    def execute(self, context):
        effect = self._effect(context)
        if effect is None:
            self.report({"WARNING"}, "The Motion Stack no longer resolves.")
            return {"CANCELLED"}
        payload = stack_records.stack_from_extras(effect["record"].get("extras", {}))
        motion = MotionStack.from_dict(payload)
        edits = {item.layer_id: item for item in self.layers}
        order = {item.layer_id: (item.order, index)
                 for index, item in enumerate(self.layers)}
        layers = []
        for layer in motion.layers:
            item = edits.get(layer.layer_id)
            values = layer.to_dict()
            if item is not None:
                values.update(weight=item.weight, muted=item.muted, solo=item.solo)
            layers.append(StackLayer.from_dict(values))
        layers.sort(key=lambda layer: order.get(layer.layer_id, (9999, 9999)))
        updated = MotionStack(motion.stack_id, tuple(layers), motion.schema_version)
        paths = applied_motion.paths_of(effect["record"])
        if len(paths) != 1:
            self.report({"WARNING"}, "This editor currently requires one resolved target channel.")
            return {"CANCELLED"}
        data_path, index = paths[0]
        ok, message = stack_runtime.apply(effect["host"], data_path, index, updated)
        self.report({"INFO" if ok else "WARNING"}, message)
        context.scene.espresso_props.driver_target_items_signature = ""
        return {"FINISHED" if ok else "CANCELLED"}


class ESPRESSO_OT_toggle_driver_target_group(bpy.types.Operator):
    bl_idname = "espresso.toggle_driver_target_group"
    bl_label = "Toggle Motion Group"
    bl_options = {"INTERNAL"}

    group_key: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.espresso_props
        is_open = espresso_props.driver_target_group_is_open(props, self.group_key)
        espresso_props.set_driver_target_group_open(props, self.group_key, not is_open)
        props.driver_target_items_signature = ""
        return {"FINISHED"}


class ESPRESSO_OT_select_driver_target_group(bpy.types.Operator):
    bl_idname = "espresso.select_driver_target_group"
    bl_label = "Select Applied Effect Group"
    bl_description = "Select or clear every removable entry nested under this motion"
    bl_options = {"INTERNAL"}

    group_key: bpy.props.StringProperty(default="")

    def execute(self, context):
        items = context.scene.espresso_props.driver_target_items
        parent = next(
            (
                item for item in items
                if item.row_kind in {"BUCKET", "CATEGORY", "HOST"}
                and item.group_key == self.group_key
            ),
            None,
        )
        if parent is not None:
            members = [
                item for item in driver_manager.descendants_under_row(items, self.group_key)
                if item.batch_eligible and driver_manager.matches_list_search(item)
            ]
        else:
            members = [
                item for item in items
                if item.group_key == self.group_key
                and item.row_kind in {"TARGET", "SETUP"}
                and item.batch_eligible
                and driver_manager.matches_list_search(item)
            ]
        if members:
            select = not all(item.batch_selected for item in members)
            for item in members:
                item.batch_selected = select
            return {"FINISHED"}
        group = next(
            (
                item for item in items
                if item.group_key == self.group_key and item.row_kind == "GROUP"
            ),
            None,
        )
        if group is None or not group.batch_eligible:
            return {"CANCELLED"}
        group.batch_selected = not group.batch_selected
        return {"FINISHED"}


class ESPRESSO_OT_driver_targets_select(bpy.types.Operator):
    bl_idname = "espresso.driver_targets_select"
    bl_label = "Select Applied Effects"
    bl_options = {"INTERNAL"}

    mode: bpy.props.EnumProperty(items=(
        ("ALL", "All", "Select every removable applied effect or driver channel"),
        ("INVERT", "Invert", "Invert the selected applied effects and driver channels"),
        ("NONE", "None", "Clear the driver-target selection"),
    ))

    def execute(self, context):
        for item in context.scene.espresso_props.driver_target_items:
            if item.row_kind in {"BUCKET", "CATEGORY", "HOST"} or not item.batch_eligible:
                continue
            if self.mode == "NONE":
                item.batch_selected = False
                continue
            if not item.visible or not driver_manager.matches_list_search(item):
                continue
            if self.mode == "ALL":
                item.batch_selected = True
            else:
                item.batch_selected = not item.batch_selected
        return {"FINISHED"}


class ESPRESSO_OT_apply_selected_driver_targets(bpy.types.Operator):
    bl_idname = "espresso.apply_selected_driver_targets"
    bl_label = "Overwrite Selected Driver Targets"
    bl_description = "Apply the current single-property template to every checked real driver"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "espresso_props", None)
        if not props or not props.is_valid:
            return False
        template = espresso_props.get_current_template(props)
        return not templates.has_motion_plan(template) and bool(_selected_descriptors(context))

    def execute(self, context):
        props = context.scene.espresso_props
        template = espresso_props.get_current_template(props)
        descriptors = _selected_descriptors(context)
        fcurves = []
        for descriptor in descriptors:
            fcurve = driver_targets.resolve_driver(descriptor)
            if fcurve is None:
                self.report({"WARNING"}, "A checked driver no longer exists; nothing was changed.")
                return {"CANCELLED"}
            if missing_required_variables(fcurve.driver, template):
                self.report({"WARNING"}, "A checked driver is missing variables required by this template.")
                return {"CANCELLED"}
            fcurves.append(fcurve)

        original = [(curve.driver, curve.driver.expression) for curve in fcurves]
        changed = []
        try:
            for fcurve in fcurves:
                ok, message = apply_expression_to_driver(
                    fcurve.driver, props.preview, template, None,
                )
                if not ok:
                    raise RuntimeError(message)
                changed.append(fcurve)
        except Exception as exc:
            for driver, expression in original:
                try:
                    driver.expression = expression
                except Exception:
                    pass
            self.report({"WARNING"}, "Batch overwrite was rolled back: %s" % exc)
            return {"CANCELLED"}

        targets = [
            type("Target", (), {
                "owner": curve.id_data, "data_path": curve.data_path,
                "index": curve.array_index,
            })()
            for curve in changed
        ]
        target_memory.remember_targets(
            context, targets, "%d selected drivers" % len(targets), "manager_batch",
            template["id"], template["name"],
            template_values=espresso_props.collect_values(props, template),
        )
        self.report({"INFO"}, "Updated %d selected drivers." % len(changed))
        return {"FINISHED"}


class ESPRESSO_OT_remove_selected_driver_targets(bpy.types.Operator):
    bl_idname = "espresso.remove_selected_driver_targets"
    bl_label = "Remove Selected Driver Targets"
    bl_description = "Remove every checked real driver and safely clean unreferenced Espresso resources"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_selected_descriptors(context) or _selected_effect_tokens(context))

    def execute(self, context):
        from ..views import guided_apply
        guided_apply.clear_preflight_cache()
        props = context.scene.espresso_props
        descriptors = _selected_descriptors(context)
        effect_tokens = _selected_effect_tokens(context)
        identities = _selected_batch_identities(props)
        ok, message, stats = remove_selected_with_rollback(
            context, descriptors, effect_tokens, props,
        )
        if not ok:
            espresso_props.set_last_apply_status(props, message)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}

        parts = []
        effect_count = int(stats.get("effects", 0))
        driver_count = int(stats.get("drivers", 0))
        if effect_count:
            parts.append("%d applied effect%s" % (
                effect_count, "" if effect_count == 1 else "s",
            ))
        if driver_count:
            parts.append("%d driver%s" % (
                driver_count, "" if driver_count == 1 else "s",
            ))
        message = "Removed %s." % " and ".join(parts)
        named = ", ".join(identities[:3])
        if len(identities) > 3:
            named += ", +%d more" % (len(identities) - 3)
        if named and named not in message:
            message = "%s — %s." % (message.rstrip("."), named)
        espresso_props.set_last_apply_status(props, message)
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ESPRESSO_OT_edit_driver_target(bpy.types.Operator):
    bl_idname = "espresso.edit_driver_target"
    bl_label = "Edit Applied Driver"
    bl_description = "Open this applied effect's settings without changing the browsed template"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    descriptor_key: bpy.props.StringProperty(default="")
    group_key: bpy.props.StringProperty(default="")
    template_id: bpy.props.StringProperty(options={"HIDDEN"})
    parameters: bpy.props.CollectionProperty(type=espresso_props.ESPRESSO_DriverEditParamItem)

    def _item(self, context):
        items = context.scene.espresso_props.driver_target_items
        if self.descriptor_key:
            return next((item for item in items if item.descriptor_key == self.descriptor_key), None)
        return next((item for item in items if item.group_key == self.group_key and item.row_kind == "TARGET"), None)

    def invoke(self, context, event):
        item = self._item(context)
        if item is None or not item.editable:
            self.report({"WARNING"}, "This driver has no editable Espresso metadata.")
            return {"CANCELLED"}
        self.template_id = item.template_id
        descriptor = driver_targets.descriptor_from_item(item)
        metadata = driver_manager.metadata_for_descriptor(descriptor)
        template = templates.TEMPLATE_BY_ID.get(self.template_id)
        if not metadata or template is None:
            self.report({"WARNING"}, "The applied settings record is no longer available.")
            return {"CANCELLED"}
        stored_mode = str(metadata["entry"].get("application_mode") or "SINGLE")
        template = templates.resolve_application_mode(template, stored_mode)
        binding = parameter_bindings.binding_for_entry(
            context, template, metadata["entry"], label=metadata["label"],
        )
        values = parameter_bindings.read_values(binding) or metadata["parameter_values"]
        _populate_dialog_parameters(context, binding, template, values, self.parameters)
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        from ..views import panels

        layout = self.layout
        template = templates.TEMPLATE_BY_ID.get(self.template_id)
        if template is None:
            layout.label(text="Template is no longer available.", icon="ERROR")
            return
        layout.label(text=template["name"], icon="PREFERENCES")
        items = _dialog_parameter_items(context, self.parameters)
        panels.draw_parameter_item_controls(
            layout, context.scene.espresso_props, template, items, context,
            destination="DIALOG",
        )

    def execute(self, context):
        item = self._item(context)
        metadata = (
            driver_manager.metadata_for_descriptor(driver_targets.descriptor_from_item(item))
            if item is not None else None
        )
        template = templates.TEMPLATE_BY_ID.get(self.template_id)
        if not metadata or template is None:
            self.report({"WARNING"}, "The applied effect changed while the editor was open.")
            return {"CANCELLED"}
        values = _parameter_values(_dialog_parameter_items(context, self.parameters))
        entry = metadata["entry"]
        if not entry:
            self.report({"WARNING"}, "This older applied effect has no editable target record.")
            return {"CANCELLED"}
        resolved, reason = target_memory.resolve_entry(entry)
        if not resolved:
            self.report({"WARNING"}, reason or "The applied targets are no longer available.")
            return {"CANCELLED"}
        template = templates.resolve_application_mode(
            template, str(entry.get("application_mode") or "SINGLE"),
        )
        binding = parameter_bindings.binding_for_entry(
            context, template, entry, label=metadata["label"],
        )
        ok, message = parameter_bindings.write_values(
            binding, values, context.scene, template=template,
        )
        if not ok:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        context.scene.espresso_props.driver_target_items_signature = ""
        self.report({"INFO"}, "Updated %s." % metadata["label"])
        return {"FINISHED"}


class ESPRESSO_OT_manage_applied_effect(BakeOptionsMixin, bpy.types.Operator):
    bl_idname = "espresso.manage_applied_effect"
    bl_label = "Applied Effect"
    bl_description = "Edit, bake or remove this applied effect"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    record_token: bpy.props.StringProperty(default="")
    effect_kind: bpy.props.StringProperty(default="", options={"HIDDEN"})
    template_id: bpy.props.StringProperty(default="", options={"HIDDEN"})
    effect_label: bpy.props.StringProperty(default="Applied Effect", options={"HIDDEN"})
    parameters: bpy.props.CollectionProperty(type=espresso_props.ESPRESSO_DriverEditParamItem)
    action: bpy.props.EnumProperty(items=(
        ("SETTINGS", "Settings", "Open the complete applied effect's Live settings"),
        ("BAKE", "Bake", "Bake the complete applied effect"),
        ("CLEAR", "Clear", "Remove the complete applied effect and owned resources"),
    ))

    def _effect(self, context):
        props = context.scene.espresso_props
        source = getattr(props, "driver_target_source", "ACTIVE")
        return applied_motion_manager.find_effect(context, self.record_token, source)

    @classmethod
    def description(cls, context, properties):
        return action_label(
            getattr(properties, "effect_kind", ""),
            getattr(properties, "action", ""),
        )

    def invoke(self, context, event):
        effect = self._effect(context)
        if effect is None:
            self.report({"WARNING"}, "The applied effect no longer resolves.")
            return {"CANCELLED"}
        self.effect_kind = effect.get("effect_kind", "")
        self.effect_label = effect.get("label") or "Applied Effect"
        if self.action == "SETTINGS":
            stack_payload = stack_records.stack_from_extras(
                effect["record"].get("extras", {}),
            )
            if stack_payload is not None:
                return bpy.ops.espresso.edit_motion_stack(
                    "INVOKE_DEFAULT", record_token=self.record_token,
                )
            binding, template, values, message = prepare_effect_settings(context, effect)
            if binding is None or template is None or not binding.editable:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            self.template_id = template.get("id", "")
            self.effect_label = effect.get("label") or template.get("name", "Applied Effect")
            _populate_dialog_parameters(context, binding, template, values, self.parameters)
            if context.scene.espresso_props.applied_effect_settings_pinned:
                populate_pinned_effect_settings(context, effect)
            return context.window_manager.invoke_props_dialog(self, width=440)
        if self.action == "BAKE":
            self._seed_range(context)
            return context.window_manager.invoke_props_dialog(self, width=380)
        return self.execute(context)

    def draw(self, context):
        from ..views import panels

        if self.action == "BAKE":
            self.layout.label(text=action_label(self.effect_kind, self.action), icon="KEYFRAME_HLT")
            BakeOptionsMixin.draw(self, context)
        elif self.action == "SETTINGS":
            self.layout.label(text=self.effect_label, icon="PREFERENCES")
            effect = self._effect(context)
            template = (effect or {}).get("template") or templates.TEMPLATE_BY_ID.get(
                self.template_id,
            )
            panels.draw_applied_effect_settings(
                self.layout, context, effect, template, self.parameters,
            )

    def execute(self, context):
        from ..views import guided_apply
        guided_apply.clear_preflight_cache()
        props = context.scene.espresso_props
        effect = self._effect(context)
        if effect is None:
            message = "The applied effect no longer resolves."
            _record_manage_status(props, None, message)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        host = effect["host"]
        if self.action == "BAKE" and not effect.get("bakeable", True):
            message = "Bake the owning effect to bake this child result."
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        if self.action == "CLEAR" and not effect.get("removable", True):
            message = "Remove or replace the owning effect to remove this child."
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        if (
            effect.get("child_effect_kind") == "SPATIAL_EFFECTOR"
            and self.action != "SETTINGS"
        ):
            if self.action == "BAKE":
                message = "Bake the owning motion to bake this Effector's composed result."
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            removed = None
            ok = removed > 0
            message = (
                "Removed the Effector."
                if ok else "The Effector no longer resolves."
            )
            _record_manage_status(props, effect, message)
            self.report({"INFO" if ok else "WARNING"}, message)
            if ok:
                clear_pinned_effect_settings(props, self.record_token)
            props.driver_target_items_signature = ""
            return {"FINISHED" if ok else "CANCELLED"}
        if self.action == "SETTINGS":
            binding, template, _values, message = prepare_effect_settings(context, effect)
            if binding is None or template is None:
                self.report({"WARNING"}, message)
                return {"CANCELLED"}
            if espresso_props.settings_share_live_props(context, effect, template):
                ok, message = espresso_props.sync_live_parameters(
                    props, context, template,
                )
            else:
                ok, message = parameter_bindings.write_values(
                    binding,
                    _parameter_values(_dialog_parameter_items(context, self.parameters)),
                    context.scene,
                    template=template,
                )
            props.driver_target_items_signature = ""
            self.report({"INFO" if ok else "WARNING"}, message)
            return {"FINISHED" if ok else "CANCELLED"}

        if effect.get("effect_kind") == applied_motion_manager.STRUCTURAL_EFFECT:
            if self.action == "BAKE":
                ok, message, _objects = layout_preparation.bake(context, host)
            else:
                ok, message = bake_applied.clear_records(
                    context, [(host, effect["record"])],
                )
            _record_manage_status(props, effect, message)
            self.report({"INFO" if ok else "WARNING"}, message)
            if ok and self.action == "CLEAR":
                clear_pinned_effect_settings(props, self.record_token)
            props.driver_target_items_signature = ""
            return {"FINISHED" if ok else "CANCELLED"}

        stack_payload = stack_records.stack_from_extras(
            effect["record"].get("extras", {}),
        )
        if stack_payload is not None:
            stack_id = str(stack_payload.get("stack_id", ""))
            if self.action == "BAKE":
                ok, message = stack_runtime.bake_resolved(
                    context.scene, host, stack_id,
                )
            else:
                ok = stack_runtime.clear(host, stack_id) > 0
                message = "Removed the Motion Stack." if ok else "The Motion Stack no longer resolves."
            _record_manage_status(props, effect, message)
            self.report({"INFO" if ok else "WARNING"}, message)
            return {"FINISHED" if ok else "CANCELLED"}
        if self.action == "BAKE":
            self._commit_range(context)
            ok, message = bake_applied.bake_records(
                context, [(host, effect["record"])],
                start=self.bake_start, end=self.bake_end, step=self.bake_step,
                smart=self.bake_smart,
                smart_tolerance=self.bake_smart_tolerance / 100.0,
                smart_passes=self.bake_smart_passes,
            )
        else:
            ok, message = bake_applied.clear_records(
                context, [(host, effect["record"])],
            )
        _record_manage_status(props, effect, message)
        self.report({"INFO" if ok else "WARNING"}, message)
        if ok and self.action == "CLEAR":
            clear_pinned_effect_settings(props, self.record_token)
        return {"FINISHED" if ok else "CANCELLED"}


class ESPRESSO_OT_set_applied_settings_preset(bpy.types.Operator):
    bl_idname = "espresso.set_applied_settings_preset"
    bl_label = "Set Applied Settings Preset"
    bl_description = "Apply a named preset to these applied-effect settings"
    bl_options = {"INTERNAL"}

    preset_index: bpy.props.IntProperty()
    template_id: bpy.props.StringProperty(default="")
    destination: bpy.props.EnumProperty(
        items=(
            ("DIALOG", "Dialog", "Write the preset into the open settings dialog"),
            ("PINNED", "Pinned", "Write the preset into the pinned applied settings"),
        ),
        default="DIALOG",
    )

    def execute(self, context):
        template = templates.TEMPLATE_BY_ID.get(self.template_id)
        presets = (template or {}).get("combined_presets", [])
        if template is None or not (0 <= self.preset_index < len(presets)):
            return {"CANCELLED"}
        values = presets[self.preset_index]["values"]
        if self.destination == "PINNED":
            props = context.scene.espresso_props
            previous = bool(getattr(props, "suspend_pinned_effect_updates", False))
            props.suspend_pinned_effect_updates = True
            try:
                apply_preset_to_parameter_items(props.pinned_effect_parameters, values)
            finally:
                props.suspend_pinned_effect_updates = previous
            ok, message = sync_pinned_effect_settings(context)
            self.report({"INFO" if ok else "WARNING"}, message)
            return {"FINISHED" if ok else "CANCELLED"}
        items = _dialog_parameter_items(context)
        if items is None:
            return {"CANCELLED"}
        apply_preset_to_parameter_items(items, values)
        return {"FINISHED"}


class ESPRESSO_InvalidMotionItem(bpy.types.PropertyGroup):
    """One finding in the Clear Invalid dialog, with its own tick."""

    enabled: bpy.props.BoolProperty(name="Clear", default=True)
    kind: bpy.props.StringProperty(default="")
    label: bpy.props.StringProperty(default="")
    code: bpy.props.StringProperty(default="")
    slot: bpy.props.StringProperty(default="")
    target_json: bpy.props.StringProperty(default="")


def _scope_hosts(context, source):
    """The hosts the Applied Effects scope is looking at, plus any a
    remembered apply names -- a ghost's host may carry no stamp at all, so
    the stamped-host collector alone would never visit it."""
    from ...apply.motion import applied_motion
    from ...apply.motion import applied_motion_manager
    from ...apply.core import target_memory

    hosts = []
    seen = set()

    def add(host):
        try:
            pointer = host.as_pointer()
        except (AttributeError, ReferenceError):
            return
        if pointer not in seen:
            seen.add(pointer)
            hosts.append(host)

    scene = getattr(context, "scene", None)
    if source == "SCENE":
        for obj in getattr(scene, "objects", ()) or ():
            for host in applied_motion.hosts_for_object(obj):
                add(host)
        for host in applied_motion.scene_hosts(scene):
            add(host)
    else:
        for host, _label, _records in applied_motion_manager._collected(context, source):
            add(host)
        if source == "ACTIVE":
            for host in applied_motion.hosts_for_object(getattr(context, "active_object", None)):
                add(host)
    props = getattr(scene, "espresso_props", None)
    for entry in target_memory._candidate_entries(props) if props is not None else ():
        for target in entry.get("targets") or ():
            resolved, _reason = target_memory.resolve_target_record(target)
            owner = (resolved or {}).get("owner")
            if owner is not None and (source == "SCENE" or owner.as_pointer() in seen):
                add(owner)
    return hosts


class ESPRESSO_OT_clear_invalid_motions(bpy.types.Operator):
    bl_idname = "espresso.clear_invalid_motions"
    bl_label = "Clear Invalid Motions"
    bl_description = (
        "Find records whose drivers are gone, and drivers of ours with no record, "
        "in the current Applied Effects scope -- then clear the ones you tick"
    )
    bl_options = {"REGISTER", "UNDO"}

    items: bpy.props.CollectionProperty(type=ESPRESSO_InvalidMotionItem)
    source: bpy.props.StringProperty(default="ACTIVE", options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        # A scene and nothing else. Blender re-runs poll() right before
        # execute(), in the context the OK button lives in; anything
        # transient demanded here makes OK fail silently.
        return getattr(context, "scene", None) is not None

    def invoke(self, context, _event):
        import json
        from ...apply.motion import applied_reconcile

        props = context.scene.espresso_props
        self.source = str(getattr(props, "driver_target_source", "ACTIVE") or "ACTIVE")
        findings = applied_reconcile.find_invalid(props, _scope_hosts(context, self.source))
        self.items.clear()
        for finding in findings:
            item = self.items.add()
            item.enabled = True
            item.kind = finding["kind"]
            item.label = finding["label"]
            item.code = finding.get("code", "")
            item.slot = finding.get("slot", "")
            item.target_json = json.dumps(finding.get("target") or {}, sort_keys=True)
        if not findings:
            self.report({"INFO"}, "Nothing invalid in this scope.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        from ...apply.motion import applied_reconcile

        layout = self.layout
        groups = (
            (applied_reconcile.DEAD_RECORD, "Invalid motions -- driver missing", "ERROR"),
            (applied_reconcile.DEAD_CHANNEL, "Invalid channels -- one channel missing", "ERROR"),
            (applied_reconcile.GHOST_DRIVER, "Ghost drivers -- ours, but no record", "DRIVER"),
        )
        for kind, heading, icon in groups:
            rows = [item for item in self.items if item.kind == kind]
            if not rows:
                continue
            box = layout.box()
            box.label(text="%s (%d)" % (heading, len(rows)), icon=icon)
            for item in rows:
                box.prop(item, "enabled", text=item.label)
        layout.label(text="Only ticked findings are cleared. Undo restores everything.",
                     icon="INFO")

    def execute(self, context):
        import json
        from ...apply.core import target_memory
        from ...apply.motion import applied_reconcile

        scene = context.scene
        props = scene.espresso_props
        chosen = [item for item in self.items if item.enabled]
        if not chosen:
            self.report({"INFO"}, "Nothing ticked; nothing cleared.")
            return {"CANCELLED"}
        by_host = {}
        ghosts = []
        for item in chosen:
            try:
                target = json.loads(item.target_json or "{}")
            except ValueError:
                continue
            resolved, _reason = target_memory.resolve_target_record(target)
            owner = (resolved or {}).get("owner")
            if owner is None:
                continue
            if item.kind == applied_reconcile.GHOST_DRIVER:
                ghosts.append((owner, str(resolved.get("data_path", "")),
                               int(resolved.get("index", -1))))
            else:
                by_host.setdefault(owner.as_pointer(), (owner, set()))[1].add(
                    (item.code, item.slot))
        purged = trimmed = 0
        for owner, identities in by_host.values():
            more_trimmed, more_purged = applied_reconcile.reconcile_host(owner, only=identities)
            trimmed += more_trimmed
            purged += more_purged
        removed = 0
        for owner, data_path, index in ghosts:
            removed += applied_reconcile.remove_ghost(scene, props, owner, data_path, index)
        props.driver_target_items_signature = ""       # the list rebuilds
        parts = []
        if purged:
            parts.append("%d invalid motion%s" % (purged, "" if purged == 1 else "s"))
        if trimmed:
            parts.append("%d dead channel%s" % (trimmed, "" if trimmed == 1 else "s"))
        if removed:
            parts.append("%d ghost driver%s" % (removed, "" if removed == 1 else "s"))
        self.report({"INFO"}, "Cleared " + ", ".join(parts) + "." if parts
                    else "Nothing resolved; nothing cleared.")
        return {"FINISHED"} if parts else {"CANCELLED"}


CLASSES = (
    ESPRESSO_InvalidMotionItem,
    ESPRESSO_OT_clear_invalid_motions,
    ESPRESSO_StackLayerEditItem,
    ESPRESSO_OT_edit_motion_stack,
    ESPRESSO_OT_toggle_driver_target_group,
    ESPRESSO_OT_select_driver_target_group,
    ESPRESSO_OT_driver_targets_select,
    ESPRESSO_OT_apply_selected_driver_targets,
    ESPRESSO_OT_remove_selected_driver_targets,
    ESPRESSO_OT_edit_driver_target,
    ESPRESSO_OT_toggle_applied_effect_settings_pin,
    ESPRESSO_OT_refresh_pinned_effect_settings,
    ESPRESSO_OT_update_pinned_effect_settings,
    ESPRESSO_OT_set_applied_settings_preset,
    ESPRESSO_OT_manage_applied_effect,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
