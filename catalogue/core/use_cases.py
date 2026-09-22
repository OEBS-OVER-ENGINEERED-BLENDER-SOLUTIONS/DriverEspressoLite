# -*- coding: utf-8 -*-
"""What each template is FOR, one template at a time.

"What would I use this for?" is answered per template rather than per category:
a shared category blurb would give Door Open Soft and Ceiling Fan Spin the same
sentence. 234 of 286 templates shared their line with at least one other, and
the worst blurb covered 21 of them. A use-case that describes the shelf cannot
help anyone choose between the things on it.

So the rule here is: this text names the OBJECT and the SHOT you would reach
for this template on, and where a template has siblings it says what picks it
over them. The description already says what the motion does - repeating that
here wastes the one line the artist reads while deciding.

Entries are keyed by template id and win over anything passed to template(). No
two templates may share a line, and none may fall back to a category blurb.
"""

SPECIFIC_USE_CASES = {
    # ---------------------------------------------------------------- doors,
    # drawers, lids. Eight ways for a hinged thing to arrive, and the whole
    # choice is what happens at the END of the travel.

    # Powered leaves. These come in claimed left/right pairs, which is the
    # thing the artist actually needs to be told.

    # ------------------------------------------------------- fixtures & rooms

    # ---------------------------------------------------- appliances & bench

    # ------------------------------------------------ architectural rattle

    # -------------------------------------------------------- secondary motion

    # ------------------------------------------------------------- ball bounce
    # The physical family. Every one of these is "a bouncing ball", so the use
    # case has to say WHICH ball, and what that choice buys.

    # ------------------------------------------------------------ characters

    # ---------------------------------------------------------------- cameras

    # --------------------------------------------------------------- vehicles

    # -------------------------------------------------------------- machinery

    # ------------------------------------------------------------ robots & UI

    # ---------------------------------------------------------------- weapons

    # ---------------------------------------------------------------- fire
    "candle_flicker": "A candle, oil lamp, or tealight - the smallest and least predictable flame in the set.",

    # -------------------------------------------------------------- lighting
    "simple_blink": "The plain on-off blink for indicator lamps, and the one to reach for when nothing fancier is needed.",

    # Emergency and traffic light channels. These arrive in coordinated sets,
    # so the use case has to say which piece of the vehicle or head it is.

    # RGB and multi-object light rigs.

    # ---------------------------------------------------------------- weather

    # ----------------------------------------------------------------- audio

    # ------------------------------------------------------------- clocks

    # ------------------------------------------------- generic building blocks
    # Nothing here is about a specific object, so the use case has to be about
    # the SHAPE of the value and when that shape is the right one.
    "constant_speed": "Anything that turns or climbs forever without resetting - fans, drills, turntables, running counters.",
    "loop_n_frames": "One full rotation over a frame count you choose, when the loop has to match a cut or a beat rather than the scene.",
    "scene_loop": "A rotation that comes out exactly even across the scene range, for turntables and looping renders.",
    "modulo_loop": "A ramp that snaps back to zero - use it where the reset is invisible, such as a texture offset or a wrapped angle.",
    "sine_osc": "The default back-and-forth: pendulums, breathing, hovering, and any gentle repeating motion.",
    "sawtooth": "A value that builds and drops instantly - reloading meters, sweeping scanners, wrapping offsets.",
    "triangle_wave": "Even travel up and back with no easing, for mechanical scanning and linear shuttles.",
    "transition_linear": "A move from A to B at constant speed, for mechanical travel and readouts that must not ease.",
    "transition_ease_out": "A move that starts fast and settles, the most natural choice for a thing coming to rest.",
    "transition_smoothstep": "The standard eased A-to-B move, gentle at both ends. The safe default for reveals and fades.",
    "fade_in": "Bringing something up from nothing: a light, an opacity, an influence value.",
    "fade_out": "Taking something down to nothing, for exits, dissolves, and powering off.",
    "pulse_repeat": "A repeating on-off pulse, for warning lamps, heartbeats, and cyclic triggers.",
    "rgb_colour_cycle": "A light or emissive material sweeping the full spectrum - party lighting, RGB hardware, idle sci-fi glow.",
    "rgb_chase": "A block of light marching along a row of lamps - marquee signs, arcade cabinets, runway markers.",
    "rgb_twinkle": "Fairy lights, star fields, or city windows switching on and off independently.",
}
