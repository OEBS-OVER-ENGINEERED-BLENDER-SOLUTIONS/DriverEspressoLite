"""Blender-free expression compiler for named Espresso input controllers."""

from __future__ import annotations

from dataclasses import dataclass

from ..expression.driver_literals import format_driver_literal


INTERPRET_SWITCH = "SWITCH"
INTERPRET_INFLUENCE = "INFLUENCE"

RESPONSE_LINEAR = "LINEAR"
RESPONSE_SMOOTH = "SMOOTH"
RESPONSE_EASE_IN_OUT = "EASE_IN_OUT"

TRANSITION_IMMEDIATE = "IMMEDIATE"
TRANSITION_LINEAR = "LINEAR"
TRANSITION_SMOOTH = "SMOOTH"
TRANSITION_EASE_IN_OUT = "EASE_IN_OUT"
TRANSITIONS = frozenset({
    TRANSITION_IMMEDIATE, TRANSITION_LINEAR,
    TRANSITION_SMOOTH, TRANSITION_EASE_IN_OUT,
})


@dataclass(frozen=True)
class CaptureState:
    frame: int
    value: float
    source_signature: str


def transition_weight(frame, capture, mode=TRANSITION_IMMEDIATE, duration=0):
    if mode == TRANSITION_IMMEDIATE or int(duration) <= 0:
        return 1.0 if float(frame) >= float(capture.frame) else 0.0
    value = max(0.0, min(1.0, (float(frame) - float(capture.frame)) / float(duration)))
    if mode == TRANSITION_SMOOTH:
        return value * value * (3.0 - 2.0 * value)
    if mode == TRANSITION_EASE_IN_OUT:
        return value * value * value * (10.0 + value * (-15.0 + 6.0 * value))
    return value


def capture_is_fresh(capture, source_signature):
    return bool(capture and capture.source_signature == str(source_signature or ""))


def supports_safe_disable(additive_profile):
    return str(additive_profile or "").upper() != "ELAPSED_TIME"


def _normalized_control(variable_name, clamp, invert):
    value = variable_name
    if clamp:
        value = f"max(0,min(1,{value}))"
    if invert:
        value = f"(1-({value}))"
    return value


def _response_expression(value, response_curve):
    if response_curve == RESPONSE_LINEAR:
        return value
    if response_curve == RESPONSE_EASE_IN_OUT:
        return f"(({value})*({value})*({value})*(10+({value})*(-15+6*({value}))))"
    return f"(({value})*({value})*(3-2*({value})))"


def _controlled_expression(
    base_expression,
    interpretation,
    rest_value,
    *,
    threshold,
    invert,
    clamp,
    response_curve,
    variable_name,
):
    base = f"({base_expression})"
    rest = format_driver_literal(rest_value)
    if interpretation == INTERPRET_SWITCH:
        operator = "<" if invert else ">="
        threshold_literal = format_driver_literal(threshold)
        return f"{base} if {variable_name}{operator}{threshold_literal} else {rest}"

    control = _normalized_control(variable_name, clamp, invert)
    weight = _response_expression(control, response_curve)
    if rest == "0":
        return f"{weight}*{base}"
    return f"{rest}+{weight}*({base}-{rest})"


def _transition_expression(expression, rest_value, mode, duration, capture_frame,
                           frame_variable_name):
    """Blend from the captured value without hidden mutable timer state."""
    if mode == TRANSITION_IMMEDIATE or int(duration) <= 0:
        return expression
    rest = format_driver_literal(rest_value)
    start = format_driver_literal(capture_frame)
    length = format_driver_literal(max(1, int(duration)))
    t = f"max(0,min(1,({frame_variable_name}-{start})/{length}))"
    if mode == TRANSITION_SMOOTH:
        weight = f"(({t})*({t})*(3-2*({t})))"
    elif mode == TRANSITION_EASE_IN_OUT:
        weight = f"(({t})*({t})*({t})*(10+({t})*(-15+6*({t}))))"
    else:
        weight = t
    return f"{rest}+{weight}*(({expression})-{rest})"


def compile_hold_expression(
    base_expression,
    hold_value,
    *,
    mode=TRANSITION_IMMEDIATE,
    duration=0,
    capture_frame=0,
    max_length=255,
):
    """Blend a scripted driver into a captured hold using scene time only.

    At the capture frame the original driver is unchanged. Over ``duration``
    frames its evaluated result converges on ``hold_value``; scrubbing backward
    or forward produces the same value because no mutable timer is involved.
    """
    hold = format_driver_literal(hold_value)
    if mode == TRANSITION_IMMEDIATE or int(duration) <= 0:
        return hold
    if mode not in TRANSITIONS:
        raise ValueError(f"Unknown Controller transition mode: {mode}")
    start = format_driver_literal(capture_frame)
    length = format_driver_literal(max(1, int(duration)))
    t = f"max(0,min(1,(frame-{start})/{length}))"
    if mode == TRANSITION_SMOOTH:
        weight = f"(({t})*({t})*(3-2*({t})))"
    elif mode == TRANSITION_EASE_IN_OUT:
        weight = f"(({t})*({t})*({t})*(10+({t})*(-15+6*({t}))))"
    else:
        weight = t
    expression = f"{hold}+(1-({weight}))*(({base_expression})-{hold})"
    if len(expression) > max_length:
        raise ValueError(
            f"The eased hold expression is {len(expression)} characters; "
            f"Blender allows at most {max_length}. Choose Immediate for this driver."
        )
    return expression


def compile_controlled_expression(
    base_expression,
    interpretation,
    rest_value,
    *,
    threshold=0.5,
    invert=False,
    clamp=True,
    response_curve=RESPONSE_SMOOTH,
    variable_name="espctl_",
    base_variable_name="espbase",
    transition_mode=TRANSITION_IMMEDIATE,
    transition_duration=0,
    capture_frame=0,
    frame_variable_name="espfrm_",
    max_length=255,
):
    """Compile inline first, then fall back to a short carrier expression."""
    if not base_expression:
        raise ValueError("The selected driver has no scripted expression.")
    inline = _controlled_expression(
        base_expression,
        interpretation,
        rest_value,
        threshold=threshold,
        invert=invert,
        clamp=clamp,
        response_curve=response_curve,
        variable_name=variable_name,
    )
    inline = _transition_expression(
        inline, rest_value, transition_mode, transition_duration,
        capture_frame, frame_variable_name,
    )
    if len(inline) <= max_length:
        return {
            "strategy": "INLINE",
            "expression": inline,
            "carrier_expression": "",
        }

    carrier = _controlled_expression(
        base_variable_name,
        interpretation,
        rest_value,
        threshold=threshold,
        invert=invert,
        clamp=clamp,
        response_curve=response_curve,
        variable_name=variable_name,
    )
    carrier = _transition_expression(
        carrier, rest_value, transition_mode, transition_duration,
        capture_frame, frame_variable_name,
    )
    if len(carrier) > max_length:
        raise ValueError(
            f"The compact controller expression is {len(carrier)} characters; "
            f"Blender allows at most {max_length}."
        )
    return {
        "strategy": "CARRIER",
        "expression": carrier,
        "carrier_expression": base_expression,
    }
