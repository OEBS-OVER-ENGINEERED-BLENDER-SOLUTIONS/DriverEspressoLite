"""One rollback boundary for apply, key writing, and bake cleanup."""
from contextlib import contextmanager, nullcontext
from functools import wraps
from types import SimpleNamespace

import bpy

_active = False
_holding_user_pointers = frozenset()


def holding_user_pointers():
    """IDs created by the active rollback snapshot, excluding artist data."""
    return _holding_user_pointers


def _snapshot_holding_user_pointers(snapshot):
    copies = [
        item['copy']
        for entries in (snapshot._objects, snapshot._data_copies,
                        snapshot._collections, snapshot._node_groups)
        for item in entries
    ]
    copies.extend(item['graph_copy'] for item in snapshot._ids if 'graph_copy' in item)
    return frozenset(copy.as_pointer() for copy in copies)


def _value(value):
    if hasattr(value, 'to_dict'):
        return {key: _value(item) for key, item in value.items()}
    if hasattr(value, 'to_list'):
        return value.to_list()
    return value


def _properties(owner):
    return {key: _value(value) for key, value in owner.items()}


def _restore_properties(owner, values):
    for key in list(owner.keys()):
        if key not in values:
            del owner[key]
    for key, value in values.items():
        current = owner.get(key)
        if isinstance(value, dict) and hasattr(current, 'items'):
            _restore_properties(current, value)
        elif _value(current) != value:
            owner[key] = value


def _target_values(targets):
    """Plain RNA values have no Action or custom-property snapshot to restore."""
    from . import bake
    from ...generated.resources import clear_snapshot
    captured = {}
    for target in targets:
        owner, path = bake._resolve_owner_and_path(target.owner, target.data_path)
        index = int(getattr(target, 'index', -1))
        value = owner.path_resolve(path)
        if index >= 0:
            values = [(index, value[index])]
        elif hasattr(value, '__len__') and not isinstance(value, str):
            values = list(enumerate(value))
        else:
            values = [(-1, value)]
        identity = clear_snapshot._owner_identity(owner)
        for component, value in values:
            captured[(owner.as_pointer(), path, component)] = (
                identity, path, component, value,
            )
    return list(captured.values())


def _restore_target_values(captured):
    from . import bake
    from ...generated.resources import clear_snapshot
    for identity, path, index, value in captured:
        owner = clear_snapshot._resolve_owner(identity)
        if owner is None:
            raise RuntimeError(f"Could not restore the owner of {path}.")
        # Restored live drivers must retain control of their evaluated output.
        if bake._find_driver(owner, path, index) is not None:
            continue
        if index >= 0:
            owner.path_resolve(path)[index] = value
        elif path.endswith(']'):
            import ast
            prefix, _separator, key = path.rpartition('[')
            container = owner.path_resolve(prefix) if prefix else owner
            container[ast.literal_eval(key[:-1])] = value
        else:
            prefix, _separator, attribute = path.rpartition('.')
            container = owner.path_resolve(prefix) if prefix else owner
            setattr(container, attribute, value)


def _bags(action):
    direct = getattr(action, 'fcurves', None)
    if direct is not None:
        return [direct]
    return [bag.fcurves for layer in action.layers for strip in layer.strips
            for bag in strip.channelbags]


def _action_state(action):
    from ...generated.resources import fcurve_snapshot
    return {
        'slots': list(getattr(action, 'slots', ())),
        'layers': list(getattr(action, 'layers', ())),
        'bags': [(bag, [(curve.data_path, curve.array_index, fcurve_snapshot.capture(curve),
                   (curve.group.name, fcurve_snapshot._values(curve.group)) if curve.group else None)
                   for curve in bag]) for bag in _bags(action)],
    }


def _restore_action(action, state):
    from ...generated.resources import fcurve_snapshot
    for bag, curves in state['bags']:
        wanted = {(path, index) for path, index, _state, _group in curves}
        for curve in list(bag):
            if (curve.data_path, curve.array_index) not in wanted:
                bag.remove(curve)
        for path, index, captured, group_state in curves:
            curve = bag.find(path, index=index)
            if curve is None:
                curve = bag.new(path, index=index)
            captured = dict(captured)
            captured['group'] = None
            if group_state:
                groups = getattr(action, 'groups', None)
                if groups is None:
                    groups = next(channelbag.groups for layer in action.layers
                                  for strip in layer.strips for channelbag in strip.channelbags
                                  if channelbag.fcurves == bag)
                name, values = group_state
                group = groups.get(name) or groups.new(name)
                fcurve_snapshot._assign(group, values)
                captured['group'] = group
            if fcurve_snapshot.errors(curve, captured, path):
                fcurve_snapshot.restore(curve, captured)
    known = [bag for bag, _curves in state['bags']]
    for bag in _bags(action):
        if bag not in known:
            for curve in list(bag):
                bag.remove(curve)
    for layer in list(getattr(action, 'layers', ())):
        if layer not in state['layers']:
            action.layers.remove(layer)
    for slot in list(getattr(action, 'slots', ())):
        if slot not in state['slots']:
            action.slots.remove(slot)


def _selected_motion_channels(context, template, bone=None):
    from ...catalogue import templates
    from ...ui.state import props as espresso_props
    from ...apply.motion import motion_channels

    channels = templates.template_channels(template)
    enabled = espresso_props.enabled_channel_ids_for_template(
        context.scene.espresso_props, template,
    )
    if enabled is not None:
        channels = [channel for channel in channels if channel.get('id') in enabled]
    if not channels:
        return []
    if bone is not None:
        selected_template = dict(template, channels=channels)
        return motion_channels.effective_channels(selected_template, bone)
    return channels


def _planned_apply_targets(context):
    """Resolve the selected recipe's existing Blender destinations before Apply."""
    from ...catalogue import templates
    from ...ui.state import props as espresso_props
    from ...apply.motion import motion_channels

    obj = context.active_object
    template = espresso_props.get_current_template(context.scene.espresso_props)
    kind = str((template.get('application_target') or {}).get('kind') or '')
    scalar = {
        'CAMERA_LENS': (obj.data, 'lens'),
        'CAMERA_SHIFT_X': (obj.data, 'shift_x'),
        'CAMERA_SHIFT_Y': (obj.data, 'shift_y'),
        'CAMERA_FOCUS_DISTANCE': (obj.data, 'dof.focus_distance'),
        'SCENE_EXPOSURE': (context.scene, 'view_settings.exposure'),
    } if obj is not None and obj.type == 'CAMERA' else {}
    if kind in scalar:
        owner, path = scalar[kind]
        return [SimpleNamespace(owner=owner, data_path=path, index=-1)]
    if kind == 'CAMERA_FORWARD_DOLLY' and obj is not None:
        return [SimpleNamespace(owner=obj, data_path='location', index=index)
                for index in range(3)]
    if templates.has_motion_plan(template) and obj is not None:
        bone = context.active_pose_bone if context.mode == 'POSE' else None
        channels = _selected_motion_channels(context, template, bone)
        if bone is not None:
            return motion_channels.motion_targets_for_bone(
                obj, bone, template, channels=channels,
            )
        return motion_channels.motion_targets_for_object(
            obj, template, channels=channels,
        )
    raise RuntimeError('The selected template has no bakeable destination.')


@contextmanager
def bake_transaction(context, *, targets=(), objects=(), chosen=()):
    """Join an outer bake, or retain all state until its cleanup also succeeds."""
    global _active, _holding_user_pointers
    if _active:
        yield
        return
    from . import bake
    from ...apply import applied_motion, internal_helpers
    from ...ui.actions import bake_applied
    from ...generated.resources import clear_snapshot

    reason = bake.preflight_targets(targets)
    if reason:
        raise RuntimeError(reason)
    values_before = _target_values(targets)
    owners = []
    def add(owner):
        if isinstance(owner, bpy.types.ID) and owner.is_editable and owner not in owners:
            owners.append(owner)
    for target in targets:
        add(bake._resolve_owner_and_path(target.owner, target.data_path)[0])
    for obj in objects:
        for owner, _label in bake._driver_blocks_for_object(obj):
            add(owner)
    for host, _record in chosen:
        add(host)
    add(context.scene)
    world = context.scene.world
    if world is not None:
        add(world)
        add(world.node_tree)
    add(getattr(context.scene, 'compositing_node_group', None))
    # Driver dependencies are the ownership map for private helper properties.
    for owner in owners:
        for curve in getattr(getattr(owner, 'animation_data', None), 'drivers', ()) or ():
            for resource in internal_helpers.resources_from_driver(curve.driver):
                helper = resource[0] if isinstance(resource, (tuple, list)) else None
                add(helper)
            for variable in curve.driver.variables:
                for target in variable.targets:
                    add(target.id)
    records = list(chosen)
    for owner in owners:
        for record in applied_motion.read(owner):
            if not any(host == owner and item == record for host, item in records):
                records.append((owner, record))
    frame, subframe = context.scene.frame_current, context.scene.frame_subframe
    initial_objects = set(bpy.data.objects.keys())
    initial_groups = set(bpy.data.node_groups.keys())
    initial_collections = set(bpy.data.collections.keys())
    properties = [(clear_snapshot._owner_identity(owner), _properties(owner)) for owner in owners]
    props = getattr(context.scene, 'espresso_props', None)
    props_state = _properties(props) if props is not None else None
    driver_paths = [(clear_snapshot._owner_identity(owner), {(c.data_path, c.array_index) for c in
                     getattr(getattr(owner, 'animation_data', None), 'drivers', ()) or ()})
                    for owner in owners]
    actions = {}
    for owner in owners:
        action = getattr(getattr(owner, 'animation_data', None), 'action', None)
        if action is not None and action not in actions:
            actions[action] = _action_state(action)
    # Scoped helper cleanup runs while rollback data is still alive. A deferred
    # artist cleanup after discard could fail after recovery became impossible.
    _hosts, snapshot = bake_applied.capture_clear_snapshot(
        records, extra_fcurve_owners=owners, defer_helper_cleanup=False,
    )
    for owner in owners:
        snapshot.preserve_action_identity(owner)
    for entry in snapshot._actions.values():
        action = entry['original']
        entry['preserve_original'] = True
        if action not in actions:
            actions[action] = _action_state(action)
    _holding_user_pointers = _snapshot_holding_user_pointers(snapshot)
    _active = True
    purge_guard = getattr(internal_helpers, 'suspend_purge', nullcontext)()
    purge_guard.__enter__()
    try:
        yield
    except Exception:
        for action, state in actions.items():
            if action.is_editable:
                _restore_action(action, state)
        for identity, paths in driver_paths:
            owner = clear_snapshot._resolve_owner(identity)
            if owner is None:
                continue
            for curve in list(getattr(getattr(owner, 'animation_data', None), 'drivers', ()) or ()):
                if (curve.data_path, curve.array_index) not in paths:
                    owner.animation_data.drivers.remove(curve)
        if not snapshot.restore():
            raise RuntimeError('Bake rollback failed: ' + '; '.join(snapshot.verify()['errors']))
        for identity, values in properties:
            owner = clear_snapshot._resolve_owner(identity)
            if owner is not None:
                _restore_properties(owner, values)
        if props is not None:
            _restore_properties(props, props_state)
        _restore_target_values(values_before)
        for obj in list(bpy.data.objects):
            if obj.name not in initial_objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        for group in list(bpy.data.node_groups):
            if group.name not in initial_groups and not group.users:
                bpy.data.node_groups.remove(group)
        for collection in list(bpy.data.collections):
            if collection.name not in initial_collections and not collection.objects and not collection.children:
                bpy.data.collections.remove(collection)
        raise
    finally:
        try:
            snapshot.discard()
            context.scene.frame_set(frame, subframe=subframe)
        finally:
            purge_guard.__exit__(None, None, None)
            _active = False
            _holding_user_pointers = frozenset()


def atomic_bake_operator(cls):
    """Keep the operator's apply and metadata cleanup in the writer transaction."""
    execute = cls.execute
    operator_id = cls.bl_idname
    @wraps(execute)
    def wrapped(self, context):
        objects = list(self._objects(context) if hasattr(self, '_objects') else context.selected_objects)
        if context.active_object is not None and context.active_object not in objects:
            objects.append(context.active_object)
        if context.scene.camera is not None and context.scene.camera not in objects:
            objects.append(context.scene.camera)
        targets = []
        if hasattr(self, '_discover'):
            targets, _unresolved = self._discover(context)
        if operator_id == 'espresso.bake_last_target':
            from ...apply.core import target_memory
            from types import SimpleNamespace
            resolved, _reason = target_memory.resolve_entry(
                target_memory.latest_entry(context.scene.espresso_props) or {})
            targets = [SimpleNamespace(**item) for item in resolved or ()]
        if operator_id.endswith('_to_button'):
            from ...ui.menus.context_menu import resolve_button_driver_targets
            targets = resolve_button_driver_targets(context)
        preflight = getattr(self, '_bake_preflight', None)
        if preflight is not None:
            reason = preflight(context, targets)
            if reason:
                self.report({'WARNING'}, reason)
                return {'CANCELLED'}
        try:
            if operator_id == 'espresso.apply_and_bake_motion':
                targets = _planned_apply_targets(context)
            elif operator_id == 'espresso.apply_and_bake_to_button':
                from ...catalogue import templates
                from ...ui.state import props as espresso_props

                template = espresso_props.get_current_template(context.scene.espresso_props)
                if template and templates.has_motion_plan(template) and targets:
                    if len(templates.template_channels(template)) > 1:
                        from ...apply.motion import motion_channels

                        owner = motion_channels.resolve_motion_object(targets[0].owner)
                        if owner is not None:
                            channels = _selected_motion_channels(context, template)
                            targets = motion_channels.motion_targets_for_object(
                                owner, template, channels=channels,
                            )
            if operator_id in {
                'espresso.apply_and_bake_motion',
                'espresso.apply_and_bake_to_button',
            }:
                from ...ui.actions import bake_applied

                if any(bake_applied.target_has_foreign_motion(target) for target in targets):
                    self.report(
                        {'WARNING'},
                        'The selected destination belongs to motion not available '
                        'in this edition; it was left unchanged.',
                    )
                    return {'CANCELLED'}
            with bake_transaction(context, targets=targets, objects=objects):
                result = execute(self, context)
                if 'FINISHED' not in result:
                    raise RuntimeError('Bake cancelled; previous state restored.')
            return result
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
    cls.execute = wrapped
    return cls
