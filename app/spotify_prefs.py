"""Read (never change) Spotify's own settings file, to see whether Crossfade is on.

With Crossfade, Spotify starts the next song a few seconds before this one ends, so the last
lines of every song are sung over the start of the next one. The AI sync needs to know that.
"""
from __future__ import annotations

import glob
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _prefs_files() -> list[Path]:
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "Spotify" / "prefs")                     # spotify.com installer
    local = os.environ.get("LOCALAPPDATA")
    if local:                                                                # Microsoft Store version
        out += [Path(p) for p in glob.glob(os.path.join(local, "Packages", "SpotifyAB.SpotifyMusic_*",
                                                        "LocalState", "Spotify", "prefs"))]
    return [p for p in out if p.is_file()]


def parse_crossfade(text: str) -> float:
    """Seconds of crossfade in a prefs file's text (0 = off)."""
    on, ms = None, None
    for line in text.splitlines():
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        if k in ("audio.crossfade_v2", "audio.crossfade"):
            on = v.lower() == "true"
        elif k in ("audio.crossfade.time_v2", "audio.crossfade.time"):
            try:
                ms = float(v)
            except ValueError:
                pass
    if not on:
        return 0.0
    return (ms if ms is not None else 5000.0) / 1000.0     # Spotify's default is 5 s


def crossfade_seconds() -> float | None:
    """Crossfade length in seconds, 0 when off, None when Spotify's settings can't be found."""
    best = None
    for p in _prefs_files():
        try:
            secs = parse_crossfade(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        best = secs if best is None else max(best, secs)
    return best
