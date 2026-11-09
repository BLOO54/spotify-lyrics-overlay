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


BACKUPS_KEPT = 20

@dataclass
class Settings:
    # Text
    font_family: str = "Segoe UI"
    font_size: int = 30                  # LYRICS size: the big current line
    side_font_size: int = 0              # previous/next lines, translation, romaji (0 = work it out from font_size)
    title_font_size: int = 0             # song title, timer, sync label (0 = work it out from font_size)
    bold: bool = True
    text_color: str = "#FFFFFF"
    highlight_color: str = "#A855F7"     # purple
    highlight_color2: str = "#3B82F6"    # blue (gradient end)
    glow: bool = True
    theme: str = "Neon"
    highlight_style: str = "wipe"        # see themes.HIGHLIGHT_STYLES
    keep_sung_colored: bool = True       # sung words stay tinted (False = back to white)
    # Overall overlay design: "classic" | "aurora" | "hud" | "spotify"
    design: str = "classic"
    # Window look
    background_color: str = "#0A0A12"
    background_opacity: float = 0.55     # 0 = fully clear, 1 = solid
    window_opacity: float = 1.0          # fades the whole overlay incl. text
    show_background: bool = True         # False = lyrics only, no box behind them
    background_style: str = "solid"      # "solid" | "frosted" (blurs whatever is behind the overlay)
    corner_radius: int = 18              # 0 = square corners
    match_album_highlight: bool = False  # take highlight colours from the album art
    match_album_background: bool = False # take the background colour from the album art
    smooth_colour_changes: bool = True   # fade colours gently when the song changes
    width: int = 1000
    fixed_size: bool = False             # keep the overlay box exactly this size (lyrics fit inside it)
    fixed_height: int = 0                # height used when fixed_size is on (0 = take the current height)
    # Placement
    position: str = "bottom-center"
    margin: int = 60
    screen_index: int = 0
    custom_x: int = 100
    custom_y: int = 100
    # Behaviour
    show_title: bool = True
    show_album_art: bool = False
    album_art_position: str = "auto"     # "auto" | "left" (big) | "header" (small, next to the title)
    header_art_size: str = "medium"      # size of the small cover next to the title: "small" | "medium" | "large"
    time_display: str = "off"            # "off" | "left" (-1:23) | "elapsed" (1:02 / 3:45)
    show_progress: bool = False          # thin song progress bar
    long_lines: str = "widen"            # "widen" (grow up to the screen edge) | "shrink" (smaller text) | "wrap" (more rows)
    lines_shown: int = 3                 # 1 = current only, 2 = current + next, 3 = previous + current + next
    click_through: bool = False
    visible: bool = True
    hide_when_paused: bool = False
    estimate_word_timing: bool = True    # karaoke-guess when real word timing isn't available
    word_timing_mode: str = "syllables"  # how to guess: "syllables" (smart) | "letters" (simple)
    sync_offset_ms: int = 0              # + makes lyrics appear earlier
    spotify_only: bool = True
    # Translation (off by default; only used for songs not in your language)
    translate_enabled: bool = False
    translate_target: str = "en"
    translation_layout: str = "big"      # "big" = translation is the big karaoke line, original small below
                                         # "small" = original big, translation small below
    translate_provider: str = "mymemory"  # "mymemory" (free, no account) | "deepl" (free key)
    translate_email: str = ""            # optional: raises MyMemory's free daily limit
    deepl_key: str = ""                  # your own DeepL API key (stored only on this PC)
    show_romaji: bool = False            # Japanese pronunciation in English letters
    first_run: bool = True
    auto_update_check: bool = True
    genius_token: str = ""                # Song meanings: your Genius API access token (not a password)
    meanings_ai: bool = False             # Song meanings: AI explains lines Genius hasn't
    anthropic_key: str = ""               # Song meanings: your Anthropic API key for the AI backup
    meanings_follow: bool = True
    meanings_text_size: str = "normal"    # Song meanings: size of the meaning text - normal | large | huge          # Song meanings: highlight the line being sung
    use_my_files: bool = True             # use the songs you synced yourself (files in "my_lyrics")
    ai_sync_enabled: bool = False         # AI sync: listen to each song once and time the lyrics
    ai_model: str = "light"               # "light" (~0.5 GB) | "best" (~1.5 GB) | "max" (~3 GB)
    ai_second_pass: bool = False          # AI sync: listen again to the parts it missed (slower)
    ai_vocals: bool = False               # AI sync: separate the voice from the music first (slower)
    ai_threads: int = 2                   # CPU cores the AI may use while it works
    ai_progress_style: str = "bar"        # AI sync progress in the overlay header: "bar" | "percent" | "off"
    ai_listen: str = "app"                # "app" = only Spotify's sound | "pc" = everything the PC plays
    popup_style: str = "app"             # "app" = sleek sliding cards + dark menu | "windows" = standard Windows look       # look for new versions on GitHub (start-up + every few hours)
    # Keyboard shortcuts (empty = off). Change them in Settings → Shortcuts.
    hk_toggle: str = "Ctrl+Alt+L"
    hk_click: str = "Ctrl+Alt+K"
    hk_settings: str = "Ctrl+Alt+O"
    hk_earlier: str = "Ctrl+Alt+Left"
    hk_later: str = "Ctrl+Alt+Right"
    hk_version: str = "Ctrl+Alt+V"
    hk_report: str = "Ctrl+Alt+R"

    # ------------------------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        s = cls()
        p = path or cls.path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                known = {f.name: f for f in fields(cls)}
                # older versions had an on/off 'show_context_lines' option
                if "lines_shown" not in data and data.get("show_context_lines") is False:
                    data["lines_shown"] = 1
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
        # older versions had one size for everything: keep their look by deriving the other two
        if int(self.side_font_size) <= 0:
            self.side_font_size = int(self.font_size * 0.62)
        if int(self.title_font_size) <= 0:
            self.title_font_size = int(self.font_size * 0.45)
        self.side_font_size = max(8, min(80, int(self.side_font_size)))
        self.title_font_size = max(8, min(60, int(self.title_font_size)))
        self.fixed_height = max(0, min(2000, int(self.fixed_height)))
        self.background_opacity = max(0.0, min(1.0, float(self.background_opacity)))
        self.window_opacity = max(0.1, min(1.0, float(self.window_opacity)))
        self.width = max(300, min(4000, int(self.width)))
        self.margin = max(0, min(1000, int(self.margin)))
        self.lines_shown = max(1, min(3, int(self.lines_shown)))
        self.sync_offset_ms = max(-10000, min(10000, int(self.sync_offset_ms)))
        if self.position not in POSITIONS:
            self.position = "bottom-center"
        if self.popup_style not in ("app", "windows"):
            self.popup_style = "app"
        if self.design not in ("classic", "aurora", "hud", "spotify"):
            self.design = "classic"
        if self.background_style not in ("solid", "frosted"):
            self.background_style = "solid"
        self.corner_radius = max(0, min(40, int(self.corner_radius)))
        if self.ai_model not in ("light", "best", "max"):
            self.ai_model = "light"
        self.ai_threads = max(1, min(8, int(self.ai_threads)))
        if self.meanings_text_size not in ("normal", "large", "huge"):
            self.meanings_text_size = "normal"
        if self.ai_progress_style not in ("bar", "percent", "off"):
            self.ai_progress_style = "bar"
        if self.ai_listen not in ("app", "pc"):
            self.ai_listen = "app"
        if self.word_timing_mode not in ("syllables", "letters"):
            self.word_timing_mode = "syllables"
        if self.long_lines not in ("widen", "shrink", "wrap"):
            self.long_lines = "widen"
        from .hotkeys import ACTIONS, is_valid, normalise
        for field, _, default in ACTIONS:
            val = getattr(self, field)
            setattr(self, field, normalise(val) if val and is_valid(val) else ("" if not val else default))
        if self.header_art_size not in ("small", "medium", "large"):
            self.header_art_size = "medium"
        if self.album_art_position not in ("auto", "left", "header"):
            self.album_art_position = "auto"
        if self.time_display not in ("off", "left", "elapsed"):
            self.time_display = "off"
        if self.translation_layout not in ("big", "small"):
            self.translation_layout = "big"
        if self.translate_provider not in ("mymemory", "deepl"):
            self.translate_provider = "mymemory"
        from .themes import HIGHLIGHT_STYLES
        from .translate import LANGUAGES
        if self.highlight_style not in HIGHLIGHT_STYLES:
            self.highlight_style = "wipe"
        if self.translate_target not in LANGUAGES:
            self.translate_target = "en"

    # ------------------------------------------------------------------ backups
    @staticmethod
    def backup_dir() -> Path:
        return app_data_dir() / "settings_backups"

    def backup(self, reason: str = "manual") -> Path | None:
        """Save a copy of the current settings (keeps the newest 20)."""
        from datetime import datetime
        try:
            d = self.backup_dir()
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            p = d / f"settings-{stamp}-{reason}.json"
            n = 1
            while p.exists():
                n += 1
                p = d / f"settings-{stamp}-{reason}-{n}.json"
            p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            for old in sorted(d.glob("settings-*.json"), key=lambda f: f.stat().st_mtime_ns)[:-BACKUPS_KEPT]:
                old.unlink(missing_ok=True)
            return p
        except Exception:
            log.exception("Could not back up settings")
            return None

    @classmethod
    def list_backups(cls) -> list[dict]:
        """Newest first: {"path", "time" (datetime), "reason"}."""
        import re
        from datetime import datetime
        out = []
        try:
            files = sorted(cls.backup_dir().glob("settings-*.json"), key=lambda f: f.stat().st_mtime_ns,
                           reverse=True)
        except OSError:
            return out
        for f in files:
            m = re.match(r"settings-(\d{8}-\d{6})-([a-z-]+?)(?:-\d+)?\.json$", f.name)
            if not m:
                continue
            try:
                when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            out.append({"path": f, "time": when, "reason": m.group(2)})
        return out

    def restore_from(self, path: Path) -> None:
        """Replace these settings with a backup (keys and tokens you have now are kept if the backup lacks them)."""
        old = Settings.load(path)
        keep = {k: getattr(self, k) for k in ("translate_email", "deepl_key", "genius_token", "anthropic_key")}
        for k, v in vars(old).items():
            setattr(self, k, v)
        for k, v in keep.items():
            if not getattr(self, k):
                setattr(self, k, v)
        self.first_run = False
        self.clamp()
        self.save()

    def auto_backup(self) -> None:
        """Once a day, quietly keep a copy of your settings."""
        from datetime import datetime, timedelta
        autos = [b for b in self.list_backups() if b["reason"] == "auto"]
        if not autos or datetime.now() - autos[0]["time"] > timedelta(hours=20):
            self.backup("auto")

    def save(self) -> None:
        try:
            p = self.path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            log.exception("Could not save settings")
