"""Authoritative delivery capabilities for every generated systems recipe.

The catalogue describes the artist-facing idea.  This module describes the
production contract shared by apply, Live, preview, bake, clear and UI code.
It deliberately contains no mutation: preflight may inspect Blender state but
never creates, selects or removes data.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import workflows
from ...generated.core.contracts import ResourceKind
from ..expression.utils import MAX_DRIVER_EXPRESSION_LENGTH


OBJECT_SET_NATIVE = "OBJECT_SET_NATIVE"
PREPARED_LAYOUT_GN = "PREPARED_LAYOUT_GN"
DIRECT_DRIVER = "DIRECT_DRIVER"
GENERATED_GRAPHIC_NATIVE = "GENERATED_GRAPHIC_NATIVE"
SHOWCASE_HYBRID = "SHOWCASE_HYBRID"
TRAVEL_REVEAL_HYBRID = "TRAVEL_REVEAL_HYBRID"
EVENT_TRACK_NATIVE = "EVENT_TRACK_NATIVE"
DRIVER_KERNEL = "DRIVER_KERNEL"
COUPLED_MOTION_PLAN = "COUPLED_MOTION_PLAN"
GENERATED_WORKFLOW = "GENERATED_WORKFLOW"
STATEFUL_SIMULATION = "STATEFUL_SIMULATION"
DATA_AUDIO_DRIVEN = "DATA_AUDIO_DRIVEN"
DELIVERY_VALUES = frozenset({
    DRIVER_KERNEL, COUPLED_MOTION_PLAN, GENERATED_WORKFLOW,
    STATEFUL_SIMULATION, DATA_AUDIO_DRIVEN,
})

LIVE_NATIVE = "LIVE_NATIVE"
ADDON_ASSISTED = "ADDON_ASSISTED"
BAKE_REQUIRED = "BAKE_REQUIRED"
PORTABILITY_VALUES = frozenset({LIVE_NATIVE, ADDON_ASSISTED, BAKE_REQUIRED})
# Compatibility name retained for existing callers; the capability ladder uses
# one explicit portability vocabulary.
NATIVE_SELF_CONTAINED = LIVE_NATIVE

_PORTABILITY_ALIASES = {
    "NATIVE_SELF_CONTAINED": LIVE_NATIVE,
    "NATIVE_PLAYBACK": LIVE_NATIVE,
    "LIVE_NATIVE": LIVE_NATIVE,
    "ADDON_ASSISTED": ADDON_ASSISTED,
    "BAKE_TO_NATIVE": BAKE_REQUIRED,
    "BAKE_REQUIRED": BAKE_REQUIRED,
}


def normalize_portability(value):
    """Translate legacy lifecycle vocabulary into the capability ladder."""
    try:
        return _PORTABILITY_ALIASES[str(value)]
    except KeyError as exc:
        raise ValueError("Unsupported portability capability: %s" % value) from exc

LIGHT = "LIGHT"
MODERATE = "MODERATE"
HEAVY = "HEAVY"
COST_TIER_VALUES = frozenset({LIGHT, MODERATE, HEAVY})

CHANNEL_REPLACE = "CHANNEL_REPLACE"
LAYERABLE = "LAYERABLE"
EXCLUSIVE_REPLACE = "EXCLUSIVE_REPLACE"
ISOLATED_DESTINATION = "ISOLATED_DESTINATION"
CONFLICT_POLICY_VALUES = frozenset({
    CHANNEL_REPLACE, LAYERABLE, EXCLUSIVE_REPLACE, ISOLATED_DESTINATION,
})

# Artist actions returned by blocked preflight.  These are deliberately not
# conflict-state identifiers: UI code may present them directly without leaking
# internal lifecycle terminology such as ``EXCLUSIVE_OCCUPIED``.
REPLACE_EXISTING = "REPLACE_EXISTING"
CHOOSE_ANOTHER_TARGET = "CHOOSE_ANOTHER_TARGET"

GENERATED_TRANSACTION = "GENERATED_TRANSACTION"

EXISTING_OBJECTS = "EXISTING_OBJECTS"
PREPARED_STRUCTURE = "PREPARED_STRUCTURE"
BOTH = "BOTH"
GENERATED_HOST = "GENERATED_HOST"
UNSUPPORTED = "UNSUPPORTED"

VISUAL_OBJECT_TYPES = frozenset({
    "MESH", "CURVE", "CURVES", "FONT", "SURFACE", "META", "EMPTY",
    "VOLUME", "POINTCLOUD", "GREASEPENCIL",
})

PREPARED_LAYOUT_LAYER_OUTPUTS = frozenset({
    "scale", "lift", "rotation", "reveal",
})
SIGNED_LAYER_OUTPUTS = frozenset({"scale", "lift", "rotation"})


@dataclass(frozen=True)
class LayerOutputDeclaration:
    """Route-owner authority for dynamic channels, separate from layer intent."""

    effect_id: str
    route: str
    channels: tuple[str, ...]
    signed_numeric_channels: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported layer output declaration version: %s" % self.schema_version)
        if (
                not isinstance(self.effect_id, str) or not self.effect_id
                or self.effect_id != self.effect_id.strip()):
            raise ValueError("Layer output declarations require an effect_id.")
        if (
                not isinstance(self.route, str) or not self.route
                or self.route != self.route.strip()):
            raise ValueError("Layer output declarations require a route.")
        if not isinstance(self.channels, (list, tuple)) or not self.channels:
            raise ValueError("Layer output declarations require named channels.")
        if any(
                not isinstance(value, str) or not value
                or value != value.strip()
                for value in self.channels):
            raise ValueError("Layer output declaration channels must be canonical strings.")
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("Layer output declaration channels must be unique.")
        if not isinstance(self.signed_numeric_channels, (list, tuple)):
            raise ValueError("Signed numeric channels must be an array.")
        signed = tuple(self.signed_numeric_channels)
        if any(
                not isinstance(value, str) or not value
                or value != value.strip()
                for value in signed):
            raise ValueError("Signed numeric channels must be canonical strings.")
        if len(signed) != len(set(signed)):
            raise ValueError("Signed numeric channels must be unique.")
        if any(value not in self.channels for value in signed):
            raise ValueError("Signed numeric channels must be declared outputs.")
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "signed_numeric_channels", signed)

    def to_dict(self):
        return {
            "schema_version": 1,
            "effect_id": self.effect_id,
            "route": self.route,
            "channels": list(self.channels),
            "signed_numeric_channels": list(self.signed_numeric_channels),
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or frozenset(value) != frozenset({
                "schema_version", "effect_id", "route", "channels",
                "signed_numeric_channels"}):
            raise ValueError("Layer output declaration must be a strict V1 mapping.")
        if type(value["channels"]) is not list:
            raise ValueError("Layer output declaration channels must be an array.")
        if type(value["signed_numeric_channels"]) is not list:
            raise ValueError("Signed numeric channels must be an array.")
        return cls(
            effect_id=value["effect_id"], route=value["route"],
            channels=value["channels"],
            signed_numeric_channels=value["signed_numeric_channels"],
            schema_version=value["schema_version"],
        )


def validate_layer_outputs(
        route, output_channels, *, effect_id="", declaration=None,
        blend_mode=""):
    """Validate the MG1 route-output matrix without mutating route state."""
    if not isinstance(route, str) or route != route.strip():
        raise ValueError("Effector Layer route must be a canonical string.")
    if not isinstance(effect_id, str) or effect_id != effect_id.strip():
        raise ValueError("Effector Layer effect_id must be a canonical string.")
    if not isinstance(output_channels, (list, tuple)):
        raise ValueError("Effector Layer output channels must be an array.")
    channels = tuple(output_channels)
    if not channels or any(not value for value in channels):
        raise ValueError("Effector Layers require at least one named output channel.")
    if any(not isinstance(value, str) or value != value.strip() for value in channels):
        raise ValueError("Effector Layer output channels must be canonical strings.")
    if len(channels) != len(set(channels)):
        raise ValueError("Effector Layer output channels must be unique.")
    if declaration is not None:
        if not isinstance(declaration, LayerOutputDeclaration):
            raise ValueError("Dynamic layer outputs require route-owner authority.")
        if declaration.route != route or declaration.effect_id != effect_id:
            raise ValueError("Layer output declaration does not match the route and effect.")
    declared = None if declaration is None else frozenset(declaration.channels)
    if route == PREPARED_LAYOUT_GN:
        unsupported = tuple(
            value for value in channels
            if value not in PREPARED_LAYOUT_LAYER_OUTPUTS
            and not (
                value.startswith("material:")
                and len(value) > len("material:")
                and declared is not None
                and value in declared
            )
        )
    elif route == DIRECT_DRIVER:
        unsupported = tuple(
            value for value in channels
            if not any(
                value.startswith(prefix) and len(value) > len(prefix)
                for prefix in ("scalar:", "property:")
            )
            or declared is None
            or value not in declared
        )
    else:
        raise ValueError("Unsupported Effector Layer route: %s" % route)
    if unsupported:
        raise ValueError(
            "Route %s does not support Effector Layer output%s: %s"
            % (route, "s" if len(unsupported) != 1 else "", ", ".join(unsupported))
        )
    if str(blend_mode or "") == "SIGNED_DIFFERENCE":
        signed = (
            SIGNED_LAYER_OUTPUTS if declaration is None
            else frozenset(declaration.signed_numeric_channels)
        )
        unsigned = tuple(
            value for value in channels
            if value not in signed
        )
        if unsigned:
            raise ValueError(
                "Signed Difference is not supported for Effector Layer output%s: %s"
                % ("s" if len(unsigned) != 1 else "", ", ".join(unsigned))
            )
    return channels


@dataclass(frozen=True)
class WorkflowCapabilities:
    template_id: str
    effect_id: str
    label: str
    route: str
    source_roles: tuple[str, ...]
    preview_kind: str
    setup_owner: str
    live_owner: str
    bake_channels: tuple[str, ...]
    clear_owner: str
    delivery: str = ""
    portability: str = ""
    cost_tier: str = ""
    target_roles: tuple[str, ...] = ()
    created_resource_kinds: tuple[ResourceKind, ...] = ()
    conflict_policy: str = ""
    lifecycle_owner: str = ""
    output_kinds: tuple[str, ...] = ()
    max_expression_length: int | None = None
    direct_driver_composition: bool | None = None
    owns_destination: bool = True
    can_prepare_layout: bool = False
    structure_support: str = EXISTING_OBJECTS
    required_fields: tuple[str, ...] = ()
    unsupported_reason: str = ""

    def __post_init__(self):
        contract = route_contract(self.route)
        derived = {
            "delivery": contract.delivery,
            "portability": contract.portability,
            "cost_tier": contract.cost_tier,
            "target_roles": contract.target_roles,
            "created_resource_kinds": contract.created_resource_kinds,
            "conflict_policy": contract.conflict_policy,
            "lifecycle_owner": contract.lifecycle_owner,
            "output_kinds": contract.output_kinds,
            "max_expression_length": contract.max_expression_length,
            "direct_driver_composition": contract.direct_driver_composition,
        }
        unset_values = {
            "delivery": "",
            "portability": "",
            "cost_tier": "",
            "target_roles": (),
            "created_resource_kinds": (),
            "conflict_policy": "",
            "lifecycle_owner": "",
            "output_kinds": (),
            "max_expression_length": None,
            "direct_driver_composition": None,
        }
        for name, value in derived.items():
            current = getattr(self, name)
            if current == unset_values[name]:
                object.__setattr__(self, name, value)
            elif current != value:
                raise ValueError(
                    "%s is route-owned by %s and cannot be overridden."
                    % (name, self.route)
                )
        if self.delivery not in DELIVERY_VALUES:
            raise ValueError("Unsupported delivery capability: %s" % self.delivery)
        if self.portability not in PORTABILITY_VALUES:
            raise ValueError("Unsupported portability capability: %s" % self.portability)
        if self.cost_tier not in COST_TIER_VALUES:
            raise ValueError("Unsupported cost capability: %s" % self.cost_tier)
        if self.conflict_policy not in CONFLICT_POLICY_VALUES:
            raise ValueError("Unsupported conflict capability: %s" % self.conflict_policy)
        if not self.target_roles or not self.created_resource_kinds or not self.lifecycle_owner:
            raise ValueError("Route capabilities require complete route-owned metadata.")
        if type(self.direct_driver_composition) is not bool:
            raise ValueError("direct_driver_composition must resolve to a route-owned boolean.")
        if self.direct_driver_composition:
            if self.route != DIRECT_DRIVER:
                raise ValueError("Only the direct-driver route may compose driver expressions.")
            if self.max_expression_length != MAX_DRIVER_EXPRESSION_LENGTH:
                raise ValueError("Direct-driver routes require the 255-character limit.")
        elif self.max_expression_length is not None:
            raise ValueError("Generated routes cannot claim a direct-driver length limit.")


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    message: str
    route: str
    source_names: tuple[str, ...] = ()
    can_prepare_layout: bool = False
    destination: str = ""
    structure_kind: str = ""
    instance_count: int = 0
    delivery: str = ""
    portability: str = ""
    cost_tier: str = ""
    target_roles: tuple[str, ...] = ()
    created_resource_kinds: tuple[ResourceKind, ...] = ()
    conflict_policy: str = ""
    disabled_reason: str = ""
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteCapabilityContract:
    route: str
    delivery: str
    portability: str
    cost_tier: str
    target_roles: tuple[str, ...]
    created_resource_kinds: tuple[ResourceKind, ...]
    conflict_policy: str
    lifecycle_owner: str = GENERATED_TRANSACTION
    output_kinds: tuple[str, ...] = ()
    max_expression_length: int | None = None
    direct_driver_composition: bool = False


_ROUTE_CONTRACTS = {
    DIRECT_DRIVER: RouteCapabilityContract(
        DIRECT_DRIVER, DRIVER_KERNEL, LIVE_NATIVE, LIGHT,
        ("PROPERTY_CHANNEL",), (ResourceKind.DRIVER,), CHANNEL_REPLACE,
        output_kinds=("DRIVER_EXPRESSION",),
        max_expression_length=MAX_DRIVER_EXPRESSION_LENGTH,
        direct_driver_composition=True,
    ),
    OBJECT_SET_NATIVE: RouteCapabilityContract(
        OBJECT_SET_NATIVE, COUPLED_MOTION_PLAN, LIVE_NATIVE, LIGHT,
        ("OBJECT_TRANSFORMS", "INSTANCED_GEOMETRY"),
        (ResourceKind.OBJECT, ResourceKind.PROPERTY, ResourceKind.DRIVER),
        CHANNEL_REPLACE, output_kinds=("OBJECT_TRANSFORMS", "GN_FIELD"),
    ),
    PREPARED_LAYOUT_GN: RouteCapabilityContract(
        PREPARED_LAYOUT_GN, GENERATED_WORKFLOW, LIVE_NATIVE, MODERATE,
        ("INSTANCED_GEOMETRY",),
        (ResourceKind.NODE_GROUP, ResourceKind.MODIFIER, ResourceKind.PROPERTY),
        LAYERABLE, output_kinds=("GN_FIELD",),
    ),
    GENERATED_GRAPHIC_NATIVE: RouteCapabilityContract(
        GENERATED_GRAPHIC_NATIVE, GENERATED_WORKFLOW, LIVE_NATIVE, MODERATE,
        ("GENERATED_GEOMETRY", "GENERATED_OBJECTS_OR_TEXT"),
        (ResourceKind.OBJECT, ResourceKind.NODE_GROUP, ResourceKind.MODIFIER,
         ResourceKind.PROPERTY),
        EXCLUSIVE_REPLACE, output_kinds=("GN_GEOMETRY", "NATIVE_ANIMATION"),
    ),
    SHOWCASE_HYBRID: RouteCapabilityContract(
        SHOWCASE_HYBRID, GENERATED_WORKFLOW, LIVE_NATIVE, MODERATE,
        ("INSTANCED_GEOMETRY", "OBJECT_TRANSFORMS"),
        (ResourceKind.OBJECT, ResourceKind.NODE_GROUP, ResourceKind.MODIFIER,
         ResourceKind.PROPERTY, ResourceKind.DRIVER),
        EXCLUSIVE_REPLACE, output_kinds=("GN_FIELD", "OBJECT_TRANSFORMS"),
    ),
    TRAVEL_REVEAL_HYBRID: RouteCapabilityContract(
        TRAVEL_REVEAL_HYBRID, COUPLED_MOTION_PLAN, LIVE_NATIVE, MODERATE,
        ("INSTANCED_GEOMETRY", "OBJECT_TRANSFORMS"),
        (ResourceKind.OBJECT, ResourceKind.NODE_GROUP, ResourceKind.MODIFIER,
         ResourceKind.PROPERTY, ResourceKind.DRIVER),
        CHANNEL_REPLACE, output_kinds=("GN_FIELD", "OBJECT_TRANSFORMS"),
    ),
    EVENT_TRACK_NATIVE: RouteCapabilityContract(
        EVENT_TRACK_NATIVE, DATA_AUDIO_DRIVEN, LIVE_NATIVE, MODERATE,
        ("OBJECT_TRANSFORMS",),
        (ResourceKind.OBJECT, ResourceKind.PROPERTY, ResourceKind.DRIVER,
         ResourceKind.ACTION),
        CHANNEL_REPLACE, output_kinds=("NATIVE_ANIMATION",),
    ),
}


def route_contract(route):
    """Return the immutable capability facts owned by a delivery route."""
    try:
        return _ROUTE_CONTRACTS[str(route)]
    except KeyError as exc:
        raise ValueError("Unsupported route: %s" % route) from exc


def _preview_kind(item):
    if item.route == workflows.OBJECT_SET:
        return "PIXEL_OBJECT_SET"
    if item.route == workflows.PREPARED_FIELD:
        return "PIXEL_SPATIAL_FIELD"
    return {
        "PIE": "PIXEL_PIE_ANGULAR",
        "BARS": "PIXEL_BARS",
        "RACE": "PIXEL_BAR_RACE",
        "COUNTER": "PIXEL_COUNTER",
        "GLYPH": "PIXEL_GLYPHS",
        "WORDS": "PIXEL_WORDS",
        "TRIM": "PIXEL_PATH_TRIM",
        "CURVE_FLOW": "PIXEL_PATH_FLOW",
        "TRAIN": "PIXEL_PATH_TRAIN",
        "HELIX": "PIXEL_HELIX",
        "CALLOUT": "PIXEL_CALLOUT",
        "ECHO": "PIXEL_ECHO",
    }.get(item.mode, "PIXEL_GENERATED_GRAPHIC")


def _source_roles(item):
    if item.route == workflows.OBJECT_SET:
        return ("TWO_OR_MORE_VISUAL_OBJECTS",)
    if item.route == workflows.PREPARED_FIELD:
        return ("PREPARED_LAYOUT",)
    return {
        "CURVE_FLOW": ("ONE_MESH", "ONE_CURVE"),
        "TRAIN": ("ONE_MESH", "ONE_CURVE"),
        "TRIM": ("ONE_CURVE",),
        "GLYPH": ("ONE_TEXT",),
        "WORDS": ("ONE_TEXT",),
        "CURVE_TEXT": ("ONE_TEXT", "ONE_CURVE"),
        "TRACKING": ("ONE_TEXT",),
        "SCRAMBLE": ("ONE_TEXT",),
        "ECHO": ("ONE_MESH",),
        "HELIX": ("ONE_MESH", "OPTIONAL_GUIDE_CURVE"),
    }.get(item.mode, ("GENERATED_FROM_PARAMETERS",))


def _bake_channels(item):
    if item.route == workflows.OBJECT_SET:
        return ("OBJECT_TRANSFORMS",)
    if item.route == workflows.PREPARED_FIELD:
        return ("GENERATED_INSTANCES", "OBJECT_TRANSFORMS")
    channels = ["GENERATED_INSTANCES", "OBJECT_TRANSFORMS"]
    if item.mode in {"TRIM", "CALLOUT"}:
        channels.append("CURVE_DATA")
    if item.mode in {"GLYPH", "WORDS", "CURVE_TEXT", "TRACKING", "SCRAMBLE", "COUNTER", "NUMBER_ROLL", "CALLOUT"}:
        channels.append("TEXT_DATA")
    if item.mode in {"CURVE_FLOW", "TRAIN"}:
        channels.append("CONSTRAINT_EVALUATION")
    return tuple(channels)


def _structure_contract(template_id):
    from ...apply.setups import prepared_structure_fields as fields

    both_position = (BOTH, (fields.POSITION,), "")
    both_angle = (BOTH, (fields.ANGLE, fields.ANGLE_NORM, fields.RADIUS), "")
    prepared_position = (PREPARED_STRUCTURE, (fields.POSITION,), "")
    generated = {
    }
    # Empty here: no recipe in this build keys a contract in this table.
    contracts = {}
    if template_id in generated:
        return GENERATED_HOST, (), generated[template_id]
    return contracts.get(template_id, (EXISTING_OBJECTS, (), ""))


def _workflow_capability(item):
    route = {
        workflows.OBJECT_SET: OBJECT_SET_NATIVE,
        workflows.PREPARED_FIELD: PREPARED_LAYOUT_GN,
        workflows.GENERATED_GRAPHIC: GENERATED_GRAPHIC_NATIVE,
    }[item.route]
    owner = {
        workflows.OBJECT_SET: "CONTROLLER_OBJECT",
        workflows.PREPARED_FIELD: "PREPARED_LAYOUT_EFFECT_RECORD",
        workflows.GENERATED_GRAPHIC: "GENERATED_GRAPHIC_SETUP",
    }[item.route]
    support, required, reason = _structure_contract(item.template_id)
    return WorkflowCapabilities(
        template_id=item.template_id,
        effect_id=item.effect_id,
        label=item.label,
        route=route,
        source_roles=_source_roles(item),
        preview_kind=_preview_kind(item),
        setup_owner=owner,
        live_owner=owner,
        bake_channels=_bake_channels(item),
        clear_owner=owner,
        can_prepare_layout=item.route == workflows.PREPARED_FIELD,
        structure_support=support,
        required_fields=required,
        unsupported_reason=reason,
    )


#: Empty in this product, and that is the whole generated-system boundary.
#:
#: `capabilities.get()` answers None for a template it does not know, so
#: `generated_routes.adapter_for()` answers None, and every caller in the
#: apply pipeline and the panels already handles that as "this recipe is not a
#: generated-system one". Emptying the registry therefore switches the whole
#: subsystem off through the code's own seam, rather than by stubbing the sixty
#: functions behind it.
#:
#: The registry is empty because no recipe in this build uses a
#: generated-system capability, checked against its frozen records.
_SPECIAL = ()

_ALL = _SPECIAL + tuple(_workflow_capability(item) for item in workflows.WORKFLOWS)
_BY_ID = {item.template_id: item for item in _ALL}


def all_capabilities():
    return _ALL


def get(template_id):
    return _BY_ID.get(str(template_id or ""))


def supports(template_id):
    return get(template_id) is not None


def owns_destination(template_id):
    item = get(template_id)
    return bool(item and item.owns_destination)


def _selection(context):
    return tuple(getattr(context, "selected_objects", ()) or ())


def _result(item, ok, message, objects=(), *, can_prepare=None, destination="",
            structure_kind="", instance_count=0, disabled_reason="",
            alternatives=()):
    return PreflightResult(
        ok=ok,
        message=message,
        route=item.route,
        source_names=tuple(obj.name for obj in objects),
        can_prepare_layout=item.can_prepare_layout if can_prepare is None else can_prepare,
        destination=destination,
        structure_kind=structure_kind,
        instance_count=int(instance_count or 0),
        delivery=item.delivery,
        portability=item.portability,
        cost_tier=item.cost_tier,
        target_roles=item.target_roles,
        created_resource_kinds=item.created_resource_kinds,
        conflict_policy=item.conflict_policy,
        disabled_reason=("" if ok else (disabled_reason or message)),
        alternatives=tuple(alternatives),
    )


def _contract_request_failure(
        item, *, target_role="", source_role="", output_kind="", conflict=""):
    """Validate a requested delivery combination without touching Blender data."""
    if target_role and target_role not in item.target_roles:
        return _result(
            item, False,
            "%s cannot target %s through %s." % (item.label, target_role, item.route),
            disabled_reason="Unsupported target role: %s." % target_role,
            alternatives=item.target_roles,
        )
    if source_role and source_role not in item.source_roles:
        return _result(
            item, False,
            "%s cannot use %s as its source." % (item.label, source_role),
            disabled_reason="Unsupported source role: %s." % source_role,
            alternatives=item.source_roles,
        )
    if output_kind and output_kind not in item.output_kinds:
        return _result(
            item, False,
            "%s cannot produce %s through %s." % (item.label, output_kind, item.route),
            disabled_reason="Unsupported output kind: %s." % output_kind,
            alternatives=item.output_kinds,
        )
    conflict = str(conflict or "")
    allowed_conflicts = {"", "NONE", "SAME_EFFECT"}
    if item.conflict_policy == LAYERABLE:
        allowed_conflicts.add("LAYERABLE_OCCUPIED")
    if conflict not in allowed_conflicts:
        if conflict == "EXCLUSIVE_OCCUPIED":
            return _result(
                item, False,
                "%s needs exclusive control of this target. Choose Replace "
                "Existing to continue, or select another target."
                % item.label,
                disabled_reason=(
                    "An existing Espresso effect already controls this target. "
                    "Replace it or select another target."
                ),
                alternatives=(REPLACE_EXISTING, CHOOSE_ANOTHER_TARGET),
            )
        return _result(
            item, False,
            "%s needs a different target or apply choice." % item.label,
            disabled_reason="The current target cannot accept this effect safely.",
            alternatives=(CHOOSE_ANOTHER_TARGET,),
        )
    return None


def preflight(
        context, template_id, *, target_role="", source_role="",
        output_kind="", conflict=""):
    """Resolve prerequisites without changing selection or generated data.

    Always the same answer here. `get()` reads an empty registry, so no recipe
    in this build has a generated-systems delivery contract and no
    route-specific prerequisite checking is reachable.
    """
    return PreflightResult(
        False, "This recipe has no generated delivery contract.", "")


