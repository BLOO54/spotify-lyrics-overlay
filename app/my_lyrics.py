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
    def __init__(self, folder: Path, readme_text: str | None = None):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "README.txt"
        if not readme.exists():
            try:
                readme.write_text(readme_text or (
                    "Put your own synced lyrics here as .lrc files named \"Artist - Title.lrc\"\n"
                    "(for example: \"Jamie Paige - birdbrain.lrc\"). They are used instead of the\n"
                    "lyrics website for that song. \"Sync this song myself\" in the tray menu makes\n"
                    "these files for you.\n"), encoding="utf-8")
            except OSError:
                pass

    def forget(self, title: str, artist: str) -> bool:
        f = self.find(title, artist)
        if f is None:
            return False
        try:
            f.unlink()
            return True
        except OSError:
            return False

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
    """Timing for every line of a song, edited by tapping, clicking and nudging.

    times[i] is when line i starts, in song time (None = not timed yet).
    `cursor` is the line the next Space tap will time.
    """
    lines: list[str]
    times: list = field(default_factory=list)
    cursor: int = 0

    def __post_init__(self):
        if len(self.times) != len(self.lines):
            self.times = (list(self.times) + [None] * len(self.lines))[:len(self.lines)]

    # kept for older callers / tests
    @property
    def taps(self) -> list[float]:
        return [t for t in self.times if t is not None]

    @property
    def next_index(self) -> int:
        return self.cursor

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.lines)

    @property
    def timed_count(self) -> int:
        return sum(t is not None for t in self.times)

    def tap(self, position: float) -> bool:
        """The line at the cursor starts now. Returns False if past the last line."""
        if self.done:
            return False
        t = max(0.0, position - TAP_REACTION)
        prev = next((self.times[j] for j in range(self.cursor - 1, -1, -1) if self.times[j] is not None), None)
        if prev is not None and t <= prev:
            t = prev + 0.05                     # keep lines in order even after a double-tap
        self.times[self.cursor] = t
        self.cursor += 1
        return True

    def undo(self) -> None:
        """Step back to the previous line so it can be tapped again."""
        if self.cursor > 0:
            self.cursor -= 1

    def go_to(self, index: int) -> None:
        self.cursor = max(0, min(index, len(self.lines)))

    def nudge(self, index: int, delta: float) -> None:
        if 0 <= index < len(self.times) and self.times[index] is not None:
            self.times[index] = max(0.0, self.times[index] + delta)

    def rewind_target(self, index: int, lead: float = 1.5) -> float:
        """Where to jump the song so you can hear line `index` coming: just before the line above it."""
        before = next((self.times[j] for j in range(index - 1, -1, -1) if self.times[j] is not None), None)
        if before is None:
            return 0.0
        return max(0.0, before - lead)

    def to_lrc(self, title: str, artist: str, song_end: float = 0.0, shift: float = 0.0) -> str:
        """Write the timed lines as LRC text (shift = seconds added to every time)."""
        out = [f"[ti:{title}]", f"[ar:{artist}]", "[by:Spotify Lyrics Overlay tap-along]"]
        timed = sorted(((t + shift, line) for line, t in zip(self.lines, self.times) if t is not None),
                       key=lambda x: x[0])
        for t, line in timed:
            out.append(f"{fmt_ts(t)}{line}")
        if timed:                              # an empty line so the last lyric clears
            last = timed[-1][0]
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


def list_synced(folder: Path, kind: str) -> list[dict]:
    """Every .lrc file in a folder, with the song name, when it was made and any AI note."""
    from datetime import datetime
    out = []
    try:
        files = sorted(folder.glob("*.lrc"))
    except OSError:
        return out
    for f in files:
        title = artist = note = ""
        try:
            head = f.read_text(encoding="utf-8-sig", errors="replace")[:2000]
        except OSError:
            continue
        for line in head.splitlines():
            m = re.match(r"^\[(ti|ar|#):(.*)\]\s*$", line.strip())
            if m:
                key, val = m.group(1), m.group(2).strip()
                if key == "ti":
                    title = val
                elif key == "ar":
                    artist = val
                else:
                    note = val
        if not title:
            stem = f.stem
            artist, title = (stem.split(" - ", 1) if " - " in stem else ("", stem))
        try:
            when = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            when = ""
        out.append({"kind": kind, "path": str(f), "title": title, "artist": artist,
                    "modified": when, "note": note})
    return out
