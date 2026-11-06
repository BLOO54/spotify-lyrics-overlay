"""Your own lyrics files, and the tap-along tool that makes them.

Plain English:
* Put a .lrc file in %APPDATA%\\SpotifyLyricsOverlay\\my_lyrics and name it
  "Artist - Title.lrc" (e.g. "Jamie Paige - birdbrain.lrc"). Whenever that song
  plays, the app uses your file instead of the lyrics website. Capital letters,
  spaces and punctuation in the name don't matter.
* "Sync this song myself" records a timestamp each time you tap at the start of a
  line, then writes such a file for you.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# People tap a little after they hear a line start; move taps this much earlier.
TAP_REACTION = 0.15


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9぀-ヿ㐀-鿿가-힯]+", "", (s or "").lower())


def safe_name(artist: str, title: str) -> str:
    raw = f"{artist} - {title}" if artist else title
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw).strip(" .")
    return (raw or "song")[:150] + ".lrc"


class MyLyrics:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "README.txt"
        if not readme.exists():
            try:
                readme.write_text(
                    "Put your own synced lyrics here as .lrc files named \"Artist - Title.lrc\"\n"
                    "(for example: \"Jamie Paige - birdbrain.lrc\"). They are used instead of the\n"
                    "lyrics website for that song. \"Sync this song myself\" in the tray menu makes\n"
                    "these files for you.\n", encoding="utf-8")
            except OSError:
                pass

    def find(self, title: str, artist: str) -> Path | None:
        want_full = _norm(f"{artist}{title}")
        want_title = _norm(title)
        title_only = None
        try:
            files = sorted(self.folder.glob("*.lrc"))
        except OSError:
            return None
        for f in files:
            stem = f.stem
            if " - " in stem:
                a, t = stem.split(" - ", 1)
                if _norm(a + t) == want_full:
                    return f
            elif _norm(stem) == want_title and want_title:
                title_only = title_only or f
        return title_only

    def read(self, title: str, artist: str) -> str | None:
        f = self.find(title, artist)
        if f is None:
            return None
        try:
            return f.read_text(encoding="utf-8-sig")
        except OSError:
            log.exception("could not read %s", f)
            return None

    def save(self, title: str, artist: str, lrc_text: str) -> Path:
        old = self.find(title, artist)
        target = old or (self.folder / safe_name(artist, title))
        target.write_text(lrc_text, encoding="utf-8")
        return target


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    m = int(t // 60)
    s = t - m * 60
    return f"[{m:02d}:{s:05.2f}]"


@dataclass
class TapSession:
    """Records one timestamp per lyric line as you tap."""
    lines: list[str]
    taps: list[float] = field(default_factory=list)

    @property
    def next_index(self) -> int:
        return len(self.taps)

    @property
    def done(self) -> bool:
        return len(self.taps) >= len(self.lines)

    def tap(self, position: float) -> bool:
        """Mark that the next line starts now. Returns False if all lines are already timed."""
        if self.done:
            return False
        t = max(0.0, position - TAP_REACTION)
        if self.taps and t <= self.taps[-1]:
            t = self.taps[-1] + 0.05            # keep lines in order even after a double-tap
        self.taps.append(t)
        return True

    def undo(self) -> None:
        if self.taps:
            self.taps.pop()

    def to_lrc(self, title: str, artist: str, song_end: float = 0.0) -> str:
        out = [f"[ti:{title}]", f"[ar:{artist}]", "[by:Spotify Lyrics Overlay tap-along]"]
        for line, t in zip(self.lines, self.taps):
            out.append(f"{fmt_ts(t)}{line}")
        # an empty line after the last one, so the last lyric doesn't stay up forever
        if self.taps:
            last = self.taps[-1]
            end = song_end if song_end > last + 1 else last + 6
            out.append(fmt_ts(min(end, last + 8)))
        return "\n".join(out) + "\n"


def lines_for_tapping(synced: str | None, plain: str | None) -> list[str]:
    """The lyric lines to tap through: from timed lyrics if we have them, else from plain text."""
    lines: list[str] = []
    if synced:
        for raw in synced.splitlines():
            text = re.sub(r"\[[^\]]*\]", "", raw)          # drop [mm:ss.xx] and [ar:...] tags
            text = re.sub(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>", "", text).strip()
            if text:
                lines.append(re.sub(r"\s+", " ", text))
    elif plain:
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in plain.splitlines() if ln.strip()]
    return lines
