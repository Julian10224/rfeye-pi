"""Compact-display support for the CUQI 3.5-inch 480x320 SPI panel.

RF Eye's native UI is designed on a 480x800 portrait canvas and then rotated
onto the physical display. The CUQI panel is physically 480x320 in landscape,
so this patch keeps the existing high-resolution logical canvas (preserving the
layout) and performs a final downscale to the real framebuffer. Touch
coordinates are mapped back through that same scale/rotation transform.

The patch is only enabled when RFEYE_DISPLAY_PROFILE=cuqi35 is present.
"""
from __future__ import annotations

import builtins
import os


def _patch_app_class(cls):
    old_init = cls.__init__

    def patched_init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self.display_profile = "cuqi35"

    def patched_present_rotated(self):
        import pygame

        rot = self.cfg.get("rotation", "cw")
        out = pygame.transform.rotate(self.ui, 90 if rot == "ccw" else -90)
        target = (int(self.pw), int(self.ph))
        if out.get_size() != target:
            # Smooth downscaling keeps small labels readable on 480x320 while
            # retaining the original 480x800 logical layout.
            out = pygame.transform.smoothscale(out, target)
        self.screen.blit(out, (0, 0))

    def patched_physical_to_ui(self, px, py):
        # Convert the physical 480x320 touch coordinate into the logical
        # 480x800 portrait canvas, including the software rotation.
        pw = max(2, int(self.pw))
        ph = max(2, int(self.ph))
        nx = max(0.0, min(1.0, float(px) / float(pw - 1)))
        ny = max(0.0, min(1.0, float(py) / float(ph - 1)))

        rot = self.cfg.get("rotation", "cw")
        if rot == "ccw":
            ux = (1.0 - ny) * (self.uw - 1)
            uy = nx * (self.uh - 1)
        else:
            ux = ny * (self.uw - 1)
            uy = (1.0 - nx) * (self.uh - 1)

        ux = max(0, min(self.uw - 1, int(round(ux))))
        uy = max(0, min(self.uh - 1, int(round(uy))))

        if self.cfg.get("touch_invert_x", False):
            ux = self.uw - 1 - ux
        if self.cfg.get("touch_invert_y", False):
            uy = self.uh - 1 - uy
        return ux, uy

    cls.__init__ = patched_init
    cls._present_rotated = patched_present_rotated
    cls._physical_to_ui = patched_physical_to_ui
    return cls


def install_app_patch():
    """Patch the RF Eye App class when the compact display profile is enabled."""
    if os.getenv("RFEYE_DISPLAY_PROFILE", "").lower() != "cuqi35":
        return

    original = builtins.__build_class__
    if getattr(original, "_rfeye_compact_display_patch", False):
        return

    def wrapper(func, name, *bases, **kwargs):
        cls = original(func, name, *bases, **kwargs)
        if name == "App" and getattr(cls, "__module__", "") in {"__main__", "app"}:
            _patch_app_class(cls)
            # If no inner RF Eye class patch already restored __build_class__,
            # restore the wrapper that we inherited. This keeps nested patches
            # (such as wifi_patch) from being re-enabled accidentally.
            if builtins.__build_class__ is wrapper:
                builtins.__build_class__ = original
        return cls

    wrapper._rfeye_compact_display_patch = True
    builtins.__build_class__ = wrapper
