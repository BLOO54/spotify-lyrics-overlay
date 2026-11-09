"""The "brain" that connects everything together.

Flow:
  Windows media info  ->  MediaWatcher (background thread)
        -> controller notices a new song -> asks LRCLIB for lyrics (background thread)
        -> overlay redraws ~60x per second using the playback clock.
"""

from __future__ import annotations

import logging
import sys
import time as _time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import APP_NAME, APP_VERSION, win32
from .clock import PlaybackClock, PlaybackReport
from .config import Settings, app_data_dir
from .lrc import build_lyrics
from .lyrics_provider import LrclibClient, LyricsNetworkError
from .media_session import MediaState, MediaWatcher
from .song_sync import SongSync
from . import ai_sync
from .lrc import parse_lrc
from .history import NotificationLog
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

HK_TOGGLE, HK_CLICK, HK_SETTINGS, HK_EARLIER, HK_LATER, HK_VERSION, HK_REPORT, HK_MINI = range(1, 9)
# hotkey id -> settings field holding its key combination
HOTKEY_FIELDS = {HK_TOGGLE: "hk_toggle", HK_CLICK: "hk_click", HK_SETTINGS: "hk_settings",
                 HK_EARLIER: "hk_earlier", HK_LATER: "hk_later", HK_VERSION: "hk_version",
                 HK_REPORT: "hk_report", HK_MINI: "hk_mini"}


class _DiagLogHandler(logging.Handler):
    """Copies the app's own log messages into the problem-report recorder (last few hundred)."""

    def __init__(self, rec):
        super().__init__(logging.INFO)
        self.rec = rec

    def emit(self, record):
        if record.name.startswith(("app", "main")) and record.name != "app.overlay":
            try:
                self.rec.event(f"{record.levelname.lower()} {record.name}: {record.getMessage()[:300]}")
            except Exception:
                pass


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
    ai_done = Signal(object, object)          # job, (lrc, result, message) or an error
    ai_progress = Signal(object, object)      # done bytes, total bytes
    ai_job_progress = Signal(object, object)  # song key, fraction 0..1 while the AI works on it
    ai_patch_done = Signal(object, object)    # missed-parts session, (lrc, result, message) or an error
    ai_downloaded = Signal(object, object)    # model name, error or None


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
        self.s.auto_backup()          # a quiet daily copy, so settings can always be brought back
        self.clock = PlaybackClock()
        from .diagnostics import Recorder
        self.diag = Recorder()
        self._last_auto_report = -1e9
        self._diag_handler = _DiagLogHandler(self.diag)
        logging.getLogger().addHandler(self._diag_handler)
        self.clock.on_correct = lambda kind, d: self.diag.event(
            f"clock {kind}: Spotify was {abs(d):.2f} s {'ahead of' if d > 0 else 'behind'} the lyrics clock")
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
        self.bridge.ai_done.connect(self._on_ai_done)
        self.bridge.ai_progress.connect(self._on_ai_progress)
        self.bridge.ai_job_progress.connect(self._on_ai_job_progress)
        self.bridge.ai_patch_done.connect(self._on_ai_patch_done)
        self.bridge.ai_downloaded.connect(self._on_ai_downloaded)
        self.update_info = None
        self._update_busy = False
        self._announced_version = None
        self.song_sync = SongSync(app_data_dir() / "song_sync.json")
        self.my_lyrics = MyLyrics(app_data_dir() / "my_lyrics")
        # AI sync (optional): timing learned by listening, kept apart from your own files
        self.ai_lyrics = MyLyrics(app_data_dir() / "ai_lyrics", readme_text=(
            "Lyrics timed by AI sync (one small .lrc file per song). Delete a file to let the\n"
            "app listen to that song again. Your own files in \"my_lyrics\" always come first.\n"))
        self.song_sync_ai = SongSync(app_data_dir() / "song_sync_ai.json")
        self.history = NotificationLog(app_data_dir() / "notifications.json")
        self.meanings_win = None
        self.ai_store = ai_sync.ModelStore()
        self.ai_listener = None
        self.ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai")
        self._ai_ctx = None
        self._ai_jobs = 0
        self._ai_job_key = None         # the song the AI is working on right now
        self._recent_ai: list[dict] = []    # this session's AI results, newest first (for the AI sync window)
        self.ai_win = None
        self.full_win = None
        self._ai_skipped: dict = {}         # song key -> why the AI didn't sync it this time
        self._one_song: dict | None = None  # "sync one song" from the AI sync window
        self._ai_job_frac = None
        self._ai_download = None        # (model name, cancel event) while downloading
        self._ai_dl_text = ""
        self._ai_prev = (self.s.ai_sync_enabled, self.s.ai_model, self.s.use_my_files)
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

        from .sized_settings import SizedSettings
        self.overlay = OverlayWindow(SizedSettings(self.s))
        self.overlay.on_painted = self.diag.painted
        self._smooth_timer = QTimer(self)
        self._smooth_timer.timeout.connect(self._check_smoothness)
        self._smooth_timer.start(30_000)
        self.overlay.moved.connect(self._on_dragged)
        self.overlay.context_menu_requested.connect(lambda pt: self.menu.popup(pt))
        self.overlay.settings_requested.connect(self.open_settings)

        self._build_tray()

        self.frame_timer = QTimer(self)
        # "precise": Windows' normal timer only ticks every ~15.6 ms, so a 16 ms timer fires after 16 or 31 ms
        # at random - that uneven rhythm is what makes moving lyrics look jerky
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.timeout.connect(self._tick)
        self.frame_timer.start(FRAME_MS)
        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self.overlay.keep_on_top)
        self.top_timer.start(TOPMOST_MS)
        self.ai_timer = QTimer(self)
        self.ai_timer.timeout.connect(self._ai_check)
        self.ai_timer.start(250)

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
        self.act_ai = QAction("AI sync", self.menu, checkable=True)
        self.act_ai.setChecked(self.s.ai_sync_enabled)
        self.act_ai.toggled.connect(self.set_ai_enabled)
        from PySide6.QtGui import QActionGroup

        from .sized_settings import SMALL_MODES
        self.small_menu = QMenu("Small mode", self.menu)
        self._small_group = QActionGroup(self.small_menu)
        self._small_acts = {}
        for key, (label, _, _) in SMALL_MODES.items():
            act = QAction(label, self.small_menu, checkable=True)
            act.setChecked(self.s.small_mode == key)
            act.triggered.connect(lambda _=False, k=key: self.set_small_mode(k))
            self._small_group.addAction(act)
            self.small_menu.addAction(act)
            self._small_acts[key] = act
        self.act_mini = self.small_menu.menuAction()
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
        self.act_ai_redo = QAction("AI sync: listen to this song again", self.menu)
        self.act_ai_redo.triggered.connect(self.ai_redo_song)
        act_full = QAction("Full-screen lyrics", self.menu)
        act_full.triggered.connect(self.open_fullscreen)
        self.act_full = act_full
        act_meanings = QAction("Song meanings…", self.menu)
        act_meanings.triggered.connect(self.open_meanings)
        self.act_meanings = act_meanings
        act_report = QAction("Save a problem report…", self.menu)
        act_report.triggered.connect(self.ask_problem_report)
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
                            "hk_version": (act_version, "Try another lyrics version"),
                            "hk_report": (act_report, "Save a problem report…"),
                            "hk_mini": (self.act_mini, "Small mode")}
        self._label_menu()
        for a in (self.act_show, self.act_mini, self.act_click, self.act_ai, act_settings, act_meanings, act_full):
            self.menu.addAction(a)
        self.menu.addSeparator()
        for a in (act_earlier, act_later, act_reset, act_version, act_tap, self.act_ai_redo, act_folder, act_refetch):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(act_report)
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

    def notify(self, text: str, kind: str = "info", ms: int = 4000, action: str = "", keep: bool = False):
        """Show a pop-up: the app's own sliding card, or a standard Windows notification.
        keep=True also saves it in Settings → Notifications (for things worth looking back at)."""
        self.diag.event(f"pop-up ({kind}): {text[:200]}")
        if keep:
            self.history.add(text, kind)
            if self.settings_dlg is not None:
                self.settings_dlg.show_history(self.history.newest_first())
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
        elif hid == HK_REPORT:
            self.save_problem_report()
        elif hid == HK_MINI:
            self.cycle_small_mode()

    # ------------------------------------------------------------ actions
    def cycle_small_mode(self):
        from .sized_settings import CYCLE
        cur = self.s.small_mode if self.s.small_mode in CYCLE else "off"
        self.set_small_mode(CYCLE[(CYCLE.index(cur) + 1) % len(CYCLE)])

    def set_small_mode(self, mode: str):
        """Small modes cover less of a game: "small" = 2 lines, "tiny" = just the current line."""
        from .sized_settings import SMALL_MODES
        if mode not in SMALL_MODES:
            mode = "off"
        for key, act in self._small_acts.items():
            if act.isChecked() != (key == mode):
                act.blockSignals(True)
                act.setChecked(key == mode)
                act.blockSignals(False)
        if self.s.small_mode == mode:
            return
        self.s.small_mode = mode
        self.s.save()
        self.overlay.apply_settings()
        if self.settings_dlg is not None and hasattr(self.settings_dlg, "refresh"):
            self.settings_dlg.refresh()
        keys = self.s.hk_mini
        text = {"off": "Normal overlay.", "small": "Small mode – 2 lines.", "tiny": "Tiny mode – just the current line."}
        self.notify(text[mode] + (f" ({keys} to switch)" if keys else ""), "info", 1800)

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
        store = self._sync_store()
        store.set(title, artist, new)
        self._song_offset_ms = store.get(title, artist)
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
        self.ai_lyrics.forget(st.title, st.artist)      # AI timing was for the old version
        self._last_result = chosen
        self._song_offset_ms = self._sync_store().get(st.title, st.artist)
        self._extras, self._extras_key = None, None
        self._show_result(chosen)
        self._ai_arm(chosen)
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
            self.settings_dlg.ai_download_requested.connect(self.ai_download)
            self.settings_dlg.ai_remove_requested.connect(self.ai_remove)
            self.settings_dlg.ai_cancel_requested.connect(self.ai_cancel_download)
            self.settings_dlg.ai_status_wanted.connect(self._refresh_ai_status)
            self.settings_dlg.history_clear_requested.connect(self._clear_history)
            self.settings_dlg.meanings_requested.connect(self.open_meanings)
            self.settings_dlg.ai_window_requested.connect(self.open_ai_window)
            self.settings_dlg.fullscreen_requested.connect(self.open_fullscreen)
            self.settings_dlg.synced_wanted.connect(self._refresh_synced)
            self.settings_dlg.synced_remove_requested.connect(self.remove_synced)
            self.settings_dlg.synced_remove_all_requested.connect(self.remove_synced_many)
            self.settings_dlg.show_history(self.history.newest_first())
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
        self._refresh_synced()
        if not self.s.use_my_files:           # you just synced it, so you want to see it
            self.s.use_my_files = True
            self.s.save()
            self._ai_prev = (self.s.ai_sync_enabled, self.s.ai_model, True)
            if self.settings_dlg is not None:
                self.settings_dlg.sync_from_settings()
        # your taps are the real timing, so any earlier/later adjustment for this song is no longer needed
        self.song_sync.set(title, artist, 0)
        if self.track and self.track.title == title:
            self._song_offset_ms = 0
            self._fetch(self.track)
        self.notify(f"Saved your timing for {title}. It's used from now on.", "info", 4000, keep=True)

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
        for key, act in getattr(self, "_small_acts", {}).items():
            if act.isChecked() != (key == self.s.small_mode):
                act.blockSignals(True)
                act.setChecked(key == self.s.small_mode)
                act.blockSignals(False)
        if self.act_ai.isChecked() != self.s.ai_sync_enabled:
            self.act_ai.setChecked(self.s.ai_sync_enabled)
        if not self.demo:
            self.watcher.spotify_only = self.s.spotify_only
        self.overlay.apply_settings()
        # re-build lyrics so "estimate word timing" takes effect immediately
        if self._last_result is not None:
            self._show_result(self._last_result)
        self._apply_visibility()
        if self.settings_dlg and self.s.fixed_height != before:
            self.settings_dlg.refresh()          # show the height that was just frozen
        if self.meanings_win is not None:
            self.meanings_win.apply_text_size()
        if self.s.ai_vocals and not self.ai_store.installed("vocals") and not self._ai_download:
            self.ai_download("vocals")              # turned on: get the vocal separation model (~40 MB)
        ai_now = (self.s.ai_sync_enabled, self.s.ai_model, self.s.use_my_files)
        if ai_now != self._ai_prev:
            self._ai_prev = ai_now
            if self.track and self.track.title and self.track.artist:
                self._fetch(self.track)          # AI-timed lyrics on / off for this song too
            self._refresh_ai_status()

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
            if self.ai_listener is not None:
                self.ai_listener.stop()
            for win in (self.meanings_win, self.ai_win, self.full_win):
                if win is not None:
                    win.close()
            if self.meanings_win is not None:
                self.meanings_win.close()
                self.meanings_win.pool.shutdown(wait=False, cancel_futures=True)
            if self._ai_download:
                self._ai_download[1].set()
            self.ai_pool.shutdown(wait=False, cancel_futures=True)
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

        changed = st.track_key != self.track_key
        if changed:
            self._pos_before_change = (self.clock.position(), self.clock.playing)
        self.clock.update(PlaybackReport(st.position, st.playing, st.last_updated, st.duration))
        self.overlay.playing = st.playing
        self.overlay.duration = st.duration
        if changed:
            self.diag.event(f"song changed to {st.title!r} by {st.artist!r} ({st.duration:.0f} s, at {st.position:.1f} s, "
                            f"{'playing' if st.playing else 'paused'}, jumping allowed: {st.can_seek})")
            self._one_song_track_changed(st)
            self._ai_finish(on_change=True)
            self.overlay.set_ai_note("")          # the 1-second AI timer shows any AI progress again
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
        ai_on = self.s.ai_sync_enabled
        one = self._one_song
        syncing_this = bool(one and one["state"] == "syncing" and one.get("key") == st.track_key)
        use_mine = self.s.use_my_files and not syncing_this     # syncing one song: listen even if you synced it
        if syncing_this:
            ai_on = False                                       # ...and don't reuse an old AI timing either

        def work():
            try:
                from .lyrics_provider import LyricsResult
                mine = self.my_lyrics.read(title, artist) if use_mine else None   # your own file wins
                if mine:
                    return LyricsResult(mine, None, False, "my file")
                if ai_on:
                    heard = self.ai_lyrics.read(title, artist)  # then timing the AI learned
                    if heard:
                        return LyricsResult(heard, None, False, "AI sync")
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
        if self.track:
            self._song_offset_ms = self._sync_store().get(self.track.title, self.track.artist)
        self._show_result(result)
        self._ai_arm(result)

    def _show_result(self, result):
        self.diag.event(f"lyrics: source={result.source}, synced={bool(result.synced)}, "
                        f"plain={bool(result.plain)}, instrumental={result.instrumental}, "
                        f"song shift={self._song_offset_ms} ms")
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

    # ------------------------------------------------------------ AI sync
    def _sync_store(self):
        """Per-song adjustments are kept separately for AI-timed lyrics."""
        r = self._last_result
        return self.song_sync_ai if (r is not None and r.source == "AI sync") else self.song_sync

    def ai_status(self) -> str:
        why = ai_sync.runtime_available()
        if why:
            return f"Not available: {why}"
        if self._ai_download:
            return self._ai_dl_text or "Downloading…"
        name = self.s.ai_model
        label, size = ai_sync.MODELS[name][1], ai_sync.MODELS[name][2]
        if not self.ai_store.installed(name):
            return f"{label} model not downloaded yet ({size})."
        mb = self.ai_store.size_on_disk(name) / 1e6
        bits = [f"{label} model installed ({mb:,.0f} MB)."]
        extra = []
        if self.s.ai_second_pass:
            extra.append("second listen on")
        if self.s.ai_vocals:
            extra.append("vocal separation " + ("on" if self.ai_store.installed("vocals") else
                                                "downloading…" if self._ai_download else "needs its download"))
        if extra:
            bits.append("Extra accuracy: " + ", ".join(extra) + ".")
        if not self.s.ai_sync_enabled:
            bits.append("AI sync is off (the model stays installed).")
        elif self.ai_listener is not None and self.ai_listener.error:
            bits.append(f"Problem: {self.ai_listener.error}")
        elif self._ai_jobs:
            bits.append("Working out the timing of a song…")
        elif self.ai_listener is not None and self.ai_listener.peek() is not None:
            rec = self.ai_listener.peek()
            how = f", {self.ai_listener.method}" if self.ai_listener.method else ""
            bits.append(f"Listening to this song ({rec.covered():.0f} s heard{how}).")
        if self.ai_listener is not None and self.s.ai_listen == "app" and self.ai_listener.app_note:
            bits.append(f"Listening to the whole PC instead: {self.ai_listener.app_note}.")
        try:
            done = len(list(self.ai_lyrics.folder.glob("*.lrc")))
            bits.append(f"{done} song{'s' if done != 1 else ''} synced so far.")
        except OSError:
            pass
        return " ".join(bits)

    def _ai_ready(self) -> bool:
        return (self.s.ai_sync_enabled and not self.demo and ai_sync.runtime_available() is None
                and self.ai_store.installed(self.s.ai_model))

    def _ai_state(self):
        return self.track_key, self.clock.position(), self.clock.playing

    def _ai_target(self):
        """Process id of the music app, if AI sync should listen to it alone."""
        if self.s.ai_listen != "app" or not self.track:
            return None
        from .app_audio import exe_for_app, find_app_pid
        exe = exe_for_app(self.track.source_app) or ("Spotify.exe" if self.s.spotify_only else None)
        return find_app_pid(exe) if exe else None

    def _ai_arm(self, result):
        """Lyrics are on screen: start listening, if AI sync should time this song."""
        one = self._one_song
        if one and one.get("mode") == "missed" and one["state"] in ("syncing", "processing"):
            return                              # listening to just the missed parts - leave it alone
        self._ai_ctx = None
        self._update_ai_indicator()
        if self.ai_listener is not None:
            self.ai_listener.take()
        if not self._ai_ready() or not self.track or result.instrumental:
            return
        if result.source in ("my file", "AI sync", "demo"):
            return
        if result.synced:
            parsed = parse_lrc(result.synced)
            lines, orig = [ln.text for ln in parsed], [ln.start for ln in parsed]
        elif result.plain:
            lines, orig = lines_for_tapping(None, result.plain), None
        else:
            return
        if sum(1 for ln in lines if ln.strip()) < 3:
            return
        self._ai_ctx = (self.track_key, self.track.title, self.track.artist, lines, orig)
        if self.ai_listener is None:
            self.ai_listener = ai_sync.Listener(self._ai_state, self._ai_target)
        self.ai_listener.arm(self.track_key, self.track.duration or 0.0)
        self._update_ai_indicator()
        log.info("AI sync: listening to %s - %s", self.track.artist, self.track.title)

    def _ai_check(self):
        """Every 2 s: once (almost) the whole song has been heard, start the AI."""
        self._update_ai_indicator()
        self._one_song_check()
        if self.ai_listener is None or self._ai_ctx is None:
            return
        rec = self.ai_listener.peek()
        if rec is None or not rec.duration:
            return
        # wait for the very end of the song - the last lines are often sung right up to it
        if self.clock.position() >= rec.duration - 2.0:
            self._ai_finish()

    def _crossfade_tail(self, rec) -> float:
        """The song changed: if that happened in the last seconds of it (Spotify's Crossfade starts
        the next song early), how many seconds of the end are still playing underneath. 0 = none."""
        pos, playing = getattr(self, "_pos_before_change", (0.0, False))
        left = (rec.duration or 0) - pos
        if not playing or not rec.duration or not (0.8 < left <= 20.0):
            return 0.0
        return left

    def _ai_finish(self, on_change: bool = False):
        ctx, self._ai_ctx = self._ai_ctx, None
        rec = self.ai_listener.take() if self.ai_listener is not None else None
        if ctx is None or rec is None or rec.key != ctx[0]:
            return
        left = self._crossfade_tail(rec) if on_change else 0.0
        if left:
            pos = self._pos_before_change[0]
            log.info("AI sync: next song started %.1f s before %s ended (Spotify Crossfade?) - "
                     "still listening to its end", left, ctx[2])
            self.ai_listener.start_tail(rec, pos, rec.duration - 0.2)
            QTimer.singleShot(int(left * 1000) + 700, lambda: self._ai_submit(ctx, rec))
            return
        self._ai_submit(ctx, rec)

    def _ai_submit(self, ctx, rec):
        key, title, artist, lines, orig = ctx
        heard = rec.covered()
        need = (0.35 if orig else 0.8) * (rec.duration or 0)
        self._update_ai_indicator()
        if heard < max(need, 25.0):
            one = self._one_song
            if one and one.get("key") == key and one["state"] in ("syncing", "processing"):
                one["state"] = "waiting"
                one["note"] = f"Only heard {heard:.0f} s – press play on it again (from the start) to retry."
            log.info("AI sync: only %.0f s of %s heard - not enough, will listen next time", heard, title)
            self._ai_skipped[key] = (f"Only heard {heard:.0f} s of this song before it changed – it needs most of "
                                     "the song, so it'll listen again next time it plays.")
            return
        vocals = self.ai_store.path("vocals") if (self.s.ai_vocals and self.ai_store.installed("vocals")) else None
        job = ai_sync.Job(key, title, artist, lines, orig, rec, second_pass=self.s.ai_second_pass,
                          vocals_dir=vocals)
        model_dir = self.ai_store.path(self.s.ai_model)
        threads = self.s.ai_threads
        self._ai_jobs += 1

        def work():
            win32.set_background_priority(True)
            try:
                return ai_sync.run_job(job, model_dir, threads,
                                       progress=lambda f: self.bridge.ai_job_progress.emit(job.key, f))
            except Exception as e:
                log.exception("AI sync failed")
                return e
            finally:
                win32.set_background_priority(False)

        fut = self.ai_pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.ai_done.emit(job, f.result()))

    def _on_ai_job_progress(self, key, frac):
        self._ai_job_key, self._ai_job_frac = key, float(frac)
        self._update_ai_indicator()

    def _update_ai_indicator(self):
        """The 'AI listening 42%' / 'AI syncing 63%' note (and bar) in the overlay header."""
        one = self._one_song
        if one and one.get("mode") == "missed" and one["state"] == "syncing" and one.get("key") == self.track_key:
            plan, i = one["plan"], min(one["i"], len(one["plan"]) - 1)
            a, b = plan[i]
            self.overlay.set_ai_note(f"AI: missed part {i + 1}/{len(plan)}",
                                     max(0.0, min(1.0, (self.clock.position() - a) / max(b - a, 0.1))))
            return
        if self._ai_job_key is not None and self._ai_jobs:
            same = self._ai_job_key == self.track_key
            self.overlay.set_ai_note("AI syncing" if same else "AI syncing last song", self._ai_job_frac)
            return
        rec = self.ai_listener.peek() if (self.ai_listener is not None and self._ai_ctx is not None) else None
        if rec is not None and rec.key == self.track_key:
            need = rec.duration - 2.0 if rec.duration else 0.0
            self.overlay.set_ai_note("AI listening", min(1.0, rec.covered() / need) if need > 0 else None)
        else:
            self.overlay.set_ai_note("")

    def _on_ai_done(self, job, out):
        self._ai_jobs = max(0, self._ai_jobs - 1)
        one = self._one_song
        if one and one.get("key") == job.key:
            self._one_song = None
            ok = not isinstance(out, Exception) and bool(out[0])
            self.notify(f"Sync one song: “{job.title}” is {'done' if ok else 'finished, but the AI was unsure'}. "
                        "Spotify stays paused.", "success" if ok else "info", 6000)
        if job.key == self._ai_job_key:
            self._ai_job_key, self._ai_job_frac = None, None
        self._update_ai_indicator()
        if isinstance(out, Exception):
            self._recent_ai.insert(0, {"title": job.title, "artist": job.artist, "ok": False,
                                       "message": f"error: {out}"})
            self._refresh_ai_window()
            self.notify(f"AI sync couldn't time “{job.title}” ({out}). The normal timing is still used.",
                        "warning", 6000, keep=True)
            return
        text, res, msg = out
        if not text:
            log.info("AI sync: %s - %s", job.title, msg)
            self._recent_ai.insert(0, {"title": job.title, "artist": job.artist, "ok": False, "message": msg,
                                       "lines": job.lines, "heard": list(res.heard) if res is not None else []})
            self._refresh_ai_window()
            self.notify(f"AI sync: “{job.title}” – {msg}.", "info", 5000, keep=True)
            return
        saved = self.ai_lyrics.save(job.title, job.artist, text)
        if res is not None:
            ai_sync.save_details(saved, job.lines, res, job.orig_starts)
        self._recent_ai.insert(0, {"title": job.title, "artist": job.artist, "ok": True, "message": msg,
                                   "path": str(saved)})
        self._refresh_synced()
        self._refresh_ai_window()
        self.notify(f"AI synced “{job.title}” ({msg}). It uses the new timing from now on.", "success", 5000,
                    keep=True)
        if self.track and self.track.title == job.title and self.track.artist == job.artist:
            self._fetch(self.track)        # same song still (or again) playing: switch now

    def ai_rematch(self, path: str, title: str, artist: str):
        """Match an AI-synced song again from the words it heard (no listening needed)."""
        p = Path(path)
        text, res, msg = ai_sync.rematch(p, title, artist)
        if not text:
            self.notify(f"Couldn't re-match “{title}”: {msg}.", "info", 5000)
            return
        p.write_text(text, encoding="utf-8")
        d = ai_sync.load_details(p) or {}
        ai_sync.save_details(p, d.get("lines") or [], res, d.get("orig"))
        self.notify(f"Re-matched “{title}” ({msg}).", "success", 4000, keep=True)
        if self.track and self.track.title == title:
            self._fetch(self.track)
        self._refresh_synced()
        self._refresh_ai_window()

    def ai_redo_song(self):
        if not (self.track and self.track.title):
            self.notify("Play a song first.", "info", 2000)
            return
        if not self._ai_ready():
            self.notify("Turn on AI sync and download a model first (Settings → Lyrics & translation).",
                        "info", 5000)
            return
        self.ai_lyrics.forget(self.track.title, self.track.artist)
        self.notify(f"AI sync will listen to “{self.track.title}” again – let it play to the end.", "info", 4000)
        self._fetch(self.track)

    def ai_download(self, name: str):
        if self._ai_download:
            return
        cancel = threading.Event()
        self._ai_download = (name, cancel)
        self._ai_dl_text = "Starting download…"
        self._refresh_ai_status()

        def progress(done, total):
            self.bridge.ai_progress.emit(done, total)

        def work():
            try:
                self.ai_store.download(name, progress, cancel)
                return None
            except Exception as e:
                log.warning("AI model download failed: %r", e)
                return e

        fut = self.pool.submit(work)
        fut.add_done_callback(lambda f: self.bridge.ai_downloaded.emit(name, f.result()))

    def ai_cancel_download(self):
        if self._ai_download:
            self._ai_download[1].set()

    def _on_ai_progress(self, done, total):
        pct = int(100 * done / total) if total else 0
        self._ai_dl_text = f"Downloading… {done / 1e6:,.0f} of {total / 1e6:,.0f} MB ({pct}%)"
        if self.settings_dlg is not None:
            self.settings_dlg.set_ai_status(self._ai_dl_text, busy=True, progress=pct)

    def _on_ai_downloaded(self, name, err):
        self._ai_download = None
        self._ai_dl_text = ""
        if err is None and name == "vocals":
            self.notify("Vocal separation is ready – the AI will separate the voice from the music before "
                        "listening.", "success", 5000, keep=True)
        elif err is None:
            if not self.s.ai_sync_enabled:
                self.s.ai_sync_enabled = True
                self.s.save()
                if self.settings_dlg is not None:
                    self.settings_dlg.sync_from_settings()
            self.notify("AI sync is ready. It learns each song the first time it plays to the end.",
                        "success", 6000, keep=True)
        elif isinstance(err, InterruptedError):
            self.ai_store.remove(name)
        else:
            self.notify(f"AI model download failed: {err}", "warning", 7000, keep=True)
        self._refresh_ai_status()

    def set_ai_enabled(self, on: bool):
        """Turn AI sync on / off (the model stays installed)."""
        if on == self.s.ai_sync_enabled:
            return
        self.s.ai_sync_enabled = on
        self.s.save()
        if self.settings_dlg is not None:
            self.settings_dlg.sync_from_settings()
        self._settings_changed()
        if not on:
            self.notify("AI sync off. The model stays installed – turn it back on any time.", "info", 3000)
        elif not self.ai_store.installed(self.s.ai_model):
            self.notify("AI sync on. Download a model in Settings → Lyrics & translation so it can learn "
                        "new songs.", "info", 5000)
        else:
            self.notify("AI sync on.", "info", 2000)

    def ai_remove(self, name: str):
        if self._ai_download and self._ai_download[0] == name:
            self.ai_cancel_download()
            return
        if self.ai_listener is not None:
            self.ai_listener.take()
        self._ai_ctx = None
        self.ai_store.remove(name)
        self.notify(f"{ai_sync.MODELS[name][1]} AI model uninstalled ({ai_sync.MODELS[name][2]} freed). "
                    "Songs it already synced keep their timing while AI sync is on.", "info", 6000, keep=True)
        self._refresh_ai_status()

    # ------------------------------------------------------------ song meanings
    def open_meanings(self):
        if self.meanings_win is None:
            from .meanings_window import MeaningsWindow
            self.meanings_win = MeaningsWindow(self)
        self.meanings_win.show()
        self.meanings_win.raise_()
        self.meanings_win.activateWindow()
        if self.meanings_win.song is None and self.track and self.track.title:
            self.meanings_win.show_now_playing()

    def meanings_now_playing(self):
        """The song playing now, with its lyric lines (and timing, if any) for the meanings window."""
        if not (self.track and self.track.title):
            return None
        ly = self.overlay.lyrics
        if ly is not None and ly.lines:
            lines, starts = [ln.text for ln in ly.lines], [ln.start for ln in ly.lines]
        elif self._last_result is not None and self._last_result.plain:
            lines, starts = lines_for_tapping(None, self._last_result.plain), None
        else:
            lines, starts = None, None
        return {"title": self.track.title, "artist": self.track.artist, "lines": lines, "starts": starts}

    # ------------------------------------------------------------ synced songs
    def _refresh_synced(self):
        if self.settings_dlg is None:
            return
        from .my_lyrics import list_synced
        self.settings_dlg.set_synced_songs(list_synced(self.my_lyrics.folder, "mine")
                                           + list_synced(self.ai_lyrics.folder, "ai"))

    def remove_synced_many(self, items):
        removed = 0
        for kind, path in items:
            if self._delete_synced_file(kind, path):
                removed += 1
        self.notify(f"Removed the timing for {removed} song{'s' if removed != 1 else ''}. "
                    "They use the normal timing now.", "info", 4000, keep=True)
        if self.track and self.track.title:
            self._fetch(self.track)
        self._refresh_synced()

    def _delete_synced_file(self, kind: str, path: str) -> bool:
        p = Path(path)
        folder = self.my_lyrics.folder if kind == "mine" else self.ai_lyrics.folder
        try:
            if p.resolve().parent != folder.resolve() or p.suffix.lower() != ".lrc":
                raise ValueError("not a synced-song file")
            p.unlink()
            p.with_suffix(".ai.json").unlink(missing_ok=True)       # AI details, if any
            self._forget_ai_song(p)
            return True
        except Exception as e:
            log.warning("could not remove %s: %s", path, e)
            return False

    def _forget_ai_song(self, p: Path):
        """A synced song was removed: stop anything still going for it and drop it from the AI window."""
        one = self._one_song
        name = p.stem.split(" - ", 1)[-1].lower()
        if one and (one.get("path") == str(p) or (one.get("mode") == "missed" and one["title"].lower() == name)):
            if self.ai_listener is not None and one["state"] == "syncing":
                self.ai_listener.take()
            self._one_song = None
        self._recent_ai = [r for r in self._recent_ai if (r.get("title") or "").lower() != name]
        lp = getattr(self, "_last_patch", None)
        if lp and (lp.get("title") or "").lower() == name:
            self._last_patch = None
        self._refresh_ai_window()

    def remove_synced(self, kind: str, path: str):
        p = Path(path)
        if not self._delete_synced_file(kind, path):
            self.notify("Couldn't remove that file.", "warning", 4000)
            self._refresh_synced()
            return
        name = p.stem.split(" - ", 1)[-1]
        self.notify(f"Removed the {'AI' if kind == 'ai' else 'your'} timing for “{name}”. "
                    "It uses the normal timing now.", "info", 4000, keep=True)
        if self.track and self.track.title:
            self._fetch(self.track)          # if it was this song, switch right away
        self._refresh_synced()

    # ------------------------------------------------------------ full-screen lyrics
    def open_fullscreen(self):
        if self.full_win is None:
            from .fullscreen import FullscreenLyrics
            self.full_win = FullscreenLyrics(self)
        screen = self.overlay.screen() if hasattr(self.overlay, "screen") else None
        self.full_win.open_on(screen)

    # ------------------------------------------------------------ sync one song
    @staticmethod
    def _same_song(a_title, a_artist, b_title, b_artist) -> bool:
        import re as _re
        n = lambda s: _re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        ta, tb = n(a_title), n(b_title)
        if not ta or not tb or not (ta in tb or tb in ta):
            return False
        aa, ab = n(a_artist), n(b_artist)
        return not aa or not ab or aa[:6] in ab or ab[:6] in aa

    def start_one_song(self, title: str, artist: str):
        """Sync just this song: show it in Spotify, sync it when it plays, pause Spotify at the end."""
        if not self._ai_ready():
            self.notify("Turn on AI sync and download a model first.", "info", 4000)
            return False
        self._one_song = {"title": title, "artist": artist, "state": "waiting", "note": ""}
        if self.track and self._same_song(title, artist, self.track.title, self.track.artist):
            self._one_song_begin(self.track)
        else:
            from urllib.parse import quote
            QDesktopServices.openUrl(QUrl("spotify:search:" + quote(f"{title} {artist}")))
            self._one_song["note"] = "Spotify is showing it – press play on the song."
        self._refresh_ai_window()
        return True

    # ------------------------------------------------------------ listen to the missed parts only
    def start_missed_parts(self, path: str, title: str, artist: str) -> bool:
        """Play just the parts of a song the AI missed (Spotify jumps to each), listen to them and
        merge what's heard into the song's timing - instead of playing the whole song again."""
        if not self._ai_ready():
            self.notify("Turn on AI sync and download a model first.", "info", 4000)
            return False
        d = ai_sync.load_details(Path(path))
        if not d or not d.get("words"):
            self.notify(f"“{title}” has no saved words yet – sync it once more (from version 1.19.0 on) "
                        "to use this.", "info", 6000)
            return False
        res, plan = ai_sync.plan_missed_parts(d)
        if not plan:
            self.notify(f"The AI already heard every line of “{title}”.", "info", 4000)
            return False
        secs = sum(b - a for a, b in plan)
        log.info("missed parts of %s - %s: %d/%d lines heard so far, will play %s", artist, title,
                 res.matched_lines, res.lyric_lines, ", ".join(f"{a:.1f}-{b:.1f} s" for a, b in plan))
        self._one_song = {"title": title, "artist": artist, "state": "waiting", "mode": "missed",
                          "path": path, "plan": plan, "i": 0, "details": d,
                          "note": f"{len(plan)} part{'s' if len(plan) != 1 else ''} to listen to "
                                  f"(about {secs:.0f} s of the song)."}
        if self.track and self._same_song(title, artist, self.track.title, self.track.artist):
            self._patch_begin(self.track)
        else:
            from urllib.parse import quote
            QDesktopServices.openUrl(QUrl("spotify:search:" + quote(f"{title} {artist}")))
            self._one_song["note"] = "Spotify is showing it – press play on the song."
        self._refresh_ai_window()
        return True

    def _patch_jump(self, seconds: float):
        log.info("missed parts: jumping Spotify to %.1f s", seconds)
        if not self.demo:
            self.watcher.request(f"seek:{seconds:.2f}")
            self.watcher.request("play")
        self.clock.reset(seconds, True)
        if self._one_song is not None:
            self._one_song["jumped"] = _time.monotonic()
        if self.ai_listener is not None:
            self.ai_listener.hold(0.9)          # Spotify may still be playing the old spot for a moment
            if self.ai_listener.peek() is None and self._one_song and self._one_song.get("key"):
                self.ai_listener.arm(self._one_song["key"], self.track.duration if self.track else 0.0)

    def _patch_begin(self, st):
        one = self._one_song
        if not st.can_seek and not self.demo:
            one["note"] = "Spotify isn't letting the app jump within songs right now – try again in a moment."
            return
        one.update(state="syncing", key=st.track_key, i=0)
        self._ai_ctx = None
        if self.ai_listener is None:
            self.ai_listener = ai_sync.Listener(self._ai_state, self._ai_target)
        self.ai_listener.take()
        self.ai_listener.arm(st.track_key, st.duration or 0.0)
        self._patch_jump(one["plan"][0][0])
        one["note"] = f"Listening to missed part 1 of {len(one['plan'])}…"
        self._refresh_ai_window()

    def _patch_check(self):
        one = self._one_song
        plan, i = one["plan"], one["i"]
        if _time.monotonic() - one.get("jumped", 0.0) < 1.5:
            return                              # an old position from before the jump may still arrive
        pos = self.clock.position()
        end = plan[i][1]
        dur = (self.track.duration if self.track else 0) or 0
        if dur:
            end = min(end, dur - 1.5)           # stop just before the song ends, so the next one doesn't start
        if pos < end:
            return
        if pos > end + 4.0:                     # the jump didn't happen (still somewhere else) - ask again
            self._patch_jump(plan[i][0])
            return
        i += 1
        one["i"] = i
        if i < len(plan):
            self._patch_jump(plan[i][0])
            one["note"] = f"Listening to missed part {i + 1} of {len(plan)}…"
            self._refresh_ai_window()
            return
        # all parts heard: stop Spotify and let the AI work
        if not self.demo:
            self.watcher.request("pause")
        rec = self.ai_listener.take() if self.ai_listener is not None else None
        self._patch_submit(rec)

    def _patch_submit(self, rec, pause: bool = False):
        one = self._one_song
        if one is None or one.get("mode") != "missed":
            return
        if pause and not self.demo:
            self.watcher.request("pause")
        log.info("missed parts: done listening (%s)", "nothing recorded" if rec is None else
                 ", ".join(f"{s.start:.1f}-{s.start + s.seconds:.1f} s" for s in rec.segments))
        one["state"] = "processing"
        one["note"] = "Done listening – Spotify paused. The AI is working on the missed parts…"
        if rec is None or not rec.segments:
            one["state"] = "waiting"
            one["note"] = "Nothing was recorded – press play on the song to try again."
            return
        model_dir = self.ai_store.path(self.s.ai_model)
        vocals = self.ai_store.path("vocals") if (self.s.ai_vocals and self.ai_store.installed("vocals")) else None
        threads, key = self.s.ai_threads, one["key"]
        self._ai_jobs += 1
        session = dict(one)

        def work():
            win32.set_background_priority(True)
            try:
                return ai_sync.patch_job(session["details"], rec, model_dir, session["title"], session["artist"],
                                         threads, vocals,
                                         progress=lambda f: self.bridge.ai_job_progress.emit(key, f))
            except Exception as e:
                log.exception("listening to the missed parts failed")
                return e
            finally:
                win32.set_background_priority(False)
        self.ai_pool.submit(work).add_done_callback(lambda f: self.bridge.ai_patch_done.emit(session, f.result()))
        self._refresh_ai_window()

    def _on_ai_patch_done(self, session, out):
        self._ai_jobs = max(0, self._ai_jobs - 1)
        if session.get("key") == self._ai_job_key:
            self._ai_job_key, self._ai_job_frac = None, None
        if self._one_song and self._one_song.get("path") == session["path"]:
            self._one_song = None
        self._update_ai_indicator()
        title = session["title"]
        if isinstance(out, Exception) or not out[0]:
            why = out if isinstance(out, Exception) else out[2]
            self.notify(f"Listening to the missed parts of “{title}” didn't work ({why}).", "warning", 6000,
                        keep=True)
            self._refresh_ai_window()
            return
        text, res, msg = out
        p = Path(session["path"])
        p.write_text(text, encoding="utf-8")
        d = session["details"]
        import datetime as _dt
        last = {"message": msg, "lines": len(res.new_lines), "words": res.new_words, "new": res.new_lines,
                "now": res.matched_lines, "total": res.lyric_lines,
                "when": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        ai_sync.save_details(p, d.get("lines") or [], res, d.get("orig"), {"last_patch": last})
        log.info("missed parts of %s: %s (now %d/%d lines)", title, msg, res.matched_lines, res.lyric_lines)
        self._last_patch = {"title": title, **last}
        self.notify(f"Missed parts of “{title}”: {msg} (now {res.matched_lines}/{res.lyric_lines} lines). "
                    "Spotify stays paused.", "success" if res.new_lines else "info", 8000, keep=True)
        if self.track and self.track.title == title:
            self._fetch(self.track)
        self._refresh_synced()
        self._refresh_ai_window()

    def cancel_one_song(self):
        if self._one_song and self._one_song.get("mode") == "missed" and self.ai_listener is not None \
                and self._one_song["state"] == "syncing":
            self.ai_listener.take()
        self._one_song = None
        self._refresh_ai_window()

    def _one_song_track_changed(self, st):
        one = self._one_song
        if not one or not st.title:
            return
        if self._same_song(one["title"], one["artist"], st.title, st.artist):
            if one["state"] == "waiting":
                self._one_song_begin(st)
        elif one["state"] == "syncing":
            pos, playing = getattr(self, "_pos_before_change", (0.0, False))
            dur = (self.track.duration if self.track else 0) or 0
            left = dur - pos if (playing and dur) else 0
            if dur and pos > 0 and left <= 20.0:
                # Spotify's Crossfade started the next song while this one's end still plays underneath
                log.info("one song: next song started %.1f s before the end - listening to the end, then pausing",
                         left)
                if one.get("mode") == "missed":
                    rec = self.ai_listener.take() if self.ai_listener is not None else None
                    if rec is not None:
                        end = min(one["plan"][min(one["i"], len(one["plan"]) - 1)][1], dur - 0.2)
                        if end - pos > 0.3:
                            self.ai_listener.start_tail(rec, pos, end)
                        one["state"] = "processing"
                        one["note"] = "Spotify's Crossfade started the next song – listening to the last bit…"
                        QTimer.singleShot(int(max(0.0, end - pos) * 1000) + 700,
                                          lambda: self._patch_submit(rec, pause=True))
                        self._refresh_ai_window()
                        return
                else:
                    one["paused"] = True
                    one["state"] = "processing"
                    one["note"] = "Song finished – the AI is working out the timing…"
                    QTimer.singleShot(int(left * 1000) + 700,
                                      lambda: None if self.demo else self.watcher.request("pause"))
                    return
            log.info("one song: another song started before it finished (%.0f s left)", left)
            if one.get("mode") == "missed":
                # don't keep waiting in the background (it would jump around the next time the song plays)
                if self.ai_listener is not None:
                    self.ai_listener.take()
                self._one_song = None
                self.notify(f"Stopped listening to the missed parts of “{one['title']}” – another song "
                            "started. Press the button again to retry.", "info", 6000)
                self._refresh_ai_window()
                return
            one["state"] = "waiting"
            one["note"] = "Another song started before it finished – press play on it again to sync it."

    def _one_song_begin(self, st):
        one = self._one_song
        if one.get("mode") == "missed":
            self._patch_begin(st)
            return
        one.update(state="syncing", key=st.track_key, paused=False, note="Listening – it pauses Spotify at the end.")
        self.ai_lyrics.forget(st.title, st.artist)          # make a fresh AI timing
        self._ai_skipped.pop(st.track_key, None)
        if st.position > 2.0:
            if st.can_seek:
                self.watcher.request("seek:0")
                self.clock.reset(0.0, self.clock.playing)
            else:
                one["note"] = "Start the song from the beginning so the AI hears all of it."
        if st.track_key == self.track_key:
            self._fetch(st)                                  # re-load the lyrics for listening

    def _one_song_check(self):
        """Every 1/4 s: pause Spotify just before the song ends, so it doesn't go on to the next one."""
        one = self._one_song
        if not one or one["state"] != "syncing" or one.get("key") != self.track_key or not self.track:
            return
        if one.get("mode") == "missed":
            self._patch_check()
            return
        dur = self.track.duration or 0
        if dur and self.clock.position() >= dur - 1.5 and not one.get("paused"):
            one["paused"] = True
            if not self.demo:
                self.watcher.request("pause")
            one["state"] = "processing"
            one["note"] = "Song finished – Spotify paused. The AI is working out the timing…"
            self._ai_finish()
            self._refresh_ai_window()

    # ------------------------------------------------------------ AI sync window
    def open_ai_window(self):
        if self.ai_win is None:
            from .ai_window import AiSyncWindow
            self.ai_win = AiSyncWindow(self)
        self.ai_win.show()
        self.ai_win.raise_()
        self.ai_win.activateWindow()
        self.ai_win.refresh()

    def _refresh_ai_window(self):
        if self.ai_win is not None and self.ai_win.isVisible():
            self.ai_win.refresh()

    def ai_snapshot(self) -> dict:
        """Everything the AI sync window shows, in plain words."""
        s = self.s
        why = ai_sync.runtime_available()
        name = s.ai_model
        label, size = ai_sync.MODELS[name][1], ai_sync.MODELS[name][2]
        installed = self.ai_store.installed(name)
        lis = self.ai_listener
        spotify_pid = None
        try:
            from .app_audio import find_app_pid
            spotify_pid = find_app_pid("Spotify.exe")
        except Exception:
            pass
        r = self._last_result
        # ---- what's happening with the song playing now
        now = {"title": self.track.title if self.track else "", "artist": self.track.artist if self.track else "",
               "state": "", "detail": "", "progress": None}
        if not (self.track and self.track.title):
            now["state"] = "Nothing playing"
            now["detail"] = "Play a song in Spotify."
        elif not s.ai_sync_enabled:
            now["state"] = "AI sync is off"
            now["detail"] = "Turn it on above (or in the tray menu)."
        elif why:
            now["state"] = "Not available"
            now["detail"] = why
        elif not installed:
            now["state"] = "No model downloaded"
            now["detail"] = f"Download the {label} model in Settings → Lyrics & translation → AI sync."
        elif self._ai_job_key == self.track_key and self._ai_jobs:
            now["state"] = "Working out the timing"
            now["detail"] = "The AI is matching what it heard to the lyrics."
            now["progress"] = self._ai_job_frac
        elif r is not None and r.source == "AI sync":
            now["state"] = "Already AI-synced ✔"
            now["detail"] = "This song uses the timing the AI worked out. Tray → \"AI sync: listen to this song " \
                            "again\" redoes it."
        elif r is not None and r.source == "my file":
            now["state"] = "Using your own synced file"
            now["detail"] = "Your own timing comes first. Turn off \"Use my own synced files\" to let the AI sync it."
        elif r is None:
            now["state"] = "Waiting for lyrics"
        elif r.instrumental or not (r.synced or r.plain):
            now["state"] = "No lyrics to sync"
        elif self._ai_ctx is not None and lis is not None and lis.peek() is not None:
            rec = lis.peek()
            need = rec.duration - 2.0 if rec.duration else 0
            now["state"] = "Listening"
            now["detail"] = (f"Heard {rec.covered():.0f} s of {rec.duration:.0f} s"
                             f" ({lis.method or 'starting…'}). It starts working when the song ends.")
            now["progress"] = min(1.0, rec.covered() / need) if need > 0 else None
        elif self.track_key in self._ai_skipped:
            now["state"] = "Will try again next time"
            now["detail"] = self._ai_skipped[self.track_key]
        else:
            now["state"] = "Waiting"
            now["detail"] = "It listens from when the lyrics appear until the song ends."
        # ---- the checklist
        need = [
            (why is None, "Speech recognition is included in this app" if why is None else f"Not available: {why}"),
            (installed, f"{label} model downloaded ({size})" if installed else
             f"Download a model – Settings → Lyrics & translation → AI sync"),
            (s.ai_sync_enabled, "AI sync is on" if s.ai_sync_enabled else "Turn AI sync on"),
            (spotify_pid is not None, "Spotify is open" if spotify_pid else "Open Spotify"),
        ]
        if s.ai_listen == "app":
            if lis is not None and lis.app_note:
                need.append((False, f"Listening to the whole PC instead of only Spotify: {lis.app_note}"))
            elif lis is not None and lis.method == "Spotify only":
                need.append((True, "Hearing Spotify by itself (game sound is ignored)"))
            else:
                need.append((None, "Will listen to Spotify by itself once a song plays"))
        else:
            need.append((None, "Listening to everything the PC plays (setting: Listen to → Everything)"))
        if lis is not None and lis.error:
            need.append((False, lis.error))
        if s.ai_second_pass:
            need.append((True, "Second listen for missed parts is on"))
        try:
            from .spotify_prefs import crossfade_seconds
            xf = crossfade_seconds()
        except Exception:
            xf = None
        if xf:
            need.append((False, f"Spotify's Crossfade is on ({xf:.0f} s): the next song starts while the last "
                                f"{xf:.0f} s of a song still play, so the AI hears the end mixed with the next "
                                "song. For the best endings, turn it off in Spotify → Settings → Playback → "
                                "Crossfade songs."))
        if s.ai_vocals:
            ok_v = self.ai_store.installed("vocals")
            need.append((ok_v, "Vocal separation is on" if ok_v else
                         "Vocal separation: its model (about 40 MB) is downloading / needs downloading"))
        # ---- working on another song?
        working = None
        if self._ai_jobs and self._ai_job_key is not None and self._ai_job_key != self.track_key:
            working = {"progress": self._ai_job_frac, "queued": max(0, self._ai_jobs - 1)}
        from .my_lyrics import list_synced
        songs = list_synced(self.ai_lyrics.folder, "ai")
        for e in songs:
            e["details"] = ai_sync.load_details(Path(e["path"]))
        return {"one_song": dict(self._one_song) if self._one_song else None,
                "last_patch": getattr(self, "_last_patch", None),
                "enabled": s.ai_sync_enabled, "model": f"{label} ({size})", "installed": installed,
                "threads": s.ai_threads, "now": now, "need": need, "working": working,
                "recent": list(self._recent_ai[:20]), "songs": songs}

    def _clear_history(self):
        self.history.clear()
        if self.settings_dlg is not None:
            self.settings_dlg.show_history([])

    def _refresh_ai_status(self):
        if self.settings_dlg is not None:
            self.settings_dlg.set_ai_status(self.ai_status(), busy=bool(self._ai_download),
                                            installed=self.ai_store.installed(self.s.ai_model))

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
    def _check_smoothness(self):
        if not self.overlay.isVisible():
            return
        fg = win32.foreground_info() if win32.IS_WINDOWS else {}
        self.diag.check_smoothness(f"(in front: {fg.get('app', '?')}, full screen: {fg.get('covers_whole_screen')})"
                                   if fg else "")

    def save_problem_report(self, note: str = "", reason: str = "you asked (shortcut / tray menu)"):
        from .diagnostics import make_report
        folder = make_report(self, note, reason)
        self.notify("Saved a problem report. Tell Claude what went wrong – it can read the report "
                    f"(folder: problem_reports\\{folder.name}).", "success", 6000, keep=True)
        return folder

    def ask_problem_report(self):
        from PySide6.QtWidgets import QInputDialog
        note, ok = QInputDialog.getMultiLineText(None, "Save a problem report",
                                                 "What went wrong? (optional – you can also just tell Claude)")
        if ok:
            self.save_problem_report(note.strip(), "you asked (tray menu)")

    def auto_problem_report(self, why: str):
        """Something unexpected broke: save a report on its own (at most every 10 minutes)."""
        now = _time.monotonic()
        if now - self._last_auto_report < 600:
            return
        self._last_auto_report = now
        try:
            from .diagnostics import make_report
            make_report(self, "", f"saved automatically: {why}")
        except Exception:
            log.exception("automatic problem report failed")

    def _tick(self):
        self.diag.tick()
        if self.overlay.isVisible():
            self.overlay.play_pos = self.clock.position()
            self.overlay.position = self.clock.position() + (self.s.sync_offset_ms + self._song_offset_ms) / 1000.0
            self.overlay.update()
