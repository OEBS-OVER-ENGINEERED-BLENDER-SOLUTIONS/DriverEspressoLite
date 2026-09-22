"""Neutral layout queries for driver-only application routes."""

LAYOUT_ID_TAG = "__espresso_layout_id"
LAYOUT_KIND_TAG = "__espresso_layout_kind"
EFFECT_ID = ""

def resolve_carrier(_obj=None):
    return None

def is_carrier(_obj=None):
    return False

def carriers_for_source(_obj=None):
    return ()

def layout_modifier(_carrier=None):
    return None

def layout_label(_carrier=None):
    return ""

def parameter_template(_carrier=None):
    return None

def input_identifier(_carrier=None, _name=None):
    return None

def source_weights(_carrier=None):
    return ()

def read_parameter_values(_carrier=None):
    return {}

def can_prepare_layout(_context=None):
    return False

def clear(_host=None):
    return 0

def write_parameter_values(_carrier=None, _values=None):
    raise RuntimeError("No layout parameter adapter is available.")

def bake(_context=None, _host=None):
    raise RuntimeError("No layout bake adapter is available.")
