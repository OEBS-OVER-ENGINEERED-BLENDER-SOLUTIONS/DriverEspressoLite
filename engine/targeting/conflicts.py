"""Preflight conflict detection for Espresso-owned and unmanaged targets.

This module deliberately contains no Blender imports.  Apply routes can inspect
and resolve policy decisions before they mutate Blender data, while Blender
adapters remain responsible for converting real drivers/manifests into claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Tuple


class ConflictPolicy(str, Enum):
    ALWAYS_ASK = "ALWAYS_ASK"
    AUTO_REPLACE = "AUTO_REPLACE"
    AUTO_LAYER = "AUTO_LAYER"
    NEVER_PROMPT = "NEVER_PROMPT"


class DecisionAction(str, Enum):
    APPLY = "APPLY"
    UPDATE = "UPDATE"
    ASK = "ASK"
    REPLACE = "REPLACE"
    LAYER = "LAYER"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ChannelClaim:
    channel: str
    effect_id: str = ""
    label: str = ""
    managed: bool = True
    layer_supported: bool = False
    resource_ids: Tuple[str, ...] = ()

    def __post_init__(self):
        if not str(self.channel).strip():
            raise ValueError("A channel claim requires a channel path")
        object.__setattr__(self, "channel", str(self.channel))
        object.__setattr__(self, "effect_id", str(self.effect_id))
        object.__setattr__(self, "label", str(self.label))
        object.__setattr__(self, "resource_ids", tuple(str(x) for x in self.resource_ids))


@dataclass(frozen=True)
class ConflictReport:
    target_label: str
    incoming_effect_id: str
    incoming_label: str
    incoming_channels: Tuple[str, ...]
    existing_claims: Tuple[ChannelClaim, ...] = ()
    occupied_claims: Tuple[ChannelClaim, ...] = ()
    partial: bool = False

    @property
    def has_conflict(self) -> bool:
        return bool(self.occupied_claims)

    @property
    def same_effect(self) -> bool:
        return bool(self.occupied_claims) and all(
            claim.effect_id == self.incoming_effect_id and claim.managed
            for claim in self.occupied_claims
        )

    @property
    def unmanaged(self) -> bool:
        return any(not claim.managed for claim in self.occupied_claims)

    @property
    def all_layerable(self) -> bool:
        return bool(self.occupied_claims) and all(
            claim.layer_supported and claim.managed for claim in self.occupied_claims
        )

    @property
    def resource_count(self) -> int:
        # A claim may describe a plain driver and therefore have no generated
        # resource IDs. The user-facing count is channels occupied, not only
        # generated resources.
        return len(self.occupied_claims)


@dataclass(frozen=True)
class ConflictDecision:
    action: DecisionAction
    allowed: bool
    prompt: bool
    report: ConflictReport
    reason: str = ""


def format_conflict_message(decision: ConflictDecision) -> str:
    """Return a concise, user-facing preflight message."""
    report = decision.report
    if decision.action is DecisionAction.APPLY:
        return "No existing effect occupies the requested channels."
    if decision.action is DecisionAction.UPDATE:
        return "This Espresso effect is already on the target and will be updated."
    if decision.action is DecisionAction.REPLACE:
        return "A different Espresso effect occupies this target and will be replaced."
    if decision.action is DecisionAction.LAYER:
        return "A different layerable Espresso effect occupies this target; the new effect will be layered."
    if decision.action is DecisionAction.ASK:
        return (
            f"{report.target_label} already has an Espresso effect on "
            f"{report.resource_count} requested channel(s). Choose replace, "
            "layer, or cancel before applying."
        )
    if decision.action is DecisionAction.BLOCKED:
        return decision.reason or "The requested channels are occupied and were left unchanged."
    return decision.reason or "The motion could not be applied safely."


def inspect_target(*, target_label: str, incoming_effect_id: str,
                   incoming_label: str, incoming_channels: Iterable[str],
                   existing_claims: Iterable[ChannelClaim]) -> ConflictReport:
    """Build a deterministic report without touching Blender state."""
    channels = tuple(dict.fromkeys(str(channel) for channel in incoming_channels))
    existing = tuple(existing_claims)
    occupied = tuple(claim for claim in existing if claim.channel in channels)
    occupied_paths = {claim.channel for claim in occupied}
    partial = bool(occupied) and len(occupied_paths) < len(channels)
    return ConflictReport(
        target_label=str(target_label),
        incoming_effect_id=str(incoming_effect_id),
        incoming_label=str(incoming_label),
        incoming_channels=channels,
        existing_claims=existing,
        occupied_claims=occupied,
        partial=partial,
    )


def resolve(report: ConflictReport, policy: ConflictPolicy) -> ConflictDecision:
    """Resolve a report while preserving unmanaged-data and atomicity rules."""
    policy = ConflictPolicy(policy)
    if not report.has_conflict:
        return ConflictDecision(DecisionAction.APPLY, True, False, report)
    # A channel toggle can intentionally leave one sibling unapplied. When
    # that same Espresso effect is re-enabled, restore the missing channel
    # while updating the one that was already present.
    if report.same_effect:
        return ConflictDecision(DecisionAction.UPDATE, True, False, report)
    if report.unmanaged:
        return ConflictDecision(
            DecisionAction.BLOCKED,
            False,
            policy is ConflictPolicy.ALWAYS_ASK,
            report,
            "Unmanaged Blender data is protected from silent replacement.",
        )
    if report.partial:
        return ConflictDecision(
            DecisionAction.BLOCKED,
            False,
            policy is ConflictPolicy.ALWAYS_ASK,
            report,
            "A multi-channel apply has both occupied and free channels; choose an explicit atomic route.",
        )
    if policy is ConflictPolicy.AUTO_REPLACE:
        return ConflictDecision(DecisionAction.REPLACE, True, False, report)
    if policy is ConflictPolicy.AUTO_LAYER and report.all_layerable:
        return ConflictDecision(DecisionAction.LAYER, True, False, report)
    if policy is ConflictPolicy.NEVER_PROMPT:
        return ConflictDecision(
            DecisionAction.BLOCKED,
            False,
            False,
            report,
            "Conflict policy does not permit an automatic replacement.",
        )
    return ConflictDecision(DecisionAction.ASK, False, True, report)
