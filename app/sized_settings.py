"""What the overlay sees of the settings, with "Overall size" and the small modes applied.

The overlay reads sizes (font sizes, width, ...) from the settings. Rather than change the
values you chose, this stand-in multiplies them by the overall size when they're read, and in
a small mode also hides the title / time and shows fewer lines. Everything else (and any change
the overlay saves) goes straight to the real settings.
"""
from __future__ import annotations

SCALED = ("font_size", "side_font_size", "title_font_size", "width", "fixed_height", "corner_radius")
_PLAIN = {"show_title": False, "show_progress": False, "show_album_art": False, "time_display": "off"}
SMALL_MODES = {
    # name: (label, size, settings it changes)
    "off": ("Off – normal overlay", 1.0, {}),
    "small": ("Small – 2 lines (current + next), no title", 0.85, {"lines_shown": 2, **_PLAIN}),
    "tiny": ("Tiny – just the current line", 0.8, {"lines_shown": 1, **_PLAIN}),
}
CYCLE = ("off", "small", "tiny")


class SizedSettings:
    def __init__(self, base):
        object.__setattr__(self, "_base", base)

    def _mode(self):
        return SMALL_MODES.get(getattr(self._base, "small_mode", "off"), SMALL_MODES["off"])

    def scale(self) -> float:
        s = float(getattr(self._base, "size_scale", 1.0) or 1.0)
        return s * self._mode()[1]

    def __getattr__(self, name):
        b = object.__getattribute__(self, "_base")
        over = self._mode()[2]
        if name in over:
            return over[name]
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
