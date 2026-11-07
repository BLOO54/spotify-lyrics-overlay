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

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import APP_NAME, APP_VERSION, win32
from .clock import PlaybackClock, PlaybackReport
from .config import Settings, app_data_dir
from .lrc import build_lyrics
from .lyrics_provider import LrclibClient, LyricsNetworkError
from .media_session import MediaState, MediaWatcher
from .song_sync import SongSync
from .my_lyrics import MyLyrics, lines_for_tapping
from . import hotkeys, updater
from .overlay import OverlayWindow
from .settings_dialog import SettingsDialog
from .toast import Toast, style_menu
from .translate import (TranslationKeyError, TranslationQuotaError,
                        TranslationService)

log = logging.getLogger(__name__)

RETRY_SECONDS = 15
FRAME_MS = 16          # ~60 fps
TOPMOST_MS = 2000

HK_TOGGLE, HK_CLICK, HK_SETTINGS, HK_EARLIER, HK_LATER, HK_VERSION = range(1, 7)
# hotkey id -> settings field holding its key combination
HOTKEY_FIELDS = {HK_TOGGLE: "hk_toggle", HK_CLICK: "hk_click", HK_SETTINGS: "hk_settings",
                 HK_EARLIER: "hk_earlier", HK_LATER: "hk_later", HK_VERSION: "hk_version"}


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
    update_checked = Signal(object, bool)     # result, silent
    update_downloaded = Signal(object)


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
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.update_downloaded.connect(self._on_update_downloaded)
        self.update_info = None
        self._update_busy = False
        self._announced_version = None
        self.song_sync = SongSync(app_data_dir() / "song_sync.json")
        self.my_lyrics = MyLyrics(app_data_dir() / "my_lyrics")
        self.tap_dlg = None
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
        self._registered = set()
        self._register_hotkeys()

        self._apply_visibility()

        if demo:
            from .demo import DemoPlayer
            art = resource_path("assets/demo_cover.png" if demo_song == "jp" else "assets/icon.png")
            self.demo_player = DemoPlayer(self.bridge.media.emit, demo_song, art, demo_start)
            self.demo_player.start()
        else:
            self.watcher = MediaWatcher(self.bridge.media.emit, self.s.spotify_only)
            self.watcher.start()

        # look for updates a little after start-up, then every few hours
        if not demo:
            QTimer.singleShot(8000, lambda: self.s.auto_update_check and self.check_updates(silent=True))
            self.update_timer = QTimer(self)
            self.update_timer.timeout.connect(lambda: self.s.auto_update_check and self.check_updates(silent=True))
            self.update_timer.start(updater.CHECK_EVERY_HOURS * 3600 * 1000)

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
        act_earlier = QAction("This song: lyrics earlier (−250 ms)", self.menu)
        act_earlier.triggered.connect(lambda: self.nudge_offset(+250))
        act_later = QAction("This song: lyrics later (+250 ms)", self.menu)
        act_later.triggered.connect(lambda: self.nudge_offset(-250))
        act_reset = QAction("This song: reset sync", self.menu)
        act_reset.triggered.connect(lambda: self.nudge_offset(None))
        act_version = QAction("Try another lyrics version", self.menu)
        act_version.triggered.connect(self.try_other_version)
        act_tap = QAction("Sync this song myself…", self.menu)
        act_tap.triggered.connect(self.open_tap_sync)
        act_folder = QAction("Open my lyrics folder", self.menu)
        act_folder.triggered.connect(self.open_lyrics_folder)
        act_refetch = QAction("Reload lyrics", self.menu)
        act_refetch.triggered.connect(self.refetch)
        self.act_update = QAction("Update available", self.menu)
        self.act_update.triggered.connect(self.start_update)
        self.act_update.setVisible(False)
        act_check = QAction("Check for updates", self.menu)
        act_check.triggered.connect(lambda: self.check_updates(silent=False))
        act_quit = QAction("Quit", self.menu)
        act_quit.triggered.connect(self.quit)
        self._hk_actions = {"hk_toggle": (self.act_show, "Show overlay"),
                            "hk_click": (self.act_click, "Click-through"),
                            "hk_settings": (act_settings, "Settings…"),
                            "hk_earlier": (act_earlier, "This song: lyrics earlier (−250 ms)"),
                            "hk_later": (act_later, "This song: lyrics later (+250 ms)"),
                            "hk_version": (act_version, "Try another lyrics version")}
        self._label_menu()
        for a in (self.act_show, self.act_click, act_settings):
            self.menu.addAction(a)
        self.menu.addSeparator()
        for a in (act_earlier, act_later, act_reset, act_version, act_tap, act_folder, act_refetch):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(self.act_update)
        self.menu.addAction(act_check)
        self.menu.addAction(act_quit)

        self.toast = Toast(self.icon)
        self.toast.clicked.connect(self._tray_message_clicked)
        if self.s.popup_style == "app":
            style_menu(self.menu)
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(APP_NAME)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._tray_message_clicked)
        self.tray.show()

    def notify(self, text: str, kind: str = "info", ms: int = 4000, action: str = ""):
        """Show a pop-up: the app's own sliding card, or a standard Windows notification."""
        if self.s.popup_style == "windows":
            icon = (QSystemTrayIcon.MessageIcon.Warning if kind == "warning"
                    else QSystemTrayIcon.MessageIcon.Information)
            self.tray.showMessage(APP_NAME, text, icon, ms)
        else:
            self.toast.show_message(APP_NAME, text, kind, ms, action)

    def _tray_message_clicked(self):
        if self.update_info is not None:
            self.start_update()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:          # left click
            self.set_visible(not self.s.visible)
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    def _label_menu(self):
        """Show each action's current shortcut next to it in the tray menu."""
        for field, (action, text) in self._hk_actions.items():
            keys = getattr(self.s, field)
            action.setText(f"{text}\t{keys}" if keys else text)

    def _welcome(self):
        parts = [f"{getattr(self.s, f)} {what}" for f, what in
                 (("hk_toggle", "hide/show"), ("hk_click", "click-through"), ("hk_settings", "settings"))
                 if getattr(self.s, f)]
        self.notify("Running in the system tray (bottom-right).\n" + " · ".join(parts), "info", 8000)

    # ------------------------------------------------------------ hotkeys
    def _register_hotkeys(self, announce_failures: bool = True):
        """(Re)register every shortcut from the settings with Windows."""
        if not win32.IS_WINDOWS:
            return []
        if self.hotkey_filter is None:
            self.hotkey_filter = HotkeyFilter(self._on_hotkey)
            self.app.installNativeEventFilter(self.hotkey_filter)
        self.unregister_hotkeys()
        failed = []
        for hid, field in HOTKEY_FIELDS.items():
            keys = getattr(self.s, field)
            parsed = hotkeys.parse(keys)
            if parsed is None:
                continue
            if win32.register_hotkey(hid, *parsed):
                self._registered.add(hid)
            else:
                failed.append(keys)
        if failed and announce_failures:
            QTimer.singleShot(1500, lambda: self.notify("These shortcuts are already used by another program: " + ", ".join(failed)
                + ". Pick different ones in Settings → Shortcuts.", "warning", 7000))
        return failed

    def unregister_hotkeys(self):
        for hid in list(getattr(self, "_registered", set())):
            win32.unregister_hotkey(hid)
        self._registered = set()

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
        self.notify("Click-through ON – the overlay ignores the mouse."
                              if on else "Click-through OFF – you can drag the overlay.", "info", 2000)

    def nudge_offset(self, delta_ms):
        """Adjust the timing of the song that's playing now (remembered for that song)."""
        if not (self.track and self.track.title):
            self.notify("Play a song first, then adjust its timing.", "info", 2000)
            return
        title, artist = self.track.title, self.track.artist
        new = 0 if delta_ms is None else self._song_offset_ms + delta_ms
        self.song_sync.set(title, artist, new)
        self._song_offset_ms = self.song_sync.get(title, artist)
        ms = self._song_offset_ms
        what = "back to normal" if ms == 0 else (f"{abs(ms) / 1000:.2f} s {'earlier' if ms > 0 else 'later'}")
        self.notify(f"{title}: lyrics {what} (remembered for this song)", "info", 1800)

    def try_other_version(self):
        """Switch to the next timed-lyrics version LRCLIB has for this song."""
        st = self.track
        if self.demo or not (st and st.title and st.artist) or self._version_busy:
            return
        self._version_busy = True
        token = self.fetch_token
        self.notify("Looking for other lyrics versions…", "info", 1500)
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
            self.notify("Couldn't reach the lyrics service – try again in a moment.", "warning", 2500)
            return
        if len(result) <= 1:
            self.notify("No other timed versions of this song's lyrics were found. "
                                  "You can still fix the timing with Ctrl+Alt+← / →.", "info", 4000)
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
        self.notify(f"Lyrics version {nxt + 1} of {len(result)} (remembered for this song)", "info", 2500)

    def open_settings(self):
        if self.settings_dlg is None:
            self.settings_dlg = SettingsDialog(self.s)
            self.settings_dlg.changed.connect(self._settings_changed)
            # while you press a new shortcut, our own shortcuts must not swallow the keys
            self.settings_dlg.capture_started.connect(self.unregister_hotkeys)
            self.settings_dlg.capture_finished.connect(self._hotkeys_changed)
            self.settings_dlg.check_updates_requested.connect(lambda: self.check_updates(silent=False))
            self.settings_dlg.update_requested.connect(self.start_update)
            self.settings_dlg.tap_sync_requested.connect(self.open_tap_sync)
            self.settings_dlg.lyrics_folder_requested.connect(self.open_lyrics_folder)
            self.settings_dlg.set_preview_source(self.overlay.grab)
            self._show_update_status()
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    # ------------------------------------------------------------ your own lyrics
    def open_lyrics_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.my_lyrics.folder)))

    def open_tap_sync(self):
        st = self.track
        if not (st and st.title):
            self.notify("Play the song you want to sync first.", "info", 3000)
            return
        res = self._last_result
        ly = self.overlay.lyrics
        offset_s = (self.s.sync_offset_ms + self._song_offset_ms) / 1000.0
        if ly and ly.synced:
            # start from the timing you see now (including any earlier/later fix), in song time
            sung = [ln for ln in ly.lines if not ln.is_break]
            lines = [ln.text for ln in sung]
            times = [max(0.0, ln.start - offset_s) for ln in sung]
        else:
            lines = lines_for_tapping(res.synced if res else None, res.plain if res else None)
            times = []
        if not lines:
            self.notify("There are no lyrics to sync for this song. You can put your own "
                                  "file in the lyrics folder (tray → Open my lyrics folder).", "info", 6000)
            return
        from .tap_sync import TapSyncDialog
        restart = (lambda: self.watcher.request("restart")) if not self.demo else None
        title, artist = st.title, st.artist
        # while editing, the overlay previews your timing directly (no per-song adjustment on top)
        self._offset_before_edit = self._song_offset_ms
        self._song_offset_ms = 0
        self.tap_dlg = TapSyncDialog(st.title, st.artist, lines, self.clock.position, restart, st.duration,
                                     initial_times=times, seek_fn=self.seek_to,
                                     can_seek_fn=lambda: bool(self.track and self.track.can_seek),
                                     shift=self.s.sync_offset_ms / 1000.0)
        self.tap_dlg.saved.connect(lambda text: self._tap_saved(title, artist, text))
        self.tap_dlg.preview.connect(self._tap_preview)
        log.info("Sync window opened: %d lines, %d already timed, player allows jumping: %s",
                 len(lines), sum(t is not None for t in times), bool(st.can_seek))
        self.tap_dlg.rejected.connect(self._tap_cancelled)
        self.tap_dlg.show()
        self.tap_dlg.raise_()
        self.tap_dlg.activateWindow()

    def seek_to(self, seconds: float):
        """Jump the song to this position (only if the player allows it)."""
        if self.demo or not (self.track and self.track.can_seek):
            return
        self.watcher.request(f"seek:{seconds:.3f}")
        self.clock.reset(seconds, self.clock.playing)      # move our stopwatch straight away

    def _tap_preview(self, text: str):
        ly = build_lyrics(text, None, False, self.s.estimate_word_timing, "my file",
                          estimate_mode=self.s.word_timing_mode)
        if ly.synced:
            self.overlay.set_lyrics(ly)

    def _tap_cancelled(self):
        self._song_offset_ms = getattr(self, "_offset_before_edit", self._song_offset_ms)
        if self._last_result is not None:
            self._show_result(self._last_result)

    def _tap_saved(self, title: str, artist: str, text: str):
        path = self.my_lyrics.save(title, artist, text)
        log.info("Saved your timing to %s", path)
        # your taps are the real timing, so any earlier/later adjustment for this song is no longer needed
        self.song_sync.set(title, artist, 0)
        if self.track and self.track.title == title:
            self._song_offset_ms = 0
            self._fetch(self.track)
        self.notify(f"Saved your timing for {title}. It's used from now on.", "info", 4000)

    # ------------------------------------------------------------ updates
    def check_updates(self, silent: bool = True):
        if self._update_busy:
            return
        self._update_busy = True
        if not silent:
            self._show_update_status("Checking for updates…")

        def work():
            try:
                return updater.check_latest()
            except Exception as e:
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.update_checked.emit(f.result(), silent))

    def _on_update_checked(self, result, silent: bool):
        self._update_busy = False
        if isinstance(result, Exception):
            log.info("Update check failed: %s", result)
            if not silent:
                self.notify("Couldn't check for updates – are you online?", "warning", 3000)
            self._show_update_status("Couldn't check for updates right now.")
            return
        self.update_info = result
        if result is None:
            if not silent:
                self.notify(f"You're up to date (version {APP_VERSION}).", "info", 3000)
            self._show_update_status()
            return
        self.act_update.setText(f"Update to version {result.version}")
        self.act_update.setVisible(True)
        self._show_update_status()
        if self._announced_version != result.version or not silent:
            self._announced_version = result.version
            new = updater.whats_new(result)
            self.notify(f"Version {result.version} is available"
                        + (f": {new}" if new else "") + "\nClick to update now.", "info", 12000, action="Update now")

    def _show_update_status(self, text: str | None = None):
        if not self.settings_dlg:
            return
        info = self.update_info
        if text is None:
            text = (f"Version {info.version} is available (you have {APP_VERSION})." if info
                    else f"You have version {APP_VERSION}.")
        self.settings_dlg.set_update_status(text, info is not None and not self._update_busy)

    def start_update(self):
        info = self.update_info
        if info is None or self._update_busy:
            return
        mode = updater.install_mode()
        if mode != "installed":
            # the portable zip / source version can't replace itself: open the download page instead
            QDesktopServices.openUrl(QUrl(info.page_url))
            self.notify("This copy wasn't installed with the installer, so it can't update "
                                  "itself. The download page has been opened.", "info", 6000)
            return
        self._update_busy = True
        self._show_update_status(f"Downloading version {info.version}…")
        self.notify(f"Downloading version {info.version}…", "info", 3000)

        def work():
            try:
                return updater.download(info)
            except Exception as e:
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.update_downloaded.emit(f.result()))

    def _on_update_downloaded(self, result):
        if isinstance(result, Exception):
            self._update_busy = False
            log.warning("Update download failed: %s", result)
            self.notify(f"Update failed: {result}", "warning", 6000)
            self._show_update_status("Update failed – try again later.")
            return
        log.info("Installing update from %s", result)
        self.notify("Installing the update – the overlay will restart in a few seconds.", "info", 4000)
        try:
            updater.launch_installer(result)
        except Exception as e:
            self._update_busy = False
            self.notify(f"Couldn't start the installer: {e}", "warning", 6000)
            return
        QTimer.singleShot(1200, self.quit)       # get out of the installer's way

    def _hotkeys_changed(self):
        failed = self._register_hotkeys(announce_failures=False)
        self._label_menu()
        if self.settings_dlg:
            self.settings_dlg.show_hotkey_problems(failed)

    def _settings_changed(self):
        before = self.s.fixed_height
        if self.s.popup_style == "app":
            style_menu(self.menu)
        else:
            self.menu.setStyleSheet("")
        self.act_click.setChecked(self.s.click_through)
        self.act_show.setChecked(self.s.visible)
        if not self.demo:
            self.watcher.spotify_only = self.s.spotify_only
        self.overlay.apply_settings()
        # re-build lyrics so "estimate word timing" takes effect immediately
        if self._last_result is not None:
            self._show_result(self._last_result)
        self._apply_visibility()
        if self.settings_dlg and self.s.fixed_height != before:
            self.settings_dlg.refresh()          # show the height that was just frozen

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
            self.unregister_hotkeys()
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
                from .lyrics_provider import LyricsResult
                mine = self.my_lyrics.read(title, artist)       # your own file always wins
                if mine:
                    return LyricsResult(mine, None, False, "my file")
                if self.demo:
                    from .demo import demo_lyrics
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
            ly = build_lyrics(result.synced, result.plain, False, self.s.estimate_word_timing, result.source,
                              estimate_mode=self.s.word_timing_mode)
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
        self.notify(text, "warning", 6000)

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
