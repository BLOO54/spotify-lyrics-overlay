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
    thumbnail: bytes | None = None      # album art image (JPEG/PNG bytes)
    can_seek: bool = False              # does the player let other apps jump to a position?

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


def buffer_to_bytes(buf) -> bytes:
    """Copy a WinRT IBuffer into Python bytes (works across winrt versions)."""
    try:
        return bytes(memoryview(buf))
    except Exception:
        pass
    from winrt.windows.storage.streams import DataReader
    reader = DataReader.from_buffer(buf)
    n = int(buf.length)
    try:
        data = bytearray(n)
        reader.read_bytes(data)
        return bytes(data)
    except Exception:
        return bytes(reader.read_bytes(n))


async def read_thumbnail(ref) -> bytes | None:
    """Read the album-art stream Windows gives us."""
    if ref is None:
        return None
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    stream = await ref.open_read_async()
    size = int(stream.size)
    if size <= 0 or size > 20_000_000:
        return None
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    data = buffer_to_bytes(buf)
    return data or None


def _is_spotify(app_id: str) -> bool:
    return "spotify" in (app_id or "").lower()


class MediaWatcher:
    """Polls Windows on a background thread and calls `callback(MediaState)`."""

    def __init__(self, callback, spotify_only: bool = True):
        self._callback = callback
        self.spotify_only = spotify_only
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="MediaWatcher", daemon=True)
        self._thumb_key = None
        self._thumb: bytes | None = None
        self._thumb_tries = 0
        self._commands: list[str] = []
        self._session = None

    def request(self, command: str):
        """Ask the player to do something (e.g. "restart"). Runs on the watcher thread."""
        self._commands.append(command)

    async def _run_commands(self):
        while self._commands:
            cmd = self._commands.pop(0)
            s = self._session
            if s is None:
                continue
            try:
                if cmd.startswith("seek:"):
                    secs = max(0.0, float(cmd.split(":", 1)[1]))
                    start = _seconds(s.get_timeline_properties().start_time)
                    await s.try_change_playback_position_async(int((start + secs) * 10_000_000))
                elif cmd == "pause":
                    await s.try_pause_async()
                elif cmd == "restart":
                    info = s.get_playback_info()
                    can_seek = bool(info and info.controls and info.controls.is_playback_position_enabled)
                    if can_seek:
                        await s.try_change_playback_position_async(0)
                    else:
                        await s.try_skip_previous_async()     # Spotify: "previous" restarts the song
                    await s.try_play_async()
            except Exception as e:
                log.warning("media command %s failed: %s", cmd, e)

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
                await self._run_commands()
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
        self._session = session
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

        # Album art: only read it once per song (Spotify sometimes adds it a moment later)
        key = (props.title, props.artist, props.album_title) if props else None
        if key != self._thumb_key:
            self._thumb_key, self._thumb, self._thumb_tries = key, None, 0
        if self._thumb is None and self._thumb_tries < 8 and props is not None:
            self._thumb_tries += 1
            try:
                self._thumb = await read_thumbnail(props.thumbnail)
            except Exception as e:
                log.debug("album art read failed: %s", e)

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
            thumbnail=self._thumb,
            can_seek=bool(info is not None and info.controls is not None
                          and info.controls.is_playback_position_enabled),
        )
