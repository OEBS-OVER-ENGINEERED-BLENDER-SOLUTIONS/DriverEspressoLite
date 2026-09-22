"""Semantic preview-channel identity. Blender-independent."""

from __future__ import annotations

from dataclasses import dataclass


AXIS_COLORS = {
    "X": (0.90, 0.22, 0.22),
    "Y": (0.22, 0.78, 0.28),
    "Z": (0.24, 0.45, 0.95),
}

_TRANSFORM_PATHS = {
    "location": "location",
    "delta_location": "location",
    "rotation_euler": "euler",
    "delta_rotation_euler": "euler",
    "rotation_quaternion": "quaternion",
    "scale": "scale",
    "delta_scale": "scale",
}


@dataclass(frozen=True)
class ChannelIdentity:
    id: str
    label: str
    unit: str
    axis: str
    family: str
    values: tuple = ()
    visible: bool = True

    @property
    def uses_axis_color(self):
        return self.family in {"location", "euler", "scale"} and self.axis in AXIS_COLORS


def identity_from_channel(channel, values=()):
    data_path = str((channel or {}).get("data_path") or "")
    index = int((channel or {}).get("index", -1))
    family = _TRANSFORM_PATHS.get(data_path, "scalar")
    axis = ""
    if family == "quaternion":
        axis = ("W", "X", "Y", "Z")[index] if 0 <= index < 4 else ""
    elif family in {"location", "euler", "scale"} and 0 <= index < 3:
        axis = ("X", "Y", "Z")[index]
    unit = str((channel or {}).get("unit") or "")
    if not unit:
        unit = {
            "location": "m",
            "euler": "rad",
            "quaternion": "quat",
            "scale": "x",
        }.get(family, "")
    return ChannelIdentity(
        id=str((channel or {}).get("id") or ""),
        label=str((channel or {}).get("label") or (channel or {}).get("id") or "Channel"),
        unit=unit,
        axis=axis,
        family=family,
        values=tuple(values),
        visible=True,
    )


def overlay_compatible(selected, sibling):
    """Whether a sibling may overlay the selected channel."""
    if selected is None or sibling is None or selected.id == sibling.id:
        return False
    if selected.family != sibling.family:
        return False
    if selected.unit != sibling.unit:
        return False
    return True


def axis_color(identity):
    if identity is None or not identity.uses_axis_color:
        return None
    return AXIS_COLORS[identity.axis]


def compatible_overlay_ids(selected, siblings):
    return tuple(
        item.id for item in siblings
        if overlay_compatible(selected, item)
    )
