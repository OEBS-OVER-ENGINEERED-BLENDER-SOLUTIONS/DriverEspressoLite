# Driver Espresso Lite

Ready-made motion for Blender. Pick a template, apply it to anything in Blender, and adjust it while the scene plays.

Driver Espresso Lite is the starter edition, with 20 templates across 6 categories:

| Category | Templates |
| --- | --- |
| Spin & Rotate | Constant Speed, Ease In Rotation, Loop in N Frames, Repeating Rotation Ramp, Scene Length Loop |
| Swing & Oscillate | Generic Sawtooth Wave, Sine Oscillation, Triangle Wave |
| Ease & Spring | Ease In Transition, Ease Out Transition, Linear Transition, Smoothstep Transition |
| Trigger & State | Linear Fade In, Smooth Fade Out, Repeating Pulse |
| Light & Flicker | Candle Flicker, Simple Blink |
| RGB & Neon Lighting | RGB Colour Cycle, RGB Marquee Chase, RGB Twinkle |

## Install

Requires Blender 4.2 or newer.

1. Download `DriverEspressoLite-v1.6.6.zip` from the [latest release](https://github.com/OEBS-OVER-ENGINEERED-BLENDER-SOLUTIONS/DriverEspressoLite/releases/latest). Leave it zipped.
2. In Blender, open **Edit > Preferences > Get Extensions**, open the menu in the top-right corner and choose **Install from Disk**.
3. Pick the ZIP, then enable **Driver Espresso Lite**.

## Quick start

1. In the 3D View, press **N** and open the **Espresso** tab.
2. Choose a category and a template.
3. Right-click the value you want to animate, such as a rotation, a light's power or a colour, and choose **Apply Current Template**.
4. Play the timeline. Switch the panel from **Setup** to **Live** to adjust the motion while it plays.

The panel is also available in the Graph Editor and in the Shader, Geometry Nodes and Compositor editors.

## Good to know

- The preview graph shows the motion before you apply it.
- **Apply to Selected Objects** gives every selected object its own copy of the motion; objects that share a material can each be given their own copy of it.
- The motion is made of standard Blender drivers, so it keeps playing in files opened without the add-on.
- **Bake Drivers to Keyframes** turns a motion into keyframes when you want to edit it by hand.

## Help

- [User guide](https://docs.oebs-studios.com/driverespresso/)
- [Ask a question or report a problem](https://oebs-studios.com/support/dre)
- [Release notes](https://github.com/OEBS-OVER-ENGINEERED-BLENDER-SOLUTIONS/DriverEspressoLite/releases)

## License

GPL-3.0-or-later. Made by OEBS Studios.
