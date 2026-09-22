"""Broad user-facing catalogue shelves for crowded categories.

These groups deliberately remain separate from the detailed taxonomy. Taxonomy
describes expression families and product contracts; browse groups reduce the
number of choices an artist scans at once.
"""

from __future__ import annotations


ALL_GROUP = "__ALL__"


_GROUPS = {'Swing & Oscillate': ({'id': 'sine_pendulum',
                        'label': 'Sine & Pendulum',
                        'description': 'Smooth sine and cosine cycles, '
                                       'offsets, and ping-pong motion.',
                        'template_ids': ('sine_osc',)},
                       {'id': 'waveforms',
                        'label': 'Waveforms',
                        'description': 'Sawtooth, reverse, triangle, and '
                                       'square wave shapes.',
                        'template_ids': ('sawtooth', 'triangle_wave')}),
 'Light & Flicker': ({'id': 'blink_strobe',
                      'label': 'Blink & Strobe',
                      'description': 'Simple, double, triple, and rapid '
                                     'strobe flashes.',
                      'template_ids': ('simple_blink',)},
                     {'id': 'flicker_electrical',
                      'label': 'Flicker & Electrical',
                      'description': 'Candle, neon, lightning, and screen '
                                     'illumination.',
                      'template_ids': ('candle_flicker',)}),
 'Trigger & State': ({'id': 'one_shot',
                      'label': 'One-shot',
                      'description': 'Binary Trigger, Binary Window, Smooth '
                                     'Fade In, and 2 more.',
                      'template_ids': ('fade_in', 'fade_out')},
                     {'id': 'delay_repeat',
                      'label': 'Delay & Repeat',
                      'description': 'Delayed Start, Repeating Pulse.',
                      'template_ids': ('pulse_repeat',)}),
 'RGB & Neon Lighting': ({'id': 'colour_cycling',
                          'label': 'Colour Cycling',
                          'description': 'Colour Cycling templates.',
                          'template_ids': ('rgb_colour_cycle',)},
                         {'id': 'sequenced_chase',
                          'label': 'Sequenced & Chase',
                          'description': 'Sequenced & Chase templates.',
                          'template_ids': ('rgb_chase',)},
                         {'id': 'sparkle_flicker',
                          'label': 'Sparkle & Flicker',
                          'description': 'Sparkle & Flicker templates.',
                          'template_ids': ('rgb_twinkle',)})}


def configured_categories():
    """Return categories that expose curated browse groups."""
    return tuple(_GROUPS)


def groups_for_category(category):
    """Return defensive copies of the named groups for ``category``."""
    return [
        {
            **group,
            "template_ids": tuple(group["template_ids"]),
        }
        for group in _GROUPS.get(category, ())
    ]


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


def validate_catalogue(catalogue):
    """Reject missing, duplicated, or stale curated memberships."""
    by_category = {}
    all_ids = {item.get("id") for item in catalogue}
    for item in catalogue:
        by_category.setdefault(item.get("category"), []).append(item.get("id"))

    errors = []
    for category, groups in _GROUPS.items():
        if not 2 <= len(groups) <= 4:
            errors.append(f"{category}: expected 2-4 groups, found {len(groups)}")
        grouped = [
            template_id
            for group in groups
            for template_id in group["template_ids"]
        ]
        duplicates = sorted({
            template_id
            for template_id in grouped
            if grouped.count(template_id) > 1
        })
        if duplicates:
            errors.append(f"{category}: duplicate ids {', '.join(duplicates)}")
        unknown = sorted(set(grouped) - all_ids)
        if unknown:
            errors.append(f"{category}: unknown ids {', '.join(unknown)}")
        expected = set(by_category.get(category, ()))
        missing = sorted(expected - set(grouped))
        extras = sorted(set(grouped) - expected)
        if missing:
            errors.append(f"{category}: missing ids {', '.join(missing)}")
        if extras:
            errors.append(f"{category}: foreign ids {', '.join(extras)}")

    if errors:
        raise ValueError("Invalid curated browse groups:\n" + "\n".join(errors))
