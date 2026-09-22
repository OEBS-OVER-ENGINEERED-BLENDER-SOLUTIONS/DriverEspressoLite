"""Graph mode and catalogue channel-ranking metadata."""

MODE_GRAPH = "GRAPH"
MODE_ITEMS = ((MODE_GRAPH, "Graph", "The exact sampled curve"),)
VISUAL_AVAILABLE = False

# Preserve the lead-channel choices used by saved multi-channel motions.
_TEMPLATE_KINDS = {'candle_flicker': 'LIGHTS',
 'constant_speed': 'SPIN',
 'fade_in': 'LIGHTS',
 'fade_out': 'LIGHTS',
 'loop_n_frames': 'SPIN',
 'modulo_loop': 'SPIN',
 'pulse_repeat': 'LIGHTS',
 'rgb_chase': 'LIGHTS',
 'rgb_colour_cycle': 'LIGHTS',
 'rgb_twinkle': 'LIGHTS',
 'sawtooth': 'SWING',
 'scene_loop': 'SPIN',
 'simple_blink': 'LIGHTS',
 'sine_osc': 'SWING',
 'transition_ease_out': 'SLIDE',
 'transition_linear': 'SLIDE',
 'transition_smoothstep': 'SLIDE',
 'triangle_wave': 'SWING'}
_NATURAL_PATHS = {'SLIDE': 'location', 'SPIN': 'rotation', 'SWING': 'rotation'}


def kind_for(template):
    return _TEMPLATE_KINDS.get(str((template or {}).get("id") or ""))


def natural_data_path(kind):
    return _NATURAL_PATHS.get(kind, "")
