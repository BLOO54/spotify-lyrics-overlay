"""Spotify Lyrics Overlay - start here.

Usage:
  SpotifyLyricsOverlay.exe              normal mode (follows Spotify)
  SpotifyLyricsOverlay.exe --demo       fake song, to try the look without Spotify
  SpotifyLyricsOverlay.exe --self-test  checks that everything works, shows a report

Developer / CI options:
  --screenshot FILE   save a picture of the overlay (use with --quit-after)
  --quit-after SEC    exit automatically after SEC seconds
  --report FILE       write the --self-test report to FILE
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler


def setup_logging():
    from app.config import app_data_dir
    log_file = app_data_dir() / "app.log"
    handlers = [RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:          # the windowed .exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return log_file


def self_test(report_path: str | None) -> int:
    """Check each part of the app and report in plain language."""
    import asyncio
    import platform

    lines, ok = [], True

    def add(passed, text):
        nonlocal ok
        ok &= passed
        lines.append(("✔ " if passed else "✘ ") + text)

    add(True, f"Python {platform.python_version()} on {platform.platform()}")
    try:
        import PySide6
        add(True, f"PySide6 {PySide6.__version__} (user interface)")
    except Exception as e:
        add(False, f"PySide6 missing: {e}")

    if sys.platform == "win32":
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as M  # noqa: F401
            add(True, "Windows media integration loaded")

            async def probe():
                mgr = await M.request_async()
                apps = [s.source_app_user_model_id for s in mgr.get_sessions()]
                return apps
            apps = asyncio.run(probe())
            spot = [a for a in apps if "spotify" in (a or "").lower()]
            add(True, "Media players seen by Windows: " + (", ".join(apps) or "none right now"))
            lines.append(("✔ " if spot else "• ") + ("Spotify detected" if spot else
                         "Spotify not detected (open Spotify and play a song, then try again)"))
        except Exception as e:
            add(False, f"Windows media integration failed: {e}")
        try:
            from winrt.windows.storage.streams import DataWriter
            from app.media_session import buffer_to_bytes
            w = DataWriter()
            w.write_bytes(b"art!")
            add(buffer_to_bytes(w.detach_buffer()) == b"art!", "Album art reader works")
        except Exception as e:
            add(False, f"Album art reader failed: {e}")
        try:
            from app import win32
            add(win32.IS_WINDOWS, "Windows click-through / always-on-top helpers loaded")
        except Exception as e:
            add(False, f"Win32 helpers failed: {e}")
    else:
        lines.append("• Not running on Windows: media and click-through checks skipped")

    try:
        from app.lyrics_provider import LrclibClient
        items = LrclibClient(None)._get_json("/search", {"q": "love"})   # simple connectivity probe
        add(isinstance(items, list), "Lyrics service (LRCLIB) reachable")
    except Exception as e:
        add(False, f"Lyrics service not reachable (internet?): {e}")

    try:
        from app.translate import mymemory_probe, romaji_available, to_romaji
        add(romaji_available() and to_romaji("ありがとう").lower().startswith("arigat"),
            "Japanese romaji available")
        for sample in ("君の名前を呼んでいる", "ありがとう また 会おうね", "夜空に光る星を見て"):
            lines.append(f"• Romaji sample: {sample} → {to_romaji(sample)}")
        ok_tr = mymemory_probe()
        lines.append(("✔ " if ok_tr else "• ") + ("Translation service (MyMemory) reachable" if ok_tr
                     else "Translation service not reachable right now (only matters if you use translation)"))
    except Exception as e:
        add(False, f"Translation check failed: {e}")

    try:
        from app.ai_sync import ModelStore, runtime_available
        why = runtime_available()
        add(why is None, "AI sync parts included" if why is None else f"AI sync: {why}")
        store = ModelStore()
        have = [n for n in ("light", "best") if store.installed(n)]
        lines.append("• AI sync model: " + (", ".join(have) if have else "not downloaded (optional)"))
        try:
            import soundcard as sc
            sp = sc.default_speaker()
            lines.append(f"✔ Sound output found for AI sync: {sp.name}")
        except Exception as e:
            lines.append(f"• No sound output to listen to right now ({e})")
        if sys.platform == "win32":
            from app.ai_sync import run_probe
            from app.app_audio import find_app_pid
            pid = find_app_pid("Spotify.exe")
            if pid is None:
                lines.append("• AI sync 'Only Spotify': open Spotify to check it")
            else:
                rep = run_probe(pid)
                if rep.get("ok"):
                    loud = "and heard sound" if rep.get("rms", 0) > 1e-4 else "(silent - play a song to hear it)"
                    lines.append(f"✔ AI sync can listen to Spotify by itself {loud}")
                else:
                    lines.append(f"• AI sync can't listen to Spotify by itself here - it will use the whole PC "
                                 f"({rep.get('error')})")
    except Exception as e:
        add(False, f"AI sync check failed: {e}")

    lines.append("")
    lines.append("RESULT: " + ("all checks passed" if ok else "some checks failed"))
    text = "\n".join(lines)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if sys.stdout is not None:
        try:
            print(text)
        except Exception:
            pass
    if not report_path:
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
            QMessageBox.information(None, "Spotify Lyrics Overlay – Self test", text)
        except Exception:
            pass
    return 0 if ok else 1


def _schedule_tap_test(app, ctl, picture):
    """CI check of the tap-along window using real keyboard events."""
    from PySide6.QtCore import QEvent, Qt, QTimer
    from PySide6.QtGui import QKeyEvent

    def key(k):
        d = ctl.tap_dlg
        if d is not None:
            app.sendEvent(d, QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))

    QTimer.singleShot(1500, ctl.open_tap_sync)
    steps = [(2200, Qt.Key.Key_Space), (2900, Qt.Key.Key_Space), (3200, Qt.Key.Key_Down),
             (3400, Qt.Key.Key_Right), (3600, Qt.Key.Key_Right)]
    for ms, k in steps:
        QTimer.singleShot(ms, lambda k=k: key(k))
    QTimer.singleShot(4000, lambda: ctl.tap_dlg and ctl.tap_dlg.grab().save(picture))
    QTimer.singleShot(4300, lambda: key(Qt.Key.Key_Return))


def _make_backdrop(app):
    """A full-screen window of bold stripes, so a screen grab can show whether glass blurs it."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QWidget

    class Stripes(QWidget):
        def paintEvent(self, _):
            p = QPainter(self)
            cols = [QColor("#ef4444"), QColor("#facc15"), QColor("#22c55e"), QColor("#3b82f6"), QColor("#ffffff")]
            w = 28
            for i in range(-self.height() // w, self.width() // w + self.height() // w + 2):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cols[i % len(cols)])
                x = i * w
                p.drawPolygon([QPointF(x, 0), QPointF(x + w, 0),
                               QPointF(x + w - self.height(), self.height()), QPointF(x - self.height(), self.height())])
            p.end()

    w = Stripes()
    w.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    w.setGeometry(app.primaryScreen().geometry())
    w.show()
    return w


def _schedule_frame_test(app, ctl, out):
    """CI: draw the Spotify look for a few seconds with Windows' normal timer, then the precise one,
    and write how evenly spaced the frames were and how long each drawing took."""
    import json
    import statistics
    import time as _t

    from PySide6.QtCore import Qt, QTimer
    ctl.s.design = "spotify"
    ctl.s.highlight_style = "wipe"
    ctl.overlay.apply_settings() if hasattr(ctl.overlay, "apply_settings") else None
    ov = ctl.overlay
    ticks, paints = [], []
    real_paint = type(ov).paintEvent

    errors = []

    def timed_paint(e):
        t0 = _t.perf_counter()
        try:
            real_paint(ov, e)
        except Exception as ex:          # noqa: BLE001 - report drawing errors instead of hiding them
            errors.append(repr(ex))
        paints.append((_t.perf_counter() - t0) * 1000)
    ov.paintEvent = timed_paint
    real_tick = ctl._tick

    def tick():
        ticks.append(_t.perf_counter())
        real_tick()
    ctl.frame_timer.timeout.disconnect()
    ctl.frame_timer.timeout.connect(tick)
    report = {}

    def stats(name):
        gaps = [(b - a) * 1000 for a, b in zip(ticks, ticks[1:])]
        gaps.sort()
        report[name] = {"frames": len(ticks), "gap_ms_median": round(statistics.median(gaps), 2),
                        "gap_ms_p95": round(gaps[int(len(gaps) * 0.95)], 2), "gap_ms_max": round(gaps[-1], 2),
                        "uneven_frames_pct": round(100 * sum(1 for g in gaps if g > 24) / len(gaps), 1),
                        "paint_ms_median": round(statistics.median(paints), 2) if paints else None,
                        "paint_ms_p95": round(sorted(paints)[int(len(paints) * 0.95)], 2) if paints else None}
        ticks.clear()
        paints.clear()

    def phase(name, timer_type, then):
        ctl.frame_timer.stop()
        ctl.frame_timer.setTimerType(timer_type)
        ctl.frame_timer.start(16)
        ticks.clear()
        paints.clear()
        QTimer.singleShot(4000, lambda: (stats(name), then()))

    def done():
        report["paint_errors"] = errors[:5]
        report["words_with_timing"] = bool(ov.lyrics and ov.lyrics.synced and any(ln.words for ln in ov.lyrics.lines))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        app.quit()
    QTimer.singleShot(2500, lambda: phase("coarse", Qt.TimerType.CoarseTimer,
                                          lambda: phase("precise", Qt.TimerType.PreciseTimer, done)))


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--demo-song", default="1")
    parser.add_argument("--demo-start", type=float, default=0.0)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--screenshot")
    parser.add_argument("--quit-after", type=float, default=0)
    parser.add_argument("--screen-grab", help="CI: photograph the real screen under the overlay")
    parser.add_argument("--test-backdrop", action="store_true", help="CI: bright stripes behind the overlay")
    parser.add_argument("--test-tap", help="CI: open the sync window, tap with real key presses, save; picture to FILE")
    parser.add_argument("--ui-shot", nargs=2, metavar=("WHAT", "FILE"),
                        help="CI: picture of a window - WHAT is 'settings' or 'toast'")
    parser.add_argument("--ai-test", nargs=4, metavar=("MODEL", "WAV", "LRC", "OUT"),
                        help="CI: run AI sync on a recording and write a report")
    parser.add_argument("--ai-store", help="CI: folder for the AI model")
    parser.add_argument("--ai-options", default="", help="CI: extra AI options, e.g. second,vocals")
    parser.add_argument("--ai-note", help="CI: show this AI sync note in the overlay header")
    parser.add_argument("--ai-progress", type=float, help="CI: progress (0-1) shown with --ai-note")
    parser.add_argument("--listen-test", help="CI: try recording the PC's sound, report to FILE")
    parser.add_argument("--frame-test", help="CI: measure how evenly the lyrics are drawn, report to FILE")
    parser.add_argument("--app-audio-probe", nargs=2, metavar=("PID", "OUT"),
                        help="check that one app's sound can be recorded (used by AI sync)")
    args, _ = parser.parse_known_args()

    log_file = setup_logging()
    log = logging.getLogger("main")

    if args.ai_test:
        from app.ai_sync import cli_test
        return cli_test(*args.ai_test, store_dir=args.ai_store, options=args.ai_options)
    if args.app_audio_probe:
        import json
        from app.app_audio import probe
        pid, out = args.app_audio_probe
        rep = probe(int(pid))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh)
        return 0
    if args.listen_test:
        from app.ai_sync import listen_test
        return listen_test(args.listen_test)

    if args.self_test:
        return self_test(args.report)

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app import APP_NAME
    from app.config import app_data_dir

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)   # keep running in the tray

    def excepthook(et, ev, tb):
        log.error("Unexpected error:\n%s", "".join(traceback.format_exception(et, ev, tb)))
        try:
            QMessageBox.warning(None, APP_NAME, f"Something went wrong:\n{ev}\n\nDetails were saved to:\n{log_file}")
        except Exception:
            pass
    sys.excepthook = excepthook

    # Only one copy at a time (demo / tests may run alongside)
    lock = None
    if not args.demo and not args.screenshot:
        lock = QLockFile(str(app_data_dir() / "running.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.information(None, APP_NAME, "Spotify Lyrics Overlay is already running.\n"
                                    "Look for its icon in the system tray (bottom-right of the taskbar).")
            return 0

    backdrop = None
    if args.test_backdrop:
        backdrop = _make_backdrop(app)

    from app.controller import Controller
    ctl = Controller(app, demo=args.demo, demo_song=args.demo_song, demo_start=args.demo_start)
    log.info("Started (demo=%s)", args.demo)

    if args.screenshot or args.screen_grab:
        def snap():
            if args.screenshot:
                ok = ctl.overlay.grab().save(args.screenshot)
                log.info("Screenshot saved=%s to %s", ok, args.screenshot)
            if args.screen_grab:
                g = ctl.overlay.frameGeometry()
                pad = 24
                shot = app.primaryScreen().grabWindow(0, g.x() - pad, g.y() - pad, g.width() + pad * 2,
                                                      g.height() + pad * 2)
                log.info("Screen grab saved=%s frosted_active=%s", shot.save(args.screen_grab),
                         ctl.overlay._frosted_active)
        QTimer.singleShot(int(max(1.0, args.quit_after - 0.5) * 1000), snap)
    if args.test_tap:
        _schedule_tap_test(app, ctl, args.test_tap)
    if args.frame_test:
        _schedule_frame_test(app, ctl, args.frame_test)
    if args.ai_note:
        note_timer = QTimer()
        note_timer.timeout.connect(lambda: ctl.overlay.set_ai_note(args.ai_note.replace("_", " "),
                                                                   args.ai_progress))
        note_timer.start(300)
    if args.ui_shot:
        what, file = args.ui_shot
        if what == "settings":
            QTimer.singleShot(1500, ctl.open_settings)
            QTimer.singleShot(3200, lambda: ctl.settings_dlg and ctl.settings_dlg.grab().save(file))
        elif what == "history":
            def fill():
                ctl.history.add("AI synced “Neon Horizon” (AI heard 31/34 lines). It uses the new timing from now on.", "success")
                ctl.history.add("AI sync: “Loud Song” – only heard 6 of 40 lines clearly - kept the normal timing.", "info")
                ctl.history.add("AI sync is ready. It learns each song the first time it plays to the end.", "success")
                ctl.open_settings()
                ctl.settings_dlg.nav.setCurrentRow(8)
            QTimer.singleShot(1500, fill)
            QTimer.singleShot(3200, lambda: ctl.settings_dlg and ctl.settings_dlg.grab().save(file))
        elif what in ("meanings", "meanings-large"):
            def fill():
                from app.meanings import Explanation, SongHit, SongMeanings
                ctl.s.meanings_ai = True
                ctl.s.meanings_text_size = "large" if what == "meanings-large" else "normal"
                ctl.open_meanings()
                win = ctl.meanings_win
                win.resize(1060, 640)
                lines = ["Lights are racing through the night", "Every pixel burning bright",
                         "Hold the line and don't let go", "We're the final boss of the show", "",
                         "Neon rivers in the sky", "Never stop and never die"]
                m = SongMeanings(SongHit(1, "Neon Horizon (Demo)", "Spotify Lyrics Overlay", "https://genius.com"),
                                 "A demo song about late-night gaming sessions.",
                                 [Explanation("Hold the line and don't let go",
                                              "The singer is talking about a tough boss fight: keep your nerve and "
                                              "don't give up, even when it feels hopeless. It doubles as advice "
                                              "for real life.", 24, True, True, "",
                                              "<p>The singer is talking about a <b>tough boss fight</b>: keep your "
                                              "nerve and don't give up. It doubles as advice for real life.</p>"
                                              '<img src="https://raw.githubusercontent.com/BLOO54/spotify-lyrics-'
                                              'overlay/main/assets/demo_cover.png">'),
                                  Explanation("Every pixel burning bright", "The screen at full brightness at "
                                              "3 AM - the whole world shrinks to the game.", 5, False, False)])
                win._req += 1
                win.song = {"title": m.hit.title, "artist": m.hit.artist, "lines": lines,
                            "starts": None, "now": False}
                win.title_lbl.setText(m.hit.title)
                win.artist_lbl.setText(m.hit.artist)
                win.kicker.setText("NOW PLAYING")
                win._on_done(win._req, "song", {"lines": lines, "starts": None, "meanings": m, "genius_error": None})
                win._set_art(ctl.overlay._art_bytes)
                win.lyrics.setCurrentRow(2)
            QTimer.singleShot(1500, fill)
            QTimer.singleShot(5200, lambda: ctl.meanings_win and ctl.meanings_win.grab().save(file))
        elif what == "reset":
            def arm():
                ctl.open_settings()
                ctl.settings_dlg.nav.setCurrentRow(3)
                ctl.settings_dlg._reset_clicked()
            QTimer.singleShot(1500, arm)
            QTimer.singleShot(2600, lambda: ctl.settings_dlg and ctl.settings_dlg.grab().save(file))
        elif what == "fullscreen":
            QTimer.singleShot(1200, ctl.open_fullscreen)
            QTimer.singleShot(3400, lambda: ctl.full_win and ctl.full_win.grab().save(file))
        elif what == "aiwindow":
            def show_ai():
                from pathlib import Path as _P
                from app import ai_sync as _A
                ctl.s.ai_sync_enabled = True
                lines = ["Lights are racing through the night", "Every pixel burning bright",
                         "Hold the line and don't let go", "", "We're the final boss of the show",
                         "Neon rivers in the sky", "Never stop and never die"]
                path = ctl.ai_lyrics.save("Neon Horizon", "Demo Band",
                                          "[ti:Neon Horizon]\n[ar:Demo Band]\n[#:AI heard 5/6 lines]\n[00:01.00]x\n")
                res = _A.AlignResult([1.0] * 7, [None] * 7, [None] * 7, 5, 6, 0.9,
                                     [True, True, False, False, True, True, True])
                _A.save_details(_P(path), lines, res)
                ctl._recent_ai.insert(0, {"title": "Loud Song", "artist": "Somebody", "ok": False,
                                          "message": "only heard 6 of 40 lines clearly - kept the normal timing"})
                ctl.open_ai_window()
                ctl.ai_win.one_search.setText("birdbrain")
                ctl.ai_win._on_found(ctl.ai_win._search_no, [("BIRDBRAIN", "Jamie Paige", True),
                                                             ("BIRDBRAIN (feat. OK Glass)", "Jamie Paige", True)])
                ctl.ai_win.refresh()
                for k in range(ctl.ai_win.songs.count()):
                    if ctl.ai_win.songs.item(k).text().startswith("Neon Horizon"):
                        ctl.ai_win.songs.setCurrentRow(k)
            QTimer.singleShot(1500, show_ai)
            QTimer.singleShot(3300, lambda: ctl.ai_win and ctl.ai_win.grab().save(file))
        elif what == "synced":
            def show_synced():
                ctl.my_lyrics.save("birdbrain", "Jamie Paige", "[ti:birdbrain]\n[ar:Jamie Paige]\n[00:01.00]x\n")
                ctl.ai_lyrics.save("Neon Horizon", "Demo Band",
                                   "[ti:Neon Horizon]\n[ar:Demo Band]\n[#:AI heard 31/34 lines]\n[00:01.00]x\n")
                ctl.ai_lyrics.save("Midnight Drive", "The Example",
                                   "[ti:Midnight Drive]\n[ar:The Example]\n[#:AI heard 22/25 lines]\n[00:01.00]x\n")
                ctl.open_settings()
                ctl.settings_dlg.nav.setCurrentRow(7)
            QTimer.singleShot(1500, show_synced)
            QTimer.singleShot(3200, lambda: ctl.settings_dlg and ctl.settings_dlg.grab().save(file))
        elif what == "meanings-search":
            def search():
                ctl.open_meanings()
                ctl.meanings_win.search.setText("birdbrain jamie paige")
                ctl.meanings_win.do_search()
            QTimer.singleShot(1500, search)
            QTimer.singleShot(7000, lambda: ctl.meanings_win and ctl.meanings_win.grab().save(file))
        elif what == "toast":
            def pop():
                ctl.s.popup_style = "app"
                ctl.notify("Version 1.14.0 is available: AI sync for near-perfect timing.\nClick to update now.",
                           "info", 8000, action="Update now")
            QTimer.singleShot(1500, pop)
            QTimer.singleShot(2600, lambda: ctl.toast.grab().save(file))
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), ctl.quit)

    code = app.exec()
    if lock:
        lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
