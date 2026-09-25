"""Restore a copied node graph in place, retaining surviving artist nodes.

Blender cannot assign a material's embedded node_tree. Keeping its ID and
restoring missing nodes also retains every material user and surviving socket.
The caller owns copies and restores nested group identities before using this.
"""

from __future__ import annotations

import bpy


_INTERNAL_SETTINGS = ('color_ramp', 'mapping', 'texture_mapping', 'color_mapping', 'image_user')


def _value(item):
    if isinstance(item, (str, bool, int, float)) or item is None:
        return item
    if isinstance(item, bpy.types.ID):
        return (item.__class__.__name__, item.name,
                item.library.filepath if item.library else '')
    if hasattr(item, 'to_dict'):
        return item.to_dict()
    if isinstance(item, set):
        return tuple(sorted(item))
    return tuple(item)


def _rna_state(source, skip=()):
    values = {}
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or prop.type == 'COLLECTION' or name in {'rna_type', *skip}:
            continue
        current = getattr(source, name)
        if prop.type == 'POINTER' and current is not None and not isinstance(current, bpy.types.ID):
            continue
        values[name] = _value(current)
    return values


def _properties(source, destination):
    for key in list(destination.keys()):
        if key not in source:
            del destination[key]
    for key in source.keys():
        destination[key] = source[key]


def _rna(source, destination, skip=()):
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in {'rna_type', 'name', 'type', *skip}:
            continue
        if prop.type == 'COLLECTION':
            continue
        current = getattr(source, name)
        if prop.type == 'POINTER' and current is not None and not isinstance(current, bpy.types.ID):
            continue
        if getattr(destination, name) != current:
            setattr(destination, name, current)


def _socket_values(source, destination):
    by_id = {socket.identifier: socket for socket in destination}
    for socket in source:
        target = by_id.get(socket.identifier)
        if target is None:
            raise RuntimeError(f'Missing socket {socket.identifier} on {socket.node.name}')
        _rna(socket, target)


def _internal_settings(source, destination):
    # These are read-only RNA pointers with editable content, unlike ID pointers.
    for name in _INTERNAL_SETTINGS:
        settings = getattr(source, name, None)
        if settings is None or not hasattr(settings, 'bl_rna'):
            continue
        target = getattr(destination, name)
        _rna(settings, target)
        if isinstance(settings, bpy.types.ColorRamp):
            while len(target.elements) > 2:
                target.elements.remove(target.elements[-1])
            for index, element in enumerate(settings.elements):
                out = target.elements[index] if index < 2 else target.elements.new(element.position)
                _rna(element, out)
        elif isinstance(settings, bpy.types.CurveMapping):
            target.initialize()
            for curve, out in zip(settings.curves, target.curves):
                while len(out.points) > 2:
                    out.points.remove(out.points[-2])
                for index, point in enumerate(curve.points):
                    dst = out.points[index] if index < 2 else out.points.new(*point.location)
                    _rna(point, dst)
                # Adding a point deselects existing points in Blender. Restore
                # selection after the full curve exists, in its final order.
                for point, dst in zip(curve.points, out.points):
                    dst.select = point.select
            target.update()
        else:
            _internal_settings(settings, target)


def _internal_state(source):
    values = {}
    for name in _INTERNAL_SETTINGS:
        settings = getattr(source, name, None)
        if settings is None or not hasattr(settings, 'bl_rna'):
            continue
        state = {'rna': _rna_state(settings)}
        if isinstance(settings, bpy.types.ColorRamp):
            state['elements'] = [_rna_state(element) for element in settings.elements]
        elif isinstance(settings, bpy.types.CurveMapping):
            state['curves'] = [[_rna_state(point) for point in curve.points] for curve in settings.curves]
        else:
            state['internal'] = _internal_state(settings)
        values[name] = state
    return values


def restore(tree, copied):
    """Restore nodes, links, sockets and properties without swapping the tree."""
    if tree is copied:
        return
    sources = {node.name: node for node in copied.nodes}
    for node in list(tree.nodes):
        source = sources.get(node.name)
        if source is None or source.bl_idname != node.bl_idname:
            tree.nodes.remove(node)
    for source in copied.nodes:
        node = tree.nodes.get(source.name)
        if node is None:
            node = tree.nodes.new(source.bl_idname)
            node.name = source.name
        _rna(source, node, {'parent', 'location', 'location_absolute'})
        _internal_settings(source, node)
        _properties(source, node)
    # Parenting changes local coordinates, so bind frames before positions.
    for source in copied.nodes:
        node = tree.nodes[source.name]
        node.parent = tree.nodes.get(source.parent.name) if source.parent else None
        node.location = source.location
        _socket_values(source.inputs, node.inputs)
        _socket_values(source.outputs, node.outputs)
    def key(link):
        return (link.from_node.name, link.from_socket.identifier,
                link.to_node.name, link.to_socket.identifier)
    wanted = {key(link) for link in copied.links}
    for link in list(tree.links):
        if key(link) not in wanted:
            tree.links.remove(link)
    existing = {key(link) for link in tree.links}
    for link in copied.links:
        if key(link) in existing:
            continue
        source = tree.nodes[link.from_node.name]
        target = tree.nodes[link.to_node.name]
        out = next(s for s in source.outputs if s.identifier == link.from_socket.identifier)
        inp = next(s for s in target.inputs if s.identifier == link.to_socket.identifier)
        tree.links.new(out, inp)
    _properties(copied, tree)


def fingerprint(tree):
    """Plain graph state remains verifiable after holding copies are discarded."""
    return {
        'nodes': sorted((n.name, n.bl_idname, n.parent.name if n.parent else '',
                         _rna_state(n, {'parent', 'location_absolute'}), _internal_state(n),
                         tuple((s.identifier, _rna_state(s)) for s in n.inputs),
                         tuple((s.identifier, _rna_state(s)) for s in n.outputs),
                         tuple(sorted((k, _value(n[k])) for k in n.keys()))) for n in tree.nodes),
        'links': sorted((l.from_node.name, l.from_socket.identifier,
                         l.to_node.name, l.to_socket.identifier) for l in tree.links),
        'interface': [(s.item_type, s.name, getattr(s, 'identifier', ''),
                       getattr(s, 'in_out', ''), getattr(s, 'socket_type', ''),
                       _value(getattr(s, 'default_value', None)))
                      for s in tree.interface.items_tree],
    }
