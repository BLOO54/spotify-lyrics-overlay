"""Keeping track of where we are in the song.

Plain English: Windows only tells us Spotify's playback position now and then
(for example when you press play, pause or skip). In between, we run our own
stopwatch. Whenever Windows gives us a fresh position, we quietly re-sync
the stopwatch, but only if it has drifted noticeably, so the lyrics don't jitter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

# Re-sync if our stopwatch disagrees with Spotify by more than this (seconds).
RESYNC_THRESHOLD = 0.35
# Smaller differences than this are caught up smoothly (a little faster / slower for a moment)
# instead of jumping; bigger ones (a seek) jump straight there.
GLIDE_LIMIT = 1.5


@dataclass
class PlaybackReport:
    """One snapshot of what Windows says about the player."""
    position: float                    # seconds into the track, as reported
    playing: bool
    last_updated: datetime | None      # when the player reported that position (UTC)
    duration: float = 0.0


class PlaybackClock:
    def __init__(self, time_fn=time.monotonic, utcnow_fn=None):
        self._time = time_fn
        self._utcnow = utcnow_fn or (lambda: datetime.now(timezone.utc))
        self._anchor_pos = 0.0
        self._anchor_time = self._time()
        self._playing = False
        self._duration = 0.0
        self._last_raw: tuple[float, datetime | None] | None = None
        self.on_correct = None         # optional: called (kind, seconds off) when the clock is corrected
        self._glide = 0.0              # correction still to fade out (seconds)
        self._glide_start = 0.0
        self._glide_len = 0.0

    # ------------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._playing

    def position(self) -> float:
        pos = self._anchor_pos
        if self._playing:
            pos += self._time() - self._anchor_time
            if self._glide:
                left = 1.0 - (self._time() - self._glide_start) / self._glide_len
                if left > 0:
                    pos += self._glide * left
                else:
                    self._glide = 0.0
        if self._duration > 0:
            pos = min(pos, self._duration)
        return max(0.0, pos)

    def reset(self, position: float = 0.0, playing: bool = False) -> None:
        self._anchor_pos = position
        self._anchor_time = self._time()
        self._playing = playing
        self._last_raw = None
        self._glide = 0.0

    def _set(self, pos: float, playing: bool) -> None:
        self._anchor_pos = pos
        self._anchor_time = self._time()
        self._playing = playing
        self._glide = 0.0

    def estimate_reported_now(self, r: PlaybackReport) -> float:
        """Where the player is *right now*, according to its last report."""
        pos = r.position
        if r.playing and r.last_updated is not None:
            lu = r.last_updated
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            delta = (self._utcnow() - lu).total_seconds()
            # Ignore nonsense timestamps (e.g. year 1601 = "not set")
            limit = (r.duration + 5) if r.duration > 0 else 3600
            if 0 <= delta <= limit:
                pos += delta
        if r.duration > 0:
            pos = min(pos, r.duration)
        return max(0.0, pos)

    def update(self, r: PlaybackReport) -> None:
        """Feed a new report from Windows into the clock."""
        self._duration = r.duration
        raw = (round(r.position, 3), r.last_updated)
        is_new_report = raw != self._last_raw
        self._last_raw = raw

        if r.playing != self._playing:
            # play <-> pause: always take the player's word for it
            self._set(self.estimate_reported_now(r), r.playing)
            return
        if not r.playing:
            self._set(r.position, False)
            return
        if not is_new_report:
            # Same old report: our own stopwatch is the better source.
            return
        target = self.estimate_reported_now(r)
        now = self.position()
        diff = target - now
        if abs(diff) > RESYNC_THRESHOLD and self.on_correct is not None:
            try:
                self.on_correct("jump" if abs(diff) > GLIDE_LIMIT else "glide", diff)
            except Exception:
                pass
        if abs(diff) > GLIDE_LIMIT:
            self._set(target, True)                 # a real jump (seek): go straight there
        elif abs(diff) > RESYNC_THRESHOLD:
            # small drift: be at the right place, but fade the difference out so the lyrics never
            # jump or run backwards (at most 1/3 slower or faster while catching up)
            self._set(target, True)
            self._glide = now - target
            self._glide_start = self._time()
            self._glide_len = max(0.5, abs(self._glide) * 3.0)
