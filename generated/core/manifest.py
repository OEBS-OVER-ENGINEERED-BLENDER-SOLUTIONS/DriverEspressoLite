"""Store generated-resource manifests inside existing applied-motion extras."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .contracts import SetupManifest


MANIFEST_KEY = "generated_manifest"


def with_manifest(extras: Mapping[str, Any] | None,
                  value: SetupManifest) -> Dict[str, Any]:
    updated = dict(extras or {})
    updated[MANIFEST_KEY] = value.to_dict()
    return updated


def from_extras(extras: Mapping[str, Any] | None) -> Optional[SetupManifest]:
    if not isinstance(extras, Mapping):
        return None
    raw = extras.get(MANIFEST_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return SetupManifest.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None
