"""Lightweight CPU expression preview for Driver Espresso.

Samples the generated expression string in a restricted namespace over a frame
range and renders it as text in several chart styles (line / bars / filled /
mirror / stats). The module is Blender-free apart from nothing — it only imports
``utils`` (also Blender-free) so every renderer can be unit-tested outside
Blender. An optional RGBA pixel buffer is produced for the image-graph mode; the
Blender-side preview wiring lives in ``image_preview.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math

from ...engine import utils
from ...catalogue.core import channel_identity


# Chart styles exposed to the UI. (id, label, description)
STYLE_ITEMS = (
    ("LINE", "Line", "Line graph of the curve"),
    ("BARS", "Bars", "Vertical bars sampled across the frame range"),
    ("FILLED", "Filled", "Filled area chart under the curve"),
    ("MIRROR", "Mirror", "Bipolar chart mirrored around the zero/center line"),
    ("STATS", "Stats", "Numeric analysis: range, cycles, shape, cursor value"),
)
VALID_STYLES = {item[0] for item in STYLE_ITEMS}


@dataclasses.dataclass(frozen=True)
class PreviewResult:
    valid: bool
    message: str
    points: tuple = ()
    frames: tuple = ()
    minimum: float = 0.0
    maximum: float = 0.0
    graph: str = ""
    cache_key: str = ""
    detail_text: str = ""
    style: str = "LINE"
    stats: dict = dataclasses.field(default_factory=dict)


_CACHE = {}


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #
def _make_cache_key(
    expression, template, frame_start, frame_end, sample_count,
    helper_expressions=None, series_visibility=None,
):
    raw = "|".join(
        [
            template.get("id", ""),
            expression,
            format(float(frame_start), ".9g"),
            format(float(frame_end), ".9g"),
            str(int(sample_count)),
            json.dumps(helper_expressions or [], sort_keys=True),
            json.dumps(series_visibility or {}, sort_keys=True),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# Cap on how many frames we evaluate. For typical timelines this means one
# sample per frame; very long ranges fall back to this many evenly spaced
# samples (still far denser than the display, so the envelope stays accurate).
_MAX_SAMPLES = 1500


def auto_sample_count(frame_start, frame_end):
    """Pick a sampling density automatically: one sample per frame, capped.

    Sampling per frame (rather than a user-chosen count) is what makes the
    preview stable: every frame is evaluated, so no pulse can fall between
    samples and flicker in and out as a count changes.
    """
    span = int(round(abs(frame_end - frame_start))) + 1
    return max(2, min(_MAX_SAMPLES, span))


def _sample_frames(frame_start, frame_end, sample_count):
    sample_count = max(2, int(sample_count))
    if frame_end == frame_start:
        return [float(frame_start)]
    step = (frame_end - frame_start) / (sample_count - 1)
    return [frame_start + step * index for index in range(sample_count)]


_ENVELOPE_SUPERSAMPLE = 2


def _column_envelopes(values, width, supersample=_ENVELOPE_SUPERSAMPLE):
    """Aggregate samples into ``width`` columns as (low, high) envelopes.

    Each column reports the min and max of every sample that falls inside it,
    so a feature narrower than a column (a 1-frame pulse) still registers
    instead of being missed by nearest-neighbour picking. Bounded supersampling
    refines column edges, then downsamples with min/max so extrema survive.
    """
    count = len(values)
    if count == 0:
        return []
    factor = max(1, min(4, int(supersample)))
    fine_width = max(int(width), int(width) * factor)

    def _raw(span):
        if count >= span:
            columns = []
            for index in range(span):
                start = index * count // span
                end = max(start + 1, (index + 1) * count // span)
                chunk = values[start:end]
                columns.append((min(chunk), max(chunk)))
            return columns
        last = count - 1
        columns = []
        for index in range(span):
            source = round(index * last / (span - 1)) if span > 1 else 0
            value = values[min(last, source)]
            columns.append((value, value))
        return columns

    fine = _raw(fine_width)
    if factor == 1 or fine_width == width:
        return fine
    columns = []
    for index in range(width):
        chunk = fine[index * factor:(index + 1) * factor]
        columns.append((min(item[0] for item in chunk), max(item[1] for item in chunk)))
    return columns


def _format_value(value):
    # Snap values that are effectively zero so symmetric curves read "0" instead
    # of a noisy "4.1e-05" in guide labels and stats.
    if abs(value) < 1e-4:
        return "0"
    return f"{value:.4g}"


def _cursor_index(frames, frame_current):
    if frame_current is None or not frames:
        return None
    return min(range(len(frames)), key=lambda item: abs(frames[item] - frame_current))


def _cursor_column(width, frame_start, frame_end, frame_current):
    if frame_current is None or frame_end <= frame_start:
        return None
    ratio = (frame_current - frame_start) / (frame_end - frame_start)
    ratio = max(0.0, min(1.0, ratio))
    return int(round(ratio * (width - 1)))


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyze(values):
    """Derive descriptive stats used by the Stats style and the detail line."""
    if not values:
        return {}
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum
    mean = sum(values) / len(values)
    midpoint = (minimum + maximum) / 2

    # Count mean-crossings to estimate cycles, and direction changes for shape.
    crossings = 0
    direction_changes = 0
    prev_above = None
    prev_delta = 0.0
    monotonic_up = True
    monotonic_down = True
    for index in range(len(values)):
        above = values[index] >= midpoint
        if prev_above is not None and above != prev_above:
            crossings += 1
        prev_above = above
        if index > 0:
            delta = values[index] - values[index - 1]
            if delta > 1e-9 and prev_delta < -1e-9:
                direction_changes += 1
            if delta < -1e-9 and prev_delta > 1e-9:
                direction_changes += 1
            if delta < -1e-9:
                monotonic_up = False
            if delta > 1e-9:
                monotonic_down = False
            if abs(delta) > 1e-9:
                prev_delta = delta

    cycles = crossings / 2.0

    if span <= 1e-9:
        shape = "constant"
    elif monotonic_up:
        shape = "rising"
    elif monotonic_down:
        shape = "falling"
    elif direction_changes >= max(4, len(values) // 8):
        shape = "noisy"
    elif cycles >= 1.5:
        shape = "oscillating"
    else:
        shape = "smooth"

    return {
        "min": minimum,
        "max": maximum,
        "span": span,
        "mean": mean,
        "midpoint": midpoint,
        "cycles": cycles,
        "direction_changes": direction_changes,
        "shape": shape,
        "bipolar": minimum < -1e-9 < maximum,
    }


# --------------------------------------------------------------------------- #
# Text renderers
# --------------------------------------------------------------------------- #
# Braille dot bit per (x in 0..1, y in 0..3) within a 2x4 cell.
_BRAILLE_DOTS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
_BLOCKS = (" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")


_BRAILLE_BLANK = "⠀"   # blank braille cell: same advance width as filled cells
_BRAILLE_CURSOR = "⡇"  # left-column vertical line (dots 1,2,3,7)


def _norm(value, minimum, span):
    return 0.5 if span <= 1e-12 else (value - minimum) / span


def zoomed_value_range(values, scale=None, zoom=1.0):
    """Return the vertical value range shown by the graph viewport.

    ``scale`` is the optional Fixed Scale reference; without one, the normal
    auto-fit range (sample extrema plus zero) is used. Zoom 1 preserves that
    range, values below 1 expand it, and values above 1 contract it. The range
    expands around zero when visible, or around the nearest bound otherwise,
    so positive-only effects keep their meaningful floor while zooming out.
    Sampled values are never modified by this operation.
    """
    if scale is None:
        if not values:
            return (-1.0, 1.0)
        minimum = min(0.0, float(min(values)))
        maximum = max(0.0, float(max(values)))
        if maximum - minimum <= 1e-12:
            minimum, maximum = -1.0, 1.0
    else:
        minimum, maximum = float(scale[0]), float(scale[1])

    try:
        factor = float(zoom)
    except (TypeError, ValueError):
        factor = 1.0
    if not math.isfinite(factor) or factor <= 0.0:
        factor = 1.0
    if factor == 1.0:
        return (minimum, maximum)

    if minimum <= 0.0 <= maximum:
        anchor = 0.0
    elif minimum > 0.0:
        anchor = minimum
    else:
        anchor = maximum
    return (
        anchor + (minimum - anchor) / factor,
        anchor + (maximum - anchor) / factor,
    )


def preview_axis_zoom_factors(zoom, x_enabled=True, y_enabled=True):
    """Route one Zoom value to the enabled graph axes.

    A disabled axis receives the neutral factor 1.0. The property callbacks
    prevent both axes being disabled in Blender, while this pure helper remains
    deterministic for tests and non-RNA callers.
    """
    try:
        factor = float(zoom)
    except (TypeError, ValueError):
        factor = 1.0
    if not math.isfinite(factor) or factor <= 0.0:
        factor = 1.0
    return (
        factor if x_enabled else 1.0,
        factor if y_enabled else 1.0,
    )


def zoomed_frame_range(frame_start, frame_end, frame_current, zoom=1.0):
    """Return the horizontal time window shown by the graph viewport.

    Zoom 1 preserves the authored scene range. Zooming in contracts that range
    and zooming out expands it from the fixed scene-start origin. The playhead
    can move inside the viewport without panning it. The expression and scene
    range themselves are not modified.
    """
    start, end = float(frame_start), float(frame_end)
    span = end - start
    if abs(span) <= 1e-12:
        return (start, end)
    try:
        factor = float(zoom)
    except (TypeError, ValueError):
        factor = 1.0
    if not math.isfinite(factor) or factor <= 0.0 or factor == 1.0:
        return (start, end)

    # Kept in the public helper signature because both preview routes pass the
    # current frame, but it must not move the graph's fixed X origin.
    _ = frame_current
    return (start, start + span / factor)


def _pack_braille(grid, width, height, cursor_col):
    """Pack a (height*4) x (width*2) on/off grid into braille rows.

    Empty cells use the braille blank (U+2800), not a space, so every glyph has
    the same advance width and the graph stays aligned in Blender's proportional
    panel font. This is what makes the lightweight text graph readable.
    """
    rows = []
    for cell_row in range(height):
        chars = []
        for cell_col in range(width):
            bits = 0
            for dy in range(4):
                row = grid[cell_row * 4 + dy]
                for dx in range(2):
                    if row[cell_col * 2 + dx]:
                        bits |= _BRAILLE_DOTS[dy][dx]
            if bits:
                chars.append(chr(0x2800 + bits))
            elif cell_col == cursor_col:
                chars.append(_BRAILLE_CURSOR)
            else:
                chars.append(_BRAILLE_BLANK)
        rows.append("".join(chars))
    return "\n".join(rows)


def _text_grid(values, style, px_w, px_h, scale=None):
    """Build the on/off sub-pixel grid for a text chart style.

    ``scale`` optionally fixes the vertical (min, max) instead of auto-fitting
    to the values' own range. Values outside it clip to the box edge, so raising
    a template's amplitude above the reference visibly grows the wave rather than
    the box rescaling to hide the change.
    """
    if scale is not None:
        minimum, maximum = float(scale[0]), float(scale[1])
        values = [min(maximum, max(minimum, v)) for v in values]
        columns = _column_envelopes(values, px_w)
    else:
        columns = _column_envelopes(values, px_w)
        minimum = min(values)
        maximum = max(values)
    span = maximum - minimum

    def y_of(value):  # 0 = top row
        return int(round((1.0 - _norm(value, minimum, span)) * (px_h - 1)))

    grid = [[0] * px_w for _ in range(px_h)]

    if style == "MIRROR":
        center = 0.0 if minimum < 0 < maximum else (minimum + maximum) / 2
        center_y = y_of(center)
        for x, (low, high) in enumerate(columns):
            top = min(y_of(high), center_y)
            bottom = max(y_of(low), center_y)
            for y in range(top, bottom + 1):
                grid[y][x] = 1
    elif style == "BARS":
        # Aggregate into discrete bars (each bar = the max over its frame span,
        # so a pulse anywhere in the bar still raises it) with gaps between them.
        body, gap = 2, 2
        period = body + gap
        bar_count = max(1, px_w // period)
        for index, (_low, high) in enumerate(_column_envelopes(values, bar_count)):
            top = y_of(high)
            x0 = index * period
            for x in range(x0, min(px_w, x0 + body)):
                for y in range(top, px_h):
                    grid[y][x] = 1
    elif style == "FILLED":
        for x, (_low, high) in enumerate(columns):
            for y in range(y_of(high), px_h):
                grid[y][x] = 1
    else:  # LINE: min-max band, bridged between columns
        prev_hi = prev_lo = None
        for x, (low, high) in enumerate(columns):
            hi_y, lo_y = y_of(high), y_of(low)
            for y in range(hi_y, lo_y + 1):
                grid[y][x] = 1
            if prev_hi is not None:
                for a, b in ((prev_hi, hi_y), (prev_lo, lo_y)):
                    for y in range(min(a, b), max(a, b) + 1):
                        grid[y][x] = 1
            prev_hi, prev_lo = hi_y, lo_y
    return grid


def render_text(values, style, width=48, height=5, frame_start=1.0, frame_end=250.0,
                frame_current=None, scale=None, marks=()):
    # ``marks`` (clamp limits) is accepted so this shares the image
    # renderer's signature, but deliberately NOT drawn: a braille cell is
    # 2x4 dots, so a horizontal line at an arbitrary height is
    # indistinguishable from the curve crossing it. The panel prints the
    # clamp values as text beneath the chart instead, which is legible.
    del marks
    """Lightweight braille text chart (no pixel buffer) for any chart style."""
    if not values:
        return ""
    grid = _text_grid(values, style, width * 2, height * 4, scale=scale)
    cursor_col = _cursor_column(width, frame_start, frame_end, frame_current)
    return _pack_braille(grid, width, height, cursor_col)


def render_line(values, width=48, height=5, frame_start=1.0, frame_end=250.0, frame_current=None, scale=None):
    return render_text(values, "LINE", width, height, frame_start, frame_end, frame_current, scale=scale)


def render_bars(values, width=48, height=5, frame_start=1.0, frame_end=250.0, frame_current=None, scale=None):
    return render_text(values, "BARS", width, height, frame_start, frame_end, frame_current, scale=scale)


def render_filled(values, width=48, height=5, frame_start=1.0, frame_end=250.0, frame_current=None, scale=None):
    return render_text(values, "FILLED", width, height, frame_start, frame_end, frame_current, scale=scale)


def render_mirror(values, width=48, height=5, frame_start=1.0, frame_end=250.0, frame_current=None, scale=None):
    return render_text(values, "MIRROR", width, height, frame_start, frame_end, frame_current, scale=scale)


_RENDERERS = {
    "LINE": render_line,
    "BARS": render_bars,
    "FILLED": render_filled,
    "MIRROR": render_mirror,
}


def _cycle_estimate(stats):
    """Whole-cycle estimate, or None when a cycle count is not meaningful.

    Mean-crossing counting undercounts by half a cycle when the signal starts on
    the midpoint (a sine reads 0.5 for one visible cycle), so round half up.
    """
    if stats.get("shape") not in ("smooth", "oscillating"):
        return None
    cycles = int(stats.get("cycles", 0) + 0.5)
    return cycles if cycles >= 1 else None


def render_stats_block(values, frames, frame_current, sample_count):
    stats = analyze(values)
    if not stats:
        return "No samples."
    lines = [
        f"Range    {_format_value(stats['min'])}  →  {_format_value(stats['max'])}",
        f"Span     {_format_value(stats['span'])}   Mean {_format_value(stats['mean'])}",
    ]
    cycles = _cycle_estimate(stats)
    lines.append(f"Shape    {stats['shape']}" + (f"   ~{cycles} cycles" if cycles else ""))
    cursor = _cursor_index(frames, frame_current)
    if cursor is not None and cursor < len(values):
        lines.append(f"Frame {int(frames[cursor])}  =  {_format_value(values[cursor])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Optional RGBA image buffer (consumed by image_preview.py)
# --------------------------------------------------------------------------- #
try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy ships with Blender
    _np = None

_ZERO_LINE = (0.45, 0.45, 0.2, 1.0)
_CURSOR_LINE = (0.95, 0.85, 0.25, 1.0)
_LABEL_COLOR = (0.85, 0.90, 0.85, 1.0)

# A 3x5 pixel font covering exactly the characters _format_value() can produce
# ("-2.5", "1.87e+03", "0", ...), used to bake the min/mid/max guide values
# directly into the graph image — the autoscaled shape alone can't tell the
# viewer what those rows actually mean.
_FONT_5PX = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    ".": ("...", "...", "...", "...", ".#."),
    "-": ("...", "...", "###", "...", "..."),
    "+": ("...", ".#.", "###", ".#.", "..."),
    "e": ("...", "###", "#.#", "#..", "###"),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": (".##", "#..", "#..", "#..", ".##"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "F": ("###", "#..", "##.", "#..", "#.."),
    "G": (".##", "#..", "#.#", "#.#", ".##"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "J": ("..#", "..#", "..#", "#.#", ".#."),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "###", "#.#", "#.#"),
    "N": ("#.#", "###", "###", "###", "#.#"),
    "O": (".#.", "#.#", "#.#", "#.#", ".#."),
    "P": ("##.", "#.#", "##.", "#..", "#.."),
    "Q": (".#.", "#.#", "#.#", ".##", "..#"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": (".##", "#..", ".#.", "..#", "##."),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "V": ("#.#", "#.#", "#.#", ".#.", ".#."),
    "W": ("#.#", "#.#", "###", "###", "#.#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    "?": ("##.", "..#", ".#.", "...", ".#."),
    " ": ("...", "...", "...", "...", "..."),
}
_GLYPH_W, _GLYPH_H, _GLYPH_GAP = 3, 5, 1


def _blit_text(pixels, width, height, x0, y_anchor, text, color,
               glyph_h=_GLYPH_H, align="center", angle=0.0):
    """Stamp ``text`` into a flat RGBA buffer (list or numpy array — both
    support slice assignment), nearest-neighbour scaled to ``glyph_h`` pixels
    tall (width follows the font's native aspect ratio). Integer block
    scaling (1x/2x/3x) only offers 5/10/15px steps; this lets the caller pick
    any in-between size, e.g. 7 or 8px. ``align`` controls how the glyph sits
    relative to ``y_anchor``: "center" (for a mid-canvas guide), "top"
    (anchor's row is the glyph's first row — for the very top guide, which
    would otherwise be centered half off-canvas), or "bottom" (anchor's row
    is the glyph's last row — same problem at the very bottom edge).
    ``angle`` rotates the stamped word around its own centre, allowing the
    typography preview to show turn amount without drawing a guide through
    the readable text."""
    glyph_h = max(_GLYPH_H, int(glyph_h))
    glyph_w = max(_GLYPH_W, round(glyph_h * _GLYPH_W / _GLYPH_H))
    gap = max(1, glyph_h // _GLYPH_H)
    if align == "top":
        y0 = int(y_anchor)
    elif align == "bottom":
        y0 = int(y_anchor) - glyph_h + 1
    else:
        y0 = int(y_anchor) - glyph_h // 2
    x = int(x0)
    text_width = max(0, len(text) * (glyph_w + gap) - gap)
    centre_x = x + (text_width - 1) * .5
    centre_y = y0 + (glyph_h - 1) * .5
    cosine = math.cos(float(angle))
    sine = math.sin(float(angle))
    for char in text:
        glyph = _FONT_5PX.get(char)
        if glyph is None:
            x += glyph_w + gap
            continue
        for out_row in range(glyph_h):
            # The glyph table is authored top-down (row 0 = the character's
            # top stroke), but the pixel buffer is bottom-to-top (increasing
            # array row = higher on screen) — out_row 0 sits at y0 (the
            # bottom of the glyph), so it must sample the font's LAST row.
            src_row = _GLYPH_H - 1 - (out_row * _GLYPH_H // glyph_h)
            bits = glyph[src_row]
            y = y0 + out_row
            for out_col in range(glyph_w):
                src_col = out_col * _GLYPH_W // glyph_w
                if bits[src_col] != "#":
                    continue
                px = x + out_col
                if angle:
                    dx = px - centre_x
                    dy = y - centre_y
                    px = int(round(centre_x + cosine * dx - sine * dy))
                    py = int(round(centre_y + sine * dx + cosine * dy))
                else:
                    py = y
                if not (0 <= px < width and 0 <= py < height):
                    continue
                base = (py * width + px) * 4
                pixels[base:base + 4] = [color[0], color[1], color[2], color[3]]
        x += glyph_w + gap


def _preview_text_colour(base, response):
    """Keep unrevealed units legible as guides while brightening live units."""
    response = max(0.0, min(1.0, float(response)))
    light = .18 + .82 * response
    return tuple(channel * light for channel in base[:3]) + (1.0,)


def _draw_axis_labels(pixels, width, height, minimum, maximum):
    # Scale the font to the canvas so labels stay legible without dominating it.
    glyph_h = max(_GLYPH_H, min(9, height // 30))
    # The middle guide is ZERO whenever zero is on screen, not the arithmetic
    # midpoint of the range. A curve running -46.9..80.1 put the labelled middle
    # at 16.63, which reads as a baseline and is not one - the eye takes the
    # centre line as "no change". Only when the data never crosses zero does the
    # midpoint get the label, because then there is no zero to show.
    span = maximum - minimum
    if minimum < 0 < maximum and span > 1e-12:
        mid = 0.0
        mid_row = int(round((0.0 - minimum) / span * (height - 1)))
    else:
        mid = (minimum + maximum) / 2
        mid_row = height // 2
    # The pixel buffer is stored bottom-to-top (see render_image_pixels'
    # docstring / to_y: minimum maps to row 0, maximum to row height-1) — so
    # row 0 is the BOTTOM of the displayed image and gets the minimum label,
    # not the maximum. "top"/"bottom" below are array-bounds alignment (which
    # way the glyph safely extends from its anchor row), not display position.
    labels = (
        (minimum, 0, "top"),
        (mid, mid_row, "center"),
        (maximum, height - 1, "bottom"),
    )
    for value, gy, align in labels:
        _blit_text(pixels, width, height, 2, gy, _format_value(value), _LABEL_COLOR, glyph_h=glyph_h, align=align)


_CLAMP_LINE = (1.0, 0.55, 0.15, 1.0)


def _weighted_preview_index(index, weights):
    weights = [max(0.0, float(value)) for value in (weights or (1.0,))]
    total = sum(weights)
    if total <= 1e-12:
        return 0
    unit = (math.sin(index * 12.9898) * 43758.5453) % 1.0
    choice = unit * total
    cumulative = 0.0
    for source_index, weight in enumerate(weights):
        cumulative += weight
        if weight > 0.0 and choice < cumulative:
            return source_index
    return len(weights) - 1


def render_showcase_grid_pixels(values, *, frame=1.0, source_weights=(1.0,),
                                width=224, height=224,
                                bg=(0.10, 0.10, 0.12, 1.0)):
    """Render a small spatial presentation of Kinetic Wave before apply.

    Placement mirrors the generated grid; glyph family identifies which source
    the deterministic mix selects, while size and brightness show the current
    travelling-wave response. This is intentionally a presentation rather than
    a numerical graph: exact values remain available in the normal preview.
    """
    values = dict(values or {})
    columns = max(1, int(values.get("COLUMNS", 8)))
    rows = max(1, int(values.get("ROWS", 5)))
    duration = max(1e-6, float(values.get("DURATION", 48.0)))
    wavelength = max(1e-6, float(values.get("WAVELENGTH", 4.0)))
    scale_min = max(0.0, float(values.get("SCALE_MIN", 0.35)))
    scale_max = max(scale_min, float(values.get("SCALE_MAX", 1.25)))
    falloff = max(0.05, float(values.get("FALLOFF", 1.5)))
    phase_offset = float(values.get("PHASE", 0.0))
    direction = -1.0 if bool(values.get("REVERSE", 0)) else 1.0
    centre_x = float(values.get("CENTRE_X", 0.0))
    centre_y = float(values.get("CENTRE_Y", 0.0))
    spacing_x = max(0.001, float(values.get("SPACING_X", 2.0)))
    spacing_y = max(0.001, float(values.get("SPACING_Y", 2.0)))
    if _np is not None:
        image = _np.empty((height, width, 4), dtype=_np.float32)
        image[:] = bg
        pixels = image.reshape(-1)
    else:
        pixels = list(bg) * (width * height)

    pad = max(8, min(width, height) // 12)
    cell_w = (width - pad * 2) / max(1, columns)
    cell_h = (height - pad * 2) / max(1, rows)
    base_radius = max(2.0, min(cell_w, cell_h) * 0.28)
    palette = (
        (0.20, 0.76, 1.00, 1.0),
        (1.00, 0.48, 0.20, 1.0),
        (0.45, 1.00, 0.48, 1.0),
        (0.82, 0.42, 1.00, 1.0),
        (1.00, 0.84, 0.22, 1.0),
    )

    def put(px, py, colour):
        if 0 <= px < width and 0 <= py < height:
            offset = (py * width + px) * 4
            pixels[offset:offset + 4] = colour

    for row in range(rows):
        for column in range(columns):
            index = row * columns + column
            world_x = (column - (columns - 1) * 0.5) * spacing_x
            world_y = (row - (rows - 1) * 0.5) * spacing_y
            distance = math.hypot(world_x - centre_x, world_y - centre_y)
            phase = distance / wavelength * math.tau - (float(frame) + phase_offset) * direction / duration * math.tau
            response = max(0.0, (0.5 + 0.5 * math.sin(phase))) ** falloff
            scale = scale_min + (scale_max - scale_min) * response
            radius = max(1, int(round(base_radius * max(0.25, scale))))
            source_index = _weighted_preview_index(index, source_weights)
            base = palette[source_index % len(palette)]
            light = 0.42 + 0.58 * response
            colour = (base[0] * light, base[1] * light, base[2] * light, 1.0)
            cx = int(round(pad + (column + 0.5) * cell_w))
            cy = int(round(pad + (row + 0.5) * cell_h))
            glyph = source_index % 3
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if glyph == 0:
                        inside = dx * dx + dy * dy <= radius * radius
                    elif glyph == 1:
                        inside = max(abs(dx), abs(dy)) <= radius
                    else:
                        inside = abs(dx) + abs(dy) <= radius
                    if inside:
                        put(cx + dx, cy + dy, colour)
    return pixels, (width, height)


def render_image_pixels(values, width=200, height=200, style="LINE",
                        bg=(0.10, 0.10, 0.12, 1.0), line=(0.30, 0.75, 1.0, 1.0),
                        guide=(0.32, 0.32, 0.36, 1.0),
                        frame_start=1.0, frame_end=250.0, frame_current=None,
                        show_axis_labels=False, scale=None, marks=(),
                        colour_strip=()):
    """Render the expression as an accurate RGBA pixel graph.

    Returns ``(pixels, (width, height))`` where ``pixels`` is a flat RGBA buffer
    ordered bottom-to-top (a numpy float32 array when numpy is available, which
    uploads via ``foreach_set`` far faster; otherwise a Python list). Every pixel
    column reports the min-max envelope of the frames inside it, so every
    pulse/beat is drawn regardless of width. ``style`` picks the chart shape.

    The Y axis always autoscales to the sampled values' own min/max, so the
    shape fills the frame regardless of the expression's actual magnitude.
    ``show_axis_labels`` bakes the min/mid/max values into the image so the
    guide rows mean something without reading a separate caption.
    """
    if not values:
        if _np is not None:
            img = _np.empty((height, width, 4), dtype=_np.float32)
            img[:] = bg
            return img.reshape(-1), (width, height)
        return list(bg) * (width * height), (width, height)

    if scale is not None:
        minimum, maximum = float(scale[0]), float(scale[1])
        # Clip into the fixed band so an amplitude above the reference visibly
        # pushes against the top/bottom edge instead of the box rescaling.
        values = [min(maximum, max(minimum, float(v))) for v in values]
    else:
        # Auto-fit ALWAYS includes zero, so the curve is read in absolute terms.
        # Fitting to the data alone magnified a hair-thin wiggle to full height:
        # a value moving 5.3233 -> 5.3240 drew as a dramatic diagonal, and all
        # three axis labels rounded to "5.32". Anchoring to zero shows that for
        # what it is - a near-flat line sitting up at 5.32 - and makes two
        # templates comparable, because they are drawn against the same floor.
        minimum = min(0.0, float(min(values)))
        maximum = max(0.0, float(max(values)))
        if maximum - minimum <= 1e-12:
            # A genuinely constant zero still needs a band to draw into.
            minimum, maximum = -1.0, 1.0

    if _np is not None:
        pixels, size = _render_image_numpy(values, width, height, style, bg, line, guide, marks,
                                           frame_start, frame_end, frame_current, minimum, maximum)
    else:
        pixels, size = _render_image_python(values, width, height, style, bg, line, guide, marks,
                                            frame_start, frame_end, frame_current, minimum, maximum)
    if show_axis_labels:
        _draw_axis_labels(pixels, width, height, minimum, maximum)
    if colour_strip:
        _draw_colour_strip(pixels, width, height, colour_strip)
    return pixels, size


def _draw_colour_strip(pixels, width, height, colours):
    """Draw the evaluated ColorRamp result as a compact band in the graph."""
    if not colours or width < 12 or height < 12:
        return
    x0, x1 = min(34, width // 4), width - 4
    y0, y1 = 6, min(height - 4, 18)
    count = len(colours)

    def put(x, y, colour):
        index = (y * width + x) * 4
        pixels[index:index + 4] = colour

    border = (0.55, 0.55, 0.58, 1.0)
    for x in range(x0 - 1, x1 + 1):
        put(x, y0 - 1, border)
        put(x, y1, border)
    for y in range(y0 - 1, y1 + 1):
        put(x0 - 1, y, border)
        put(x1, y, border)
    span = max(1, x1 - x0)
    for x in range(x0, x1):
        sample = min(count - 1, int((x - x0) * count / span))
        colour = tuple(colours[sample])
        for y in range(y0, y1):
            put(x, y, colour)


def _render_image_numpy(values, width, height, style, bg, line, guide, marks,
                        frame_start, frame_end, frame_current, minimum, maximum):
    np = _np
    img = np.empty((height, width, 4), dtype=np.float32)
    img[:] = bg

    span = maximum - minimum

    def to_y(value):
        norm = 0.5 if span <= 1e-12 else (value - minimum) / span
        return int(round(min(1.0, max(0.0, norm)) * (height - 1)))

    def column_rows(buckets):
        columns = _column_envelopes(values, buckets)
        if span <= 1e-12:
            mid = np.full(len(columns), height // 2, dtype=np.int64)
            return mid, mid.copy()
        lo_y = np.array([to_y(low) for low, _high in columns], dtype=np.int64)
        hi_y = np.array([to_y(high) for _low, high in columns], dtype=np.int64)
        return lo_y, hi_y

    yy = np.arange(height).reshape(height, 1)

    zero_y = to_y(0.0) if minimum < 0 < maximum else None
    # Middle guide == zero line when zero is visible; see _draw_axis_labels.
    for gy in (0, zero_y if zero_y is not None else height // 2, height - 1):
        img[gy, ::3] = guide
    if zero_y is not None:
        img[zero_y, :] = _ZERO_LINE
    # Clamp limits, drawn where the curve actually meets them. The curve itself
    # is NOT moved to fit between them: shifting it made the shape readable at
    # the cost of showing it at a height the values never occupy. Lines say the
    # same thing without lying about the values.
    for mark in (marks or ()):
        try:
            if minimum <= float(mark) <= maximum:
                img[to_y(float(mark)), ::2] = _CLAMP_LINE
        except (TypeError, ValueError):
            continue
    baseline = zero_y if zero_y is not None else 0

    if style == "MIRROR":
        lo_y, hi_y = column_rows(width)
        center = 0.0 if minimum < 0 < maximum else (minimum + maximum) / 2
        center_y = to_y(center)
        bottom = np.minimum(lo_y, center_y)
        top = np.maximum(hi_y, center_y)
        img[(yy >= bottom[None, :]) & (yy <= top[None, :])] = line
    elif style == "BARS":
        body, gap = 4, 2
        period = body + gap
        bar_count = max(1, width // period)
        _lo, btop = column_rows(bar_count)
        for index in range(bar_count):
            top = int(btop[index])
            x0 = index * period
            y0, y1 = (baseline, top) if top >= baseline else (top, baseline)
            img[y0:y1 + 1, x0:min(width, x0 + body)] = line
    elif style == "FILLED":
        _lo, hi_y = column_rows(width)
        if zero_y is not None:
            low = np.minimum(hi_y, zero_y)
            high = np.maximum(hi_y, zero_y)
            img[(yy >= low[None, :]) & (yy <= high[None, :])] = line
        else:
            img[yy <= hi_y[None, :]] = line
    else:  # LINE: min-max band, bridging both envelope edges to the prev column
        lo_y, hi_y = column_rows(width)
        mask = (yy >= lo_y[None, :]) & (yy <= hi_y[None, :])
        for edge in (lo_y, hi_y):
            seg_lo = np.minimum(edge[:-1], edge[1:])
            seg_hi = np.maximum(edge[:-1], edge[1:])
            mask[:, 1:] |= (yy >= seg_lo[None, :]) & (yy <= seg_hi[None, :])
        img[mask] = line

    cursor_col = _cursor_column(width, frame_start, frame_end, frame_current)
    if cursor_col is not None:
        img[::3, cursor_col] = _CURSOR_LINE
    return img.reshape(-1), (width, height)


def _render_image_python(values, width, height, style, bg, line, guide, marks,
                         frame_start, frame_end, frame_current, minimum, maximum):
    pix = list(bg) * (width * height)

    def put(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            base = (y * width + x) * 4
            pix[base:base + 4] = [color[0], color[1], color[2], color[3]]

    def vline(x, y0, y1, color):
        for y in range(max(0, min(y0, y1)), min(height - 1, max(y0, y1)) + 1):
            base = (y * width + x) * 4
            pix[base:base + 4] = [color[0], color[1], color[2], color[3]]

    columns = _column_envelopes(values, width)
    span = maximum - minimum

    def to_y(value):
        norm = 0.5 if span <= 1e-12 else (value - minimum) / span
        return int(round(min(1.0, max(0.0, norm)) * (height - 1)))

    # Horizontal guides: min, middle (zero when visible), max.
    zero_y = to_y(0.0) if minimum < 0 < maximum else None
    for gy in (0, zero_y if zero_y is not None else height // 2, height - 1):
        for x in range(0, width, 3):
            put(x, gy, guide)
    if zero_y is not None:
        for x in range(width):
            put(x, zero_y, _ZERO_LINE)
    # Clamp limits - see the numpy renderer for why the curve is not moved.
    for mark in (marks or ()):
        try:
            value = float(mark)
        except (TypeError, ValueError):
            continue
        if minimum <= value <= maximum:
            my = to_y(value)
            for x in range(0, width, 2):
                put(x, my, _CLAMP_LINE)

    baseline = zero_y if zero_y is not None else 0
    if style == "MIRROR":
        center = 0.0 if minimum < 0 < maximum else (minimum + maximum) / 2
        center_y = to_y(center)
        for x in range(width):
            low, high = columns[x]
            vline(x, min(to_y(low), center_y), max(to_y(high), center_y), line)
    elif style == "BARS":
        # Discrete bars, each the max over its frame span so no pulse is skipped.
        body, gap = 4, 2
        period = body + gap
        bar_count = max(1, width // period)
        for index, (_low, high) in enumerate(_column_envelopes(values, bar_count)):
            top = to_y(high)
            x0 = index * period
            for x in range(x0, min(width, x0 + body)):
                vline(x, baseline, top, line)
    elif style == "FILLED":
        for x in range(width):
            _low, high = columns[x]
            vline(x, baseline, to_y(high), line)
    else:  # LINE
        prev_lo = prev_hi = None
        for x in range(width):
            low, high = columns[x]
            lo_y, hi_y = to_y(low), to_y(high)
            vline(x, lo_y, hi_y, line)
            if prev_lo is not None:  # bridge gaps so the curve stays connected
                vline(x, prev_lo, lo_y, line)
                vline(x, prev_hi, hi_y, line)
            prev_lo, prev_hi = lo_y, hi_y

    # Current-frame cursor on top.
    cursor_col = _cursor_column(width, frame_start, frame_end, frame_current)
    if cursor_col is not None:
        for y in range(0, height, 3):
            put(cursor_col, y, (0.95, 0.85, 0.25, 1.0))
    return pix, (width, height)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _detail_line(values, frames, frame_current, stats):
    # The preview message directly above this row already owns min/max. Keeping
    # that range here as well made cursor values with more digits wrap onto a
    # third row during playback, causing the panel height to jump.
    parts = []
    cycles = _cycle_estimate(stats)
    if cycles:
        parts.append(f"~{cycles} cyc")
    parts.append(stats["shape"])
    cursor = _cursor_index(frames, frame_current)
    if cursor is not None and cursor < len(values):
        parts.append(f"f{int(frames[cursor])}={_format_value(values[cursor])}")
    return " · ".join(parts)


def _finalize_preview_result(
    cached, cache_key, style, frame_current, detailed, sample_count,
    scale=None, zoom=1.0, want_text=True,
):
    """Build a PreviewResult from a cached samples dict. Shared by
    sample_preview (template-expression eval) and sample_fcurve_driver
    (real fcurve.evaluate()) so both data sources render through the exact
    same renderers/styles/detail-line logic."""
    style = style if style in VALID_STYLES else "LINE"
    if not cached["valid"]:
        return PreviewResult(False, cached["message"], cache_key=cache_key, style=style)

    values = cached["values"]
    frames = cached["frames"]
    minimum = cached["minimum"]
    maximum = cached["maximum"]
    stats = cached["stats"]
    frame_start = frames[0]
    frame_end = frames[-1]

    if style == "STATS":
        graph = render_stats_block(values, frames, frame_current, sample_count)
        detail_text = ""
    else:
        # Taller (and a touch wider) when Detailed is on. No text guide labels:
        # a proportional UI font would misalign them and break the braille grid;
        # min/max already live in the status line and the detail line.
        height = 7 if detailed else 5
        width = 48
        if want_text:
            renderer = _RENDERERS[style]
            visible_range = zoomed_value_range(values, scale, zoom)
            graph = renderer(values, width=width, height=height,
                             frame_start=frame_start, frame_end=frame_end,
                             frame_current=frame_current, scale=visible_range)
        else:
            # Pixel-image mode: the braille chart is only ever used as a
            # fallback, so building it here cost 419-602us on EVERY draw and was
            # then thrown away. The fallback path re-renders it on demand.
            graph = ""
        # detail_text is NOT gated: it is drawn as a caption in both modes.
        detail_text = _detail_line(values, frames, frame_current, stats) if detailed else ""

    return PreviewResult(
        True,
        cached["message"],
        tuple(values),
        tuple(frames),
        minimum,
        maximum,
        graph,
        cache_key,
        detail_text,
        style,
        stats,
    )


def default_reference_range(
    template, frame_start, frame_end, build_expression, scene,
    channel_id="", current_values=None,
):
    """Vertical (min, max) for the active channel's default-scale motion.

    Amplitude values stay at catalogue defaults so Fixed Scale remains stable,
    but structural selectors follow the current configuration. For example,
    Ball Bounce's Drop Height chooses between a downward drop and an upward
    in-place launch; those modes cannot honestly share one reference range.
    Multi-channel templates likewise use the selected channel instead of always
    inheriting channel zero's units and bounds.

    One-sided ranges keep their real boundary while adding headroom away from
    it. A 0..10 bounce therefore displays as 0..12.5, never -1.25..11.25.
    Returns None when the selected default output is flat.
    """
    values = {p["token"]: p.get("default") for p in template.get("params", [])}
    if current_values:
        structural_tokens = set()
        for param in template.get("params", []):
            structural_tokens.update((param.get("visible_if") or {}).keys())
        for channel in template.get("channels", []):
            structural_tokens.update((channel.get("enabled_if") or {}).keys())
        for token in structural_tokens:
            if token in current_values:
                values[token] = current_values[token]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values.values()):
        return None
    try:
        built = utils.build_template_expressions(template, dict(values), scene)
        channel = next(
            (item for item in built if item.get("id") == channel_id),
            built[0] if built else None,
        )
        if channel is None:
            return None
        expr = channel["expression"]
        helper_expressions = channel.get("internal_helpers")
    except Exception:
        return None
    ref = sample_preview(
        str(expr), template, frame_start, frame_end, None, None, False, "LINE",
        helper_expressions=helper_expressions,
    )
    if not ref.valid:
        return None
    lo, hi = ref.minimum, ref.maximum
    span = hi - lo
    if span <= 1e-9:
        return None
    padded_span = span * 1.25
    if lo >= -1e-9:
        return (max(0.0, lo), lo + padded_span)
    if hi <= 1e-9:
        return (hi - padded_span, min(0.0, hi))
    mid = (lo + hi) / 2.0
    half = span / 2.0 * 1.25  # default fills ~80% of the box, leaving headroom
    return (mid - half, mid + half)


def sample_preview(expression, template, frame_start, frame_end, sample_count=None,
                   frame_current=None, detailed=False, style="LINE", scale=None,
                   zoom=1.0, want_text=True, helper_expressions=None,
                   series_visibility=None):
    # Sample density is automatic (per frame, capped): a fixed, predictable basis
    # so the preview never aliases as a user-tweakable count would.
    if sample_count is None:
        sample_count = auto_sample_count(frame_start, frame_end)
    else:
        sample_count = max(2, min(_MAX_SAMPLES, int(sample_count)))
    cache_key = _make_cache_key(
        expression, template, frame_start, frame_end, sample_count,
        helper_expressions=helper_expressions,
        series_visibility=series_visibility,
    )

    cached = _CACHE.get(cache_key)
    if cached is None:
        cached = _compute_samples(
            expression, template, frame_start, frame_end, sample_count, cache_key,
            helper_expressions=helper_expressions,
        )
        _CACHE[cache_key] = cached

    return _finalize_preview_result(
        cached, cache_key, style, frame_current, detailed, sample_count,
        scale=scale, zoom=zoom, want_text=want_text,
    )


def _make_fcurve_cache_key(owner_name, data_path, array_index, expression, frame_start, frame_end, sample_count):
    raw = "|".join(
        [
            "fcurve",
            owner_name,
            data_path,
            str(array_index),
            expression,
            format(float(frame_start), ".9g"),
            format(float(frame_end), ".9g"),
            str(int(sample_count)),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _compute_fcurve_samples(fcurve, frame_start, frame_end, sample_count):
    frames = _sample_frames(float(frame_start), float(frame_end), sample_count)
    values = []
    try:
        for frame in frames:
            value = float(fcurve.evaluate(frame))
            if not math.isfinite(value):
                raise ValueError(f"non-finite value at frame {frame:g}")
            values.append(value)
    except Exception as exc:
        return {"valid": False, "message": "Preview unavailable: " + str(exc)}

    minimum = min(values)
    maximum = max(values)
    return {
        "valid": True,
        "message": f"min {minimum:.3g} · max {maximum:.3g} · span {maximum - minimum:.3g}",
        "values": values,
        "frames": frames,
        "minimum": minimum,
        "maximum": maximum,
        "stats": analyze(values),
    }


def sample_fcurve_driver(fcurve, owner_name, data_path, array_index, frame_start, frame_end,
                          sample_count=None, frame_current=None, detailed=False, style="LINE",
                          scale=None, zoom=1.0, want_text=True):
    """Sample a real, already-applied driver via Blender's own
    FCurve.evaluate(), which correctly resolves that driver's actual
    variables — no sandboxed-eval reimplementation needed. Mirrors
    sample_preview()'s PreviewResult shape and shares its cache/renderers so
    the Preview panel can display either data source identically."""
    if sample_count is None:
        sample_count = auto_sample_count(frame_start, frame_end)
    else:
        sample_count = max(2, min(_MAX_SAMPLES, int(sample_count)))

    expression = getattr(getattr(fcurve, "driver", None), "expression", "")
    cache_key = _make_fcurve_cache_key(owner_name, data_path, array_index, expression, frame_start, frame_end, sample_count)

    cached = _CACHE.get(cache_key)
    if cached is None:
        cached = _compute_fcurve_samples(fcurve, frame_start, frame_end, sample_count)
        _CACHE[cache_key] = cached

    return _finalize_preview_result(
        cached, cache_key, style, frame_current, detailed, sample_count,
        scale=scale, zoom=zoom, want_text=want_text,
    )



# Evaluating every frame of a very long scene is not free, so the peak-preserving
# path stops here and accepts sparse sampling beyond it. 20000 frames is about
# fourteen minutes at 24fps - past any shot, and cheap enough to do once, which
# is all that happens because the result is cached.
_MAX_EVALUATED_FRAMES = 20000


def _peak_preserving_frames(frame_start, frame_end, sample_count):
    """Every frame, when there are more frames than samples - else None.

    One sample per frame stops a narrow pulse falling between samples, but only
    while the scene is shorter than the sample cap. Past that the spacing
    exceeds a frame and pulses start disappearing outright: measured on a
    5000-frame scene, the blink template fired 52 times and the graph drew 43.
    The nine that vanished were not faint, they were absent.
    """
    span = abs(frame_end - frame_start)
    total = int(round(span)) + 1
    if total <= sample_count or total > _MAX_EVALUATED_FRAMES:
        return None
    step = 1.0 if frame_end >= frame_start else -1.0
    return [frame_start + step * index for index in range(total)]


def _reduce_to_extremes(frames, values, sample_count):
    """Collapse per-frame values into buckets, keeping each bucket's extremes.

    Lowest and highest per bucket, emitted in time order: the extremes ARE the
    pulse, and the column envelopes downstream carry them to pixels.
    """
    total = len(values)
    buckets = max(1, min(sample_count, total))
    kept_frames, kept_values = [], []
    for bucket in range(buckets):
        low_index = (bucket * total) // buckets
        high_index = max(low_index + 1, ((bucket + 1) * total) // buckets)
        window = range(low_index, min(high_index, total))
        lowest = min(window, key=lambda index: values[index])
        highest = max(window, key=lambda index: values[index])
        for index in sorted({lowest, highest}):
            kept_frames.append(frames[index])
            kept_values.append(values[index])
    return kept_frames, kept_values


def _compute_samples(
    expression, template, frame_start, frame_end, sample_count, cache_key,
    helper_expressions=None,
):
    valid, message = utils.validate_expression(expression, template, None)
    if not valid:
        return {"valid": False, "message": "Preview unavailable: " + message}
    try:
        code = compile(expression, "<driver_espresso_preview>", "eval")
    except SyntaxError as exc:
        return {"valid": False, "message": "Preview unavailable: " + exc.msg}

    namespace = dict(utils.SAFE_NAMESPACE)
    for variable in (
        *template.get("requires_driver_variables", []),
        *template.get("managed_driver_variables", []),
    ):
        namespace[variable["name"]] = variable.get("preview_default", 0)
    # A helper may carry no expression -- its real value only exists once the
    # rig is built -- in which case it contributes its declared preview number.
    # Indexing "expression" unconditionally turned that into a KeyError that
    # took the whole preview panel down rather than drawing a curve.
    helper_codes = []
    for item in (helper_expressions or []):
        expression = item.get("expression")
        if not expression:
            expression = repr(float(item.get("preview_default", 0.0) or 0.0))
        helper_codes.append((
            item["name"],
            compile(expression, "<driver_espresso_helper_preview>", "eval"),
        ))

    dense_frames = _peak_preserving_frames(float(frame_start), float(frame_end), sample_count)
    frames = dense_frames or _sample_frames(float(frame_start), float(frame_end), sample_count)
    values = []
    try:
        for frame in frames:
            namespace["frame"] = frame
            for name, helper_code in helper_codes:
                namespace[name] = eval(helper_code, {"__builtins__": {}}, namespace)
            value = eval(code, {"__builtins__": {}}, namespace)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"non-numeric value at frame {frame:g}")
            values.append(float(value))
    except Exception as exc:
        return {"valid": False, "message": "Preview unavailable: " + str(exc)}

    if dense_frames:
        frames, values = _reduce_to_extremes(frames, values, sample_count)

    minimum = min(values)
    maximum = max(values)
    return {
        "valid": True,
        "message": f"min {minimum:.3g} · max {maximum:.3g} · span {maximum - minimum:.3g}",
        "values": values,
        "frames": frames,
        "minimum": minimum,
        "maximum": maximum,
        "stats": analyze(values),
    }


def overlay_presentation(template, selected_channel_id, enabled=False):
    """Selected-channel default plus compatible sibling overlay state."""
    channels = [
        channel_identity.identity_from_channel(item)
        for item in (template or {}).get("channels") or ()
    ]
    selected = next((item for item in channels if item.id == selected_channel_id), None)
    if selected is None and channels:
        selected = channels[0]
    compatible = [
        item for item in channels
        if selected is not None and channel_identity.overlay_compatible(selected, item)
    ]
    can_overlay = bool(enabled and selected is not None and compatible)
    visibility = {selected.id: True} if selected is not None else {}
    if can_overlay:
        for item in compatible:
            visibility[item.id] = True
    return {
        "selected": selected,
        "compatible_ids": tuple(item.id for item in compatible),
        "can_overlay": can_overlay,
        "enabled": bool(enabled),
        "reason": (
            ""
            if can_overlay or not enabled
            else "Overlay needs a sibling with the same unit and meaning."
        ),
        "series_visibility": visibility,
        "legend": [
            {
                "id": item.id,
                "label": item.label,
                "color": channel_identity.axis_color(item),
            }
            for item in ((selected,) if selected is not None else ()) + tuple(compatible if can_overlay else ())
        ],
    }


def clear_cache():
    _CACHE.clear()


def cursor_column_for(width, frame_start, frame_end, frame_current):
    """The pixel column the playhead falls in, or None if it is off-graph."""
    return _cursor_column(width, frame_start, frame_end, frame_current)


def stamp_cursor(pixels, width, height, column):
    """Return a COPY of ``pixels`` with the playhead column drawn on it.

    Split out of the renderer so playback can redraw the moving cursor without
    re-running the curve maths. The curve, guides, clamp marks and axis labels
    are frame-independent, so they are rendered once and this stamps the only
    part that moves - measured as the difference between a full 224x224
    re-render per frame and a buffer copy plus one column write.

    A copy, never in-place: the caller keeps the cursorless buffer cached, and
    stamping into it would leave a trail of every previous playhead position.
    """
    if column is None:
        return pixels
    if _np is not None and hasattr(pixels, "reshape"):
        img = pixels.reshape((height, width, 4)).copy()
        img[::3, column] = _CURSOR_LINE
        return img.reshape(-1)
    out = list(pixels)
    for y in range(0, height, 3):
        base = (y * width + column) * 4
        out[base:base + 4] = list(_CURSOR_LINE)
    return out
