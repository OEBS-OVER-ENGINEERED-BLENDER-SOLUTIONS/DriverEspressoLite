"""Typed, mutation-free plans for source-aware Driver Espresso application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple


SCHEMA_VERSION = 2
SINGLE = "SINGLE"
BROADCAST = "BROADCAST"
MOTION_SET = "MOTION_SET"
SCOPES = frozenset({SINGLE, BROADCAST, MOTION_SET})
SAME_SCOPE = "SAME_SCOPE"
MOTION_TO_SINGLE = "MOTION_TO_SINGLE"
SINGLE_TO_MOTION = "SINGLE_TO_MOTION"


@dataclass(frozen=True)
class TargetClaim:
    data_path: str
    index: int = -1
    channel_id: str = ""
    rest_state: Mapping[str, object] = field(default_factory=dict)
    id_type: str = ""
    id_name: str = ""
    owner_path: str = ""

    @property
    def key(self):
        return (
            self.id_type, self.id_name, self.owner_path,
            self.data_path, int(self.index), self.channel_id,
        )


@dataclass(frozen=True)
class ApplicationPlan:
    scope: str
    origin: str
    targets: Tuple[TargetClaim, ...]
    source: Mapping[str, object] = field(default_factory=dict)
    source_required: bool = False
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class PreflightReport:
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def ok(self):
        return not self.errors


def infer_scope(entry, template=None):
    """Infer old records without rewriting them or confusing scope with origin."""
    explicit = str((entry or {}).get("application_scope") or "").upper()
    if explicit in SCOPES:
        return explicit
    mode = str((entry or {}).get("application_mode") or "").upper()
    if mode == "MOTION" or (template or {}).get("channels"):
        return MOTION_SET
    origin = str((entry or {}).get("apply_kind") or "").lower()
    if origin in {"motion", "camera", "role_set"}:
        return MOTION_SET
    targets = tuple((entry or {}).get("targets") or ())
    return BROADCAST if len(targets) > 1 else SINGLE


def template_scope(template, *, target_count=1):
    """The scope a recipe intends, independent of how it was invoked."""
    if (template or {}).get("channels"):
        return MOTION_SET
    return BROADCAST if int(target_count or 0) > 1 else SINGLE


def scope_transition(stored_scope, requested_scope):
    """Classify Last Target conversion before any Blender mutation occurs."""
    stored = str(stored_scope or SINGLE).upper()
    requested = str(requested_scope or SINGLE).upper()
    if stored == requested or {stored, requested} <= {SINGLE, BROADCAST}:
        return SAME_SCOPE
    if stored == MOTION_SET:
        return MOTION_TO_SINGLE
    if requested == MOTION_SET:
        return SINGLE_TO_MOTION
    return SAME_SCOPE


def preflight_transition(plan, template, *, source_status=None, confirmed=False,
                         has_object_target=False):
    """Validate a Last Target conversion without touching any target driver."""
    base = preflight(plan, source_status=source_status)
    errors = list(base.errors)
    warnings = list(base.warnings)
    requested = template_scope(template, target_count=len(plan.targets))
    transition = scope_transition(plan.scope, requested)
    if transition != SAME_SCOPE and not confirmed:
        errors.append("Confirm the Last Target scope conversion before applying it.")
    if transition == SINGLE_TO_MOTION and not has_object_target:
        errors.append("A Motion Set needs a valid Object or Pose Bone target.")
    if transition == MOTION_TO_SINGLE:
        warnings.append("Choose one remembered channel as the single-property destination.")
    return transition, PreflightReport(tuple(errors), tuple(warnings))


def from_entry(entry, template=None):
    data = dict(entry or {})
    targets = tuple(
        TargetClaim(
            str(item.get("data_path") or ""),
            int(item.get("index", -1)),
            str(item.get("channel_id") or ""),
            dict(item.get("rest_state") or {}),
            str(item.get("id_type") or ""),
            str(item.get("id_name") or ""),
            str(item.get("owner_path") or ""),
        )
        for item in data.get("targets", ()) if isinstance(item, Mapping)
    )
    return ApplicationPlan(
        infer_scope(data, template),
        str(data.get("apply_kind") or "template"),
        targets,
        dict(data.get("source_entry") or {}),
        bool(data.get("source_required", False)),
        int(data.get("application_schema", SCHEMA_VERSION)),
    )


def entry_metadata(plan):
    return {
        "application_schema": SCHEMA_VERSION,
        "application_scope": plan.scope,
        "source_required": bool(plan.source_required),
        "source_entry": dict(plan.source),
    }


def preflight(plan, *, source_status=None):
    errors = []
    if plan.scope not in SCOPES:
        errors.append("Unknown application scope: %s." % plan.scope)
    if not plan.targets:
        errors.append("The application plan has no target channels.")
    keys = [item.key for item in plan.targets]
    if len(keys) != len(set(keys)):
        errors.append("The application plan contains duplicate target channels.")
    if any(not item.data_path for item in plan.targets):
        errors.append("One application target has no property path.")
    if plan.source_required:
        status = dict(source_status or {})
        if status.get("code") != "ACTIVE":
            errors.append(status.get("reason") or "Choose a valid Espresso Input source.")
    return PreflightReport(tuple(errors))
