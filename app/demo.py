"""Demo mode: shows the overlay with a fake song so you can try the look and
settings without Spotify.

    SpotifyLyricsOverlay.exe --demo                  English demo song
    SpotifyLyricsOverlay.exe --demo --demo-song jp   Japanese demo song (try translation / romaji)

All demo lyrics and translations below are original placeholder text written for this app.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .media_session import MediaState

DEMO_DURATION = 40.0

# First half has real word timestamps (enhanced LRC), second half only line
# timestamps, so you can see both karaoke modes.
DEMO_LRC = """[00:02.00]<00:02.00>Lights <00:02.60>are <00:02.90>racing <00:03.60>through <00:04.10>the <00:04.40>night <00:05.40>
[00:06.00]<00:06.00>Every <00:06.60>pixel <00:07.20>burning <00:07.90>bright <00:09.00>
[00:10.00]<00:10.00>Hold <00:10.40>the <00:10.60>line <00:11.10>and <00:11.40>don't <00:11.80>let <00:12.10>go <00:13.00>
[00:14.00]<00:14.00>We're <00:14.40>the <00:14.60>final <00:15.20>boss <00:15.70>of <00:15.90>the <00:16.10>show <00:17.20>
[00:18.50]
[00:21.00]This line only has a line timestamp
[00:24.50]so the words are estimated for you
[00:28.00]turn estimating off in settings
[00:31.50]to see plain line-by-line mode
[00:35.00]Thanks for trying the demo
"""

# Japanese demo: words separated by spaces so the karaoke highlight can step through them.
DEMO_JP_LRC = """[00:02.00]<00:02.00>夜空に <00:03.00>光る <00:03.80>星を <00:04.60>見て <00:05.60>
[00:06.00]<00:06.00>君の <00:06.80>名前を <00:07.80>呼んでいる <00:09.40>
[00:10.00]<00:10.00>明日も <00:11.00>きっと <00:11.80>会えるから <00:13.40>
[00:14.00]<00:14.00>走り出そう <00:15.40>今 <00:15.80>すぐに <00:17.00>
[00:18.50]
[00:21.00]Oh yeah 一緒に行こう
[00:25.00]消えない 光の 中へ
[00:29.00]ありがとう また 会おうね
"""

DEMO_JP_TRANSLATIONS = {
    "夜空に 光る 星を 見て": "Looking at the stars shining in the night sky",
    "君の 名前を 呼んでいる": "I'm calling out your name",
    "明日も きっと 会えるから": "Because I'm sure we'll meet tomorrow too",
    "走り出そう 今 すぐに": "Let's start running, right now",
    "Oh yeah 一緒に行こう": "Oh yeah, let's go together",
    "消えない 光の 中へ": "Into a light that never fades",
    "ありがとう また 会おうね": "Thank you, let's meet again",
}

SONGS = {
    "1": ("Neon Horizon (Demo)", "Spotify Lyrics Overlay", DEMO_LRC),
    "jp": ("星の道 Hoshi no Michi (Demo)", "Spotify Lyrics Overlay", DEMO_JP_LRC),
}


def demo_lyrics(song: str) -> str:
    return SONGS.get(song, SONGS["1"])[2]


def demo_extras(song: str, lines: list[str], opts: dict):
    """Canned translations for the demo (no internet use); romaji is real."""
    from .translate import Extras, detect_language, to_romaji
    ex = Extras(source_lang=detect_language(lines), target_lang=opts.get("target", "en"))
    if song == "jp":
        if opts.get("romaji"):
            for i, ln in enumerate(lines):
                r = to_romaji(ln)
                if r and r != ln:
                    ex.romaji[i] = r
        if opts.get("translate"):
            for i, ln in enumerate(lines):
                if ln in DEMO_JP_TRANSLATIONS:
                    ex.translations[i] = DEMO_JP_TRANSLATIONS[ln]
            ex.provider = "demo"
    return ex


class DemoPlayer:
    """Pretends to be Spotify: loops the demo song forever."""

    def __init__(self, callback, song: str = "1", art_path: Path | None = None):
        self._cb = callback
        self._song = song if song in SONGS else "1"
        self._stop = threading.Event()
        self._start = time.monotonic()
        self._art = None
        try:
            if art_path and Path(art_path).exists():
                self._art = Path(art_path).read_bytes()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._run, daemon=True, name="DemoPlayer")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def position(self) -> float:
        return (time.monotonic() - self._start) % DEMO_DURATION

    def _run(self):
        title, artist, _ = SONGS[self._song]
        while not self._stop.is_set():
            self._cb(MediaState(True, title, artist, "Demo", DEMO_DURATION,
                                self.position(), datetime.now(timezone.utc), True, "demo",
                                thumbnail=self._art))
            time.sleep(0.4)
