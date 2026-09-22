"""Expression building and validation helpers for Driver Espresso."""

from __future__ import annotations

import ast
import functools
import json
import math
import re

from .driver_literals import format_computed_literal, format_driver_literal, round_parameter
from ...catalogue.core.templates import CUSTOM_RANGE, ENDPOINT_HIT, FIXED_PERIOD, PLAYBACK_WRAP


MAX_DRIVER_EXPRESSION_LENGTH = 255


ALLOWED_NAMES = {
    "frame",
    "pi",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "atan2",
    "degrees",
    "e",
    "abs",
    "fmod",
    "min",
    "max",
    "hypot",
    "int",
    "pow",
    "radians",
    "round",
    "floor",
    "ceil",
    "exp",
    "log",
    "sqrt",
    "tanh",
}

SAFE_NAMESPACE = {
    "pi": math.pi,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "degrees": math.degrees,
    "e": math.e,
    "abs": abs,
    "fmod": math.fmod,
    "min": min,
    "max": max,
    "hypot": math.hypot,
    "int": int,
    "pow": pow,
    "radians": math.radians,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "exp": math.exp,
    "log": math.log,
    "sqrt": math.sqrt,
    "tanh": math.tanh,
}

REST_START_OFF = "OFF"
REST_START_ADDITIVE = "ADDITIVE"
REST_START_OFFSET_ONLY = "OFFSET_ONLY"


def rest_start_summary(mode, apply_frame, *, is_motion=False, additive_profile=None):
    """Friendly UI copy for the effective apply-time contract."""
    if mode == REST_START_ADDITIVE:
        text = (
            f"Starts from the current value on frame {int(apply_frame)}; "
            "earlier frames hold that value."
        )
    elif mode == REST_START_OFFSET_ONLY:
        if additive_profile == "BOUNDED_MIN":
            text = (
                "Keeps the scene-time phase and adds Current Value + Minimum "
                "as one fixed shift; it may jump when applied."
            )
        elif additive_profile == "BOUNDED_MAX":
            text = (
                "Keeps the scene-time phase and adds Current Value + Maximum "
                "as one fixed shift; it may jump when applied."
            )
        else:
            text = (
                f"Keeps the scene-time phase and applies one fixed shift so frame "
                f"{int(apply_frame)} matches the current value."
            )
    else:
        text = "Uses the template's authored scene-time phase without a timeline restart."
    if is_motion:
        text += " Object placement remains relative to the current transform."
    return text


DEFAULT_FPS = 24  # Blender's own default, used when no scene is available.


def scene_fps(scene):
    render = getattr(scene, "render", None)
    fps = getattr(render, "fps", None)
    try:
        fps = int(fps)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return fps if fps > 0 else DEFAULT_FPS


def frame_constants(template, scene):
    start = int(getattr(scene, "frame_start", 1))
    end = int(getattr(scene, "frame_end", 250))
    mode = template.get("frame_mode", PLAYBACK_WRAP)
    if mode == ENDPOINT_HIT:
        length = max(1, end - start)
    elif mode == FIXED_PERIOD:
        length = max(1, end - start + 1)
    elif mode == CUSTOM_RANGE:
        length = max(1, end - start + 1)
    else:
        length = max(1, end - start + 1)
    # FPS lets friendly parameters speak in real seconds ("60 s per revolution")
    # instead of unitless multipliers. No template's expression contains FPS, so
    # adding it here substitutes nothing and changes no existing output.
    return {
        "FRAME_START": start,
        "FRAME_END": end,
        "FRAME_LEN": length,
        "FPS": scene_fps(scene),
    }


@functools.lru_cache(maxsize=256)
def _token_pattern(tokens_longest_first):
    """One regex matching any of this template's tokens, longest-first."""
    if not tokens_longest_first:
        return None
    alternation = "|".join(re.escape(tok) for tok in tokens_longest_first)
    return re.compile(r"\b(?:" + alternation + r")\b")


@functools.lru_cache(maxsize=512)
def _cached_compile(source, filename):
    """Compile once and reuse the code object.

    A code object is immutable bytecode - every eval() below still builds a
    fresh namespace, so nothing leaks between calls. Only the parse/compile
    step is skipped, which measured as ~76% of resolve_derived_tokens' cost.
    Cleared in unregister() for reload hygiene.
    """
    return compile(source, filename, "eval")


BLOCKING_WARNING_PREFIX = "Cannot apply: "


def is_blocking_warning(text):
    """A warning that refuses the apply rather than annotating it."""
    return str(text or "").startswith(BLOCKING_WARNING_PREFIX)


def template_checks(template, token_values):
    """Evaluate the recipe's ``warn_if`` checks against the resolved tokens.

    Returns the texts of every check whose ``when`` formula is true, blocking
    ones prefixed so the preview and the apply can tell them apart. A check
    that cannot be evaluated is reported rather than swallowed: a formula
    naming a token the recipe does not have is a catalogue bug.
    """
    out = []
    for check in (template or {}).get("warn_if") or ():
        formula = str(check.get("when") or "")
        if not formula:
            continue
        namespace = dict(SAFE_NAMESPACE)
        namespace.update(token_values)
        try:
            hit = bool(eval(_cached_compile(formula, "<warn_if>"), {"__builtins__": {}}, namespace))
        except Exception as exc:  # noqa: BLE001 - a broken check must be visible
            out.append(BLOCKING_WARNING_PREFIX + "%s: check %r failed (%s)." % (
                (template or {}).get("id", "?"), formula, exc))
            continue
        if hit:
            text = str(check.get("text") or "This recipe's values need attention.")
            out.append((BLOCKING_WARNING_PREFIX + text) if check.get("blocking") else text)
    return out


def resolve_derived_tokens(template, token_values):
    """Compute the expression tokens produced by friendly parameters.

    Returns ``{}`` for any template that declares no ``derives``, which is what
    makes the friendly-parameter layer byte-neutral for the rest of the catalogue.
    """
    # A COLOR swatch reaches here as three components (SR/SG/SB) when the
    # caller came through build_template_expressions, and as the raw tuple - or
    # not at all - when it did not. A derive that reads a colour has to work
    # either way, so the components are filled in here rather than being the
    # caller's problem.
    token_values = dict(token_values)
    for param in template.get("params", []):
        if param.get("type") != "COLOR":
            continue
        components = colour_component_tokens(param["token"])
        if all(name in token_values for name in components):
            continue
        swatch = token_values.get(param["token"], param.get("default")) or (0.0, 0.0, 0.0)
        for name, component in zip(components, swatch):
            token_values[name] = float(component)

    derived = {}
    for param in template.get("params", []):
        formulas = param.get("derives")
        if not formulas:
            continue
        for target, formula in formulas.items():
            namespace = dict(SAFE_NAMESPACE)
            namespace.update(token_values)
            namespace.update(derived)
            try:
                value = eval(_cached_compile(formula, "<derived_param>"), {"__builtins__": {}}, namespace)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"{template.get('id', '?')}: cannot derive {target} from {param['token']}: {exc}"
                ) from exc
            derived[target] = float(value)
    return derived


def colour_component_tokens(token):
    """A COLOR parameter feeds three scalar tokens: C -> CR, CG, CB."""
    return tuple(f"{token}{axis}" for axis in "RGB")


def coerce_value(param, raw_value):
    kind = param.get("type", "FLOAT")
    if kind in {"STRING", "ENUM"}:
        # Declarative setup metadata is intentionally non-numeric. It belongs
        # to generated-route builders and UI summaries, never to Blender's
        # scalar driver-expression namespace.
        return str(raw_value)
    if kind == "COLOR":
        # A colour is three numbers behind one swatch. It never substitutes as
        # itself - expand_colour_tokens() turns it into <token>R/G/B, which is
        # what the expression actually names - so just keep it well formed.
        rgb = tuple(raw_value) if hasattr(raw_value, "__iter__") else (float(raw_value),) * 3
        return tuple(min(1.0, max(0.0, float(component))) for component in rgb[:3])
    if kind == "INT":
        value = int(round(raw_value))
    elif kind == "BOOL":
        value = 1 if bool(raw_value) else 0
    else:
        value = float(raw_value)
    minimum = param.get("min")
    maximum = param.get("max")
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def default_values(template):
    return {param["token"]: param.get("default", 0.0) for param in template.get("params", [])}


def format_result_summary(template, values, scene):
    """Describe the selected parameter result in user-facing units.

    Summary strings are catalogue metadata, not executable expressions. Values
    still pass through the same coercion rules as expression generation so the
    sentence always describes what Blender will actually receive.
    """
    summary = template.get("result_summary", "")
    if not summary:
        return ""
    merged = default_values(template)
    merged.update(values or {})
    resolved = {
        param["token"]: coerce_value(param, merged[param["token"]])
        for param in template.get("params", [])
    }
    resolved.update({key: float(value) for key, value in frame_constants(template, scene).items()})
    try:
        return summary.format_map(resolved)
    except (KeyError, ValueError):
        return ""


def advanced_defaults(template, scene):
    # Start/End default to 0, which the activation-window helper reads as
    # "use the scene boundary". This keeps untouched advanced timing controls
    # neutral instead of clamping output to a hard-coded frame window.
    defaults = {
        "ADV_DELAY": 0,
        "ADV_ADVANCE": 0,
        "ADV_START_FRAME": 0,
        "ADV_END_FRAME": 0,
        "ADV_LOOP_FIT": 0,
        "ADV_MULT": 1.0,
        "ADV_OFFSET": 0.0,
        "ADV_OVERRIDE_FPS": 0,
        "ADV_TIMING_FPS": 24.0,
    }
    resolved = {}
    for control in template.get("advanced_controls", []):
        token = control.get("token")
        if token in defaults:
            resolved[token] = defaults[token]
    return resolved


def apply_advanced_time(expression, token_values):
    advance = token_values.get("ADV_ADVANCE", 0)
    delay = token_values.get("ADV_DELAY", 0)
    # Neutral at defaults: no time shift means the expression is left untouched.
    if not advance and not delay:
        return expression
    time_expr = f"(frame + {advance} - {delay})"
    return re.sub(r"\bframe\b", time_expr, expression)


def wrap_activation_window(expression, token_values, scene, output_baseline=0.0):
    scene_start = int(getattr(scene, "frame_start", 1))
    scene_end = int(getattr(scene, "frame_end", 250))
    delay = int(token_values.get("ADV_DELAY", 0))
    raw_start = int(token_values.get("ADV_START_FRAME", 0))
    raw_end = int(token_values.get("ADV_END_FRAME", 0))
    # 0 is a sentinel meaning "use the scene boundary".
    start_frame = (raw_start if raw_start else scene_start) + delay
    end_frame = raw_end if raw_end else scene_end
    if end_frame < start_frame:
        end_frame = start_frame
    # Neutral: a window that spans the whole scene with no delay imposes nothing,
    # so the base expression is returned unchanged (no silent truncation).
    if delay == 0 and start_frame <= scene_start and end_frame >= scene_end:
        return expression
    return f"(({expression}) if {start_frame} <= frame <= {end_frame} else {_format_literal(output_baseline)})"


def wrap_output_shaping(expression, token_values, output_baseline=0.0):
    mult = token_values.get("ADV_MULT", 1.0)
    offset = token_values.get("ADV_OFFSET", 0.0)
    # Neutral at defaults: a unit multiplier and zero offset change nothing.
    if mult == 1.0 and offset == 0.0:
        return expression
    baseline_value = float(output_baseline)
    baseline = _format_driver_literal(baseline_value)
    core = _strip_redundant_outer_parentheses(expression)
    authored_excursion = None
    if not _is_positive_zero(baseline_value):
        prefix = f"{baseline}+"
        if core.startswith(prefix) and not _has_top_level_conditional(core):
            authored_excursion = core[len(prefix):]
    if authored_excursion is not None:
        shaped = f"({authored_excursion})"
    else:
        shaped = f"({core})"
        if not _is_positive_zero(baseline_value):
            shaped = f"({shaped}-{baseline})"
    if float(mult) != 1.0:
        shaped += f"*{_format_computed_literal(mult)}"
    constant = baseline_value + float(offset)
    if constant > 0.0:
        shaped += f"+{_format_computed_literal(constant)}"
    elif constant < 0.0:
        shaped += f"-{_format_computed_literal(abs(constant))}"
    return shaped


def fit_period_to_scene_loop(frame_length, desired_period):
    frame_length = max(1.0, float(frame_length))
    desired_period = max(1e-6, float(desired_period))
    repeats = max(1, int(round(frame_length / desired_period)))
    return frame_length / repeats


def evaluate_expression_at_frame(expression, template, frame, helper_expressions=None):
    code = _cached_compile(expression, "<driver_espresso_eval>")
    namespace = dict(SAFE_NAMESPACE)
    for variable in _driver_variable_specs(template):
        namespace[variable["name"]] = variable.get("preview_default", 0)
    namespace["frame"] = frame
    for helper in helper_expressions or ():
        namespace[helper["name"]] = eval(
            _cached_compile(helper["expression"], "<driver_espresso_helper_eval>"),
            {"__builtins__": {}}, namespace,
        )
    value = eval(code, {"__builtins__": {}}, namespace)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"non-numeric value at frame {frame!r}")
    return float(value)


def _format_literal(value):
    return _format_driver_literal(value)


_format_driver_literal = format_driver_literal
# Calculated values only - see format_computed_literal for why measured
# anchors must not go through it.
_format_computed_literal = format_computed_literal


def _is_positive_zero(value):
    value = float(value)
    return value == 0.0 and math.copysign(1.0, value) > 0.0


def _normalize_zero(value):
    """Collapse negative zero to positive zero before it reaches the compiler.

    -0.0 and 0.0 are numerically identical for every use here - subtracting
    either changes nothing - but -0.0 slips past the ``_is_positive_zero``
    guards. The compiler then wraps the term in redundant parentheses *and*
    appends ``-0``, spending five characters to subtract nothing. Against
    Blender's 255-character driver ceiling that is the difference between a
    template applying and failing outright.
    """
    value = float(value)
    return 0.0 if value == 0.0 else value


# Raw-expression budget for the modulo rewrite. The wrappers applied later
# (additive rest state, output multiplier, offset) can add ~50 characters, so
# leaving headroom below Blender's 255 limit keeps a template applying rather
# than trading correctness for speed it cannot use.
_FAST_PATH_LENGTH_BUDGET = 200


def _call(name, *args):
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=[])


class _PowToCall(ast.NodeTransformer):
    """Rewrite ``a ** b`` as ``pow(a, b)``."""

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            return _call("pow", node.left, node.right)
        return node


class _PowAndModToCalls(_PowToCall):
    """Also rewrite ``a % b`` as Python-equivalent ``fmod`` calls.

    ``fmod(a, b)`` alone is NOT ``a % b``: they disagree in sign whenever ``a``
    is negative, which happens every time a driver is evaluated before
    FRAME_START. ``fmod(fmod(a, b) + b, b)`` matches Python exactly for the
    positive divisors this catalogue uses (periods, durations, tick counts), and
    benchmarked within 2% of the naive form - so there is no reason to take the
    unsafe shortcut.
    """

    def visit_BinOp(self, node):
        node = super().visit_BinOp(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            inner = _call("fmod", node.left, node.right)
            return _call("fmod", ast.BinOp(left=inner, op=ast.Add(), right=node.right), node.right)
        return node


def _use_driver_fast_path(expression):
    """Swap ``**`` for ``pow()`` so Blender can use its fast driver evaluator.

    Blender compiles a driver to bytecode (``BLI_expr_pylike``) only when the
    expression stays inside a supported subset; anything else falls back to full
    Python evaluation, which is far slower and serialised on the GIL. Measured
    against Blender 5.1, the only unsupported things this catalogue used were
    ``**``, ``%`` and ``tanh`` - everything else (trig, floor, ternaries, clamp,
    lerp, comparisons) is already fast.

    Measured on 300 drivers over 120 frames: 354 ms on the Python path versus
    53 ms on the fast one, a 6.7x difference.

    Both rewrites preserve values exactly, so this is purely an evaluator-speed
    change. That also makes it safe to *skip*: the modulo rewrite spends about
    14 characters per ``%`` and some templates already sit within a couple of
    characters of Blender's 255-character driver ceiling, where being correct
    but slow beats not applying at all. ``**`` is always rewritten because
    ``pow()`` is no longer than the operator it replaces.

    Only expressions actually containing ``**`` or ``%`` are re-parsed, keeping
    the blast radius off templates that are already fast.
    """
    has_pow, has_mod = "**" in expression, "%" in expression
    if not (has_pow or has_mod):
        return expression

    def rendered(transformer):
        tree = transformer.visit(ast.parse(expression, mode="eval"))
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    try:
        without_mod = rendered(_PowToCall()) if has_pow else expression
        if not has_mod:
            return without_mod
        with_mod = rendered(_PowAndModToCalls())
    except (SyntaxError, ValueError, RecursionError):
        return expression
    return with_mod if len(with_mod) <= _FAST_PATH_LENGTH_BUDGET else without_mod


def _compact_generated_expression(expression):
    """Remove syntactically redundant spacing without joining keywords."""
    expression = _use_driver_fast_path(expression)
    expression = re.sub(r"\s*([(),+\-*/%<>=!])\s*", r"\1", expression)
    expression = re.sub(r"\b(if|else|and|or|not)\b", r" \1 ", expression)
    expression = re.sub(r"\s+", " ", expression).strip()

    number = r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"

    def fold_binary(match):
        left_text, operator, right_text = match.groups()
        integers = all("." not in item and "e" not in item.lower() for item in (left_text, right_text))
        if integers:
            left, right = int(left_text), int(right_text)
        else:
            left, right = float(left_text), float(right_text)
        value = left + right if operator == "+" else left - right
        folded = _format_driver_literal(value)
        return folded if len(folded) < len(match.group(0)) else match.group(0)

    # The lookbehind matters: this fold drops the parentheses it matched, which
    # is correct for a bare "(14-4)" but silently corrupts a CALL - "radians(14-4)"
    # would become "radians10". Only fold when the "(" is not a function's.
    pattern = rf"(?<![\w)])\(({number})([+-])({number})\)"
    for _index in range(3):
        compacted = re.sub(pattern, fold_binary, expression)
        if compacted == expression:
            break
        expression = compacted
    return expression


def _strip_redundant_outer_parentheses(expression):
    """Drop one pair only when it encloses the complete expression."""
    if not (expression.startswith("(") and expression.endswith(")")):
        return expression
    depth = 0
    for index, character in enumerate(expression):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and index != len(expression) - 1:
                return expression
    return expression[1:-1] if depth == 0 else expression


def _compact_localized_frame_arithmetic(expression):
    """Flatten exact left-associative shifts such as ((frame+12)-1)."""
    number = r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
    pattern = rf"\(\(frame([+-]{number})\)([+-]{number})\)"
    expression = re.sub(
        pattern,
        lambda match: f"(frame{match.group(1)}{match.group(2)})",
        expression,
    )

    def combine_offsets(match):
        total = int(match.group(1)) + int(match.group(2))
        if total == 0:
            return "frame"
        return f"frame{total:+d}"

    return re.sub(r"frame([+-]\d+)([+-]\d+)", combine_offsets, expression)


def _split_trailing_numeric_constant(expression):
    """Return ``dynamic, constant`` for a top-level trailing numeric term."""
    if _strip_redundant_outer_parentheses(expression) != expression:
        return expression, None
    try:
        root = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return expression, None
    if not isinstance(root, ast.BinOp) or not isinstance(root.op, (ast.Add, ast.Sub)):
        return expression, None
    # The sign in `value+-0` belongs to a unary literal, not the outer
    # addition. Regex splitting left a dangling '+' in valid eye recipes.
    try:
        constant = ast.literal_eval(root.right)
    except (ValueError, TypeError, SyntaxError):
        return expression, None
    if type(constant) not in (int, float):
        return expression, None
    dynamic = ast.get_source_segment(expression, root.left)
    if dynamic is None:
        return expression, None
    return dynamic, float(constant) * (-1.0 if isinstance(root.op, ast.Sub) else 1.0)


def _split_affine_output_bias(expression):
    """Extract constant output bias through scalar gain, never nonlinear ops."""
    def number(node):
        try:
            value = ast.literal_eval(node)
            return float(value) if type(value) in (int, float) else None
        except (ValueError, TypeError, SyntaxError):
            return None

    def split(node):
        if not isinstance(node, ast.BinOp):
            return node, 0.0
        right = number(node.right)
        if right is None or not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            return node, 0.0
        dynamic, bias = split(node.left)
        if isinstance(node.op, ast.Mult):
            return ast.BinOp(left=dynamic, op=ast.Mult(), right=node.right), bias * right
        return dynamic, bias + right * (-1 if isinstance(node.op, ast.Sub) else 1)

    root = ast.parse(expression, mode="eval").body
    dynamic, bias = split(root)
    return _compact_generated_expression(ast.unparse(dynamic)), bias


def _has_top_level_conditional(expression):
    try:
        return isinstance(ast.parse(expression, mode="eval").body, ast.IfExp)
    except SyntaxError:
        return True


def _apply_output_clamp(expression, state):
    """Hold an additive result inside an artist-set min/max.

    Wraps the FINISHED value, not the excursion: the artist sets the limits in
    the target's own units ("stay between 0 and 20"), not as a distance from
    rest. Templates that swing both ways around the resting value are the point
    of this - a wobble on a light strength of 10 can be told to stay within
    0..20 instead of dipping negative.
    """
    # Wrapped catalogue expressions can cross the RNA length limit even
    # when their unwrapped form fits. Decimal leading zeros are optional;
    # remove them only when needed, without rewriting custom string data.
    if len(expression) > MAX_DRIVER_EXPRESSION_LENGTH - 2 and not any(quote in expression for quote in ('"', "'")):
        try:
            # Rest wrapping adds protective parentheses at each stage.
            # Re-render their syntax tree to remove only redundant grouping.
            expression = _compact_generated_expression(
                ast.unparse(ast.parse(expression, mode="eval")),
            )
        except (SyntaxError, ValueError, RecursionError):
            pass  # Validation reports the original malformed expression.
        expression = re.sub(r"(?<![\w.])0\.(?=\d)", ".", expression)
        expression = re.sub(r"(?<=\d)([eE][+-]?)0+(?=\d)", r"\1", expression)
    low = state.get("clamp_min")
    high = state.get("clamp_max")
    if low is None or high is None:
        return expression
    return (
        f"max({_format_driver_literal(float(low))},"
        f"min({_format_driver_literal(float(high))},{expression}))"
    )


def capture_rest_start_state(
    expression, template, scene, mode, rest_value, snapshot_frame=None,
    output_baseline=None, additive_profile=None, clamp_range=None,
):
    mode = mode or REST_START_OFF
    if mode == REST_START_OFF:
        return {}
    snapshot_frame = int(
        snapshot_frame
        if snapshot_frame is not None
        else getattr(scene, "frame_current", getattr(scene, "frame_start", 1))
    )
    rest_value = _normalize_zero(rest_value)
    if mode == REST_START_ADDITIVE:
        profile = additive_profile or resolve_additive_profile(template)
        origin = find_additive_origin(
            expression,
            template,
            profile,
            float(output_baseline if output_baseline is not None else 0.0),
            int(getattr(scene, "frame_start", 1)),
            int(getattr(scene, "frame_end", 250)),
        )
        state = {
            "mode": REST_START_ADDITIVE,
            "rest_value": rest_value,
            "snapshot_frame": snapshot_frame,
            "output_baseline": float(output_baseline if output_baseline is not None else 0.0),
            "additive_profile": profile,
            "phase_origin": origin,
            "scene_start": int(getattr(scene, "frame_start", 1)),
        }
        if clamp_range is not None:
            low, high = (float(clamp_range[0]), float(clamp_range[1]))
            # Tolerate the pair arriving the wrong way round rather than
            # producing max(20,min(0,x)), which pins the output to a constant.
            state["clamp_min"], state["clamp_max"] = min(low, high), max(low, high)
        if profile in {"BOUNDED_MIN", "BOUNDED_MAX"}:
            origin_value, sampled_min, sampled_max = additive_origin_values(
                expression,
                template,
                profile,
                float(output_baseline if output_baseline is not None else 0.0),
                int(getattr(scene, "frame_start", 1)),
                int(getattr(scene, "frame_end", 250)),
                origin,
            )
            if profile == "BOUNDED_MIN":
                actual_span = max(1e-12, sampled_max - origin_value)
                semantic_span = max(0.0, sampled_max - float(output_baseline or 0.0))
            else:
                actual_span = max(1e-12, origin_value - sampled_min)
                semantic_span = max(0.0, float(output_baseline or 0.0) - sampled_min)
            state["bounded_gain"] = semantic_span / actual_span if semantic_span else 1.0
        return state
    if mode == REST_START_OFFSET_ONLY:
        profile = additive_profile or resolve_additive_profile(template)
        if profile in {"BOUNDED_MIN", "BOUNDED_MAX"}:
            semantic_bound = output_baseline
            if semantic_bound is None:
                semantic_bound = resolve_output_baseline(template, default_values(template))
            semantic_bound = float(semantic_bound)
            return {
                "mode": REST_START_OFFSET_ONLY,
                "offset": rest_value + semantic_bound,
                "additive_profile": profile,
                "output_baseline": semantic_bound,
            }
        baseline = evaluate_expression_at_frame(expression, template, snapshot_frame)
        return {
            "mode": REST_START_OFFSET_ONLY,
            "offset": rest_value - baseline,
        }
    return {}


def wrap_expression_with_rest_state(expression, template, state):
    if not state:
        return expression
    mode = state.get("mode", REST_START_OFF)
    if mode == REST_START_OFF:
        return expression
    if mode == REST_START_ADDITIVE:
        snapshot_frame = int(state.get("snapshot_frame", 1))
        rest_value = float(state.get("rest_value", 0.0))
        profile = state.get("additive_profile") or resolve_additive_profile(template)
        if profile == "STATIC_UTILITY":
            return expression
        rest_literal = _format_driver_literal(rest_value)
        delta = None
        if profile == "INPUT_RELATIVE":
            baseline = _normalize_zero(state.get("output_baseline", 0.0))
            excursion = f"({expression})"
            if not _is_positive_zero(baseline):
                excursion += f"-{_format_driver_literal(baseline)}"
            delta = excursion
            active = excursion if _is_positive_zero(rest_value) else f"{rest_literal}+{excursion}"
        else:
            origin = float(state.get("phase_origin", 0.0))
            shift = origin - snapshot_frame
            scene_start = float(state.get("scene_start", 1.0))
            frame_count = len(re.findall(r"\bframe\b", expression))
            localized = expression
            optimized = False
            if abs(origin - scene_start) <= 1e-12 and frame_count:
                start_literal = _format_driver_literal(scene_start)
                localized, replacements = re.subn(
                    rf"\bframe\s*-\s*{re.escape(start_literal)}\b",
                    f"frame-{snapshot_frame}",
                    expression,
                )
                optimized = replacements == frame_count
            if optimized:
                pass
            elif abs(shift) <= 1e-12:
                localized = expression
            elif shift > 0:
                localized = re.sub(r"\bframe\b", f"(frame+{_format_driver_literal(shift)})", expression)
            else:
                localized = re.sub(r"\bframe\b", f"(frame-{_format_driver_literal(abs(shift))})", expression)
            localized = _compact_localized_frame_arithmetic(localized)
            reference = _normalize_zero(
                evaluate_expression_at_frame(expression, template, origin)
            )
            if profile in {
                "BOUNDED_MIN", "BOUNDED_MAX", "CENTERED_ZERO",
                "ONE_SHOT_REST", "ELAPSED_TIME",
            }:
                dynamic_localized, trailing_constant = _split_trailing_numeric_constant(localized)
                if len(localized) > 190:
                    # Stress-sized channels can carry a bias inside an
                    # amplitude multiplier and another outside it. Both
                    # cancel under rest subtraction; collect them before
                    # generating the final bounded-length driver string.
                    dynamic_localized, trailing_constant = _split_affine_output_bias(localized)
                if trailing_constant is not None:
                    # Remove a shared output bias before subtracting the
                    # sampled reference, including nonzero excursions.
                    # (motion + bias) - reference == motion - (reference-bias).
                    # This also prevents large biases wasting driver space.
                    localized = dynamic_localized
                    reference = _normalize_zero(reference - trailing_constant)
            reference_literal = _format_computed_literal(reference)
            if profile == "BOUNDED_MIN":
                gain_value = float(state.get("bounded_gain", 1.0))
                localized = _strip_redundant_outer_parentheses(localized)
                excursion = f"max(0,({localized})"
                if reference > 0.0:
                    excursion += f"-{reference_literal}"
                elif reference < 0.0:
                    excursion += f"+{_format_driver_literal(abs(reference))}"
                excursion += ")"
                if gain_value != 1.0:
                    excursion += f"*{_format_computed_literal(gain_value)}"
                delta = excursion
                active = excursion if _is_positive_zero(rest_value) else f"{rest_literal}+{excursion}"
            elif profile == "BOUNDED_MAX":
                gain_value = float(state.get("bounded_gain", 1.0))
                if _is_positive_zero(reference):
                    excursion = f"max(0,-({localized}))"
                else:
                    excursion = f"max(0,{reference_literal}-({localized}))"
                if gain_value != 1.0:
                    excursion += f"*{_format_computed_literal(gain_value)}"
                delta = f"-{excursion}"
                active = f"-{excursion}" if _is_positive_zero(rest_value) else f"{rest_literal}-{excursion}"
            elif profile == "RELATIVE_SCALE":
                # Scale kernels are authored around a non-zero multiplicative
                # rest (normally 1). The epsilon only protects malformed custom
                # expressions; catalogue contracts never rely on it.
                denominator = max(1e-12, abs(reference))
                numerator = f"({localized})"
                if rest_value != 1.0:
                    numerator = f"{rest_literal}*{numerator}"
                active = numerator if denominator == 1.0 else f"{numerator}/{_format_driver_literal(denominator)}"
            else:
                if _is_positive_zero(reference) and not _has_top_level_conditional(localized):
                    excursion = localized
                else:
                    excursion = f"({localized})"
                if not _is_positive_zero(reference):
                    excursion += f"-{reference_literal}"
                delta = excursion
                active = excursion if _is_positive_zero(rest_value) else f"{rest_literal}+{excursion}"
        if _is_positive_zero(rest_value):
            return _apply_output_clamp(f"frame>{snapshot_frame} and {active}", state)
        if rest_value != 0.0 and delta is not None:
            return _apply_output_clamp(
                f"{rest_literal}+(frame>{snapshot_frame} and {delta})", state
            )
        return _apply_output_clamp(
            f"{rest_literal} if frame<={snapshot_frame} else {active}", state
        )
    if mode == REST_START_OFFSET_ONLY:
        offset = float(state.get("offset", 0.0))
        if abs(offset) <= 1e-12:
            return expression
        core = f"({_strip_redundant_outer_parentheses(expression)})"
        if offset > 0.0:
            return f"{core}+{_format_literal(offset)}"
        return f"{core}-{_format_literal(abs(offset))}"
    return expression


def _evaluate_baseline_contract(explicit, token_values):
    if isinstance(explicit, (int, float)):
        return float(explicit)
    if not isinstance(explicit, str) or not explicit.strip():
        return None
    baseline_expression = explicit
    for token in sorted(token_values, key=len, reverse=True):
        baseline_expression = re.sub(
            r"\b" + re.escape(token) + r"\b", str(token_values[token]),
            baseline_expression,
        )
    try:
        value = eval(
            compile(baseline_expression, "<driver_espresso_baseline>", "eval"),
            {"__builtins__": {}}, dict(SAFE_NAMESPACE),
        )
        return float(value)
    except Exception:
        return None


def resolve_output_baseline(template, token_values, channel=None):
    """Resolve the value representing no visible excursion for a template."""
    from ...catalogue import catalogue_contracts

    explicit = catalogue_contracts.explicit_baseline(template, channel)
    evaluated = _evaluate_baseline_contract(explicit, token_values)
    if evaluated is not None:
        return evaluated
    if template.get("data_path") == "scale":
        return 1.0
    # START and A are deliberately absent: both have represented timeline/input
    # values as well as output anchors. Current legitimate uses are explicit in
    # catalogue_contracts.BASELINE_OVERRIDES, preventing the original collision
    # from silently returning in a future recipe.
    for token in ("MIN", "CENTER", "BASE", "REST", "LOW", "OUT_MIN"):
        if token in token_values:
            return float(token_values[token])
    for token in ("START_DEG", "OPEN_DEG"):
        if token in token_values:
            return math.radians(float(token_values[token]))
    return 0.0


def resolve_additive_profile(template, channel=None):
    from ...catalogue import catalogue_contracts

    return catalogue_contracts.resolve_additive_profile(template, channel)


@functools.lru_cache(maxsize=512)
def _search_additive_origin(expression, template_id, profile, output_baseline, start, end, variables):
    namespace = dict(SAFE_NAMESPACE)
    namespace.update(dict(variables))
    code = compile(expression, "<driver_espresso_additive_origin>", "eval")
    # Search a long deterministic horizon. BOUNDED_MIN is clamped against any
    # later undershoot by the wrapper, so this chooses a useful start phase
    # without pretending every mixed-frequency kernel has a short exact period.
    horizon = max(2048, abs(end - start) * 4)
    best_frame = float(start)
    best_score = float("inf")
    sampled_min = float("inf")
    sampled_max = float("-inf")
    for frame in range(start, start + horizon + 1):
        namespace["frame"] = frame
        try:
            value = float(eval(code, {"__builtins__": {}}, namespace))
        except Exception:
            continue
        sampled_min = min(sampled_min, value)
        sampled_max = max(sampled_max, value)
        if profile == "BOUNDED_MIN":
            score = value
        elif profile == "BOUNDED_MAX":
            score = -value
        else:
            score = abs(value - output_baseline)
        if score < best_score:
            best_score = score
            best_frame = float(frame)
    return best_frame, sampled_min, sampled_max


def find_additive_origin(expression, template, profile, output_baseline, scene_start, scene_end):
    if profile in {"ONE_SHOT_REST", "ELAPSED_TIME", "INPUT_RELATIVE", "STATIC_UTILITY"}:
        return float(scene_start)
    variables = tuple(
        sorted(
            (item["name"], float(item.get("preview_default", 0.0)))
            for item in template.get("requires_driver_variables", [])
        )
    )
    return _search_additive_origin(
        expression, template.get("id", ""), profile, float(output_baseline),
        int(scene_start), int(scene_end), variables,
    )[0]


def additive_origin_values(
    expression, template, profile, output_baseline, scene_start, scene_end, origin,
):
    variables = tuple(
        sorted(
            (item["name"], float(item.get("preview_default", 0.0)))
            for item in template.get("requires_driver_variables", [])
        )
    )
    _best, sampled_min, sampled_max = _search_additive_origin(
        expression, template.get("id", ""), profile, float(output_baseline),
        int(scene_start), int(scene_end), variables,
    )
    origin_value = evaluate_expression_at_frame(expression, template, origin)
    return origin_value, sampled_min, sampled_max


def _prepare_expression_tokens(template, values, scene):
    """Resolve channel-independent parameters once for an expression build."""
    token_values = {}
    warnings = []
    merged = default_values(template)
    merged.update(advanced_defaults(template, scene))
    merged.update(values or {})

    for param in template.get("params", []):
        token = param["token"]
        value = coerce_value(param, merged.get(token, param.get("default", 0.0)))
        if param.get("type") in {"STRING", "ENUM"}:
            continue
        if param.get("type") == "COLOR":
            # A swatch is three numbers wearing one control. Only the components
            # are ever named by an expression, so ONLY those enter token_values -
            # leaving the tuple in would reach the literal formatter, which
            # rightly has no idea what to print for it. Expanding here (rather
            # than only in the UI) lets a template build straight from defaults.
            for component_token, component in zip(colour_component_tokens(token), value):
                token_values[component_token] = float(component)
            continue
        token_values[token] = value

    for control in template.get("advanced_controls", []):
        token = control["token"]
        value = coerce_value(control, merged.get(token, control.get("default", 0.0)))
        token_values[token] = value

    # Only these tokens were chosen by the artist - a slider they dragged or a
    # preset they picked. Everything merged in after this point (frame
    # constants, derives output) is machine-computed and must not be rounded
    # for "readability": nobody reads FRAME_LEN or a template's internal rate
    # constant on a UI, and rounding one has already caused two real bugs -
    # a divide-by-zero guard erased and a clock hand's tick
    # rate drifting audibly over a long scene. A template that DOES want a
    # short literal from a derived value (e.g. a pre-converted radian angle,
    # to stay under the 255-character driver limit) can round(...) inside its
    # own derives formula - opt-in per template, not silently system-wide.
    _artist_chosen_tokens = frozenset(token_values)

    from ...catalogue import catalogue_contracts

    if catalogue_contracts.has_ordered_output_range(template):
        if token_values["MAX"] < token_values["MIN"]:
            token_values["MIN"], token_values["MAX"] = token_values["MAX"], token_values["MIN"]
            warnings.append("Minimum and maximum were swapped to keep the output range ordered.")

    if (
        template.get("frame_mode") == FIXED_PERIOD
        and token_values.get("ADV_LOOP_FIT")
        and "PERIOD" in token_values
    ):
        token_values["PERIOD"] = fit_period_to_scene_loop(
            frame_constants(template, scene)["FRAME_LEN"],
            token_values["PERIOD"],
        )

    token_values.update(frame_constants(template, scene))

    if "ADV_OVERRIDE_FPS" in token_values and "ADV_TIMING_FPS" in token_values:
        token_values["EFFECTIVE_FPS"] = (
            token_values["ADV_TIMING_FPS"]
            if token_values["ADV_OVERRIDE_FPS"]
            else token_values["FPS"]
        )
        token_values["FPS_SCALE"] = 24.0 / max(1e-9, token_values["EFFECTIVE_FPS"])

    # Friendly parameters resolve here: after the frame constants exist (they may
    # be needed by the formulas) and before any token is substituted. A no-op for
    # every template that declares no `derives`.
    token_values.update(resolve_derived_tokens(template, token_values))
    warnings.extend(template_checks(template, token_values))

    # Every token now exists (parameters, frame constants, derived values), and
    # nothing has consumed them yet. Rounding here - once - keeps the emitted
    # expression, the semantic baseline, and the output shaping all agreeing on
    # the same numbers. Rounding later, or in only one of those places, would
    # let them disagree by a hair and reintroduce an apply-time jump.
    token_values = {
        token: (round_parameter(value) if token in _artist_chosen_tokens else value)
        for token, value in token_values.items()
    }

    # AFTER rounding, deliberately. This guard stops an input range of zero
    # width from emitting a literal `/0`, which errors in Blender and stops the
    # property animating. It runs after the rounding pass, because rounding
    # would undo it: IN_MAX is an artist-chosen token, so a nudged value of 1 +
    # 1e-6 would round straight back to 1 and the divide-by-zero would return.
    # Only INT ranges survived, because their step of 1 is too big to round
    # away.
    #
    # Running last also catches the case the old placement could never see: an
    # artist entering 1.0 and 1.000001 has a valid range going in, and it is
    # ROUNDING that collapses the two to the same number.
    if "IN_MIN" in token_values and "IN_MAX" in token_values:
        if token_values["IN_MAX"] <= token_values["IN_MIN"]:
            specs = {param["token"]: param for param in template.get("params", [])}
            step = 1 if specs.get("IN_MAX", {}).get("type") == "INT" else max(
                1e-6,
                abs(float(token_values["IN_MIN"])) * 1e-6,
            )
            token_values["IN_MAX"] = token_values["IN_MIN"] + step
            warnings.append("Input maximum was adjusted to stay above input minimum.")

    for constraint in template.get("constraints", ()):
        if constraint.get("kind") != "phase_budget":
            continue
        total_token = constraint["total"]
        required = sum(max(0, token_values.get(token, 0)) for token in constraint["parts"])
        if token_values.get(total_token, 0) < required:
            token_values[total_token] = required
            warnings.append(constraint["message"].format(required=int(required)))

    return token_values, warnings


def _emit_expression(
    template, token_values, warnings, scene, include_details=False,
    prepared_literals=None,
):
    """Substitute one channel expression from already-resolved tokens."""
    # Each channel may declare a different semantic baseline, and the loop
    # guard below appends a warning. Work on private copies so a multi-channel
    # build cannot leak either channel-specific detail into its siblings.
    token_values = dict(token_values)
    warnings = list(warnings)

    output_baseline = resolve_output_baseline(template, token_values)

    if "LOOP_END" in token_values and "LOOP_START" in token_values:
        if token_values["LOOP_END"] <= token_values["LOOP_START"]:
            token_values["LOOP_END"] = token_values["LOOP_START"] + 1
            warnings.append("Loop end was adjusted to stay after loop start.")

    expression = template["expression"]
    if "ADV_DELAY" in token_values or "ADV_ADVANCE" in token_values:
        expression = apply_advanced_time(expression, token_values)
    # One compiled alternation instead of one re.sub per token. The alternation
    # is built LONGEST-FIRST, exactly as the old loop was ordered, so a short
    # token can never consume part of a longer one. Verified byte-identical
    # across 665 build cases. The pattern is cached on the token NAMES (fixed
    # per template); only the values change between calls.
    pattern = _token_pattern(tuple(sorted(token_values, key=len, reverse=True)))
    if pattern is not None:
        literals = prepared_literals or {
            token: _format_driver_literal(value)
            for token, value in token_values.items()
        }
        expression = pattern.sub(lambda m: literals[m.group(0)], expression)
    expression = _compact_generated_expression(expression)
    # Only these two calls can change the expression after the first
    # compaction, so when neither fires the second pass is pure cost
    # (_compact_generated_expression is idempotent). Verified output-identical
    # across 1,561 build cases with advanced controls both off and on.
    wrapped = False
    if any(token in token_values for token in ("ADV_DELAY", "ADV_START_FRAME", "ADV_END_FRAME")):
        candidate = wrap_activation_window(expression, token_values, scene, output_baseline)
        if candidate != expression:
            expression = candidate
            wrapped = True
    if "ADV_MULT" in token_values or "ADV_OFFSET" in token_values:
        candidate = wrap_output_shaping(expression, token_values, output_baseline)
        if candidate != expression:
            expression = candidate
            wrapped = True
    if wrapped:
        expression = _compact_generated_expression(expression)

    unresolved = sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", expression)))
    if unresolved:
        warnings.append("Unresolved token(s): " + ", ".join(unresolved))
    if include_details:
        return expression, warnings, {
            "output_baseline": output_baseline,
            "additive_profile": resolve_additive_profile(template),
        }
    return expression, warnings


def build_expression(template, values, scene, include_details=False):
    token_values, warnings = _prepare_expression_tokens(template, values, scene)
    return _emit_expression(
        template, token_values, warnings, scene, include_details=include_details,
    )


def build_template_expressions(template, values, scene):
    """Build every expression owned by a template through the same pipeline.

    The synthetic primary channel keeps historic single-expression templates on
    the exact existing builder. Explicit multi channels differ only in the raw
    expression selected before friendly parameters and advanced controls resolve.
    """
    from ...catalogue import templates as template_catalogue

    token_values, common_warnings = _prepare_expression_tokens(template, values, scene)
    channels = template_catalogue.template_channels(template)
    # Channel expressions share every resolved parameter literal. Formatting
    # Blender float values is deliberately exact, but comparatively expensive;
    # do it once for a motion set instead of once per axis. An invalid loop
    # range is the sole post-prepare token mutation, so keep that rare case on
    # the per-channel path where its adjusted literal is guaranteed correct.
    prepared_literals = None
    loop_invalid = (
        "LOOP_END" in token_values
        and "LOOP_START" in token_values
        and token_values["LOOP_END"] <= token_values["LOOP_START"]
    )
    if len(channels) > 1 and not loop_invalid:
        prepared_literals = {
            token: _format_driver_literal(value)
            for token, value in token_values.items()
        }
    helper_expressions = []
    built = []
    for channel in channels:
        channel_template = dict(template)
        channel_template["expression"] = channel["expression"]
        channel_template["data_path"] = channel.get("data_path", "")
        channel_template["index"] = channel.get("index", -1)
        channel_template["_channel_id"] = channel.get("id", "primary")
        channel_template["additive_profile"] = channel.get("additive_profile")
        if channel.get("output_baseline") is not None:
            channel_template["output_baseline"] = channel["output_baseline"]
        expression, warnings, details = _emit_expression(
            channel_template, token_values, common_warnings, scene,
            include_details=True, prepared_literals=prepared_literals,
        )
        item = dict(channel)
        item["expression"] = expression
        item["warnings"] = list(warnings)
        if channel.get("driver_expression"):
            driver_template = dict(channel_template)
            driver_template["expression"] = channel["driver_expression"]
            driver_expression, driver_warnings, _driver_details = _emit_expression(
                driver_template, token_values, common_warnings, scene,
                include_details=True, prepared_literals=prepared_literals,
            )
            item["driver_expression"] = driver_expression
            item["warnings"].extend(driver_warnings)
        item["output_baseline"] = details["output_baseline"]
        item["additive_profile"] = resolve_additive_profile(template, channel)
        if helper_expressions:
            item["internal_helpers"] = helper_expressions
        built.append(item)
    return built


def _driver_variable_specs(template):
    """Variables available while validating both public and helper drivers."""
    return [
        *list((template or {}).get("requires_driver_variables", [])),
        *list((template or {}).get("managed_driver_variables", [])),
    ]


def _names_in_expression(expression):
    tree = ast.parse(expression, mode="eval")
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def validate_expression(expression, template=None, scene=None):
    try:
        code = compile(expression, "<driver_espresso>", "eval")
    except SyntaxError as exc:
        return False, f"Syntax error: {exc.msg}"

    required = {var["name"] for var in _driver_variable_specs(template)}
    try:
        names = _names_in_expression(expression)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc.msg}"

    unknown = sorted(names - ALLOWED_NAMES - required)
    if unknown:
        return False, "Unknown name(s): " + ", ".join(unknown)

    start = int(getattr(scene, "frame_start", 1)) if scene is not None else 1
    end = int(getattr(scene, "frame_end", 120)) if scene is not None else 120
    frames = [start, (start + end) // 2, end]
    namespace = dict(SAFE_NAMESPACE)
    for var in _driver_variable_specs(template):
        namespace[var["name"]] = var.get("preview_default", 0)
    try:
        for frame in frames:
            namespace["frame"] = frame
            value = eval(code, {"__builtins__": {}}, namespace)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                return False, f"Non-finite value at frame {frame}"
    except Exception as exc:
        return False, f"Runtime error: {exc}"
    return True, ""


def validate_driver_expression(expression, template=None, scene=None):
    """Validate the exact final string that will be stored by Blender."""
    if len(expression) > MAX_DRIVER_EXPRESSION_LENGTH:
        return False, (
            f"Expression is {len(expression)} characters; Blender drivers allow "
            f"at most {MAX_DRIVER_EXPRESSION_LENGTH}. Nothing was applied."
        )
    return validate_expression(expression, template, scene)


def assign_driver_expression(driver, expression, template=None, scene=None):
    """Store a complete scripted expression or restore the previous driver."""
    valid, message = validate_driver_expression(expression, template, scene)
    if not valid:
        return False, message

    previous_type = getattr(driver, "type", None)
    previous_expression = getattr(driver, "expression", "")
    try:
        driver.type = "SCRIPTED"
        driver.expression = expression
        stored = driver.expression
        if stored != expression:
            raise ValueError(
                f"Blender stored {len(stored)} of {len(expression)} characters."
            )
        valid, message = validate_expression(stored, template, scene)
        if not valid:
            raise ValueError(message)
    except Exception as exc:
        try:
            driver.type = "SCRIPTED"
            driver.expression = previous_expression
            if previous_type is not None:
                driver.type = previous_type
        except Exception:
            pass
        return False, f"Driver expression was not applied: {exc}"
    return True, "Driver expression applied."


def read_variant_memory(props):
    try:
        data = json.loads(props.variant_mem or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_variant_memory(props, data):
    props.variant_mem = json.dumps(data, sort_keys=True)


def read_template_memory(props):
    try:
        data = json.loads(props.template_mem or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_template_memory(props, data):
    props.template_mem = json.dumps(data, sort_keys=True)


def _driver_fcurve_from_context(ctx):
    # In DRIVERS mode context.selected_editable_fcurves is empty; use active instead.
    active = getattr(ctx, "active_editable_fcurve", None)
    candidates = ([active] if active is not None else []) + list(
        getattr(ctx, "selected_editable_fcurves", None) or []
    )
    seen = set()
    for fcurve in candidates:
        if id(fcurve) in seen:
            continue
        seen.add(id(fcurve))
        driver = getattr(fcurve, "driver", None)
        if driver is not None:
            return fcurve, driver
    return None, None


def get_active_driver_fcurve(context):
    # Drivers-Editor-selection-based detector only. Callers outside this
    # module should use get_target_driver_fcurve below, which layers the
    # explicit Driver Target picker on top of this and falls back to it.
    fcurve, driver = _driver_fcurve_from_context(context)
    if driver is not None:
        return fcurve, driver, ""

    # Driver Espresso's panel is usually drawn from the 3D Viewport sidebar,
    # whose context never carries active_editable_fcurve /
    # selected_editable_fcurves at all — those only exist inside a Graph
    # Editor or Drivers Editor context. Scan any Graph Editor area open on
    # this screen directly via a context override so selection made there is
    # still picked up regardless of which editor's N-panel is showing.
    screen = getattr(context, "screen", None)
    if screen is not None:
        for area in screen.areas:
            if area.type != "GRAPH_EDITOR":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with context.temp_override(area=area, region=region):
                    fcurve, driver = _driver_fcurve_from_context(context)
            except Exception:
                continue
            if driver is not None:
                return fcurve, driver, ""

    return None, None, "Select a driver F-Curve in the Graph Editor or Drivers Editor."


def resolve_addon_package(package_name):
    """Return the addon root for source installs and bl_ext installs.

    Source: ``<package>.engine.expression`` -> ``<package>``.
    Installed: ``bl_ext.<repository>.<addon>.engine.expression`` ->
    ``bl_ext.<repository>.<addon>``. One ``rpartition`` is not enough; it
    stops at ``<package>.engine`` and preference lookup returns None.
    """
    parts = [part for part in str(package_name or "").split(".") if part]
    if not parts:
        return ""
    if parts[0] == "bl_ext" and len(parts) >= 3:
        return ".".join(parts[:3])
    return parts[0]


# Preferences are registered against the addon root, not this sub-package.
ADDON_PACKAGE = resolve_addon_package(__package__)


def addon_preferences(context):
    addons = getattr(getattr(context, "preferences", None), "addons", None)
    if addons is None:
        return None
    addon = addons.get(ADDON_PACKAGE)
    return getattr(addon, "preferences", None) if addon else None


def driver_target_picker_enabled(context):
    return bool(getattr(addon_preferences(context), "enable_driver_target_picker", True))


def list_object_driver_fcurves(obj):
    """Every driven F-Curve on obj.animation_data.drivers.

    Scoped to the object's own animation_data only: this covers object
    transforms, custom properties, and pose-bone-driven properties (bone data
    paths live under the object's own animation_data, not the armature
    data-block). Shape-key and material node-tree drivers live on separate ID
    blocks and are out of scope for this feature.
    """
    anim = getattr(obj, "animation_data", None)
    if anim is None:
        return []
    return [fcurve for fcurve in anim.drivers if getattr(fcurve, "driver", None) is not None]


_INDEXED_DATA_PATH_RE = re.compile(r'^(.*)\[(-?\d+)\]$')


def resolve_variable_ui_binding(target):
    """For a driver-variable SINGLE_PROP target, return ``(id_data, prop_path,
    index)`` suitable for ``layout.prop(id_data, prop_path, index=index)`` (or
    without ``index`` when it's ``-1``), or ``None`` if the target isn't wired
    to anything the UI can bind a live control to.

    Handles the common shapes: a custom property (``["name"]`` — what Setup
    Missing Variables creates), an indexed array component (``location[0]``),
    or a plain scalar attribute.
    """
    id_data = getattr(target, "id", None)
    data_path = getattr(target, "data_path", "") or ""
    if id_data is None or not data_path:
        return None
    if data_path.startswith('["') and data_path.endswith('"]'):
        return id_data, data_path, -1
    match = _INDEXED_DATA_PATH_RE.match(data_path)
    if match:
        return id_data, match.group(1), int(match.group(2))
    return id_data, data_path, -1


def set_active_driver_target(context, owner_or_descriptor, data_path="", array_index=-1):
    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    if props is None or owner_or_descriptor is None:
        return
    if isinstance(owner_or_descriptor, dict):
        record = dict(owner_or_descriptor)
    else:
        owner = owner_or_descriptor
        record = {
            "id_type": owner.__class__.__name__, "id_name": getattr(owner, "name", ""),
            "owner_path": "", "data_path": data_path, "index": int(array_index),
        }
        try:
            from ...apply import target_memory
            record.update(target_memory.serialize_target(owner, data_path, array_index))
        except (ImportError, AttributeError):
            pass
    props.active_target_owner = record.get("id_name", "")
    props.active_target_id_type = record.get("id_type", "")
    props.active_target_id_name = record.get("id_name", "")
    props.active_target_owner_path = record.get("owner_path", "")
    props.active_target_data_path = record.get("data_path", data_path)
    props.active_target_index = int(record.get("index", array_index))


def clear_active_driver_target(props):
    props.active_target_owner = ""
    props.active_target_id_type = ""
    props.active_target_id_name = ""
    props.active_target_owner_path = ""
    props.active_target_data_path = ""
    props.active_target_index = -1


def get_target_driver_fcurve(context, fcurves=None, targets=None):
    """Resolve "the driver to act on", preferring an explicit pick made via
    the Driver Target picker over Drivers-Editor selection state.

    Priority order:
    1. If the picker feature is disabled (addon preference), skip straight to
       get_active_driver_fcurve — a full bypass, unchanged legacy behavior.
    2. An explicit pick (active_target_owner/data_path/index on
       espresso_props) that still resolves to a real driver on the current
       active object.
    3. If the active object has exactly one driver, there's nothing
       ambiguous about it — use it. This covers switching to a different
       object after a pick was made elsewhere: the stored pick (e.g. an
       owner name from a previously-selected object) won't match step 2, but
       there's still only one sane candidate on the object in front of you.
       With 2+ drivers this is skipped and an explicit picker-row click is
       still required, since guessing which one risks acting on the wrong
       property.
    4. Fall back to get_active_driver_fcurve (Drivers-Editor selection
       detection, unchanged).

    `fcurves`, if given, is used as the candidate list for steps 2-3 instead
    of re-enumerating the active object's drivers — callers that already
    have that list (e.g. a panel drawing every driver as a row) can pass it
    in to avoid scanning the object's drivers twice per redraw.
    """
    if not driver_target_picker_enabled(context):
        return get_active_driver_fcurve(context)

    props = getattr(getattr(context, "scene", None), "espresso_props", None)
    active_obj = getattr(context, "active_object", None)
    if props is not None and getattr(props, "active_target_id_type", ""):
        try:
            from ...apply import driver_targets
            descriptor = {
                "id_type": props.active_target_id_type,
                "id_name": props.active_target_id_name,
                "owner_path": props.active_target_owner_path,
                "data_path": props.active_target_data_path,
                "index": props.active_target_index,
            }
            fcurve = driver_targets.resolve_driver(descriptor)
            if fcurve is not None:
                return fcurve, fcurve.driver, ""
        except (ImportError, AttributeError):
            pass
    if targets is not None:
        try:
            from ...apply import driver_targets
            live = [driver_targets.resolve_driver(item) for item in targets]
            live = [fcurve for fcurve in live if fcurve is not None]
            if len(live) == 1:
                return live[0], live[0].driver, ""
        except (ImportError, AttributeError):
            pass
    candidates = None
    if active_obj is not None:
        candidates = fcurves if fcurves is not None else list_object_driver_fcurves(active_obj)

    if props is not None and candidates is not None and props.active_target_owner == active_obj.name:
        for fcurve in candidates:
            if fcurve.data_path == props.active_target_data_path and fcurve.array_index == props.active_target_index:
                return fcurve, fcurve.driver, ""

    if candidates is not None and len(candidates) == 1:
        fcurve = candidates[0]
        return fcurve, fcurve.driver, ""

    return get_active_driver_fcurve(context)


def wrap_text(text, width=48):
    words = (text or "").split()
    lines = []
    current = []
    length = 0
    for word in words:
        if current and length + len(word) + 1 > width:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += len(word) + (1 if current[:-1] else 0)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


RECOMMENDED_SUFFIX = " (Recommended)"


def mark_recommended(items, recommended_id):
    """Label the shipped default in an enum list so it can be found again.

    Blender shows no hint of which value a property started with, so a user who
    changes one of these has no way back short of resetting every preference.
    These are all judgement calls rather than obvious ones, which is exactly
    when knowing the intended answer matters.

    Derived from the identifier the property actually defaults to, rather than
    hand-written into each label, so the mark cannot drift away from the real
    default. Item tuples may carry an icon and index after the description, so
    the tail is preserved as-is.
    """
    marked = []
    for item in items:
        identifier, label, description, *tail = item
        if identifier == recommended_id and RECOMMENDED_SUFFIX not in label:
            label = label + RECOMMENDED_SUFFIX
        marked.append((identifier, label, description, *tail))
    return marked
