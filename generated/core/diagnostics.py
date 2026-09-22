"""Small, explicit health reports for generated setup manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Tuple

from .contracts import ResourceRecord, SetupManifest


class Status(str, Enum):
    HEALTHY = "HEALTHY"
    BROKEN_REFERENCE = "BROKEN_REFERENCE"
    EMPTY = "EMPTY"
    DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"
    BINDING_DRIFT = "BINDING_DRIFT"
    ORPHAN_RESOURCE = "ORPHAN_RESOURCE"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    NON_PORTABLE = "NON_PORTABLE"


@dataclass(frozen=True)
class Report:
    setup_id: str
    status: Status
    present: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    issues: Tuple[str, ...] = ()


def inspect(value: SetupManifest,
            resolver: Callable[[ResourceRecord], object]) -> Report:
    present = []
    missing = []
    for resource in value.resources:
        try:
            resolved = resolver(resource)
        except Exception:
            resolved = None
        (present if resolved else missing).append(resource.resource_id)
    ids = [item.resource_id for item in value.resources]
    issues = []
    if len(ids) != len(set(ids)):
        status = Status.DUPLICATE_RESOURCE
        issues.append("The setup manifest contains duplicate resource IDs.")
    elif value.schema_version != 1:
        status = Status.UNSUPPORTED_VERSION
        issues.append("The setup manifest schema version is unsupported.")
    elif value.portability not in {"NATIVE_PLAYBACK", "LIVE_NATIVE", "BAKE_TO_NATIVE"}:
        status = Status.NON_PORTABLE
        issues.append("The setup does not declare a supported portability contract.")
    elif missing:
        status = Status.BROKEN_REFERENCE
    elif not value.resources:
        status = Status.EMPTY
    else:
        status = Status.HEALTHY
    return Report(value.setup_id, status, tuple(present), tuple(missing), tuple(issues))


def inspect_bindings(value: SetupManifest, resolver, *, binding_validator=None,
                     referenced_resource_ids=()):
    """Add binding-drift and orphan checks without scanning Blender continuously."""
    report = inspect(value, resolver)
    if report.status is not Status.HEALTHY:
        return report
    issues = []
    if binding_validator is not None:
        for resource in value.resources:
            resolved = resolver(resource)
            if resolved is not None and not binding_validator(resource, resolved):
                issues.append("Binding drift: %s" % resource.resource_id)
    referenced = set(str(item) for item in referenced_resource_ids)
    manifest_ids = set(value.resource_ids())
    orphans = sorted(referenced - manifest_ids)
    if orphans:
        issues.extend("Orphan resource: %s" % item for item in orphans)
    if any(item.startswith("Binding drift") for item in issues):
        status = Status.BINDING_DRIFT
    elif issues:
        status = Status.ORPHAN_RESOURCE
    else:
        return report
    return Report(value.setup_id, status, report.present, report.missing, tuple(issues))
