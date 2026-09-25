# Driver Espresso Lite Addon Package
Driver Espresso Lite is an OEBS Blender addon for building scripted driver
expressions from curated templates.

The complete **Espresso** and **Preview** sidebars are available in the
3D View, Graph Editor, Shader Editor, Geometry Nodes Editor, and Compositor.
Node Editor panels appear only for Shader, Geometry, and Compositor node trees,
where users can drive practical properties such as emission strength, shader
inputs, procedural geometry controls, mix factors, exposure, blur, and
distortion. Texture Nodes and unrelated editors remain uncluttered.
This edition includes 18 templates across 6 categories. Generic building blocks are listed first, followed by tailored
groups for RGB and neon lighting, and lights.
Single-expression and multi-expression recipes share the same friendly
parameter, validation, preview, and application pipeline. One multi-expression
recipe counts as one template, regardless of channel count.
Bounded templates such as fades, triggers, blinks, strobes, and pulses
now expose explicit `Minimum` and `Maximum` output controls where that
range matters artistically.
Version 1.5.0 adds optional advanced controls for supported templates. Enable
them in addon preferences when you want extra timing and output shaping without
making every template permanently heavier.

Version 1.5.1 fixes the advanced controls so they are neutral by default:
when Advanced Controls are off, or are on but untouched, the generated
expression is exactly the base template with no hidden frame window or output
scaling applied.

Version 1.6.0 is a feature release:

- **Native driver portability.** Catalogue expressions are compiled to
  Blender's supported fast evaluator, checked against the 255-character limit,
  and remain functional after Driver Espresso Lite is disabled or uninstalled.
- **Multi-style preview.** The waveform preview can be drawn as a high-resolution
  Line (Braille) graph, Bars, Filled area, bipolar Mirror, a Stats readout, or a
  rendered Image graph with a current-frame cursor.
- **Edit Expression.** Toggle hand-editing of the generated expression before you
  copy or apply it.
- **Remove Driver.** Clear a driver from the selected F-Curve, the right-clicked
  property, or the remembered last target.
- Apply actions now support Undo, and a note shows the baked frame range.
- Ships a `blender_manifest.toml` so it installs as a Blender 4.2+ extension.

Version 1.6.1 reworks the preview for accuracy:

- The manual "Samples" control is gone. The graph samples automatically (one
  sample per frame) and draws each column as a min-max envelope, so every pulse
  and beat is shown and nothing flickers in and out with a sample count.
- Every chart style (Line, Bars, Filled, Mirror) now renders as a real pixel
  image, sized to fill the panel. Blender panel labels use a proportional font,
  which misaligns text/ASCII graphs, so an image is the only faithful
  representation. Stats stays as text since it is just numbers.
- A **Lightweight Preview** option (addon preferences > Waveform Preview) draws
  the graph as a braille text curve instead of an image. It uses far less CPU
  during playback, so it is the recommended choice on weaker computers. The
  braille cells use a fixed-width blank so the curve stays aligned in the panel.
- Bars aggregate each bar from the max of its frame span, so narrow pulses are
  never skipped, and the cycle estimate shown in the preview is now accurate.
- The image preview is optimised: the graph is generated with numpy and uploaded
  via `foreach_set`, and an already-rendered frame is reused from cache instead
  of being rebuilt on every redraw. End-to-end cost dropped from about 5 ms to
  under 1 ms per frame (numpy is bundled with Blender; a pure-Python fallback is
  used if it is ever unavailable).

Version 1.6.2 expands the real-world motion layer:
- **Apply as Sequence** applies a motion plan across selected objects or pose
  bones with a deterministic frame delay, useful for tails, chains, crowds,
  repeated props, and staggered prop motion.
- This edition ships 18 templates across 6 categories.

## Expression Integrity

Driver Espresso Lite compacts generated expressions before they reach Blender. RNA
float noise is rendered as the shortest practical literal, constant numeric
subexpressions and redundant spacing are folded, neutral wrapper fragments are
omitted, and Additive/Offset wrappers reuse rather than repeat known anchors.
The friendly parameter values and the visible motion contract do not change.
Repeatable recipes produce exactly the same motion after compaction; random
recipes stay finite and within their stated range.

Blender limits a scripted driver expression to 255 characters. Every Espresso
apply route validates the complete final expression—including Rest Start,
Advanced Output, motion anchors, and multi-channel wrappers—before editing a
driver. The stored text is read back after assignment. If it is too long,
truncated, or invalid, Driver Espresso Lite restores the previous driver and reports
the problem instead of leaving a broken partial expression.

Generated recipes are verified against real Blender float properties across Off,
Additive, and Offset Graph modes, so the motion you preview is the motion you
get.

## Install

Install `DriverEspressoLite-v1.6.5.zip` from Blender:

```text
Edit > Preferences > Get Extensions > Install from Disk...
```

Then enable **Driver Espresso Lite**.

## Location

```text
View3D > Sidebar > Espresso
Graph Editor > Sidebar > Espresso
```

## Core Workflow

1. Choose a category.
2. Choose a template.
3. Tune the parameters.
4. Copy the expression, copy it as an Espresso driver, or apply it to a selected driver F-Curve.

Multi-expression templates show every destination channel and its complete
expression in the N-panel. Click a channel's radio button to graph it, use its
own **Copy** action to copy only that expression, or click **Apply Motion to
Selected Object** to route all channels to the active Object's matching
Location, Rotation, and Scale properties.

For repeated selected objects or pose bones, **Apply as Sequence** uses the
same complete motion plan but shifts each target by a chosen number of frames.
The active target is first, then the remaining targets are ordered by name, so
the result is predictable when driving chains, tails, ears, crowds, lights, or
multiple handheld props.

Crowded categories expose one additional **Subcategory** shelf with two to four
broad choices. Search remains global, while Previous/Next navigation stays
inside the selected shelf. Categories that are already easy to scan remain flat.

The real-world catalogue includes RGB and neon lighting, and lights. Every recipe uses the same expression
builder, validation, preview, and application path as the generic templates.

The Apply button is active when a valid driver F-Curve is selected in the Graph
Editor. Copy Expression is available anywhere when the generated expression is
valid.

**Copy Driver** stores the current template expression in the Espresso
driver clipboard. After copying, right-click any drivable value and choose
**Paste Copied Driver** from the Espresso menu.

Successful apply actions can also be remembered as a last target inside the
`.blend`, so **Update Last Target** can push later template tweaks back onto the
same property without making you reselect it. The target record stores the
owning ID plus RNA path, so object transforms, custom properties, shape keys,
Shader sockets, and Geometry Nodes use the same owner-aware update workflow.

## Advanced Controls

Enable **Advanced Controls** in addon preferences to expose optional extra
controls on templates that support them. The first rollout includes:

- `Start delay`
- `Start early`
- `Start frame` (0 = scene start)
- `End frame` (0 = scene end)
- `Output gain`
- `Output offset`

Output gain and offset are available to every numeric template because they are
neutral and broadly useful; timing controls remain template-specific. No
friendly primary control is moved into Advanced. Each control is neutral at its default value,
so an untouched control leaves the base expression unchanged. `Start frame` and
`End frame` treat `0` as "use the scene boundary", so the activation window only
limits output once you set a real frame range.

Selected fixed-period pulse templates also expose **Fit loop to scene**.
When enabled, Driver Espresso Lite snaps the effective repeat period to a
scene-friendly value close to the chosen `Period`, which helps strobe, blink,
and similar loops hand off cleanly at the frame-range seam.

Output gain scales excursion rather than the whole absolute value:
`rest + (signal - rest) * gain + offset`. A Simple Blink with Minimum 1
therefore keeps 1 as its low state, and scale motion keeps 1.0 as neutral scale.
Rest Start uses the same semantic baseline. Each Additive profile restarts from
the captured property value using a template-appropriate contract, so bounded
effects such as Candle Flicker cannot shift their later low state below the
captured rest value.

Applied effects are listed in the sidebar's **Organize** tab, categorized with
object transforms first, then custom/object/data properties and shape keys.
Only selected Shader and Geometry nodes are added, preventing large node trees
from flooding the list.

The Preview/apply workflow also exposes **Rest Start** modes:

- `Additive — Restart from Current Value (Recommended)` captures the current
  property value and application frame. Earlier frames hold that value; the
  effect begins at its declared rest point and then advances on a local clock.
- `Offset Graph — Keep Scene Time` preserves the authored scene-time phase and
  applies one fixed shift. On a bounded-minimum template the shift is `Current
  Value + Minimum`; on a bounded-maximum template it is `Current Value +
  Maximum`. This deliberately may jump when applied because it does not try to
  match the curve's value at that frame. For example, `MIN=1`, `MAX=15`, and a
  current value of `4` shift a bounded curve by `5`, mapping a raw `1..15`
  range to `6..20`. Templates without a declared minimum/maximum keep the
  original phase-matching behavior: their one fixed shift makes the application
  frame equal the current property value.
- `Off — Keep Scene Time` keeps the original raw single-property behavior.

Existing drivers retain the numeric shift captured when they were applied;
changing this contract does not silently rewrite stored Offset states. Reapply
the template to capture the new bounded Offset Graph behavior.

Multi-channel motion templates use **Relative to Current Transform** placement:
location and rotation add their excursions to the captured transform, while
scale multiplies the captured size. This edition declares
9 declared channels across 3 motion-plan templates.

Exact `Minimum`/`Maximum` equality is allowed. If those two explicitly ordered
output fields are entered backwards, Espresso swaps them and shows a warning;
directional and remap ranges remain intentionally reversible.

## Right-Click Driver Workflow

Right-click a drivable UI value and choose from the Espresso menu:

```text
Apply Current Template
Paste Copied Driver
```

Arrayed properties also expose:

```text
Apply Current Template (Multi)
```

This applies the currently configured template across every index in the
clicked property family, such as `Location X/Y/Z`, `Scale X/Y/Z`, RGBA colors,
or array-style custom properties.

That command repeats one single expression across an array. A template
that owns a real multi-channel motion plan instead uses its own corresponding
expressions. In the N-panel choose **Apply Motion to Selected Object**. In the
right-click workflow, **Apply Current Template** or enabling **Apply Driver on
OK** applies the entire plan to the Object that owns the clicked transform—even
if another Object becomes active before the popup is confirmed.

Scalar properties receive one driver. Array properties, including colors, use
Blender's active channel when available; when Blender exposes the whole color
field, Driver Espresso Lite applies the current expression to each color channel.

## Notes

- Expressions use baked `FRAME_START`, `FRAME_END`, and `FRAME_LEN` values.
- Fixed-period templates anchor to scene start with `(frame - FRAME_START)`.
- The CPU visualizer keeps the original compact text waveform available.
- Enable **Detailed** for a taller technical text graph with min/mid/max guides,
  frame labels, current-frame value, sample count, and range stats.
