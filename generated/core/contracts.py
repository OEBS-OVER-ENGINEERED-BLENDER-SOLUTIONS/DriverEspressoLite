"""Serializable contracts shared by all generated-resource services."""

from __future__ import annotations

from enum import Enum


class Ownership(str, Enum):
    """Who may remove a resource when an Espresso setup is cleared."""

    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"
    BORROWED = "BORROWED"
    USER_OWNED = "USER_OWNED"


class ResourceKind(str, Enum):
    NODE_GROUP = "NODE_GROUP"
    NODE_INSTANCE = "NODE_INSTANCE"
    OBJECT = "OBJECT"
    COLLECTION = "COLLECTION"
    PROPERTY = "PROPERTY"
    DRIVER = "DRIVER"
    CONSTRAINT = "CONSTRAINT"
    MODIFIER = "MODIFIER"
    ACTION = "ACTION"
    MATERIAL = "MATERIAL"
    DATABLOCK = "DATABLOCK"


