"""Broad user-facing catalogue shelves for crowded categories.

These groups deliberately remain separate from the detailed taxonomy. Taxonomy
describes expression families and product contracts; browse groups reduce the
number of choices an artist scans at once.
"""

from __future__ import annotations


ALL_GROUP = "__ALL__"


_GROUPS = {'Swing & Oscillate': ({'id': 'sine_pendulum',
                        'label': 'Sine & Pendulum',
                        'description': 'Smooth back-and-forth oscillation.',
                        'template_ids': ('sine_osc',)},
                       {'id': 'waveforms',
                        'label': 'Waveforms',
                        'description': 'Sawtooth and triangle wave shapes.',
                        'template_ids': ('sawtooth', 'triangle_wave')}),
 'Light & Flicker': ({'id': 'blink_strobe',
                      'label': 'Blink & Strobe',
                      'description': 'Simple on/off blinking.',
                      'template_ids': ('simple_blink',)},
                     {'id': 'flicker_electrical',
                      'label': 'Flicker & Electrical',
                      'description': 'Candle-style flicker.',
                      'template_ids': ('candle_flicker',)}),
 'Trigger & State': ({'id': 'one_shot',
                      'label': 'One-shot',
                      'description': 'Linear Fade In and Smooth Fade Out.',
                      'template_ids': ('fade_in', 'fade_out')},
                     {'id': 'delay_repeat',
                      'label': 'Delay & Repeat',
                      'description': 'Repeating Pulse.',
                      'template_ids': ('pulse_repeat',)}),
 'RGB & Neon Lighting': ({'id': 'colour_cycling',
                          'label': 'Colour Cycling',
                          'description': 'Colours that cycle over time.',
                          'template_ids': ('rgb_colour_cycle',)},
                         {'id': 'sequenced_chase',
                          'label': 'Sequenced & Chase',
                          'description': 'Marquee-style chasing colours.',
                          'template_ids': ('rgb_chase',)},
                         {'id': 'sparkle_flicker',
                          'label': 'Sparkle & Flicker',
                          'description': 'Twinkling colours.',
                          'template_ids': ('rgb_twinkle',)})}


def _visible_groups(category, available_ids=None):
    groups = _GROUPS.get(category, ())
    if available_ids is None:
        return groups
    available = set(available_ids)
    return tuple(
        group for group in groups
        if available.intersection(group["template_ids"])
    )


def has_groups(category, available_ids=None):
    """Return whether ``category`` benefits from a browse-group filter."""
    return len(_visible_groups(category, available_ids)) >= 2


def enum_items_for_category(category, available_ids=None):
    """Build stable Blender EnumProperty items for a category."""
    items = [
        (
            ALL_GROUP,
            "All Templates",
            "Show every template in this category.",
            "COLLECTION_NEW",
            0,
        ),
    ]
    items.extend(
        (
            group["id"],
            group["label"],
            group["description"],
            "OUTLINER_OB_GROUP_INSTANCE",
            index,
        )
        for index, group in enumerate(
            _visible_groups(category, available_ids),
            start=1,
        )
    )
    return items


def group_for_template(category, template_id):
    """Return the named group containing a template, or All Templates."""
    for group in _GROUPS.get(category, ()):
        if template_id in group["template_ids"]:
            return group["id"]
    return ALL_GROUP


def label_for_template(category, template_id):
    """Return the artist-facing shelf label for a template."""
    group_id = group_for_template(category, template_id)
    for group in _GROUPS.get(category, ()):
        if group["id"] == group_id:
            return group["label"]
    return ""


def templates_for_group(catalogue, category, group_id):
    """Filter a catalogue while preserving its authored template order."""
    category_items = [
        item for item in catalogue
        if item.get("category") == category
    ]
    if not has_groups(
        category,
        {item.get("id") for item in category_items},
    ) or group_id == ALL_GROUP:
        return category_items
    group = next(
        (
            item for item in _GROUPS[category]
            if item["id"] == group_id
        ),
        None,
    )
    if group is None:
        return category_items
    allowed = set(group["template_ids"])
    return [item for item in category_items if item.get("id") in allowed]

