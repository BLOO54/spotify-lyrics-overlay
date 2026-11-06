"""Demo mode: shows the overlay with a fake song so you can try the look and
settings without Spotify. Start it with:  SpotifyLyricsOverlay.exe --demo

The demo lyrics below are original placeholder text written for this app.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .media_session import MediaState

DEMO_TITLE = "Neon Horizon (Demo)"
DEMO_ARTIST = "Spotify Lyrics Overlay"
DEMO_DURATION = 40.0

# First half uses real word timestamps (enhanced LRC), second half only line
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


class DemoPlayer:
    """Pretends to be Spotify: loops the demo song forever."""

    def __init__(self, callback):
        self._cb = callback
        self._stop = threading.Event()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="DemoPlayer")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def position(self) -> float:
        return (time.monotonic() - self._start) % DEMO_DURATION

    def _run(self):
        while not self._stop.is_set():
            self._cb(MediaState(True, DEMO_TITLE, DEMO_ARTIST, "Demo", DEMO_DURATION,
                                self.position(), datetime.now(timezone.utc), True, "demo"))
            time.sleep(0.4)
