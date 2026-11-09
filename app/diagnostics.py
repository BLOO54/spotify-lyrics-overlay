"""Problem reports: a snapshot of everything useful for working out what went wrong.

Press the "Save a problem report" shortcut (or use the tray menu) right when something looks
wrong. The app saves a folder under  %APPDATA%\\SpotifyLyricsOverlay\\problem_reports\\  with:

  report.md          what was playing, which lyrics/timing were used, AI sync state, how smoothly
                     the overlay was drawing, the last things that happened, recent pop-ups
  overlay.png        a picture of the overlay at that moment
  lyrics.lrc         the exact lyrics + timing that were on screen
  ai-details.json    what the AI heard for this song (if it was AI-synced)
  settings.json      your settings, with keys / passwords removed
  app-log.txt        the end of the app's log

Nothing is sent anywhere - the files stay on this PC.
"""
from __future__ import annotations

import json
import logging
import platform
import shutil
import statistics
import time
from collections import deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

REPORTS_KEPT = 10
SECRET_FIELDS = ("deepl_key", "translate_email", "genius_token", "anthropic_key")


def reports_dir() -> Path:
    from .config import app_data_dir
    d = app_data_dir() / "problem_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Recorder:
    """Keeps the last minute of drawing timings and the last few hundred app events in memory."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.ticks: deque = deque(maxlen=3600)        # (monotonic time) of every frame
        self.paints: deque = deque(maxlen=3600)       # (monotonic time, ms to draw)
        self.events: deque = deque(maxlen=400)        # (wall time text, text)
        self._last_warn = -1e9

    # ---- recording
    def tick(self):
        self.ticks.append(self._clock())

    def painted(self, ms: float):
        self.paints.append((self._clock(), ms))

    def event(self, text: str):
        self.events.append((datetime.now().strftime("%H:%M:%S.%f")[:-3], text))

    # ---- summaries
    def frame_stats(self, seconds: float = 60.0) -> dict:
        now = self._clock()
        ticks = [t for t in self.ticks if now - t <= seconds]
        gaps = sorted((b - a) * 1000 for a, b in zip(ticks, ticks[1:]) if b - a < 1.0)
        paints = sorted(ms for t, ms in self.paints if now - t <= seconds)
        if len(gaps) < 10:
            return {"frames": len(ticks), "note": "the overlay was hidden or just started"}

        def pct(xs, q):
            return round(xs[min(len(xs) - 1, int(len(xs) * q))], 2)
        return {"seconds": seconds, "frames": len(ticks),
                "fps": round(len(ticks) / max(ticks[-1] - ticks[0], 0.001), 1),
                "frame_gap_ms_median": round(statistics.median(gaps), 2), "frame_gap_ms_p95": pct(gaps, 0.95),
                "frame_gap_ms_max": round(gaps[-1], 2),
                "late_frames_pct": round(100 * sum(1 for g in gaps if g > 24) / len(gaps), 1),
                "draw_ms_median": round(statistics.median(paints), 2) if paints else None,
                "draw_ms_p95": pct(paints, 0.95) if paints else None,
                "draw_ms_max": round(paints[-1], 2) if paints else None}

    def check_smoothness(self, extra: str = "") -> dict | None:
        """Called every ~30 s: writes a warning to the log when drawing was jerky (at most every 5 min)."""
        st = self.frame_stats(30.0)
        if "fps" not in st:
            return None
        bad = st["late_frames_pct"] > 5 or (st["draw_ms_p95"] or 0) > 12
        if bad and self._clock() - self._last_warn > 300:
            self._last_warn = self._clock()
            log.warning("overlay drawing was uneven: %s %s", st, extra)
            self.event(f"drawing was uneven: {st['late_frames_pct']}% late frames, "
                       f"draw p95 {st['draw_ms_p95']} ms {extra}")
            return st
        return None


def _sanitised_settings(s) -> dict:
    from dataclasses import asdict
    try:
        d = asdict(s)
    except Exception:
        d = dict(vars(s))
    for k in SECRET_FIELDS:
        if k in d:
            d[k] = "(set)" if d[k] else ""
    return d


def _lrc_of(ly) -> str:
    def ts(t):
        return f"{int(t // 60):02d}:{t % 60:05.2f}"
    out = []
    for ln in ly.lines:
        words = getattr(ln, "words", None)
        if words:
            out.append(f"[{ts(ln.start)}]" + " ".join(f"<{ts(w.start)}>{w.text}" for w in words))
        else:
            out.append(f"[{ts(ln.start)}]{ln.text}")
    return "\n".join(out) + "\n"


def _tail(path: Path, lines: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(deque(fh, maxlen=lines))
    except OSError as e:
        return f"(couldn't read the log: {e})"


def _section(title: str, rows) -> str:
    out = [f"\n## {title}\n"]
    for k, v in rows:
        out.append(f"- **{k}:** {v}")
    return "\n".join(out) + "\n"


def make_report(ctl, note: str = "", reason: str = "asked") -> Path:
    """Save a problem report folder; returns its path. Never raises - a broken part is noted instead."""
    from . import APP_VERSION
    from .config import app_data_dir
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = reports_dir() / stamp
    folder.mkdir(parents=True, exist_ok=True)
    md = [f"# Problem report {stamp}\n",
          f"- **Why saved:** {reason}",
          f"- **What you said went wrong:** {note or '(nothing typed)'}",
          f"- **App version:** {APP_VERSION}",
          f"- **Windows:** {platform.platform()} · Python {platform.python_version()}"]

    def safe(name, fn):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001 - the report must always be written
            log.exception("problem report: %s failed", name)
            return f"\n## {name}\n(couldn't collect this: {e!r})\n"

    def song():
        st, ov = ctl.track, ctl.overlay
        if not st:
            return _section("Song", [("Playing", "nothing (or Spotify not found)")])
        ly = ov.lyrics
        idx = ly.line_index_at(ov.position) if (ly and ly.synced) else None
        cur = ly.lines[idx].text if (ly and idx is not None and 0 <= idx < len(ly.lines)) else ""
        r = ctl._last_result
        rows = [("Title / artist", f"{st.title} — {st.artist} ({st.album})"),
                ("Length", f"{st.duration:.1f} s"),
                ("Position (app's clock)", f"{ctl.clock.position():.2f} s, "
                                           f"{'playing' if ctl.clock.playing else 'paused'}"),
                ("Position Spotify last reported", f"{st.position:.2f} s at {st.last_updated}"),
                ("Player", f"{st.source_app} · can jump within songs: {st.can_seek}"),
                ("Lyrics source", (r.source if r else "none") + (" (instrumental)" if r and r.instrumental else "")),
                ("Lyrics on screen", f"{len(ly.lines)} lines, synced={ly.synced}, real word timing="
                                     f"{ly.has_word_timing}" if ly else (ov.status or "none")),
                ("Line on screen", f"#{idx}: {cur!r}" if idx is not None else "-"),
                ("Timing shift", f"all songs {ctl.s.sync_offset_ms} ms, this song {ctl._song_offset_ms} ms"),
                ("Overlay", f"visible={ctl.s.visible}, design={ctl.s.design}, highlight={ctl.s.highlight_style}, "
                            f"lines shown={ctl.s.lines_shown}, size={ov.width()}×{ov.height()}")]
        return _section("Song", rows)

    def screens():
        from PySide6.QtWidgets import QApplication
        rows = []
        for i, sc in enumerate(QApplication.screens()):
            g = sc.geometry()
            rows.append((f"Screen {i}", f"{g.width()}×{g.height()} at {g.x()},{g.y()}, scale "
                                        f"{sc.devicePixelRatio():g}, {sc.refreshRate():.0f} Hz"))
        from . import win32
        fg = win32.foreground_info() if hasattr(win32, "foreground_info") else {}
        rows.append(("App in front", fg or "unknown"))
        return _section("Screens", rows)

    def smooth():
        rows = [(k, v) for k, v in ctl.diag.frame_stats(60.0).items()]
        return _section("Drawing smoothness (last minute)", rows)

    def ai():
        snap = ctl.ai_snapshot()
        rows = [("Status", ctl.ai_status()), ("Right now", snap.get("now")),
                ("Checklist", "; ".join(f"{'✔' if ok else '✖' if ok is False else '…'} {t}"
                                        for ok, t in snap.get("need") or [])),
                ("Sync one song / missed parts", {k: v for k, v in (snap.get("one_song") or {}).items()
                                                  if k not in ("details", "plan")} or "-"),
                ("Last missed-parts result", snap.get("last_patch") or "-"),
                ("Recent AI results", [(r.get("title"), r.get("message")) for r in snap.get("recent", [])[:6]])]
        try:
            from .spotify_prefs import crossfade_seconds
            rows.append(("Spotify crossfade", crossfade_seconds()))
        except Exception:
            pass
        return _section("AI sync", rows)

    def events():
        lines = [f"    {t}  {txt}" for t, txt in list(ctl.diag.events)[-150:]]
        return "\n## What happened recently (newest last)\n\n" + ("\n".join(lines) or "    (nothing yet)") + "\n"

    def pops():
        items = ctl.history.newest_first()[:15] if hasattr(ctl, "history") else []
        rows = [(h.get("time", ""), f"[{h.get('kind', '')}] {h.get('text', '')}") for h in items]
        return _section("Recent pop-ups", rows)

    for name, fn in (("Song", song), ("Screens", screens), ("Smoothness", smooth), ("AI sync", ai),
                     ("Events", events), ("Pop-ups", pops)):
        md.append(safe(name, fn))

    # ---- files next to the report
    def files():
        ly = ctl.overlay.lyrics
        if ly:
            (folder / "lyrics.lrc").write_text(_lrc_of(ly), encoding="utf-8")
        if ctl.track and ctl._last_result and ctl._last_result.source == "AI sync":
            from . import ai_sync
            from .my_lyrics import list_synced
            for e in list_synced(ctl.ai_lyrics.folder, "ai"):
                if ctl._same_song(e.get("title", ""), e.get("artist", ""), ctl.track.title, ctl.track.artist):
                    d = ai_sync.details_path(Path(e["path"]))
                    if d.exists():
                        shutil.copy2(d, folder / "ai-details.json")
        (folder / "settings.json").write_text(json.dumps(_sanitised_settings(ctl.s), indent=1, ensure_ascii=False),
                                             encoding="utf-8")
        (folder / "app-log.txt").write_text(_tail(app_data_dir() / "app.log", 1500), encoding="utf-8")
        try:
            ctl.overlay.grab().save(str(folder / "overlay.png"))
        except Exception as e:                       # noqa: BLE001
            log.warning("problem report: no overlay picture (%s)", e)
        return ""
    md.append(safe("Files", files))
    (folder / "report.md").write_text("\n".join(md), encoding="utf-8")
    log.info("problem report saved: %s (%s)", folder, reason)
    # keep only the newest few
    old = sorted((p for p in reports_dir().iterdir() if p.is_dir()), key=lambda p: p.name)[:-REPORTS_KEPT]
    for p in old:
        shutil.rmtree(p, ignore_errors=True)
    return folder
