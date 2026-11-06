"""User settings, saved as a small JSON file.

Location on Windows: %APPDATA%\\SpotifyLyricsOverlay\\settings.json
No passwords or Spotify credentials are ever stored - the app doesn't need any.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

POSITIONS = [
    "top-left", "top-center", "top-right",
    "center",
    "bottom-left", "bottom-center", "bottom-right",
    "custom",
]


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "SpotifyLyricsOverlay"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    # Text
    font_family: str = "Segoe UI"
    font_size: int = 30
    bold: bool = True
    text_color: str = "#FFFFFF"
    highlight_color: str = "#A855F7"     # purple
    highlight_color2: str = "#3B82F6"    # blue (gradient end)
    glow: bool = True
    # Window look
    background_color: str = "#0A0A12"
    background_opacity: float = 0.55     # 0 = fully clear, 1 = solid
    window_opacity: float = 1.0          # fades the whole overlay incl. text
    width: int = 1000
    # Placement
    position: str = "bottom-center"
    margin: int = 60
    screen_index: int = 0
    custom_x: int = 100
    custom_y: int = 100
    # Behaviour
    show_title: bool = True
    show_context_lines: bool = True      # previous / next lines, dimmed
    click_through: bool = False
    visible: bool = True
    hide_when_paused: bool = False
    estimate_word_timing: bool = True    # karaoke-guess when real word timing isn't available
    sync_offset_ms: int = 0              # + makes lyrics appear earlier
    spotify_only: bool = True
    first_run: bool = True

    # ------------------------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = cls.path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                known = {f.name: f for f in fields(cls)}
                for k, v in data.items():
                    if k in known:
                        default = getattr(s, k)
                        # keep defaults if the stored type is wrong
                        if isinstance(default, bool):
                            if isinstance(v, bool):
                                setattr(s, k, v)
                        elif isinstance(default, (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool):
                            setattr(s, k, type(default)(v))
                        elif isinstance(default, str) and isinstance(v, str):
                            setattr(s, k, v)
            except Exception:  # corrupt file -> start fresh, don't crash
                log.exception("Could not read settings, using defaults")
        s.clamp()
        return s

    def clamp(self) -> None:
        self.font_size = max(10, min(120, int(self.font_size)))
        self.background_opacity = max(0.0, min(1.0, float(self.background_opacity)))
        self.window_opacity = max(0.1, min(1.0, float(self.window_opacity)))
        self.width = max(300, min(4000, int(self.width)))
        self.margin = max(0, min(1000, int(self.margin)))
        self.sync_offset_ms = max(-10000, min(10000, int(self.sync_offset_ms)))
        if self.position not in POSITIONS:
            self.position = "bottom-center"

    def save(self) -> None:
        try:
            p = self.path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            log.exception("Could not save settings")
