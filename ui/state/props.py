"""Blender properties for Driver Espresso."""

from __future__ import annotations

import json

import bpy

from ...catalogue import browse_groups
from ...apply.setups import parameter_bindings
from ...catalogue import templates
from ...engine import utils


def _category_icon(category):
    """Icons live in taxonomy.py, next to the category they belong to."""
    return templates.category_icon(category)


def _visualizer_style_items():
    from ..views import visualizer

    return [(sid, label, desc) for sid, label, desc in visualizer.STYLE_ITEMS]


def _rest_start_mode_items():
    from ...engine import utils

    items = [
        (
            utils.REST_START_OFF,
            "Off — Keep Scene Time",
            "Use the template's authored scene-time phase; coordinated motion still stays relative to the object's current transform",
        ),
        (
            utils.REST_START_ADDITIVE,
            "Additive — Restart from Current Value",
            "Capture the current value and application frame, hold earlier frames, then start the effect from rest on a local clock",
        ),
        (
            utils.REST_START_OFFSET_ONLY,
            "Offset Graph — Keep Scene Time",
            "Keep the authored scene-time phase with one fixed shift; bounded templates add Current Value + Minimum/Maximum, while other templates match the current value at the application frame",
        ),
    ]
    return utils.mark_recommended(items, utils.REST_START_ADDITIVE)


def _live_controller_items(self, context):
    from . import live_controls

    return live_controls.controller_items(self, context)


def effective_rest_start_mode(props):
    from ...engine import utils

    return getattr(props, "rest_start_mode", utils.REST_START_ADDITIVE)


def _token_proxy_name(token):
    return f"token_prop_{token.lower()}"


def _advanced_token_prop_name(token):
    return f"advanced_prop_{token.lower()}"


def _collect_token_specs():
    specs = {}
    for template in templates.TEMPLATES:
        for param in template.get("params", []):
            specs.setdefault(param["token"], dict(param))
    return specs


def _collect_proxy_safe_token_specs():
    signatures = {}
    specs = {}
    for template in templates.TEMPLATES:
        for param in template.get("params", []):
            token = param["token"]
            specs.setdefault(token, dict(param))
            signatures.setdefault(token, set()).add(
                (
                    param.get("type"),
                    param.get("min"),
                    param.get("max"),
                )
            )
    return {
        token: specs[token]
        for token, token_signatures in signatures.items()
        if len(token_signatures) == 1
    }


# Blender requires that EnumProperty item callbacks keep a Python reference to the
# returned strings; otherwise the items can be garbage collected, corrupting the menu
# text or crashing. These module-level caches hold the most recent results so the
# strings stay alive while Blender uses them.
#
# They are keyed because Blender calls these callbacks many times per panel redraw, and
# recomputing would re-filter the whole catalogue and rebuild every description string
# for a result that only changes when the category, browse group or search text does.
_CATEGORY_ITEMS_CACHE = []
_CATEGORY_ITEMS_KEY = None
_BROWSE_GROUP_ITEMS_CACHE = []
_BROWSE_GROUP_ITEMS_KEY = None
_TEMPLATE_ITEMS_CACHE = []
_TEMPLATE_ITEMS_KEY = None


# These label categories, so they must read as a description of a container: name the
# category, say it holds several templates, and only then describe what one of them is.
# The UI calls it a Category, so the tooltip uses that word.
GENERIC_TIP = "Generic category - holds several templates, each one a raw shape that you give the meaning to"
TAILORED_TIP = "Tailored category - holds several templates, each one calibrated to a real-world behaviour, with parameters named for it"


def kind_allows(kind, category):
    if kind == "GENERIC":
        return templates.is_generic_category(category)
    if kind == "TAILORED":
        return not templates.is_generic_category(category)
    return True


def category_items(self, _context):
    global _CATEGORY_ITEMS_CACHE, _CATEGORY_ITEMS_KEY
    kind = getattr(self, "template_kind", "ALL") or "ALL"
    key = (kind, len(templates.CATEGORIES))
    if key == _CATEGORY_ITEMS_KEY and _CATEGORY_ITEMS_CACHE:
        return _CATEGORY_ITEMS_CACHE

    generic, tailored = [], []
    for index, category in enumerate(templates.CATEGORIES):
        is_generic = templates.is_generic_category(category)
        # A dynamic enum stores the NUMBER, not the identifier, so the number
        # must stay tied to the category's canonical position. Numbering by
        # position in the FILTERED list would silently re-point an already
        # saved category the moment the filter changed.
        entry = (
            category,
            category,
            GENERIC_TIP if is_generic else TAILORED_TIP,
            _category_icon(category),
            index,
        )
        (generic if is_generic else tailored).append(entry)

    items = []
    if kind in {"ALL", "GENERIC"} and generic:
        # Headings only earn their row when both kinds are on screen; with the
        # list already filtered to one kind they would state the obvious.
        if kind == "ALL":
            items.append(("", "Generic  ·  raw shapes, you supply the meaning", ""))
        items.extend(generic)
    if kind in {"ALL", "TAILORED"} and tailored:
        if kind == "ALL":
            items.append(("", "Tailored  ·  calibrated real-world behaviours", ""))
        items.extend(tailored)

    _CATEGORY_ITEMS_CACHE = items
    _CATEGORY_ITEMS_KEY = key
    return items


# A static list, deliberately: a dynamic (callback) enum is stored by index,
# so its meaning shifts if the order ever changes. This one is fixed and tiny,
# and a static list is also stored by identifier - safer and easier to read.
TEMPLATE_KIND_ITEMS = [
    ("ALL", "All", "Show every category"),
    ("GENERIC", "Generic", "Show only the categories of raw shapes you give the meaning to"),
    ("TAILORED", "Tailored", "Show only the categories calibrated to real-world behaviours"),
]


def on_template_kind_update(self, context):
    """Keep the selected category valid when the filter hides it."""
    categories = list(templates.CATEGORIES)
    # Read the raw stored value rather than self.category. A dynamic enum resolves
    # through its items callback on every read, and by this point the callback already
    # reflects the new filter, so reading the property while the old category is still
    # selected makes Blender log "current value 'N' matches no enum". ``get()`` returns
    # the stored index without that round trip.
    stored = self.get("category")
    if isinstance(stored, int) and 0 <= stored < len(categories):
        current = categories[stored]
    else:
        current = categories[0] if categories else ""
    if current and not kind_allows(self.template_kind, current):
        allowed = [c for c in categories if kind_allows(self.template_kind, c)]
        if allowed:
            # Assigning category runs on_category_update, which re-scopes the
            # template enum - exactly what is needed here.
            self.category = allowed[0]


def browse_group_items(self, _context):
    global _BROWSE_GROUP_ITEMS_CACHE, _BROWSE_GROUP_ITEMS_KEY
    category = getattr(self, "category", "")
    # The catalogue length guards against the list changing underneath the cache
    # (a catalogue change or a reload); it is a cheap read, unlike rebuilding.
    key = (category, len(templates.TEMPLATES))
    if key == _BROWSE_GROUP_ITEMS_KEY and _BROWSE_GROUP_ITEMS_CACHE:
        return _BROWSE_GROUP_ITEMS_CACHE
    available = {
        item["id"]
        for item in templates.templates_by_category(category)
    }
    _BROWSE_GROUP_ITEMS_CACHE = browse_groups.enum_items_for_category(
        category,
        available,
    )
    _BROWSE_GROUP_ITEMS_KEY = key
    return _BROWSE_GROUP_ITEMS_CACHE


SEARCH_RESULT_ROW_LIMIT = 5


def search_result_templates(props):
    """Collapsed search hits only. Does not pin or auto-select the current template."""
    query = getattr(props, "search_text", "") or ""
    if not query:
        return []
    return templates.search_templates(query, getattr(props, "category", None))


def _pin_current_template(matches, current_id):
    """Keep the active template inside a filtered enum so RNA cannot snap."""
    current = templates.TEMPLATE_BY_ID.get(current_id)
    if current is None:
        return list(matches)
    if any(item["id"] == current["id"] for item in matches):
        return list(matches)
    return [current] + list(matches)


def template_items(self, _context):
    global _TEMPLATE_ITEMS_CACHE, _TEMPLATE_ITEMS_KEY
    key = (
        self.search_text,
        self.category,
        getattr(self, "browse_group", browse_groups.ALL_GROUP),
        getattr(self, "last_template_id", ""),
        len(templates.TEMPLATES),
    )
    if key == _TEMPLATE_ITEMS_KEY and _TEMPLATE_ITEMS_CACHE:
        return _TEMPLATE_ITEMS_CACHE
    if self.search_text:
        # Empty hits stay empty. Pin the active template so the enum remains
        # assignable; do not refill the current category into the result set.
        matches = _pin_current_template(
            templates.search_templates(self.search_text, self.category),
            getattr(self, "last_template_id", ""),
        )
        if not matches:
            matches = list(templates.TEMPLATES[:1])
    else:
        matches = browse_groups.templates_for_group(
            templates.TEMPLATES,
            self.category,
            getattr(self, "browse_group", browse_groups.ALL_GROUP),
        )
        if not matches:
            matches = templates.templates_by_category(self.category)
    # Every item carries its own description, so hovering the template name shows what
    # the loaded template does, and browsing the open dropdown explains each entry. The
    # general explanation of what a template is lives in Help, since it is read once and
    # repeating it on every hover would bury the description the user came for.
    _TEMPLATE_ITEMS_CACHE = [
        (
            item["id"],
            item["name"],
            item.get("description", "") + (" Use cases: " + item.get("use_cases", "") if item.get("use_cases") else ""),
            _category_icon(item.get("category", self.category)),
            idx,
        )
        for idx, item in enumerate(matches)
    ]
    _TEMPLATE_ITEMS_KEY = key
    return _TEMPLATE_ITEMS_CACHE


# How many numbered slots exist per type (the widest Lite template has eight parameters). A template with more visible
# parameters than this has nowhere to store the extras, and the panel would
# fail to draw them - _assert_slots_cover_catalogue() below turns that into a
# startup error instead of a silently broken template.
PARAM_SLOT_COUNT = 8


def _param_float_name(index):
    return f"param_float_{index}"


def _param_int_name(index):
    return f"param_int_{index}"


def _param_color_name(index):
    return f"param_color_{index}"


def _slot_kind(param):
    """Which numbered slot family a template parameter is stored in."""
    return {"INT": "int", "COLOR": "color"}.get(param.get("type"), "float")


def advanced_param_property_name(control):
    spec = templates.resolve_advanced_param(control)
    return _advanced_token_prop_name(spec["token"])


def param_property_name(param, index):
    if param.get("advanced"):
        return advanced_param_property_name(param)
    proxy_name = _token_proxy_name(param["token"])
    if param["token"] in TOKEN_PROXY_SPECS and hasattr(ESPRESSO_Props, proxy_name):
        return proxy_name
    kind = _slot_kind(param)
    if kind == "int":
        return _param_int_name(index)
    if kind == "color":
        return _param_color_name(index)
    return _param_float_name(index)


_GENERIC_SLOT_TIP = (
    "Driver Espresso template parameter. Hover the parameter label for "
    "template-specific meaning, default, and range."
)


_SLOT_NAME = {"float": _param_float_name, "int": _param_int_name, "color": _param_color_name}


def _redefine_slot_property(name, param, kind):
    kwargs = {
        "name": param.get("label", "Parameter"),
        "description": templates.format_param_input_tooltip(param),
        "update": on_param_update,
    }
    if kind == "color":
        # subtype COLOR    elif kind == "color":
        # subtype COLOR draws Blender's own swatch and colour wheel. Picking a
        # colour by eye is the whole point; three 0-to-1 sliders made the artist
        # do the conversion in their head.
        setattr(ESPRESSO_Props, name, bpy.props.FloatVectorProperty(
            size=3, subtype="COLOR", min=0.0, max=1.0,
            default=tuple(param.get("default", (1.0, 1.0, 1.0))),
            **kwargs,
        ))
    elif kind == "int":
        if param.get("min") is not None:
            kwargs["min"] = int(param["min"])
        if param.get("max") is not None:
            kwargs["max"] = int(param["max"])
        setattr(ESPRESSO_Props, name, bpy.props.IntProperty(**kwargs))
    else:
        if param.get("min") is not None:
            kwargs["min"] = float(param["min"])
        if param.get("max") is not None:
            kwargs["max"] = float(param["max"])
        setattr(ESPRESSO_Props, name, bpy.props.FloatProperty(**kwargs))


def _reset_slot_property(name, kind):
    kwargs = {"description": _GENERIC_SLOT_TIP, "update": on_param_update}
    if kind == "color":
        setattr(ESPRESSO_Props, name, bpy.props.FloatVectorProperty(
            size=3, subtype="COLOR", min=0.0, max=1.0, default=(1.0, 1.0, 1.0), **kwargs))
        return
    factory = bpy.props.IntProperty if kind == "int" else bpy.props.FloatProperty
    setattr(ESPRESSO_Props, name, factory(**kwargs))


def _refresh_slot_tooltips(template):
    """Numbered param slots (param_float_N / param_int_N) are reused across
    every template with a different meaning each time, so a fixed
    registration-time description can't describe them accurately. Redefine
    the slots the new template actually uses with its own name/description/
    min/max right before applying values, and reset the rest to the generic
    fallback so no stale per-template text lingers on an unused slot."""
    used = {"float": set(), "int": set(), "color": set()}
    for index, param in enumerate(template.get("params", [])):
        if param.get("advanced"):
            continue
        if param["token"] in TOKEN_PROXY_SPECS and hasattr(ESPRESSO_Props, _token_proxy_name(param["token"])):
            continue
        kind = _slot_kind(param)
        _redefine_slot_property(_SLOT_NAME[kind](index), param, kind)
        used[kind].add(index)

    for index in range(PARAM_SLOT_COUNT):
        for kind, names in used.items():
            if index not in names:
                _reset_slot_property(_SLOT_NAME[kind](index), kind)


_SCOPE_SOURCES = ("ACTIVE", "LAST", "SCENE")


def _scoped_driver_target_payload(raw):
    """Normalize collapse/selection storage to per-tab scope buckets."""
    if not isinstance(raw, dict):
        return {}
    if any(key in raw for key in _SCOPE_SOURCES):
        return {
            str(source): {
                str(key): value for key, value in scoped.items()
                if isinstance(value, bool)
            }
            for source, scoped in raw.items()
            if source in _SCOPE_SOURCES and isinstance(scoped, dict)
        }
    return {"ACTIVE": {str(key): bool(value) for key, value in raw.items()}}


def read_driver_target_group_state(props, source=None):
    source = source or getattr(props, "driver_target_source", "ACTIVE")
    try:
        raw = json.loads(getattr(props, "driver_target_group_state", "") or "{}")
    except Exception:
        return {}
    scoped = _scoped_driver_target_payload(raw)
    return {str(key): bool(value) for key, value in scoped.get(source, {}).items()}


def driver_target_group_is_open(props, group_key, source=None):
    return read_driver_target_group_state(props, source=source).get(str(group_key), True)


def set_driver_target_group_open(props, group_key, is_open, source=None):
    source = source or getattr(props, "driver_target_source", "ACTIVE")
    try:
        raw = json.loads(getattr(props, "driver_target_group_state", "") or "{}")
    except Exception:
        raw = {}
    scoped = _scoped_driver_target_payload(raw)
    bucket = dict(scoped.get(source, {}))
    bucket[str(group_key)] = bool(is_open)
    scoped[source] = bucket
    props.driver_target_group_state = json.dumps(scoped, sort_keys=True)


def _selection_state_for_scope(props, source=None):
    source = source or getattr(props, "driver_target_source", "ACTIVE")
    try:
        raw = json.loads(getattr(props, "driver_target_selection_state", "") or "{}")
    except Exception:
        return set()
    if not isinstance(raw, dict):
        return set()
    return {str(key) for key in raw.get(source, ())}


def _store_selection_state_for_scope(props, source, keys):
    try:
        raw = json.loads(getattr(props, "driver_target_selection_state", "") or "{}")
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw[str(source)] = sorted(str(key) for key in keys)
    props.driver_target_selection_state = json.dumps(raw, sort_keys=True)


def on_driver_target_source_update(self, context):
    """Applied Effects rows are rebuilt per scope; invalidate stale list cache."""
    self.driver_target_items_signature = ""
    self.driver_target_active_index = -1
    if context is not None:
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()


def on_last_apply_status_update(self, _context):
    """A newly written result replaces a dismissed strip."""
    self.last_apply_status_dismissed = not bool(
        str(getattr(self, "last_apply_status", "") or "").strip()
    )


def set_last_apply_status(props, text):
    """Show the result strip, including when the next write repeats the text."""
    if props is None:
        return
    text = str(text or "")
    props.last_apply_status_dismissed = False
    if str(getattr(props, "last_apply_status", "") or "") != text:
        props.last_apply_status = text


def get_current_template(props):
    # Every navigation boundary keeps this StringProperty truthful. Reading it
    # avoids rebuilding the dynamic EnumProperty's item list on every draw.
    return (
        templates.TEMPLATE_BY_ID.get(props.last_template_id)
        or templates.templates_by_category(props.category)[0]
    )


def get_advanced_param_value(props, control):
    spec = templates.resolve_advanced_param(control)
    return getattr(props, advanced_param_property_name(spec))


def set_advanced_param_value(props, control, value):
    spec = templates.resolve_advanced_param(control)
    if spec.get("type") == "INT":
        value = int(round(value))
    elif spec.get("type") == "BOOL":
        value = 1 if bool(value) else 0
    else:
        value = float(value)
    setattr(props, advanced_param_property_name(spec), value)


def collect_advanced_values(props, template_or_id=None):
    template = template_or_id or get_current_template(props)
    return {
        spec["token"]: get_advanced_param_value(props, spec)
        for spec in templates.advanced_controls_for_template(template)
    }


def get_param_value(props, param, index):
    if param.get("advanced"):
        return get_advanced_param_value(props, param)
    if param.get("type") == "INT":
        return getattr(props, _param_int_name(index))
    if param.get("type") == "COLOR":
        return tuple(getattr(props, _param_color_name(index)))
    return getattr(props, _param_float_name(index))


def set_param_value(props, param, index, value):
    if param.get("advanced"):
        set_advanced_param_value(props, param, value)
        return
    kind = _slot_kind(param)
    if kind == "int":
        setattr(props, _param_int_name(index), int(round(value)))
    elif kind == "color":
        rgb = tuple(value)[:3] if hasattr(value, "__iter__") else (float(value),) * 3
        setattr(props, _param_color_name(index), rgb)
    else:
        setattr(props, _param_float_name(index), float(value))


def colour_component_tokens(token):
    """A COLOR parameter feeds three scalar tokens: C -> CR, CG, CB."""
    return tuple(f"{token}{axis}" for axis in "RGB")


def collect_values(props, template):
    """Token values for one template.

    A COLOR parameter is stored as one swatch but read by the expression as
    three separate numbers, so it contributes BOTH: the tuple under its own
    token (which is what writes back to the swatch) and one scalar per
    component (which is what the expression substitutes).
    """
    values = {}
    for index, param in enumerate(template.get("params", [])):
        value = get_param_value(props, param, index)
        values[param["token"]] = value
        if param.get("type") == "COLOR":
            for component_token, component in zip(colour_component_tokens(param["token"]), value):
                values[component_token] = float(component)
    return values


def _remember_template_state_enabled(context):
    prefs = utils.addon_preferences(context)
    return True if prefs is None else bool(getattr(prefs, "remember_template_params", True))


def _carry_manual_params_enabled(context):
    prefs = utils.addon_preferences(context)
    return True if prefs is None else bool(getattr(prefs, "carry_manual_params", True))


def _carry_manual_params_for_switch(context, previous, current):
    """Whether a switch carries the last hand-edited values across."""
    return _carry_manual_params_enabled(context)


def _advanced_controls_enabled(context):
    prefs = utils.addon_preferences(context)
    return bool(getattr(prefs, "enable_advanced_controls", False)) if prefs is not None else False


def _apply_values(props, template, values):
    props.suspend_param_updates = True
    try:
        for index, param in enumerate(template.get("params", [])):
            set_param_value(props, param, index, values.get(param["token"], param.get("default", 0)))
    finally:
        props.suspend_param_updates = False


def apply_combined_preset(props, template, preset_values):
    """Set several parameters together from one button click, unlike the per-parameter
    preset dropdowns, which touch one token at a time. Only the tokens named in
    ``preset_values`` change; anything else the user has set (Period, Min, Max) is
    left alone.
    """
    token_to_param = {
        param["token"]: (index, param)
        for index, param in enumerate(template.get("params", []))
    }
    props.suspend_param_updates = True
    try:
        for token, value in preset_values.items():
            entry = token_to_param.get(token)
            if entry is None:
                continue
            index, param = entry
            set_param_value(props, param, index, value)
    finally:
        props.suspend_param_updates = False
def _defaults_for_template(template):
    values = {}
    for param in template.get("params", []):
        default = param.get("default", 0)
        values[param["token"]] = default
        if param.get("type") == "COLOR":
            for component_token, component in zip(colour_component_tokens(param["token"]), default):
                values[component_token] = float(component)
    return values


def _setup_values_for_template(props, template, context):
    """Resolve the remembered pre-apply values without borrowing Live state."""
    values = _defaults_for_template(template)
    variant_saved = utils.read_variant_memory(props).get(template["id"])
    remembered = (
        utils.read_template_memory(props).get(template["id"])
        if _remember_template_state_enabled(context) else None
    )
    if variant_saved:
        values.update(variant_saved)
    if remembered:
        values.update(remembered)
    return values


def live_or_latest_entry(context, template=None):
    """The Live-selected effect's remembered entry, else the last apply.

    Live sliders already resolve through ``live_parameter_binding``. Update
    Selected must use that same identity, or it silently rewrites the previous
    apply while the artist is looking at a different Live effect.
    """
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None:
        return None
    template = template or get_current_template(props)
    if getattr(props, "parameter_mode", "SETUP") == "LIVE":
        token = (
            str(getattr(props, "last_applied_effect_token", "") or "")
            or _selected_applied_token(props)
        )
        if token:
            from ...apply.motion import applied_motion_manager

            source = getattr(props, "driver_target_source", "ACTIVE")
            effect = applied_motion_manager.find_effect(context, token, source)
            extras = ((effect or {}).get("record") or {}).get("extras") or {}
            entry = extras.get("target_entry")
            effect_id = (
                (effect or {}).get("template_id")
                or ((effect or {}).get("template") or {}).get("id")
            )
            if isinstance(entry, dict) and effect_id == (template or {}).get("id"):
                return entry
    from ...apply import target_memory

    return target_memory.latest_entry(props)


def selected_live_effect(context, *, for_draw=False):
    """Return the exact effect chosen in the Live picker, when it still exists."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None or getattr(props, "parameter_mode", "SETUP") != "LIVE":
        return None
    token = _selected_applied_token(props) if props is not None else ""
    if not token:
        return None
    from ...apply.motion import applied_motion_manager

    source = getattr(props, "driver_target_source", "ACTIVE")
    lookup = (applied_motion_manager.find_effect_for_draw if for_draw
              else applied_motion_manager.find_effect)
    return lookup(context, token, source)


def live_parameter_template(context, fallback=None, *, for_draw=False):
    """Parameter schema owned by the selected Live effect.

    Selecting an applied effect edits its settings without navigating the main
    template browser away from the artist's current motion recipe.
    """
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    fallback = fallback or (get_current_template(props) if props else None)
    effect = selected_live_effect(context, for_draw=for_draw)
    template = (effect or {}).get("template")
    return template if parameter_bindings.supports(template) else fallback


def live_parameter_binding(context, template=None, *, for_draw=False):
    """Resolve the exact Live-picked effect, else the active compatible one."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    template = template or (get_current_template(props) if props else None)
    effect = selected_live_effect(context, for_draw=for_draw)
    if effect is not None:
        bound = parameter_bindings.binding_for_effect(context, effect)
        if bound is not None and bound.available:
            return bound
    if not parameter_bindings.supports(template):
        return None
    return parameter_bindings.resolve(context, template)


def settings_share_live_props(context, effect, template=None):
    """True when the settings dialog can reuse the N-panel Live controls."""
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None or getattr(props, "parameter_mode", "SETUP") != "LIVE":
        return False
    template = template or (effect or {}).get("template")
    current = get_current_template(props)
    if not template or current.get("id") != template.get("id"):
        return False
    live = live_parameter_binding(context, template)
    bound = parameter_bindings.binding_for_effect(context, effect)
    if not live or not bound or not live.available or not bound.available:
        return False
    return parameter_bindings.binding_key(live) == parameter_bindings.binding_key(bound)


def _focus_applied_effect_row(props, record_token):
    items = getattr(props, "driver_target_items", None)
    if not items or not record_token:
        return
    for index, item in enumerate(items):
        if getattr(item, "record_token", "") == record_token:
            if int(getattr(props, "driver_target_active_index", -1)) != index:
                props.driver_target_active_index = index
            return


def select_live_effect(props, context, effect):
    """Make this applied effect the live Applied identity. No settings dialog."""
    template = (effect or {}).get("template")
    if template is None:
        return False, "The applied template is no longer available."
    token = str(effect.get("record_token", "") or "")
    if not token:
        return False, "The applied effect no longer resolves."
    binding = parameter_bindings.binding_for_effect(context, effect)
    if binding is None or not binding.available:
        return False, (binding.reason if binding is not None else "The applied effect no longer resolves.")
    # Catalogue motions follow the main picker. Structural effects intentionally
    # use synthetic schemas, so selecting one changes only the Live editor.
    if template.get("id") in templates.TEMPLATE_BY_ID and props.template != template.get("id"):
        if not apply_template_selection(props, template["id"]):
            return False, "The applied recipe is no longer in the catalogue."
    props.last_applied_effect_token = token
    _focus_applied_effect_row(props, token)
    if props.parameter_mode != "LIVE":
        props.parameter_mode = "LIVE"
        return True, props.live_parameter_status
    _refresh_slot_tooltips(template)
    return load_live_parameters(props, context, template, binding=binding)


def _selected_applied_token(props):
    items = getattr(props, "driver_target_items", None)
    if items is not None:
        index = int(getattr(props, "driver_target_active_index", -1))
        if 0 <= index < len(items):
            item = items[index]
            token = str(getattr(item, "record_token", "") or "")
            if token:
                return token
            group_key = getattr(item, "group_key", "")
            for value in items:
                if value.group_key == group_key and getattr(value, "record_token", ""):
                    return str(value.record_token)
    return str(getattr(props, "last_applied_effect_token", "") or "")


def _stored_effect_values(effect):
    record = (effect or {}).get("record") or {}
    extras = record.get("extras") if isinstance(record.get("extras"), dict) else {}
    entry = extras.get("target_entry")
    if not isinstance(entry, dict):
        return {}
    values = entry.get("parameter_values") or entry.get("template_values")
    return dict(values) if isinstance(values, dict) else {}


def _values_differ(left, right, template):
    if not left or not right or not template:
        return False
    for param in template.get("params") or ():
        token = param.get("token")
        if not token:
            continue
        default = param.get("default", 0)
        current = left.get(token, default)
        stored = right.get(token, default)
        kind = param.get("type")
        if kind == "COLOR":
            if any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(tuple(current), tuple(stored))):
                return True
        elif kind == "INT":
            if int(round(current)) != int(round(stored)):
                return True
        elif abs(float(current) - float(stored)) > 1e-6:
            return True
    return False


def mode_presentation(props, context=None):
    """Read-only Setup vs Live identity. Does not mutate catalogue state.

    The saved RNA identifier stays ``LIVE``. Artist-facing copy uses Live.
    """
    mode_id = getattr(props, "parameter_mode", "SETUP") or "SETUP"
    is_applied = mode_id == "LIVE"
    template = get_current_template(props)
    parameter_template = (
        live_parameter_template(context, template)
        if is_applied and context is not None else template
    )
    identity = {
        "mode_id": mode_id,
        "mode_label": "Live" if is_applied else "Setup",
        "is_applied": is_applied,
        "resolved": False,
        "effect_name": "",
        "host_name": "",
        "record_token": "",
        "template_id": parameter_template.get("id", "") if parameter_template else "",
        "reason": "",
        "headline": "Setup",
        "can_compare": False,
        "modified": False,
    }
    if context is None:
        return identity

    from ...apply.motion import applied_motion_manager

    source = getattr(props, "driver_target_source", "ACTIVE")
    token = _selected_applied_token(props)
    effect = (
        applied_motion_manager.find_effect(context, token, source)
        if token else None
    )
    binding = live_parameter_binding(context, parameter_template) if is_applied else None
    if effect is None and token and not (binding is not None and binding.available):
        identity["record_token"] = token
        identity["reason"] = "The selected applied effect no longer resolves."
        identity["headline"] = identity["reason"]
        if is_applied:
            return identity

    if effect is not None:
        host = effect.get("host")
        identity["record_token"] = effect.get("record_token", "")
        identity["effect_name"] = effect.get("label") or (effect.get("template") or {}).get("name", "")
        identity["host_name"] = effect.get("host_label") or getattr(host, "name", "")
        identity["template_id"] = effect.get("template_id") or identity["template_id"]
        identity["resolved"] = True
        identity["headline"] = (
            "%s on %s" % (identity["effect_name"], identity["host_name"])
            if identity["host_name"] else identity["effect_name"]
        )
        stored = _stored_effect_values(effect)
        compare_template = effect.get("template") or parameter_template
        if stored and compare_template:
            identity["can_compare"] = True
            identity["modified"] = _values_differ(
                collect_values(props, compare_template), stored, compare_template,
            )
        return identity

    if is_applied and binding is not None:
        identity["effect_name"] = binding.label or (parameter_template.get("name") if parameter_template else "")
        identity["reason"] = "" if binding.available else (binding.reason or "")
        identity["resolved"] = bool(binding.available)
        identity["headline"] = (
            identity["effect_name"] if binding.available else identity["reason"]
        )
        if binding.available:
            stored = parameter_bindings.read_values(binding)
            if stored and parameter_template:
                identity["can_compare"] = True
                identity["modified"] = _values_differ(
                    collect_values(props, parameter_template), stored, parameter_template,
                )
        return identity

    if is_applied:
        identity["reason"] = "No applied effect is selected."
        identity["headline"] = identity["reason"]
    return identity


def commit_slot_presentation(props, context=None):
    """Read-only commit slot: one label and one operator family per mode."""
    from ...engine import utils
    from ...apply import target_memory
    from ...catalogue import templates as catalogue_templates

    identity = mode_presentation(props, context)
    template = get_current_template(props)
    is_motion = catalogue_templates.has_motion_plan(template)
    drives_transforms = False
    if is_motion and template is not None:
        channels = catalogue_templates.template_channels(template)
        paths = {channel.get("data_path", "") for channel in channels if channel.get("data_path")}
        transform_paths = {
            "location", "rotation_euler", "rotation_quaternion", "scale",
            "delta_location", "delta_rotation_euler", "delta_scale",
        }
        drives_transforms = bool(paths) and all(path in transform_paths for path in paths)

    slot = {
        "mode_id": identity["mode_id"],
        "mode_label": identity["mode_label"],
        "family": "update" if identity["is_applied"] else "apply",
        "label": "Update Selected" if identity["is_applied"] else "Apply New",
        "operator_id": (
            "espresso.update_last_target"
            if identity["is_applied"]
            else "espresso.apply_expression"
        ),
        "enabled": False,
        "reason": "",
        "destination": identity["headline"] if identity["is_applied"] and identity["resolved"] else "",
        "conflict": "",
        "resources": "",
        "opens_route_dialog": False,
    }

    if identity["is_applied"]:
        latest = target_memory.latest_entry(props)
        if not identity["resolved"] and not latest:
            slot["reason"] = identity["reason"] or "No applied effect is selected."
            return slot
        if not slot["destination"] and latest:
            slot["destination"] = latest.get("display_label", "")
        slot["enabled"] = bool(getattr(props, "is_valid", False))
        if not slot["enabled"]:
            slot["reason"] = getattr(props, "validation_message", "") or "The expression is not ready."
        slot["resources"] = "Updates the selected applied effect."
        return slot

    if is_motion and drives_transforms:
        active = getattr(context, "active_object", None) if context is not None else None
        slot["destination"] = getattr(active, "name", "") if active is not None else ""
        slot["enabled"] = bool(getattr(props, "is_valid", False) and active is not None)
        if active is None:
            slot["reason"] = "Select an object to apply this motion."
        elif not getattr(props, "is_valid", False):
            slot["reason"] = getattr(props, "validation_message", "") or "The expression is not ready."
        slot["resources"] = "Creates a motion set"
    else:
        slot["operator_id"] = "espresso.apply_expression"
        if context is not None:
            fcurve, driver, reason = utils.get_target_driver_fcurve(context)
            if fcurve is not None and driver is not None:
                owner = getattr(fcurve.id_data, "name", "Target")
                dest = "%s > %s" % (owner, fcurve.data_path)
                if getattr(fcurve, "array_index", -1) >= 0:
                    dest += "[%d]" % fcurve.array_index
                slot["destination"] = dest
                slot["enabled"] = bool(getattr(props, "is_valid", False))
            else:
                slot["reason"] = reason or "Choose a driver target first."
        if slot["enabled"] and not getattr(props, "is_valid", False):
            slot["enabled"] = False
            slot["reason"] = getattr(props, "validation_message", "") or "The expression is not ready."
        slot["resources"] = "Creates 1 native driver"

    latest = target_memory.latest_entry(props)
    if latest:
        slot["conflict"] = "Existing Espresso effect - will replace"
    return slot


def clear_pinned_live_entry(props):
    props.pinned_live_template_id = ""
    props.pinned_live_label = ""
    props.pinned_live_entry_json = ""


def load_live_parameters(props, context, template=None, binding=None):
    template = live_parameter_template(context, template or get_current_template(props))
    binding = binding or live_parameter_binding(context, template)
    if binding is None:
        props.live_parameter_status = "Live parameters are unavailable for this template."
        props.live_parameter_binding_key = ""
        return False, props.live_parameter_status
    if not binding.available:
        props.live_parameter_status = binding.reason
        props.live_parameter_binding_key = ""
        return False, binding.reason
    try:
        _apply_values(props, template, parameter_bindings.read_values(binding))
    except (KeyError, TypeError, ValueError, ReferenceError, RuntimeError) as exc:
        props.live_parameter_status = "Could not read the live setup: %s" % exc
        return False, props.live_parameter_status
    props.live_parameter_status = "%s · %s" % (binding.label, binding.route)
    props.live_parameter_binding_key = parameter_bindings.binding_key(binding)
    return True, props.live_parameter_status


def sync_live_parameters(props, context, template=None):
    template = live_parameter_template(context, template or get_current_template(props))
    binding = live_parameter_binding(context, template)
    if binding is None:
        props.live_parameter_status = "Live parameters are unavailable for this template."
        return False, props.live_parameter_status
    if not binding.available or not binding.editable:
        props.live_parameter_status = binding.reason
        return False, binding.reason
    if parameter_bindings.binding_key(binding) != props.live_parameter_binding_key:
        props.live_parameter_status = 'Loading the active effect; wait for its Live controls to load.'
        return False, props.live_parameter_status
    ok, message = parameter_bindings.write_values(
        binding, collect_values(props, template), context.scene,
        # The same toggle Apply read: which Euler axes feed a quaternion
        # bone cannot be told from its four stored components. Only a motion
        # plan has channels to toggle; a structural schema has none to read.
        enabled_channel_ids=(
            enabled_channel_ids_for_template(props, template)
            if templates.has_motion_plan(template) else None
        ),
    )
    props.live_parameter_status = (
        "%s · %s" % (binding.label, binding.route) if ok else message
    )
    return ok, message


def on_parameter_mode_update(self, context):
    if getattr(self, "suspend_param_updates", False):
        return
    template = get_current_template(self)
    if not parameter_bindings.supports(template):
        return
    if self.parameter_mode == "LIVE":
        if _remember_template_state_enabled(context):
            _save_template_state(self, template)
        load_live_parameters(self, context, template)
    else:
        _apply_values(self, template, _setup_values_for_template(self, template, context))
        self.live_parameter_status = ""
        self.live_parameter_binding_key = ""
    refresh_preview(self, context)


def _is_changed_from_default(param, value):
    default = param.get("default", 0)
    if param.get("type") == "COLOR":
        return any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(tuple(value), tuple(default)))
    if param.get("type") == "INT":
        return int(round(value)) != int(round(default))
    return abs(float(value) - float(default)) > 1e-6


def _manual_overrides(template, values):
    overrides = {}
    for param in template.get("params", []):
        token = param["token"]
        value = values.get(token, param.get("default", 0))
        if _is_changed_from_default(param, value):
            overrides[token] = value
    return overrides


def _shared_tokens(template, values):
    template_tokens = {param["token"] for param in template.get("params", [])}
    return {token: value for token, value in (values or {}).items() if token in template_tokens}


def _save_template_state(props, template):
    if template is None:
        return
    from ...engine import utils

    memory = utils.read_template_memory(props)
    memory[template["id"]] = collect_values(props, template)
    utils.write_template_memory(props, memory)


def refresh_preview(props, context):
    from ...engine import utils

    template = get_current_template(props)

    if getattr(props, "manual_mode", False) and not templates.has_motion_plan(template):
        # User is hand-editing the expression: validate it as-is, do not rebuild
        # from the template parameters.
        expression = props.manual_expression
        valid, message = utils.validate_driver_expression(expression, template, context.scene)
        props.preview = expression
        props.is_valid = bool(valid)
        props.validation_message = message
        props.preview_output_baseline = 0.0
        return

    preview_values = collect_values(props, template)
    # Advanced controls are opt-in; only fold them into the expression when the
    # user has enabled them, so the default workflow stays the exact base template.
    if _advanced_controls_enabled(context):
        preview_values.update(collect_advanced_values(props, template))
    built_channels = utils.build_template_expressions(template, preview_values, context.scene)
    channel_ids = {item["id"] for item in built_channels}
    if props.selected_motion_channel not in channel_ids:
        props.selected_motion_channel = built_channels[0]["id"] if built_channels else ""

    validation_messages = []
    all_valid = bool(built_channels)
    for item in built_channels:
        valid, message = utils.validate_driver_expression(item["expression"], template, context.scene)
        all_valid = all_valid and bool(valid)
        validation_messages.extend(item.get("warnings") or [])
        # A recipe's own blocking check (a linkage that cannot close) makes
        # the expression as unusable as a syntax error would.
        if any(utils.is_blocking_warning(text) for text in item.get("warnings") or ()):
            all_valid = False
        if not valid and message:
            validation_messages.append(f"{item['label']}: {message}")

    selected = next(
        (item for item in built_channels if item["id"] == props.selected_motion_channel),
        built_channels[0] if built_channels else {"expression": "", "output_baseline": 0.0},
    )
    props.motion_channel_previews = json.dumps(built_channels, sort_keys=True)
    props.preview = selected["expression"]
    props.preview_output_baseline = float(selected.get("output_baseline", 0.0))
    props.is_valid = all_valid
    props.validation_message = "; ".join(dict.fromkeys(validation_messages))


def built_channel_previews(props):
    try:
        data = json.loads(getattr(props, "motion_channel_previews", "[]") or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _disabled_channel_map(props):
    """{template_id: [channel_id, ...]}, tolerant of a corrupt/missing store."""
    try:
        data = json.loads(getattr(props, "disabled_motion_channels", "{}") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def motion_channel_enabled(props, template_id, channel_id):
    disabled = _disabled_channel_map(props).get(template_id) or []
    return channel_id not in disabled


def set_motion_channel_enabled(props, template_id, channel_id, enabled):
    """Flip one channel's toggle for one template, keyed so ids shared across
    many templates (``rotation_x`` is one of dozens) never cross-contaminate."""
    store = _disabled_channel_map(props)
    disabled = list(store.get(template_id) or [])
    if enabled:
        disabled = [c for c in disabled if c != channel_id]
    elif channel_id not in disabled:
        disabled.append(channel_id)
    if disabled:
        store[template_id] = disabled
    else:
        store.pop(template_id, None)
    props.disabled_motion_channels = json.dumps(store, sort_keys=True)


def enabled_channel_ids_for_template(props, template):
    """The set to pass as ``enabled_channel_ids`` to the apply layer, or
    ``None`` when nothing is toggled off - the apply layer treats ``None`` as
    "no filtering", which is the zero-overhead common case."""
    template_id = template.get("id", "")
    disabled = set(_disabled_channel_map(props).get(template_id) or [])
    if not disabled:
        return None
    all_ids = {channel["id"] for channel in templates.template_channels(template)}
    return all_ids - disabled


def apply_template_defaults(props, template):
    _apply_values(props, template, _defaults_for_template(template))
# The ``template`` EnumProperty's item list is recomputed from both ``search_text`` and
# ``category`` (see template_items), and assigning an id that is not in the list as it
# stands at that instant raises TypeError. Two rules keep this safe:
#
# 1. Narrow the item list before assigning into it: clear an active search so the list
#    is scoped to the category about to be selected from.
# 2. Never read ``self.template`` to decide what to assign. When the stored enum value
#    has fallen out of the current item list, Blender resolves the read to the first
#    item, which hides the mismatch. Read ``self.last_template_id`` (a plain
#    StringProperty) instead.
#
# Clearing ``search_text`` fires on_search_update, which would otherwise chase the
# category back and recurse, so both callbacks share a re-entrancy guard.


def apply_template_selection(props, template_id):
    """Move the UI to ``template_id``, keeping category, search and enum consistent.

    This is the only sanctioned way for an operator to change the selected
    template. It derives the category from the template itself — never from a
    caller-supplied one, which goes stale as soon as a cross-category search
    result is chosen — and narrows the enum's item list before assigning into it.

    Returns False when ``template_id`` is unknown.
    """
    template = templates.TEMPLATE_BY_ID.get(template_id)
    if template is None:
        return False

    # Point last_template_id at the target before the enum is assigned. The template
    # list collapses each channel set to one member and keeps the one last_template_id
    # names; without this, switching to a sibling would assign a value the collapsed
    # list does not contain, and Blender rejects an enum value not in its items. This is
    # a plain StringProperty with no update hook, and the items cache key includes it,
    # so setting it here makes the list recompute with the target present. The outgoing
    # template is remembered first, because on_template_update needs it to carry shared
    # values across and the line below overwrites it to keep the collapsed enum valid.
    if props.last_template_id != template_id:
        props.switch_from_template_id = props.last_template_id
    props.last_template_id = template_id

    category = template["category"]
    browse_group = browse_groups.group_for_template(category, template_id)
    if props.category != category:
        # on_category_update clears the search and resolves the pending id, in
        # that order, so the enum is guaranteed to contain the target.
        props.pending_template_id = template_id
        props.category = category
    else:
        props.suspend_nav_updates = True
        try:
            props.pending_template_id = ""
            props.search_text = ""
            props.browse_group = browse_group
        finally:
            props.suspend_nav_updates = False
        props.template = template_id
    return True


def on_category_update(self, context):
    if self.suspend_nav_updates:
        return

    self.suspend_nav_updates = True
    try:
        # Rule 1: scope the enum to this category before touching `template`.
        if self.search_text:
            self.search_text = ""

        pending = templates.TEMPLATE_BY_ID.get(self.pending_template_id)
        self.browse_group = (
            browse_groups.group_for_template(
                self.category, self.pending_template_id,
            )
            if pending and pending.get("category") == self.category
            else browse_groups.ALL_GROUP
        )
        matches = browse_groups.templates_for_group(
            templates.TEMPLATES,
            self.category,
            self.browse_group,
        )
        if not matches:
            return

        available = {item["id"] for item in matches}
        fallback = matches[0]["id"]
        target_id = self.pending_template_id if self.pending_template_id in available else fallback
        self.pending_template_id = ""

        previous = self.last_template_id  # Rule 2
        self.template = target_id  # safe: target_id is in the now-current item list
    finally:
        self.suspend_nav_updates = False

    # Assigning an unchanged value does not fire on_template_update, so the
    # preview would go stale without this.
    if previous == target_id:
        refresh_preview(self, context)


def on_browse_group_update(self, context):
    if self.suspend_nav_updates:
        return

    self.suspend_nav_updates = True
    try:
        if self.search_text:
            self.search_text = ""
        matches = browse_groups.templates_for_group(
            templates.TEMPLATES,
            self.category,
            self.browse_group,
        )
        if not matches:
            return
        available = {item["id"] for item in matches}
        current_id = self.last_template_id
        fallback = matches[0]["id"]
        target_id = current_id if current_id in available else fallback
        previous = self.last_template_id
        self.template = target_id
    finally:
        self.suspend_nav_updates = False

    if previous == target_id:
        refresh_preview(self, context)


def on_search_update(self, context):
    if self.suspend_nav_updates:
        return

    if self.search_text:
        # Typing filters only. Selecting a hit is an explicit click through
        # espresso.select_template or the Template dropdown.
        return

    # Search cleared. The selected template may belong to another category, which
    # would strand it outside the enum. Move the category to wherever it lives.
    current = templates.TEMPLATE_BY_ID.get(self.last_template_id)
    if current is not None and self.category != current["category"]:
        self.pending_template_id = current["id"]
        self.category = current["category"]  # on_category_update resolves the pending id
        return
    if current is not None:
        target_group = browse_groups.group_for_template(
            current["category"], current["id"],
        )
        if self.browse_group != target_group:
            self.suspend_nav_updates = True
            try:
                self.browse_group = target_group
            finally:
                self.suspend_nav_updates = False
            self.template = current["id"]
            return

    refresh_preview(self, context)


def on_driver_target_active_index_update(self, context):
    """The Driver Target UIList writes its own selected index directly (a
    genuine user click, not our code writing during draw()), so reacting here
    is safe. Does implicitly what a per-row 'set active' button would do
    explicitly."""
    if 0 <= self.driver_target_active_index < len(self.driver_target_items):
        item = self.driver_target_items[self.driver_target_active_index]
        if item.row_kind == "TARGET":
            from ...apply import driver_targets
            from ...engine import utils
            utils.set_active_driver_target(context, driver_targets.descriptor_from_item(item))
            return
        if (
            self.applied_effect_settings_pinned
            and item.row_kind == "GROUP"
            and item.record_token
        ):
            from ...apply.motion import applied_motion_manager
            from ..actions.driver_manager import populate_pinned_effect_settings

            effect = applied_motion_manager.find_effect(
                context, item.record_token, self.driver_target_source,
            )
            if effect is not None:
                populate_pinned_effect_settings(context, effect)


def on_template_update(self, context):
    # apply_template_selection points last_template_id at the target before assigning
    # the enum (the collapsed channel list must contain the value being set), so it
    # cannot answer "what were we on?". It stashes the outgoing id here instead; a
    # direct enum edit falls back to last_template_id, where nothing has been
    # overwritten.
    previous_id = self.switch_from_template_id or self.last_template_id
    self.switch_from_template_id = ""
    selected_id = self.template
    if selected_id in templates.TEMPLATE_BY_ID:
        self.last_template_id = selected_id
    template = get_current_template(self)
    previous = templates.TEMPLATE_BY_ID.get(previous_id) if previous_id else None

    previous_mode = getattr(self, "parameter_mode", "SETUP")
    previous_values = {}
    carry_values = {}
    if previous and previous["id"] != template["id"]:
        if previous_mode != "LIVE":
            previous_values = collect_values(self, previous)
            if _remember_template_state_enabled(context):
                _save_template_state(self, previous)
            if _carry_manual_params_for_switch(context, previous, template):
                carry_values = _manual_overrides(previous, previous_values)

    self.suspend_param_updates = True
    try:
        if getattr(self, "pinned_live_template_id", "") and selected_id != getattr(self, "pinned_live_template_id", ""):
            clear_pinned_live_entry(self)
        self.parameter_mode = "SETUP"
        self.live_parameter_status = ""
        self.live_parameter_binding_key = ""
    finally:
        self.suspend_param_updates = False

    from ...engine import utils

    memory = utils.read_template_memory(self)
    target_values = _defaults_for_template(template)

    remembered = memory.get(template["id"]) if _remember_template_state_enabled(context) else None
    variant_saved = utils.read_variant_memory(self).get(template["id"])
    if variant_saved:
        target_values.update(variant_saved)
    if remembered:
        target_values.update(remembered)
    if carry_values:
        target_values.update(_shared_tokens(template, carry_values))

    _refresh_slot_tooltips(template)
    _apply_values(self, template, target_values)
    self.last_template_id = template["id"]
    # Selecting a template drops out of any hand-edit session and logs a recent.
    if getattr(self, "manual_mode", False):
        self.manual_mode = False
    push_recent_template(self, template["id"])
    refresh_preview(self, context)


def on_param_update(self, context):
    if getattr(self, "suspend_param_updates", False):
        return
    template = get_current_template(self)
    if (getattr(self, "parameter_mode", "SETUP") == "LIVE"
            and parameter_bindings.supports(live_parameter_template(context, template))):
        template = live_parameter_template(context, template)
        sync_live_parameters(self, context, template)
        refresh_preview(self, context)
        return
    if _remember_template_state_enabled(context):
        _save_template_state(self, template)
    refresh_preview(self, context)


def _on_visualizer_zoom_axis_update(self, context, changed_axis):
    """Keep at least one graph zoom axis active, including scripted changes."""
    if getattr(self, "suspend_param_updates", False):
        return
    if not self.visualizer_zoom_x and not self.visualizer_zoom_y:
        self.suspend_param_updates = True
        try:
            setattr(self, changed_axis, True)
        finally:
            self.suspend_param_updates = False
    refresh_preview(self, context)


def on_visualizer_zoom_x_update(self, context):
    _on_visualizer_zoom_axis_update(self, context, "visualizer_zoom_x")


def on_visualizer_zoom_y_update(self, context):
    _on_visualizer_zoom_axis_update(self, context, "visualizer_zoom_y")


def on_manual_expression_update(self, context):
    if getattr(self, "suspend_param_updates", False):
        return
    refresh_preview(self, context)


def on_manual_mode_update(self, context):
    """Seed edit mode before validating so the generated formula cannot vanish."""
    if getattr(self, "suspend_param_updates", False):
        return
    if getattr(self, "manual_mode", False):
        self.suspend_param_updates = True
        try:
            self.manual_expression = getattr(self, "preview", "")
        finally:
            self.suspend_param_updates = False
    refresh_preview(self, context)


# --------------------------------------------------------------------------- #
# Recent templates (a JSON-backed list of template ids)
# --------------------------------------------------------------------------- #
def _read_id_list(raw):
    import json

    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []


def _write_id_list(values):
    import json

    return json.dumps(values)


def get_recents(props):
    return _read_id_list(getattr(props, "recents_mem", "[]"))


def push_recent_template(props, template_id):
    recents = [tid for tid in get_recents(props) if tid != template_id]
    recents.insert(0, template_id)
    props.recents_mem = _write_id_list(recents[:8])


def _make_token_getter(token, fallback):
    def getter(self):
        template = get_current_template(self)
        for index, param in enumerate(template.get("params", [])):
            if param["token"] == token:
                return get_param_value(self, param, index)
        return fallback

    return getter


def _make_token_setter(token):
    def setter(self, value):
        template = get_current_template(self)
        for index, param in enumerate(template.get("params", [])):
            if param["token"] == token:
                set_param_value(self, param, index, value)
                return

    return setter


TOKEN_SPECS = _collect_token_specs()
TOKEN_PROXY_SPECS = _collect_proxy_safe_token_specs()


def _token_proxy_property(token):
    spec = TOKEN_PROXY_SPECS.get(token) or TOKEN_SPECS.get(token) or {
        "token": token,
        "label": token.title(),
        "type": "FLOAT",
        "default": 0,
    }
    kwargs = {
        "name": spec.get("label", token.title()),
        "description": templates.format_param_input_tooltip(spec),
        "get": _make_token_getter(token, spec.get("default", 0)),
        "set": _make_token_setter(token),
    }
    if spec.get("type") == "INT":
        if spec.get("min") is not None:
            kwargs["min"] = int(spec["min"])
        if spec.get("max") is not None:
            kwargs["max"] = int(spec["max"])
        return bpy.props.IntProperty(**kwargs)
    if spec.get("min") is not None:
        kwargs["min"] = float(spec["min"])
    if spec.get("max") is not None:
        kwargs["max"] = float(spec["max"])
    return bpy.props.FloatProperty(**kwargs)


def _advanced_control_property(token):
    spec = templates.resolve_advanced_param(token)
    kwargs = {
        "name": spec.get("label", token.title()),
        "description": templates.format_param_input_tooltip(spec),
        "default": spec.get("default", 0),
        "update": on_param_update,
    }
    if spec.get("type") == "BOOL":
        return bpy.props.BoolProperty(**kwargs)
    if spec.get("type") == "INT":
        if spec.get("min") is not None:
            kwargs["min"] = int(spec["min"])
        if spec.get("max") is not None:
            kwargs["max"] = int(spec["max"])
        return bpy.props.IntProperty(**kwargs)
    if spec.get("min") is not None:
        kwargs["min"] = float(spec["min"])
    if spec.get("max") is not None:
        kwargs["max"] = float(spec["max"])
    return bpy.props.FloatProperty(**kwargs)


class ESPRESSO_DriverTargetItem(bpy.types.PropertyGroup):
    """One row in the Driver Target list — a cheap cache (path/index/label
    strings only, no live FCurve reference) so panels.template_list can drive
    a real scrollable, searchable UIList instead of drawing every driver as a
    flat row every redraw."""
    data_path: bpy.props.StringProperty(name="Data Path", default="")
    array_index: bpy.props.IntProperty(name="Array Index", default=-1)
    label: bpy.props.StringProperty(name="Label", default="")
    category: bpy.props.StringProperty(name="Category", default="")
    icon: bpy.props.StringProperty(name="Icon", default="DRIVER")
    owner_id_type: bpy.props.StringProperty(name="Owner ID Type", default="")
    owner_id_name: bpy.props.StringProperty(name="Owner ID Name", default="")
    owner_path: bpy.props.StringProperty(name="Owner Path", default="")
    row_kind: bpy.props.StringProperty(name="Row Kind", default="TARGET")
    group_key: bpy.props.StringProperty(name="Group Key", default="")
    descriptor_key: bpy.props.StringProperty(name="Descriptor Key", default="")
    effect_code: bpy.props.StringProperty(name="Effect Code", default="")
    template_id: bpy.props.StringProperty(name="Template ID", default="")
    editable: bpy.props.BoolProperty(name="Editable", default=False)
    effect_kind: bpy.props.StringProperty(name="Effect Kind", default="SINGLE_PROPERTY")
    record_token: bpy.props.StringProperty(name="Applied Effect Token", default="")
    batch_eligible: bpy.props.BoolProperty(name="Batch Eligible", default=True)
    bakeable: bpy.props.BoolProperty(name="Bakeable", default=False)
    removable: bpy.props.BoolProperty(name="Removable", default=False)
    visible: bpy.props.BoolProperty(name="Visible", default=True)
    badge_text: bpy.props.StringProperty(name="Badge", default="")
    member_count: bpy.props.IntProperty(name="Member Count", default=0)
    nest_depth: bpy.props.IntProperty(name="Nest Depth", default=0)
    batch_selected: bpy.props.BoolProperty(
        name="Selected",
        description="Include this applied target in manager batch actions",
        default=False,
    )


def on_pinned_effect_parameter_update(self, context):
    """Write only Scene-owned pinned controls; popup-dialog values stay staged."""
    scene = getattr(context, "scene", None)
    props = getattr(scene, "espresso_props", None) if scene is not None else None
    if props is None or not getattr(props, "applied_effect_settings_pinned", False):
        return
    if getattr(props, "suspend_pinned_effect_updates", False):
        return
    try:
        if self.id_data is not scene:
            return
        pointer = self.as_pointer()
        if not any(item.as_pointer() == pointer for item in props.pinned_effect_parameters):
            return
    except (AttributeError, ReferenceError):
        return

    from ..actions import driver_manager as driver_manager_actions

    driver_manager_actions.sync_pinned_effect_settings(context)


class ESPRESSO_DriverEditParamItem(bpy.types.PropertyGroup):
    """Temporary popup value; the applied effect remains the source of truth."""
    token: bpy.props.StringProperty(default="")
    label: bpy.props.StringProperty(default="")
    value_type: bpy.props.StringProperty(default="FLOAT")
    unit: bpy.props.StringProperty(default="")
    float_value: bpy.props.FloatProperty(
        default=0.0, update=on_pinned_effect_parameter_update,
    )
    int_value: bpy.props.IntProperty(
        default=0, update=on_pinned_effect_parameter_update,
    )
    color_value: bpy.props.FloatVectorProperty(
        size=4, subtype="COLOR", min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        update=on_pinned_effect_parameter_update,
    )


class ESPRESSO_Props(bpy.types.PropertyGroup):
    template_kind: bpy.props.EnumProperty(
        name="Kind",
        description=(
            "Narrow the category list to generic shapes or to tailored "
            "real-world behaviours"
        ),
        items=TEMPLATE_KIND_ITEMS,
        default="ALL",
        update=on_template_kind_update,
    )
    category: bpy.props.EnumProperty(name="Category", description="Filter templates by animation/use-case family", items=category_items, update=on_category_update)
    browse_group: bpy.props.EnumProperty(
        name="Subcategory",
        description="Show a broad shelf inside crowded categories without changing global search",
        items=browse_group_items,
        update=on_browse_group_update,
    )
    template: bpy.props.EnumProperty(
        name="Template",
        # Deliberately empty. Blender prepends this to the selected item's tooltip, so
        # anything here would be repeated on every hover and push the loaded template's
        # description down the tooltip. The general explanation lives in Preferences >
        # Help.
        description="",
        items=template_items,
        update=on_template_update,
    )
    search_text: bpy.props.StringProperty(name="Search", description="Search template names, categories, descriptions, and use cases", default="", update=on_search_update)
    parameter_mode: bpy.props.EnumProperty(
        name="Parameter Mode",
        description="Choose whether these controls prepare the next apply or edit a selected applied effect",
        items=(
            ("SETUP", "Setup", "Edit values used the next time this motion is applied"),
            ("LIVE", "Live", "Edit the selected applied effect. The saved identifier stays LIVE."),
        ),
        default="SETUP",
        update=on_parameter_mode_update,
    )
    live_parameter_status: bpy.props.StringProperty(
        name="Live Parameter Status", default="", options={"HIDDEN"},
    )
    live_parameter_binding_key: bpy.props.StringProperty(
        name="Live Parameter Binding Key", default="", options={"HIDDEN", "SKIP_SAVE"},
    )
    pinned_live_template_id: bpy.props.StringProperty(
        name="Pinned Live Template", default="", options={"HIDDEN"},
    )
    pinned_live_entry_json: bpy.props.StringProperty(
        name="Pinned Live Entry", default="", options={"HIDDEN"},
    )
    pinned_live_label: bpy.props.StringProperty(
        name="Pinned Live Label", default="", options={"HIDDEN"},
    )
    applied_effect_settings_pinned: bpy.props.BoolProperty(
        name="Show Settings",
        description="Show or hide structure and motion settings below the applied effects list",
        default=True,
    )
    pinned_applied_effect_token: bpy.props.StringProperty(
        name="Pinned Applied Effect", default="", options={"HIDDEN"},
    )
    pinned_applied_effect_source: bpy.props.StringProperty(
        name="Pinned Applied Effect Scope", default="ACTIVE", options={"HIDDEN"},
    )
    pinned_applied_effect_label: bpy.props.StringProperty(
        name="Pinned Applied Effect Label", default="", options={"HIDDEN"},
    )
    pinned_effect_parameters: bpy.props.CollectionProperty(
        type=ESPRESSO_DriverEditParamItem,
    )
    suspend_pinned_effect_updates: bpy.props.BoolProperty(
        default=False, options={"HIDDEN", "SKIP_SAVE"},
    )
    param_color_0: bpy.props.FloatVectorProperty(size=3, subtype="COLOR", min=0.0, max=1.0, default=(1.0, 1.0, 1.0), description=_GENERIC_SLOT_TIP, update=on_param_update)
    param_color_1: bpy.props.FloatVectorProperty(size=3, subtype="COLOR", min=0.0, max=1.0, default=(1.0, 1.0, 1.0), description=_GENERIC_SLOT_TIP, update=on_param_update)
    param_color_2: bpy.props.FloatVectorProperty(size=3, subtype="COLOR", min=0.0, max=1.0, default=(1.0, 1.0, 1.0), description=_GENERIC_SLOT_TIP, update=on_param_update)
    param_color_3: bpy.props.FloatVectorProperty(size=3, subtype="COLOR", min=0.0, max=1.0, default=(1.0, 1.0, 1.0), description=_GENERIC_SLOT_TIP, update=on_param_update)

    param_float_0: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_1: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_2: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_3: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_4: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_5: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_6: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_float_7: bpy.props.FloatProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)


    param_int_0: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_1: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_2: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_3: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_4: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_5: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_6: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)
    param_int_7: bpy.props.IntProperty(description="Driver Espresso template parameter. Hover the parameter label for template-specific meaning, default, and range.", update=on_param_update)

    preview: bpy.props.StringProperty(name="Expression", default="")
    preview_output_baseline: bpy.props.FloatProperty(default=0.0, options={"HIDDEN"})
    motion_channel_previews: bpy.props.StringProperty(
        name="Motion Channel Previews",
        description="Built expression and destination metadata for every channel in the current template",
        default="[]",
        options={"HIDDEN"},
    )
    selected_motion_channel: bpy.props.StringProperty(
        name="Selected Motion Channel",
        description="Channel currently shown in the graph preview",
        default="",
        options={"HIDDEN"},
    )
    disabled_motion_channels: bpy.props.StringProperty(
        name="Disabled Motion Channels",
        description=(
            "Per-template channel toggles: {template_id: [channel_id, ...]}. "
            "Keyed by template so a channel id shared by many templates "
            '("rotation_x" is common) never leaks a toggle from one template '
            "into an unrelated one that happens to reuse the same id"
        ),
        default="{}",
        options={"HIDDEN"},
    )
    is_valid: bpy.props.BoolProperty(name="Valid", default=False)
    validation_message: bpy.props.StringProperty(name="Validation", default="")
    compact_level: bpy.props.IntProperty(
        name="Compact Level",
        description=(
            "Panel density: 0 normal, 1 hides secondary sections, 2 keeps "
            "only template controls and applicable actions"
        ),
        min=0,
        max=2,
        default=0,
        options={"HIDDEN"},
    )
    compact_mode: bpy.props.BoolProperty(
        name="Compact Mode",
        description=(
            "Compatibility flag for the compact panel view. The header cycles "
            "Normal, Compact L1, and Compact L2; existing files with this flag "
            "enabled open in Compact L1"
        ),
        default=False,
    )
    preview_compact_level: bpy.props.IntProperty(
        name="Preview Compact Level",
        description=(
            "Preview panel density: 0 normal, 1 keeps graph controls, "
            "2 shows only the graph"
        ),
        min=0,
        max=2,
        default=0,
        options={"HIDDEN"},
    )
    template_variants_open: bpy.props.BoolProperty(name="Template Variants Open", description="Expand or collapse the template variants section", default=True)
    parameters_open: bpy.props.BoolProperty(name="Parameters Open", description="Expand or collapse the template parameters section", default=True)
    param_group_timing_open: bpy.props.BoolProperty(
        name="Timing Open",
        description="Expand or collapse timing controls",
        default=False,
    )
    param_group_spatial_open: bpy.props.BoolProperty(
        name="Spatial Open",
        description="Expand or collapse spatial controls",
        default=False,
    )
    param_group_additional_open: bpy.props.BoolProperty(
        name="Additional Open",
        description="Expand or collapse additional controls",
        default=False,
    )
    advanced_timing_open: bpy.props.BoolProperty(name="Advanced Timing Open", description="Expand or collapse the advanced timing controls section", default=True)
    advanced_output_open: bpy.props.BoolProperty(name="Advanced Output Open", description="Expand or collapse the advanced output controls section", default=True)
    applied_effects_cached_count: bpy.props.IntProperty(
        name="Applied Effects Cached Count",
        description="Last known Applied Effects count for the collapsed panel header",
        default=0,
        min=0,
    )
    applied_effects_cached_label: bpy.props.StringProperty(
        name="Applied Effects Cached Label",
        description="Last known Applied Effects header label, written during discovery",
        default="0",
    )
    applied_effects_scene_cached_count: bpy.props.IntProperty(
        name="Applied Effects Scene Cached Count",
        description="Last known Scene-scope effect count, used when Active is empty",
        default=0,
        min=0,
    )
    applied_motions_open: bpy.props.BoolProperty(
        name="Applied Effects Open",
        description="Expand or collapse the applied effects list and scope tabs",
        default=True,
    )
    sidebar_tab: bpy.props.EnumProperty(
        name="Workspace",
        description="Switch between choosing a motion, organizing applied effects, and input controllers inside the Espresso panel",
        items=(
            (
                "MOTION",
                "Motion",
                "Choose a motion and set its parameters; Preview remains in its own panel",
                "ANIM_DATA",
                0,
            ),
            (
                "ORGANIZE",
                "Organize",
                "Prepare layouts and manage applied effects",
                "NLA",
                1,
            ),
            (
                "CONTROLLER",
                "Controller",
                "Named input controllers and live control",
                "SETTINGS",
                2,
            ),
        ),
        default="MOTION",
    )
    pinned_effect_settings_open: bpy.props.BoolProperty(
        name="Settings Open",
        description="Expand or collapse settings for the selected layout or motion",
        default=True,
    )
    actions_open: bpy.props.BoolProperty(
        name="Apply & Manage Open",
        description="Expand or collapse the apply and manage buttons",
        default=True,
    )
    # The last bake range the artist confirmed. Remembered per scene, because the useful
    # range is a property of the shot rather than of the session, so a deliberate choice
    # (bake 40-90 to check one beat) is not thrown away when the dialog closes.
    # ``bake_range_remembered`` gates it: without the flag a start of 0 is
    # indistinguishable from "never set", and the first bake in a fresh scene should
    # offer the scene range.
    bake_range_remembered: bpy.props.BoolProperty(default=False)
    bake_last_start: bpy.props.IntProperty(default=1)
    bake_last_end: bpy.props.IntProperty(default=250)
    bake_last_step: bpy.props.IntProperty(default=1, min=1)
    bake_last_duration: bpy.props.IntProperty(default=250, min=1)
    bake_last_use_duration: bpy.props.BoolProperty(default=True)
    bake_last_match_motion: bpy.props.BoolProperty(default=True)
    bake_last_start_at_current: bpy.props.BoolProperty(default=False)

    preview_open: bpy.props.BoolProperty(name="Show Formula", description="Expand or collapse the generated expression preview", default=True)
    rest_start_mode: bpy.props.EnumProperty(
        name="Rest Start",
        description="Choose whether the template keeps scene time or restarts locally from the property's current value",
        items=_rest_start_mode_items(),
        default="ADDITIVE",
    )
    clamp_additive: bpy.props.BoolProperty(
        name="Clamp",
        description=(
            "Hold the applied result between a minimum and a maximum. Templates "
            "that swing both ways around the resting value can otherwise carry "
            "the target somewhere it should never go - a wobble on a light "
            "strength of 10 dipping below zero. Set Min 0 and it can never go "
            "negative"
        ),
        default=False,
    )
    graph_preview_mode: bpy.props.EnumProperty(
        name="Graph",
        description="What the preview graph draws",
        items=[
            ("LIVE", "Evaluation",
             "Draw what will actually be applied, including Rest Start and any "
             "clamp - it holds before the apply frame, starts from the target's "
             "current value, and stops at the limits"),
            ("RAW", "Source",
             "Draw the template's own expression, ignoring Rest Start and clamp "
             "- the shape as authored"),
        ],
        default="LIVE",
    )
    clamp_min: bpy.props.FloatProperty(
        name="Min",
        description="Lowest value the applied driver may ever output",
        default=0.0,
    )
    clamp_max: bpy.props.FloatProperty(
        name="Max",
        description="Highest value the applied driver may ever output",
        default=1.0,
    )
    visualizer_enabled: bpy.props.BoolProperty(name="Show Graph", description="Sample the expression and draw the compact waveform preview", default=True, update=on_param_update)
    preview_display_settings_open: bpy.props.BoolProperty(
        name="Display Settings",
        description="Show display-only preview choices such as style, scale, and overlay",
        default=False,
    )
    preview_overlay_enabled: bpy.props.BoolProperty(
        name="Overlay",
        description="Overlay compatible sibling channels that share this unit and meaning",
        default=False,
        update=on_param_update,
    )
    visualizer_detailed: bpy.props.BoolProperty(name="Detailed", description="Use a taller graph with min/mid/max guides and cursor stats", default=False, update=on_param_update)
    visualizer_scale: bpy.props.FloatProperty(
        name="Scale",
        description="Enlarge or shrink the graph image in the panel without changing its visible value range",
        default=1.0, min=0.6, max=3.0, soft_min=0.75, soft_max=2.5,
        subtype="FACTOR",
    )
    visualizer_zoom: bpy.props.FloatProperty(
        name="Zoom",
        description=(
            "Zoom the graph horizontally through time and vertically through values. "
            "Values below 1 zoom out; values above 1 zoom in while keeping the "
            "graph's start frame fixed"
        ),
        default=1.0, min=0.05, max=10.0, soft_min=0.1, soft_max=4.0,
        subtype="FACTOR",
    )
    visualizer_zoom_x: bpy.props.BoolProperty(
        name="X",
        description="Let Zoom change the horizontal time window",
        default=True,
        update=on_visualizer_zoom_x_update,
    )
    visualizer_zoom_y: bpy.props.BoolProperty(
        name="Y",
        description="Let Zoom change the vertical value window",
        default=True,
        update=on_visualizer_zoom_y_update,
    )
    visualizer_anchored: bpy.props.BoolProperty(
        name="Fixed Scale",
        description=(
            "Scale the graph against the template's default look instead of auto-fitting "
            "to the curve. With this on, raising a parameter's amplitude visibly grows the "
            "wave and lowering it shrinks it; off, the graph always fills the box so only "
            "the shape is shown"
        ),
        default=False,
        update=on_param_update,
    )
    visualizer_style: bpy.props.EnumProperty(
        name="Style",
        description="How to draw the expression preview",
        items=utils.mark_recommended(_visualizer_style_items(), "LINE"),
        default="LINE",
        update=on_param_update,
    )
    manual_mode: bpy.props.BoolProperty(name="Edit Expression", description="Hand-edit the generated expression instead of building it from parameters", default=False, update=on_manual_mode_update)
    manual_expression: bpy.props.StringProperty(name="Expression (editable)", description="Editable driver expression used while Edit Expression is on", default="", update=on_manual_expression_update)
    recents_mem: bpy.props.StringProperty(name="Recents", default="[]")
    lightweight_preview: bpy.props.BoolProperty(
        name="Lightweight Preview",
        description="Draw the waveform as a text curve instead of a rendered image. Cheapest on slower PCs.",
        default=False,
    )
    copied_driver_expression: bpy.props.StringProperty(name="Copied Driver Expression", default="")
    copied_driver_template: bpy.props.StringProperty(name="Copied Driver Template", default="")
    copied_driver_label: bpy.props.StringProperty(name="Copied Driver Label", default="")
    copied_driver_rest_mode: bpy.props.StringProperty(name="Copied Driver Rest Mode", default="OFF")
    copied_driver_output_baseline: bpy.props.FloatProperty(default=0.0, options={"HIDDEN"})
    copied_driver_parameters: bpy.props.StringProperty(default="{}", options={"HIDDEN"})
    copied_driver_scope: bpy.props.StringProperty(default="SINGLE", options={"HIDDEN"})
    copied_driver_channel_plan: bpy.props.StringProperty(default="[]", options={"HIDDEN"})
    espresso_input_source: bpy.props.StringProperty(name="Espresso Input Source", default="{}")
    live_controllers: bpy.props.StringProperty(name="Espresso Controllers", default="[]", options={"HIDDEN"})
    live_control_bindings: bpy.props.StringProperty(name="Espresso Controller Bindings", default="{}", options={"HIDDEN"})
    live_controller_uid: bpy.props.EnumProperty(
        name="Controller",
        description="Named Espresso Controller used for this driver or motion scope",
        items=_live_controller_items,
    )
    live_reset_mode: bpy.props.EnumProperty(
        name="Reset Mode",
        description="Value used when the controller disables or reduces the driver",
        items=utils.mark_recommended([
            ("CAPTURED", "Captured Rest", "Use the property's value when the controller is first attached"),
            ("BASELINE", "Template Baseline", "Use the template's audited semantic baseline"),
            ("CUSTOM", "Custom Value", "Use a value entered below"),
        ], "CAPTURED"),
        default="CAPTURED",
    )
    live_custom_value: bpy.props.FloatProperty(
        name="Custom Reset Value",
        description="Value produced when the controller is fully disabled",
        default=0.0,
    )
    live_control_scope: bpy.props.EnumProperty(
        name="Scope",
        description=(
            "Drivers that receive this controller. A switch control reads 0 as the "
            "rest value and 1 as the template animation; a blend control reads 0 as "
            "rest, 0.5 as a blend of the two, and 1 as full animation"
        ),
        items=[
            ("ACTIVE", "Active Driver", "Only the active driver target"),
            ("RELATED", "Related Pair", "The active driver and the current template's declared paired variant"),
            ("MOTION", "Entire Motion Set", "Every channel in the most recently applied Espresso Motion Set"),
            ("SCENE", "Scene Motion", "A motion you choose from anywhere in the scene, whatever is active"),
        ],
        default="ACTIVE",
    )
    # The record token of the motion the Scene Motion scope points at. A token
    # rather than an object name: it survives renames, and it names ONE
    # application of a recipe when the same recipe is applied more than once
    # to one host.
    live_control_scene_token: bpy.props.StringProperty(
        name="Scene Motion", default="", options={"HIDDEN"},
    )
    apply_target_memory: bpy.props.StringProperty(name="Apply Target Memory", default="{}")
    last_apply_status: bpy.props.StringProperty(
        name="Last Apply Status",
        default="No remembered target yet.",
        update=on_last_apply_status_update,
    )
    last_apply_status_dismissed: bpy.props.BoolProperty(
        name="Last Apply Status Dismissed",
        default=False,
        options={"SKIP_SAVE"},
    )

    # Which property "apply to my selection" writes to. Stores the route to it (owner
    # kind, data path, index), never a pointer to one object's property, because the
    # same route resolved against each selected object is what gives every object its
    # own driver.
    espresso_apply_target: bpy.props.StringProperty(
        name="Espresso Apply Target", default="")

    variant_mem: bpy.props.StringProperty(name="Variant Memory", default="{}")
    template_mem: bpy.props.StringProperty(name="Template Memory", default="{}")
    switch_from_template_id: bpy.props.StringProperty(
        name="Switching From",
        description="Internal: the template being left, held across one switch",
        default="",
    )
    last_template_id: bpy.props.StringProperty(name="Last Template", default="")
    pending_template_id: bpy.props.StringProperty(name="Pending Template", default="", options={"HIDDEN"})
    suspend_param_updates: bpy.props.BoolProperty(name="Suspend Parameter Updates", default=False, options={"HIDDEN"})
    # Re-entrancy guard for the category/search callbacks, which assign into each
    # other's properties while resolving the template enum.
    suspend_nav_updates: bpy.props.BoolProperty(name="Suspend Navigation Updates", default=False, options={"HIDDEN"})
    active_target_owner: bpy.props.StringProperty(
        name="Active Target Owner",
        description="Object name of the explicitly picked driver target (empty = no explicit pick)",
        default="",
        options={"HIDDEN"},
    )
    active_target_id_type: bpy.props.StringProperty(default="", options={"HIDDEN"})
    active_target_id_name: bpy.props.StringProperty(default="", options={"HIDDEN"})
    active_target_owner_path: bpy.props.StringProperty(default="", options={"HIDDEN"})
    active_target_data_path: bpy.props.StringProperty(
        name="Active Target Data Path",
        description="Data path of the explicitly picked driver target",
        default="",
        options={"HIDDEN"},
    )
    active_target_index: bpy.props.IntProperty(
        name="Active Target Array Index",
        description="Array index of the explicitly picked driver target (-1 = scalar/no array index)",
        default=-1,
        options={"HIDDEN"},
    )
    driver_target_source: bpy.props.EnumProperty(
        name="Effect Scope",
        description="Whether Applied Effects shows the active scope, last applied effect, or scene-wide applied effects",
        items=[
            ("ACTIVE", "Active", "Applied drivers, motion sets, and generated setups related to the active object and selected nodes"),
            ("LAST", "Last", "The channels of your most recent remembered apply"),
            ("SCENE", "Scene", "All stamped Espresso motions and healthy generated setups in the current scene"),
        ],
        default="ACTIVE",
        update=on_driver_target_source_update,
    )
    last_applied_effect_token: bpy.props.StringProperty(
        name="Last Applied Effect Token",
        description="Stable pointer used by the Last Applied Effect scope",
        default="",
        options={"HIDDEN"},
    )
    driver_target_items: bpy.props.CollectionProperty(
        name="Driver Target Items",
        description="Cached rows for the Driver Target UIList — repopulated from the active object's actual drivers whenever they change",
        type=ESPRESSO_DriverTargetItem,
    )
    driver_target_active_index: bpy.props.IntProperty(
        name="Driver Target Active Row",
        description="Index of the selected row in the Driver Target list",
        default=-1,
        update=on_driver_target_active_index_update,
    )
    driver_target_group_state: bpy.props.StringProperty(default="{}", options={"HIDDEN"})
    driver_target_selection_state: bpy.props.StringProperty(default="{}", options={"HIDDEN"})
    driver_target_items_source: bpy.props.StringProperty(default="", options={"HIDDEN"})
    driver_target_items_signature: bpy.props.StringProperty(default="", options={"HIDDEN"})
    preview_view_mode: bpy.props.EnumProperty(
        name="Preview View",
        description="What the Preview graph samples",
        items=[
            ("TEMPLATE", "Template", "Graph the expression you are currently editing"),
            ("ACTIVE_TARGET", "Active Target", "Graph the real, already-applied expression on the current active driver target"),
        ],
        default="TEMPLATE",
    )
    advanced_prop_adv_delay: _advanced_control_property("ADV_DELAY")
    advanced_prop_adv_advance: _advanced_control_property("ADV_ADVANCE")
    advanced_prop_adv_start_frame: _advanced_control_property("ADV_START_FRAME")
    advanced_prop_adv_end_frame: _advanced_control_property("ADV_END_FRAME")
    advanced_prop_adv_loop_fit: _advanced_control_property("ADV_LOOP_FIT")
    advanced_prop_adv_mult: _advanced_control_property("ADV_MULT")
    advanced_prop_adv_offset: _advanced_control_property("ADV_OFFSET")
    token_prop_speed: _token_proxy_property("SPEED")
    token_prop_n: _token_proxy_property("N")
    token_prop_min: _token_proxy_property("MIN")
    token_prop_max: _token_proxy_property("MAX")
    token_prop_phase: _token_proxy_property("PHASE")
    token_prop_a: _token_proxy_property("A")
    token_prop_b: _token_proxy_property("B")
    token_prop_range: _token_proxy_property("RANGE")
    token_prop_start_frame: _token_proxy_property("START_FRAME")
    token_prop_period: _token_proxy_property("PERIOD")
    token_prop_duration: _token_proxy_property("DURATION")
    token_prop_ramp_frames: _token_proxy_property("RAMP_FRAMES")
    token_prop_duty: _token_proxy_property("DUTY")


def _assert_slots_cover_catalogue():
    """Fail at import if any template has more parameters than there are slots.

    Parameter values live in numbered slots (param_float_0 ... param_bool_N).
    A template whose Nth parameter has no slot cannot be drawn at all: the
    panel raises mid-draw, and because Blender swallows exceptions inside
    draw(), the visible symptom is the PARAMETERS section silently ending
    early - with nothing in the console explaining why.

    That is far too quiet a failure for something a new parameter can trigger,
    so it is checked once, loudly, at import.
    """
    over = [
        (item["id"], len([p for p in item.get("params", []) if not p.get("advanced")]))
        for item in templates.TEMPLATES
        if len([p for p in item.get("params", []) if not p.get("advanced")]) > PARAM_SLOT_COUNT
    ]
    if over:
        detail = ", ".join(f"{tid} has {count}" for tid, count in over)
        raise ValueError(
            f"Templates exceed the {PARAM_SLOT_COUNT} available parameter slots "
            f"({detail}). Raise PARAM_SLOT_COUNT in props.py and add the matching "
            "param_float_/param_int_/param_bool_ declarations."
        )


_assert_slots_cover_catalogue()


CLASSES = (
    ESPRESSO_DriverTargetItem,
    ESPRESSO_DriverEditParamItem,
    ESPRESSO_Props,
)


def sync_driver_target_items(props, targets, effects=None, source=None):
    """Repopulate props.driver_target_items from the given fcurves, but only when the
    set of drivers has changed: rebuilding a collection of hundreds of items on every
    panel redraw (rigs with 700+ drivers exist) would be needless overhead.
    """
    from ...apply import driver_manager
    active_source = source or getattr(props, "driver_target_source", "ACTIVE")
    if getattr(props, "driver_target_items_source", "") == active_source:
        _store_selection_state_for_scope(props, active_source, {
            item.descriptor_key for item in props.driver_target_items
            if item.batch_eligible and item.batch_selected
        })
    selected = _selection_state_for_scope(props, active_source)
    normalized = []
    for target in targets:
        if isinstance(target, dict):
            normalized.append(target)
        else:
            normalized.append({
                "id_type": "Object", "id_name": "", "owner_path": "",
                "data_path": target.data_path, "index": target.array_index,
                "category": "Object Properties",
                "label": f"Object Properties  |  {target.data_path}", "icon": "DRIVER",
            })
    group_state = read_driver_target_group_state(props, source=active_source)
    normalized = driver_manager.rows_for_targets(
        normalized, group_state=group_state, source=active_source,
    )
    signature = "%s\x1f%s" % (active_source, driver_manager.rows_signature(normalized))
    if (
        props.driver_target_items_signature == signature
        and getattr(props, "driver_target_items_source", "") == active_source
    ):
        return
    props.driver_target_items.clear()
    for target in normalized:
        item = props.driver_target_items.add()
        item.data_path = target["data_path"]
        item.array_index = int(target.get("index", -1))
        item.label = target.get("label", target["data_path"])
        item.category = target.get("category", "Other")
        item.icon = target.get("icon", "DRIVER")
        item.owner_id_type = target.get("id_type", "")
        item.owner_id_name = target.get("id_name", "")
        item.owner_path = target.get("owner_path", "")
        item.row_kind = target.get("row_kind", "TARGET")
        item.group_key = target.get("group_key", "")
        item.descriptor_key = target.get("descriptor_key", "")
        item.effect_code = target.get("effect_code", "")
        item.template_id = target.get("template_id", "")
        item.editable = bool(target.get("editable", False))
        item.effect_kind = target.get("effect_kind", "SINGLE_PROPERTY")
        item.record_token = target.get("record_token", "")
        item.batch_eligible = bool(target.get("batch_eligible", True))
        item.bakeable = bool(target.get("bakeable", False))
        item.removable = bool(target.get("removable", False))
        item.visible = bool(target.get("visible", True))
        item.badge_text = str(target.get("badge_text", "") or "")
        item.member_count = int(target.get("member_count", 0) or 0)
        item.nest_depth = int(target.get("nest_depth", 0) or 0)
        item.batch_selected = item.batch_eligible and item.descriptor_key in selected
    props.driver_target_items_signature = signature
    props.driver_target_items_source = active_source


def _prune_orphaned_template_ids(props):
    """A removed or renamed template (an old id folded into heartbeat, say) would
    otherwise leave its id in saved recents and per-template memory indefinitely.
    """
    known_ids = set(templates.TEMPLATE_BY_ID)

    recents = get_recents(props)
    pruned_recents = [tid for tid in recents if tid in known_ids]
    if pruned_recents != recents:
        props.recents_mem = _write_id_list(pruned_recents)

    for attr, reader, writer in (
        ("template_mem", utils.read_template_memory, utils.write_template_memory),
        ("variant_mem", utils.read_variant_memory, utils.write_variant_memory),
    ):
        memory = reader(props)
        pruned = {tid: values for tid, values in memory.items() if tid in known_ids}
        if pruned != memory:
            writer(props, pruned)


@bpy.app.handlers.persistent
def _refresh_all_previews_on_load(_dummy):
    # Sample cache keys include the frame range, so opening a file with a
    # different range orphans every existing entry. Clear on load.
    from ..views import visualizer
    visualizer.clear_cache()
    """props.preview is a plain StringProperty, so once written it's saved
    verbatim into the .blend file. Nothing recomputes it on file load —
    Blender doesn't fire property `update=` callbacks for values merely being
    deserialized, only on an actual change. So a scene saved with one version
    of a template's expression keeps showing that EXACT cached string after
    the addon is updated (or, in dev, hot-reloaded) and the file is reopened,
    until the user happens to touch a parameter. Force every scene's cached
    preview to match whatever the currently-loaded addon code actually
    produces, every time a file loads. Also prunes any saved recent/
    remembered-value reference to a template id that no longer exists."""
    context = bpy.context
    for scene in bpy.data.scenes:
        props = getattr(scene, "espresso_props", None)
        if props is None:
            continue
        try:
            _prune_orphaned_template_ids(props)
        except Exception:
            pass
        # Empty last_template_id means a fresh scene; that case is handled by
        # panels._ensure_template_initialized, which defers the write through a timer
        # because draw() cannot write props.
        if not props.last_template_id:
            continue
        try:
            with context.temp_override(scene=scene):
                refresh_preview(props, context)
        except Exception:
            pass
    _refresh_slot_tooltips_for_active_scene()


def _refresh_slot_tooltips_for_active_scene():
    """Re-describe the numbered param slots for whatever template is selected.

    _refresh_slot_tooltips() runs on load and registration as well as on a
    template change, so inputs do not keep the generic "hover the parameter
    label" tooltip after a fresh start, a file load or an add-on reload. The
    slot descriptions were correct code that simply never ran for the template
    already selected.

    Cheap enough to call unconditionally: it only redefines the handful of slots
    the current template uses.
    """
    try:
        scene = getattr(bpy.context, "scene", None)
        props = getattr(scene, "espresso_props", None)
        if props is not None:
            _refresh_slot_tooltips(get_current_template(props))
    except Exception:
        # Never let a tooltip refresh stop the add-on registering or a file
        # opening; the worst case is the pre-existing generic text.
        pass


def _refresh_slot_tooltips_deferred():
    _refresh_slot_tooltips_for_active_scene()
    # Rebuild every cached preview too, not only the tooltips. load_post covers opening
    # a file, but not the add-on being updated or reloaded while a file stays open,
    # where a cached string would outlive the code that produced it. A template whose
    # expression changed would then apply with variables the binder no longer creates,
    # and Blender would report "Unknown name(s)" naming variables that exist nowhere.
    try:
        _refresh_all_previews_on_load(None)
    except Exception:
        # Never let this stop the add-on registering; the worst case is the
        # pre-existing stale string, which the next parameter touch fixes.
        pass
    return None


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.espresso_props = bpy.props.PointerProperty(type=ESPRESSO_Props)
    bpy.types.WindowManager.espresso_applied_dialog_parameters = bpy.props.CollectionProperty(
        type=ESPRESSO_DriverEditParamItem,
    )
    bpy.types.WindowManager.espresso_applied_dialog_template_id = bpy.props.StringProperty(
        default="",
    )
    if _refresh_all_previews_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_refresh_all_previews_on_load)
    # Deferred, not inline: the add-on installer runs register() against a
    # restricted context where reading scene data raises. A zero-interval timer
    # runs on the next tick, once bpy.data is addressable again. This covers
    # enabling the add-on and hot-reloading it; load_post covers opening files.
    bpy.app.timers.register(_refresh_slot_tooltips_deferred, first_interval=0.0)


def unregister():
    if _refresh_all_previews_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_refresh_all_previews_on_load)
    if hasattr(bpy.types.WindowManager, "espresso_applied_dialog_parameters"):
        del bpy.types.WindowManager.espresso_applied_dialog_parameters
    if hasattr(bpy.types.WindowManager, "espresso_applied_dialog_template_id"):
        del bpy.types.WindowManager.espresso_applied_dialog_template_id
    if hasattr(bpy.types.Scene, "espresso_props"):
        del bpy.types.Scene.espresso_props
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
