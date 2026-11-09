"""Theme presets: one click sets the font, colours, background and highlight style."""

from __future__ import annotations

HIGHLIGHT_STYLES = {
    "wipe": "Wipe – colour sweeps across the word",
    "word": "Pop – whole word lights up",
    "glow": "Glow – word glows brightly",
    "grow": "Grow – word gets bigger",
    "bounce": "Bounce – word hops up",
    "underline": "Underline – bar slides under the word",
}

LINE_TRANSITIONS = {
    "scroll": "Smooth scroll – lines slide up together (like Spotify)",
    "fade": "Quick fade – lines stay put, the text fades over",
    "slide": "Snappy slide – the older, quicker slide-up",
    "grow": "Rise and grow – old line fades, next one rises and grows",
    "instant": "Instant – no animation",
}

# Every setting a theme controls
THEME_KEYS = ("font_family", "bold", "text_color", "highlight_color", "highlight_color2",
              "background_color", "background_opacity", "show_background", "glow", "highlight_style")

THEMES: dict[str, dict] = {
    "Neon": dict(font_family="Segoe UI", bold=True, text_color="#FFFFFF",
                 highlight_color="#A855F7", highlight_color2="#3B82F6",
                 background_color="#0A0A12", background_opacity=0.55, show_background=True,
                 glow=True, highlight_style="wipe"),
    "Ocean": dict(font_family="Segoe UI", bold=True, text_color="#F0FBFF",
                  highlight_color="#22D3EE", highlight_color2="#3B82F6",
                  background_color="#04121C", background_opacity=0.55, show_background=True,
                  glow=True, highlight_style="wipe"),
    "Sunset": dict(font_family="Segoe UI", bold=True, text_color="#FFF7ED",
                   highlight_color="#F97316", highlight_color2="#EC4899",
                   background_color="#1A0B12", background_opacity=0.5, show_background=True,
                   glow=True, highlight_style="wipe"),
    "Sakura": dict(font_family="Segoe UI", bold=True, text_color="#FFF5FA",
                   highlight_color="#F9A8D4", highlight_color2="#F472B6",
                   background_color="#1F0F18", background_opacity=0.5, show_background=True,
                   glow=True, highlight_style="grow"),
    "Arcade": dict(font_family="Consolas", bold=True, text_color="#ECFCCB",
                   highlight_color="#A3E635", highlight_color2="#22C55E",
                   background_color="#050A05", background_opacity=0.6, show_background=True,
                   glow=True, highlight_style="word"),
    "Gold": dict(font_family="Segoe UI", bold=True, text_color="#FFFBEB",
                 highlight_color="#FACC15", highlight_color2="#F59E0B",
                 background_color="#120E02", background_opacity=0.55, show_background=True,
                 glow=True, highlight_style="underline"),
    "Minimal": dict(font_family="Segoe UI", bold=True, text_color="#C8C8D0",
                    highlight_color="#FFFFFF", highlight_color2="#FFFFFF",
                    background_color="#000000", background_opacity=0.0, show_background=False,
                    glow=True, highlight_style="glow"),
    "Light": dict(font_family="Segoe UI", bold=True, text_color="#1F2937",
                  highlight_color="#7C3AED", highlight_color2="#2563EB",
                  background_color="#F5F5F7", background_opacity=0.85, show_background=True,
                  glow=False, highlight_style="wipe"),
}

CUSTOM = "Custom"


def apply_theme(settings, name: str) -> bool:
    theme = THEMES.get(name)
    if not theme:
        return False
    for k, v in theme.items():
        setattr(settings, k, v)
    settings.theme = name
    return True
