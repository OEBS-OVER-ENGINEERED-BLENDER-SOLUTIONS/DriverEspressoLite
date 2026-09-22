# Driver Espresso Lite

Driver Espresso Lite is a free Blender add-on for setting up procedural animation with drivers. Choose a template, apply it to a property, and adjust the motion while your scene plays. You don't need to write the expression yourself.

Use it to keep a prop spinning, make a light flicker, fade a value over time, or cycle through colours. Lite includes 18 templates across 6 categories.

[Download the latest release](https://github.com/OEBS-OVER-ENGINEERED-BLENDER-SOLUTIONS/DriverEspressoLite/releases/latest) · [Documentation](https://docs.oebs-studios.com/driverespresso/)

## Install

Requires Blender 4.2 or newer. No extra Python packages are needed.

1. Download the **DriverEspressoLite** ZIP attached to the release. Use this file, not GitHub's **Source code** archive or **Code > Download ZIP**.
2. In Blender, open **Edit > Preferences > Get Extensions**.
3. Open the menu in the upper-right corner, choose **Install from Disk**, and select the ZIP without unpacking it.
4. Enable **Driver Espresso Lite**. In the 3D Viewport, press **N** and open the **Espresso** tab.

If you're updating an existing installation, restart Blender afterwards.

## Try a simple rotation

1. Select a cube and open **Espresso** in the sidebar.
2. Choose **Spin & Rotate > Constant Speed** and set **Speed** to `0.03`.
3. In the sidebar's **Item** tab, right-click **Rotation Z** and choose **Espresso > Apply Current Template**.
4. Play the timeline. The cube rotates continuously.

For further adjustments, return to Espresso's **Live** controls. The graph preview shows the value the driver produces. Rotation drivers use radians, so a small speed value is a sensible starting point.

## What's included

- **Rotation and loops:** constant rotation, repeating ramps, and loops fitted to a frame count or scene length.
- **Back-and-forth motion:** sine, triangle and sawtooth waves.
- **Lighting:** candle flicker, an on/off blink, RGB colour cycling, marquee chase and twinkle.
- **Transitions:** fades, a repeating pulse, and linear, ease-out and smoothstep transitions.

You can preview a template before applying it, edit its expression, or copy a driver to another property. **Apply as Sequence** offsets the timing across selected objects or pose bones.

The add-on creates native Blender drivers. Once applied, those drivers keep working without Lite installed. Keep the add-on if you want to return to its template controls; the drivers themselves remain editable in Blender's Drivers Editor.

## Lite and the paid editions

Lite is the free edition, not a timed trial. Driver Espresso and Driver Espresso Premium are paid editions with larger template libraries. Premium also includes Visual Preview; Lite includes the graph preview.

[Compare the editions on Superhive](https://superhivemarket.com/products/driver-espresso).

## Help and feedback

The [documentation](https://docs.oebs-studios.com/driverespresso/) covers the interface, templates and live controls.

If something isn't working, [open an issue](https://github.com/OEBS-OVER-ENGINEERED-BLENDER-SOLUTIONS/DriverEspressoLite/issues) or email [support@oebs-studios.com](mailto:support@oebs-studios.com). Include your Blender version, the template you used, the property you applied it to, and what happened. A small `.blend` file helps if you can share one.

Made by OEBS Studios. Licensed under [GPL-3.0-or-later](LICENSE).
