"""The "brain" that connects everything together.

Flow:
  Windows media info  ->  MediaWatcher (background thread)
        -> controller notices a new song -> asks LRCLIB for lyrics (background thread)
        -> overlay redraws ~60x per second using the playback clock.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import APP_NAME, win32
from .clock import PlaybackClock, PlaybackReport
from .config import Settings, app_data_dir
from .lrc import build_lyrics
from .lyrics_provider import LrclibClient, LyricsNetworkError
from .media_session import MediaState, MediaWatcher
from .song_sync import SongSync
from .overlay import OverlayWindow
from .settings_dialog import SettingsDialog
from .translate import (TranslationKeyError, TranslationQuotaError,
                        TranslationService)

log = logging.getLogger(__name__)

RETRY_SECONDS = 15
FRAME_MS = 16          # ~60 fps
TOPMOST_MS = 2000

HK_TOGGLE, HK_CLICK, HK_SETTINGS, HK_EARLIER, HK_LATER, HK_VERSION = range(1, 7)
HOTKEYS = {
    HK_TOGGLE: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["L"]),
    HK_CLICK: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["K"]),
    HK_SETTINGS: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["O"]),
    HK_EARLIER: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["LEFT"]),
    HK_LATER: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["RIGHT"]),
    HK_VERSION: (win32.MOD_CONTROL | win32.MOD_ALT, win32.VK["V"]),
}


def resource_path(rel: str) -> Path:
    """Find bundled files both when running from source and from the .exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / rel


class _Bridge(QObject):
    """Signals let background threads hand data safely to the UI thread."""
    media = Signal(object)
    lyrics = Signal(int, object)
    extras = Signal(int, object)
    versions = Signal(int, object)


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, handler):
        super().__init__()
        self._handler = handler

    def nativeEventFilter(self, event_type, message):
        try:
            et = event_type.data() if hasattr(event_type, "data") else bytes(event_type)
            if bytes(et) == b"windows_generic_MSG":
                msg, wparam = win32.parse_msg(int(message))
                if msg == win32.WM_HOTKEY:
                    self._handler(int(wparam))
                    return True, 0
        except Exception:
            log.exception("hotkey filter")
        return False, 0


class Controller(QObject):
    def __init__(self, app: QApplication, demo: bool = False, demo_song: str = "1", demo_start: float = 0.0):
        super().__init__()
        self.app = app
        self.s = Settings.load()
        self.clock = PlaybackClock()
        self.client = LrclibClient(app_data_dir() / "lyrics_cache")
        self.translator = TranslationService(app_data_dir() / "translation_cache")
        self._extras = None
        self._extras_key = None
        self._warned = set()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lyrics")
        self.bridge = _Bridge()
        self.bridge.media.connect(self._on_media)
        self.bridge.lyrics.connect(self._on_lyrics)
        self.bridge.extras.connect(self._on_extras)
        self.bridge.versions.connect(self._on_versions)
        self.song_sync = SongSync(app_data_dir() / "song_sync.json")
        self._song_offset_ms = 0
        self._version_busy = False

        self.track_key = None
        self.track: MediaState | None = None
        self.fetch_token = 0
        self.settings_dlg: SettingsDialog | None = None
        self.demo = demo
        self.demo_song = demo_song
        self._media_available = False

        self.icon = QIcon(str(resource_path("assets/icon.png")))
        app.setWindowIcon(self.icon)

        self.overlay = OverlayWindow(self.s)
        self.overlay.moved.connect(self._on_dragged)
        self.overlay.context_menu_requested.connect(lambda pt: self.menu.popup(pt))
        self.overlay.settings_requested.connect(self.open_settings)

        self._build_tray()

        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self._tick)
        self.frame_timer.start(FRAME_MS)
        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self.overlay.keep_on_top)
        self.top_timer.start(TOPMOST_MS)

        self.hotkey_filter = None
        self._register_hotkeys()

        self._apply_visibility()

        if demo:
            from .demo import DemoPlayer
            self.demo_player = DemoPlayer(self.bridge.media.emit, demo_song,
                                          resource_path("assets/icon.png"), demo_start)
            self.demo_player.start()
        else:
            self.watcher = MediaWatcher(self.bridge.media.emit, self.s.spotify_only)
            self.watcher.start()

        if self.s.first_run:
            self.s.first_run = False
            self.s.save()
            QTimer.singleShot(800, self._welcome)

    # ------------------------------------------------------------ tray & menu
    def _build_tray(self):
        self.menu = QMenu()
        self.act_show = QAction("Show overlay", self.menu, checkable=True)
        self.act_show.setChecked(self.s.visible)
        self.act_show.toggled.connect(self.set_visible)
        self.act_click = QAction("Click-through", self.menu, checkable=True)
        self.act_click.setChecked(self.s.click_through)
        self.act_click.toggled.connect(self.set_click_through)
        act_settings = QAction("Settings…", self.menu)
        act_settings.triggered.connect(self.open_settings)
        act_earlier = QAction("This song: lyrics earlier (−250 ms)\tCtrl+Alt+←", self.menu)
        act_earlier.triggered.connect(lambda: self.nudge_offset(+250))
        act_later = QAction("This song: lyrics later (+250 ms)\tCtrl+Alt+→", self.menu)
        act_later.triggered.connect(lambda: self.nudge_offset(-250))
        act_reset = QAction("This song: reset sync", self.menu)
        act_reset.triggered.connect(lambda: self.nudge_offset(None))
        act_version = QAction("Try another lyrics version\tCtrl+Alt+V", self.menu)
        act_version.triggered.connect(self.try_other_version)
        act_refetch = QAction("Reload lyrics", self.menu)
        act_refetch.triggered.connect(self.refetch)
        act_quit = QAction("Quit", self.menu)
        act_quit.triggered.connect(self.quit)
        for a in (self.act_show, self.act_click, act_settings):
            self.menu.addAction(a)
        self.menu.addSeparator()
        for a in (act_earlier, act_later, act_reset, act_version, act_refetch):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(act_quit)

        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(APP_NAME)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:          # left click
            self.set_visible(not self.s.visible)
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    def _welcome(self):
        self.tray.showMessage(
            APP_NAME,
            "Running in the system tray (bottom-right).\n"
            "Ctrl+Alt+L hide/show · Ctrl+Alt+K click-through · Ctrl+Alt+O settings",
            QSystemTrayIcon.MessageIcon.Information, 8000)

    # ------------------------------------------------------------ hotkeys
    def _register_hotkeys(self):
        if not win32.IS_WINDOWS:
            return
        self.hotkey_filter = HotkeyFilter(self._on_hotkey)
        self.app.installNativeEventFilter(self.hotkey_filter)
        failed = [hid for hid, (m, vk) in HOTKEYS.items() if not win32.register_hotkey(hid, m, vk)]
        if failed:
            QTimer.singleShot(1500, lambda: self.tray.showMessage(
                APP_NAME, "Some keyboard shortcuts are already used by another program. "
                "You can still use the tray icon menu.", QSystemTrayIcon.MessageIcon.Warning, 6000))

    def _on_hotkey(self, hid: int):
        if hid == HK_TOGGLE:
            self.set_visible(not self.s.visible)
        elif hid == HK_CLICK:
            self.set_click_through(not self.s.click_through)
        elif hid == HK_SETTINGS:
            self.open_settings()
        elif hid == HK_EARLIER:
            self.nudge_offset(+250)
        elif hid == HK_LATER:
            self.nudge_offset(-250)
        elif hid == HK_VERSION:
            self.try_other_version()

    # ------------------------------------------------------------ actions
    def set_visible(self, visible: bool):
        self.s.visible = bool(visible)
        self.s.save()
        if self.act_show.isChecked() != self.s.visible:
            self.act_show.setChecked(self.s.visible)
        self._apply_visibility()

    def _apply_visibility(self):
        should_show = self.s.visible
        if self.s.hide_when_paused and self.track is not None and not self.clock.playing:
            should_show = False
        if should_show and not self.overlay.isVisible():
            self.overlay.show()
            self.overlay.reposition()
        elif not should_show and self.overlay.isVisible():
            self.overlay.hide()

    def set_click_through(self, on: bool):
        self.s.click_through = bool(on)
        self.s.save()
        if self.act_click.isChecked() != self.s.click_through:
            self.act_click.setChecked(self.s.click_through)
        self.overlay.apply_click_through()
        if self.settings_dlg:
            self.settings_dlg.sync_from_settings()
        self.tray.showMessage(APP_NAME, "Click-through ON – the overlay ignores the mouse."
                              if on else "Click-through OFF – you can drag the overlay.",
                              QSystemTrayIcon.MessageIcon.Information, 2000)

    def nudge_offset(self, delta_ms):
        """Adjust the timing of the song that's playing now (remembered for that song)."""
        if not (self.track and self.track.title):
            self.tray.showMessage(APP_NAME, "Play a song first, then adjust its timing.",
                                  QSystemTrayIcon.MessageIcon.Information, 2000)
            return
        title, artist = self.track.title, self.track.artist
        new = 0 if delta_ms is None else self._song_offset_ms + delta_ms
        self.song_sync.set(title, artist, new)
        self._song_offset_ms = self.song_sync.get(title, artist)
        ms = self._song_offset_ms
        what = "back to normal" if ms == 0 else (f"{abs(ms) / 1000:.2f} s {'earlier' if ms > 0 else 'later'}")
        self.tray.showMessage(APP_NAME, f"{title}: lyrics {what} (remembered for this song)",
                              QSystemTrayIcon.MessageIcon.Information, 1800)

    def try_other_version(self):
        """Switch to the next timed-lyrics version LRCLIB has for this song."""
        st = self.track
        if self.demo or not (st and st.title and st.artist) or self._version_busy:
            return
        self._version_busy = True
        token = self.fetch_token
        self.tray.showMessage(APP_NAME, "Looking for other lyrics versions…",
                              QSystemTrayIcon.MessageIcon.Information, 1500)
        title, artist, album, dur = st.title, st.artist, st.album, st.duration

        def work():
            try:
                return self.client.alternatives(title, artist, album, dur)
            except Exception as e:
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.versions.emit(token, f.result()))

    def _on_versions(self, token: int, result):
        self._version_busy = False
        if token != self.fetch_token or not self.track:
            return
        if isinstance(result, Exception):
            self.tray.showMessage(APP_NAME, "Couldn't reach the lyrics service – try again in a moment.",
                                  QSystemTrayIcon.MessageIcon.Warning, 2500)
            return
        if len(result) <= 1:
            self.tray.showMessage(APP_NAME, "No other timed versions of this song's lyrics were found. "
                                  "You can still fix the timing with Ctrl+Alt+← / →.",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        current = self._last_result.synced if self._last_result else None
        idx = next((i for i, r in enumerate(result) if r.synced == current), -1)
        nxt = (idx + 1) % len(result)
        chosen = result[nxt]
        st = self.track
        self.client.remember_choice(st.title, st.artist, st.duration, chosen)
        self._last_result = chosen
        self._extras, self._extras_key = None, None
        self._show_result(chosen)
        self.tray.showMessage(APP_NAME, f"Lyrics version {nxt + 1} of {len(result)} (remembered for this song)",
                              QSystemTrayIcon.MessageIcon.Information, 2500)

    def open_settings(self):
        if self.settings_dlg is None:
            self.settings_dlg = SettingsDialog(self.s)
            self.settings_dlg.changed.connect(self._settings_changed)
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def _settings_changed(self):
        self.act_click.setChecked(self.s.click_through)
        self.act_show.setChecked(self.s.visible)
        if not self.demo:
            self.watcher.spotify_only = self.s.spotify_only
        self.overlay.apply_settings()
        # re-build lyrics so "estimate word timing" takes effect immediately
        if self._last_result is not None:
            self._show_result(self._last_result)
        self._apply_visibility()

    def _on_dragged(self, x, y):
        self.s.position = "custom"
        self.s.custom_x, self.s.custom_y = x, y
        self.s.save()
        if self.settings_dlg:
            self.settings_dlg.sync_from_settings()

    def refetch(self):
        if self.track and self.track.title and self.track.artist:
            if self.client.cache_dir:
                f = self.client._cache_file(self.track.title, self.track.artist, self.track.duration)
                if f and f.exists():
                    f.unlink()
            self._fetch(self.track)

    def quit(self):
        try:
            for hid in HOTKEYS:
                win32.unregister_hotkey(hid)
            if self.demo:
                self.demo_player.stop()
            else:
                self.watcher.stop()
            self.pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self.tray.hide()
            self.app.quit()

    # ------------------------------------------------------------ media updates
    _last_result = None

    def _on_media(self, st: MediaState):
        if not st.available:
            if self._media_available or self.track_key is not None:
                self.track_key = None
                self.track = None
                self._last_result = None
                self.overlay.set_track("", "")
                self.overlay.set_album_art(None)
                self.overlay.duration = 0.0
                self.overlay.playing = False
                self.overlay.set_lyrics(None)
            self._media_available = False
            self.overlay.set_status(st.error or "Waiting for Spotify… (start playing a song)")
            return
        self._media_available = True

        self.clock.update(PlaybackReport(st.position, st.playing, st.last_updated, st.duration))
        self.overlay.playing = st.playing
        self.overlay.duration = st.duration
        if st.track_key != self.track_key:
            self.track_key = st.track_key
            self.track = st
            self._last_result = None
            self.clock.reset(self.clock.estimate_reported_now(
                PlaybackReport(st.position, st.playing, st.last_updated, st.duration)), st.playing)
            self.overlay.set_track(st.title, st.artist)
            self._extras, self._extras_key = None, None
            self._song_offset_ms = self.song_sync.get(st.title, st.artist) if st.title else 0
            if not st.title:
                self.overlay.set_lyrics(None, "")
            elif not st.artist:
                # Spotify adverts and podcasts often have no artist
                self.overlay.set_lyrics(None, "No lyrics for this item")
            else:
                self.overlay.set_lyrics(None, "Searching for lyrics…")
                # wait a moment: Spotify sometimes sends the song length a beat later
                token = self.fetch_token = self.fetch_token + 1
                QTimer.singleShot(350, lambda: token == self.fetch_token and self._fetch(self.track))
        else:
            self.track = st
        self.overlay.set_album_art(st.thumbnail)
        self._apply_visibility()

    def _fetch(self, st: MediaState):
        self.fetch_token += 1
        token = self.fetch_token
        title, artist, album, dur = st.title, st.artist, st.album, st.duration

        def work():
            try:
                if self.demo:
                    from .demo import demo_lyrics
                    from .lyrics_provider import LyricsResult
                    return LyricsResult(demo_lyrics(self.demo_song), None, False, "demo")
                return self.client.fetch(title, artist, album, dur)
            except Exception as e:  # handed back to UI thread
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.lyrics.emit(token, f.result()))

    def _on_lyrics(self, token: int, result):
        if token != self.fetch_token:
            return  # an old answer for a song that's no longer playing
        if isinstance(result, LyricsNetworkError):
            log.warning("Lyrics network error: %s", result)
            self.overlay.set_lyrics(None, f"Can't reach the lyrics service – retrying in {RETRY_SECONDS}s")
            QTimer.singleShot(RETRY_SECONDS * 1000,
                              lambda: token == self.fetch_token and self.track and self._fetch(self.track))
            return
        if isinstance(result, Exception):
            log.error("Lyrics error: %r", result)
            self.overlay.set_lyrics(None, "Something went wrong loading lyrics")
            return
        self._last_result = result
        self._show_result(result)

    def _show_result(self, result):
        if result.instrumental:
            self.overlay.set_lyrics(None, "Instrumental ♪")
        elif result.synced:
            ly = build_lyrics(result.synced, result.plain, False, self.s.estimate_word_timing, result.source)
            if ly.synced:
                self.overlay.set_lyrics(ly)
                self._request_extras(ly)
            else:
                self.overlay.set_lyrics(None, "Lyrics found, but couldn't read their timing")
        elif result.plain:
            self.overlay.set_lyrics(None, "Only unsynced lyrics exist for this song")
        else:
            self.overlay.set_lyrics(None, "No lyrics found for this song")

    # ------------------------------------------------------------ translation / romaji
    def _extras_settings(self):
        s = self.s
        return (s.translate_enabled, s.translate_target, s.translate_provider,
                s.translate_email, s.deepl_key, s.show_romaji)

    def _request_extras(self, ly):
        s = self.s
        if not (s.translate_enabled or s.show_romaji):
            return
        key = (self.fetch_token, self._extras_settings())
        if key == self._extras_key and self._extras is not None:
            self.overlay.set_extras(self._extras)      # same song, same settings: reuse
            return
        self._extras_key = key
        token = self.fetch_token
        lines = [ln.text for ln in ly.lines]
        if s.translate_enabled:
            from .translate import detect_language
            src = detect_language(lines)
            if src is not None and src != s.translate_target:
                self.overlay.set_translation_pending(True)
        opts = dict(translate=s.translate_enabled, romaji=s.show_romaji, target=s.translate_target,
                    provider=s.translate_provider, email=s.translate_email, deepl_key=s.deepl_key)

        def work():
            try:
                if self.demo:
                    from .demo import demo_extras
                    return demo_extras(self.demo_song, lines, opts)
                return self.translator.build(lines, **opts)
            except Exception as e:
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.extras.emit(token, f.result()))

    def _warn_once(self, key, text):
        if key in self._warned:
            return
        self._warned.add(key)
        self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _on_extras(self, token: int, result):
        if token != self.fetch_token:
            return
        if isinstance(result, Exception):
            self.overlay.set_translation_pending(False)
        if isinstance(result, TranslationQuotaError):
            self._warn_once("quota", f"{result}. Translations will work again later – or add an "
                            "email / DeepL key in Settings → Lyrics & translation.")
            self._extras_key = None
            return
        if isinstance(result, TranslationKeyError):
            self._warn_once("key", str(result))
            self._extras_key = None
            return
        if isinstance(result, Exception):
            log.warning("Translation failed: %r", result)
            self._warn_once("neterr", "Couldn't reach the translation service – lyrics still work.")
            self._extras_key = None
            return
        self._extras = result
        self.overlay.set_extras(result)

    # ------------------------------------------------------------ per-frame
    def _tick(self):
        if self.overlay.isVisible():
            self.overlay.play_pos = self.clock.position()
            self.overlay.position = self.clock.position() + (self.s.sync_offset_ms + self._song_offset_ms) / 1000.0
            self.overlay.update()
