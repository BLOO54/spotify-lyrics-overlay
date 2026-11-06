"""Remembers a sync adjustment for individual songs.

If one song's lyrics are always a little early or late, press Ctrl+Alt+← / →
while it plays; the adjustment is saved for that song only and used every time
it plays again. Stored in %APPDATA%\\SpotifyLyricsOverlay\\song_sync.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


def song_key(title: str, artist: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return f"{norm(title)}|{norm(artist)}"


class SongSync:
    def __init__(self, path: Path | None):
        self.path = path
        self._data: dict[str, int] = {}
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._data = {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}
            except Exception:
                log.exception("could not read per-song sync file")

    def get(self, title: str, artist: str) -> int:
        return self._data.get(song_key(title, artist), 0)

    def set(self, title: str, artist: str, ms: int) -> None:
        key = song_key(title, artist)
        ms = max(-15000, min(15000, int(ms)))
        if ms == 0:
            self._data.pop(key, None)
        else:
            self._data[key] = ms
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.exception("could not save per-song sync file")
