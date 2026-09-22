"""Blender-free helpers for deterministic multi-target time offsets."""

from __future__ import annotations

import re
import random
import math
from dataclasses import dataclass
from collections.abc import Mapping

from ..expression import utils


_FRAME = re.compile(r"\bframe\b")


def shift_expression_time(expression, offset_frames):
    """Shift scene time without changing the expression's motion shape."""
    offset = float(offset_frames)
    if offset == 0.0 or not _FRAME.search(expression):
        return expression
    literal = utils._format_driver_literal(offset)
    shifted = _FRAME.sub(f"(frame-{literal})", expression)
    return utils._compact_generated_expression(shifted)


def shift_built_channels(built_channels, offset_frames):
    """Return defensive channel copies sharing one coherent target offset."""
    shifted = []
    for channel in built_channels:
        item = dict(channel)
        item["expression"] = shift_expression_time(
            channel["expression"],
            offset_frames,
        )
        shifted.append(item)
    return shifted


def ordered_active_first(items, active=None, *, reverse=False):
    """Stable order without pretending Blender exposes selection chronology."""
    unique = []
    seen = set()
    for item in items:
        marker = id(item)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    rest = sorted(
        (item for item in unique if item is not active),
        key=lambda item: getattr(item, "name", "").casefold(),
    )
    ordered = ([active] if active in unique else []) + rest
    if reverse:
        ordered.reverse()
    return ordered


def offsets_for_count(count, start_offset, offset_step):
    return [float(start_offset) + index * float(offset_step) for index in range(count)]


def order_indices(count, mode="FORWARD", *, seed=0):
    """Return stable member indices for one sequencing strategy.

    The result expresses order only; callers remain responsible for mapping an
    order position to a delay.  Keeping those concerns separate lets the same
    centre-out or seeded order drive drivers, nodes, or sampled Actions.
    """
    count = max(0, int(count))
    mode = str(mode or "FORWARD").upper()
    indices = list(range(count))
    if mode == "FORWARD":
        return indices
    if mode == "REVERSE":
        return list(reversed(indices))
    if mode == "ALTERNATING":
        return indices[::2] + list(reversed(indices[1::2]))
    if mode == "CENTRE_OUT":
        if not indices:
            return []
        centre_left = (count - 1) // 2
        ordered = [centre_left]
        if count % 2 == 0:
            ordered.append(centre_left + 1)
        distance = 1
        while len(ordered) < count:
            left = centre_left - distance
            right = centre_left + distance + (1 if count % 2 == 0 else 0)
            if left >= 0:
                ordered.append(left)
            if right < count:
                ordered.append(right)
            distance += 1
        return ordered
    if mode == "SEEDED":
        rng = random.Random(int(seed))
        rng.shuffle(indices)
        return indices
    raise ValueError("Unknown stagger order mode: %s" % mode)


def distributed_offsets(
    count,
    start_offset,
    offset_step,
    *,
    mode="ARITHMETIC",
    distances=None,
    custom=None,
):
    """Return per-target delays without depending on Blender selection state."""
    count = max(0, int(count))
    start = float(start_offset)
    step = float(offset_step)
    mode = str(mode or "ARITHMETIC").upper()
    if mode == "ARITHMETIC":
        return offsets_for_count(count, start, step)
    if mode == "EXPONENTIAL":
        return [start + step * (2.0 ** index - 1.0) for index in range(count)]
    if mode == "DISTANCE":
        values = list(distances or ())
        if len(values) != count:
            raise ValueError("DISTANCE distribution requires %d distance values." % count)
        return [start + float(distance) * step for distance in values]
    if mode == "CUSTOM":
        values = list(custom or ())
        if len(values) != count:
            raise ValueError("CUSTOM distribution requires %d offset values." % count)
        return [start + float(value) for value in values]
    raise ValueError("Unknown stagger distribution mode: %s" % mode)


@dataclass(frozen=True)
class SequenceStep:
    """One resolved target in an artist-previewable sequence."""

    item: object
    label: str
    position: tuple[float, float, float]
    source_index: int
    offset: float


def _sequence_target(item, source_index):
    if isinstance(item, Mapping):
        label = str(item.get("label") or item.get("name") or source_index)
        position = item.get("position") or (0.0, 0.0, 0.0)
        payload = item.get("item", item)
    else:
        label = str(getattr(item, "name", item))
        position = getattr(item, "position", (0.0, 0.0, 0.0))
        payload = item
    values = tuple(float(value) for value in position)
    if len(values) != 3:
        raise ValueError("Sequence target positions must contain X, Y, and Z.")
    return {
        "item": payload,
        "label": label,
        "position": values,
        "source_index": int(source_index),
    }


def _decelerating_offsets(count, start, step):
    offsets = [float(start)]
    current = float(start)
    for remaining in range(max(0, count - 1), 0, -1):
        current += float(step) * remaining
        offsets.append(current)
    return offsets[:count]


def sequence_plan(
    targets,
    *,
    order="SELECTION",
    direction="FORWARD",
    distribution="EVEN",
    start_offset=0.0,
    offset_step=6.0,
    seed=0,
):
    """Resolve ordering and delays once for drivers, nodes, or sampled data."""
    normalized = [_sequence_target(item, index) for index, item in enumerate(targets)]
    order = str(order or "SELECTION").upper()
    if order in {"SELECTION", "ACTIVE_NAME"}:
        ordered = normalized
    elif order == "NAME":
        ordered = sorted(normalized, key=lambda item: item["label"].casefold())
    elif order in {"X", "Y", "Z"}:
        axis = {"X": 0, "Y": 1, "Z": 2}[order]
        ordered = sorted(normalized, key=lambda item: (item["position"][axis], item["label"].casefold()))
    elif order == "DISTANCE":
        ordered = sorted(normalized, key=lambda item: (math.dist((0.0, 0.0, 0.0), item["position"]), item["label"].casefold()))
    elif order == "RANDOM":
        ordered = list(normalized)
        random.Random(int(seed)).shuffle(ordered)
    else:
        raise ValueError("Unknown sequence order: %s" % order)

    direction = str(direction or "FORWARD").upper()
    indices = order_indices(len(ordered), direction, seed=seed)
    ordered = [ordered[index] for index in indices]

    distribution = str(distribution or "EVEN").upper()
    if distribution == "EVEN":
        offsets = distributed_offsets(len(ordered), start_offset, offset_step)
    elif distribution == "ACCELERATE":
        offsets = distributed_offsets(
            len(ordered), start_offset, offset_step, mode="EXPONENTIAL",
        )
    elif distribution == "DECELERATE":
        offsets = _decelerating_offsets(
            len(ordered), start_offset, offset_step,
        )
    elif distribution == "DISTANCE":
        origin = ordered[0]["position"] if ordered else (0.0, 0.0, 0.0)
        distances = [math.dist(origin, item["position"]) for item in ordered]
        offsets = distributed_offsets(
            len(ordered), start_offset, offset_step,
            mode="DISTANCE", distances=distances,
        )
    else:
        raise ValueError("Unknown sequence distribution: %s" % distribution)

    return [
        SequenceStep(
            item=item["item"],
            label=item["label"],
            position=item["position"],
            source_index=item["source_index"],
            offset=float(offset),
        )
        for item, offset in zip(ordered, offsets)
    ]


def preview_lines(plan, limit=8):
    """Return the compact labels the popup previews show."""
    limit = max(0, int(limit))
    lines = [f"{step.label}  {step.offset:g}f" for step in list(plan)[:limit]]
    remaining = len(plan) - limit
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return lines

