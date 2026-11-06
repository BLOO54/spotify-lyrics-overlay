"""Reading "what's playing" from Windows.

Plain English: Windows 10/11 has a built-in "media session" system - it's what
powers the little media popup when you press a volume key. Spotify reports
the current song, artist, playback position and play/pause state to it.
We simply *read* that information. We never touch Spotify itself, never log
in, and never need a password.

This uses Microsoft's official API (GlobalSystemMediaTransportControlsSessionManager)
through the `winrt` Python packages.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.4  # seconds

_EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)


@dataclass
class MediaState:
    available: bool                 # False = no matching player running
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    position: float = 0.0
    last_updated: datetime | None = None
    playing: bool = False
    source_app: str = ""
    error: str = ""

    @property
    def track_key(self) -> tuple:
        return (self.title, self.artist, self.album)


def _seconds(v) -> float:
    """winrt TimeSpan -> seconds (handles both old and new winrt bindings)."""
    if v is None:
        return 0.0
    if isinstance(v, timedelta):
        return v.total_seconds()
    if hasattr(v, "duration"):          # older bindings: 100-nanosecond ticks
        return v.duration / 10_000_000
    try:
        return float(v)
    except Exception:
        return 0.0


def _datetime(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    elif hasattr(v, "universal_time"):  # ticks since 1601
        dt = _EPOCH_1601 + timedelta(microseconds=v.universal_time / 10)
    else:
        return None
    if dt.year < 2000:
        return None                      # "not set"
    return dt


def _is_spotify(app_id: str) -> bool:
    return "spotify" in (app_id or "").lower()


class MediaWatcher:
    """Polls Windows on a background thread and calls `callback(MediaState)`."""

    def __init__(self, callback, spotify_only: bool = True):
        self._callback = callback
        self.spotify_only = spotify_only
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="MediaWatcher", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # never let this thread die silently
            log.exception("Media watcher crashed")
            self._callback(MediaState(False, error=f"Media watcher error: {e}"))

    async def _main(self):
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )
        except Exception as e:
            log.error("winrt not available: %s", e)
            self._callback(MediaState(False, error="Windows media integration is not available "
                                                    "(this feature needs Windows 10 or 11)."))
            return

        manager = None
        while not self._stop.is_set():
            try:
                if manager is None:
                    manager = await Manager.request_async()
                state = await self._read(manager)
            except Exception as e:
                log.warning("Media read failed: %s", e)
                manager = None
                state = MediaState(False, error="Could not read Windows media info")
            self._callback(state)
            await asyncio.sleep(POLL_INTERVAL)

    async def _read(self, manager) -> MediaState:
        session = None
        sessions = list(manager.get_sessions())
        for s in sessions:
            if _is_spotify(s.source_app_user_model_id):
                session = s
                break
        if session is None and not self.spotify_only:
            session = manager.get_current_session()
        if session is None:
            return MediaState(False)

        props = await session.try_get_media_properties_async()
        tl = session.get_timeline_properties()
        info = session.get_playback_info()

        status = info.playback_status if info is not None else None
        # PlaybackStatus enum: 4 = Playing, 5 = Paused
        try:
            playing = int(status) == 4
        except Exception:
            playing = str(status).upper().endswith("PLAYING")

        start = _seconds(tl.start_time) if tl else 0.0
        end = _seconds(tl.end_time) if tl else 0.0
        pos = _seconds(tl.position) if tl else 0.0
        return MediaState(
            available=True,
            title=(props.title or "") if props else "",
            artist=(props.artist or "") if props else "",
            album=(props.album_title or "") if props else "",
            duration=max(0.0, end - start),
            position=max(0.0, pos - start),
            last_updated=_datetime(tl.last_updated_time) if tl else None,
            playing=playing,
            source_app=session.source_app_user_model_id or "",
        )
