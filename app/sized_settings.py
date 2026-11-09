"""What the overlay sees of the settings, with "Overall size" and "Mini mode" applied.

The overlay reads sizes (font sizes, width, ...) from the settings. Rather than change the
values you chose, this stand-in multiplies them by the overall size when they're read, and
in Mini mode also shows just the current line without the title. Everything else (and any
change the overlay saves) goes straight to the real settings.
"""
from __future__ import annotations

SCALED = ("font_size", "side_font_size", "title_font_size", "width", "fixed_height", "corner_radius")
MINI_SCALE = 0.8
MINI_OVERRIDES = {"lines_shown": 1, "show_title": False, "show_progress": False, "show_album_art": False}


class SizedSettings:
    def __init__(self, base):
        object.__setattr__(self, "_base", base)

    def scale(self) -> float:
        b = self._base
        s = float(getattr(b, "size_scale", 1.0) or 1.0)
        return s * MINI_SCALE if getattr(b, "mini_mode", False) else s

    def __getattr__(self, name):
        b = object.__getattribute__(self, "_base")
        if getattr(b, "mini_mode", False) and name in MINI_OVERRIDES:
            return MINI_OVERRIDES[name]
        v = getattr(b, name)
        if name in SCALED and isinstance(v, (int, float)) and not isinstance(v, bool):
            sc = self.scale()
            if sc != 1.0:
                return max(1, int(round(v * sc))) if isinstance(v, int) else v * sc
        return v

    def __setattr__(self, name, value):
        b = self._base
        if name in SCALED and isinstance(value, (int, float)) and not isinstance(value, bool):
            sc = self.scale()
            if sc != 1.0:
                value = int(round(value / sc)) if isinstance(value, int) else value / sc
        setattr(b, name, value)
