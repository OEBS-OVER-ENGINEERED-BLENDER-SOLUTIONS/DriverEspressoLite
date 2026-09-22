"""Neutral spatial-material queries for direct-driver motion plans."""

TEMPLATE_KIND = {}
_BUILDERS = {}

def kind_for(_template):
    return None

def remove_group_for_driver_path(_tree, _data_path):
    return False

def remove_group_feeding_target_path(_tree, _data_path):
    return False

def write_live_values(_entry, _template, _values, scene=None):
    return False, "No shared-material parameter adapter is available."
