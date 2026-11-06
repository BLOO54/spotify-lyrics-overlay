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
        add(romaji_available() and to_romaji("日本").lower().startswith("nihon"),
            "Japanese romaji available")
        ok_tr = mymemory_probe()
        lines.append(("✔ " if ok_tr else "• ") + ("Translation service (MyMemory) reachable" if ok_tr
                     else "Translation service not reachable right now (only matters if you use translation)"))
    except Exception as e:
        add(False, f"Translation check failed: {e}")

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


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--demo-song", default="1")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--screenshot")
    parser.add_argument("--quit-after", type=float, default=0)
    args, _ = parser.parse_known_args()

    log_file = setup_logging()
    log = logging.getLogger("main")

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

    from app.controller import Controller
    ctl = Controller(app, demo=args.demo, demo_song=args.demo_song)
    log.info("Started (demo=%s)", args.demo)

    if args.screenshot:
        def snap():
            ok = ctl.overlay.grab().save(args.screenshot)
            log.info("Screenshot saved=%s to %s", ok, args.screenshot)
        QTimer.singleShot(int(max(1.0, args.quit_after - 0.5) * 1000), snap)
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), ctl.quit)

    code = app.exec()
    if lock:
        lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
