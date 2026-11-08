"""The "Song meanings" window: search a song (or use the one playing), click a lyric line,
read what it means - from Genius, or the AI backup if you turned it on."""

from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget, QCheckBox)

from .meanings import AiExplainer, GeniusClient, MeaningsError, SongHit, line_explanations
from .settings_dialog import STYLE

log = logging.getLogger(__name__)

EXTRA_STYLE = """
QListWidget#lyrics, QListWidget#results { background: #121219; border: 1px solid #23232e; border-radius: 10px;
    padding: 8px; color: #d9d9e6; font-size: 15px; outline: none; }
QListWidget#lyrics::item, QListWidget#results::item { padding: 7px 10px; border-radius: 7px; }
QListWidget#lyrics::item:selected, QListWidget#results::item:selected { background: #3b2a6b; color: #ffffff; }
QListWidget#lyrics::item:hover, QListWidget#results::item:hover { background: #1f1f2b; }
QTextBrowser#meaning { background: #15151d; border: 1px solid #23232e; border-radius: 10px; padding: 14px;
    color: #e6e6f0; font-size: 14px; }
QLabel#songTitle { font-size: 20px; font-weight: 600; }
QLabel#songArtist { color: #9a9ab0; font-size: 13px; }
QLabel#credit { color: #6f6f88; font-size: 11px; }
"""

MARK = QColor("#2a2340")          # background of lines Genius explained
NOW = QColor("#c4b5fd")           # the line being sung right now


class MeaningsWindow(QWidget):
    _done = Signal(int, str, object)       # request number, what, result (or MeaningsError)

    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.s = ctl.s
        self.setWindowTitle("Song meanings")
        self.setStyleSheet(STYLE + EXTRA_STYLE)
        self.resize(1060, 720)
        self.setMinimumSize(780, 520)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="meanings")
        self._req = 0
        self._done.connect(self._on_done)
        self.song: dict | None = None        # title, artist, lines, starts, now
        self.meanings = None
        self.line_map: list[list[int]] = []
        self.rows: list[int] = []             # list row -> line index (-1 = gap)
        self._now_row = -1
        self._ai_quote = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(12)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search a song or artist…")
        self.search.returnPressed.connect(self.do_search)
        go = QPushButton("Search")
        go.clicked.connect(self.do_search)
        self.now_btn = QPushButton("♪  Now playing")
        self.now_btn.setObjectName("primary")
        self.now_btn.clicked.connect(self.show_now_playing)
        top.addWidget(self.search, 1)
        top.addWidget(go)
        top.addWidget(self.now_btn)
        root.addLayout(top)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        self.title_lbl = QLabel("Song meanings")
        self.title_lbl.setObjectName("songTitle")
        self.artist_lbl = QLabel("Search a song, or press Now playing.")
        self.artist_lbl.setObjectName("songArtist")
        titles.addWidget(self.title_lbl)
        titles.addWidget(self.artist_lbl)
        head.addLayout(titles, 1)
        self.follow = QCheckBox("Follow the song")
        self.follow.setChecked(self.s.meanings_follow)
        self.follow.toggled.connect(self._follow_changed)
        head.addWidget(self.follow, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(head)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.left = QStackedWidget()
        self.lyrics = QListWidget()
        self.lyrics.setObjectName("lyrics")
        self.lyrics.setWordWrap(True)
        self.lyrics.currentRowChanged.connect(self._line_clicked)
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.itemActivated.connect(self._result_chosen)
        self.results.itemClicked.connect(self._result_chosen)
        self.left.addWidget(self.lyrics)
        self.left.addWidget(self.results)
        self.meaning = QTextBrowser()
        self.meaning.setObjectName("meaning")
        self.meaning.setOpenExternalLinks(True)
        split.addWidget(self.left)
        split.addWidget(self.meaning)
        split.setSizes([560, 480])
        root.addWidget(split, 1)

        foot = QHBoxLayout()
        self.credit = QLabel("Explanations from <a style='color:#a78bfa' href='https://genius.com'>Genius</a> · "
                             "lyrics from LRCLIB")
        self.credit.setObjectName("credit")
        self.credit.setOpenExternalLinks(True)
        self.status = QLabel("")
        self.status.setObjectName("credit")
        foot.addWidget(self.credit)
        foot.addStretch(1)
        foot.addWidget(self.status)
        root.addLayout(foot)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._follow_tick)
        self.timer.start(300)
        self._intro()

    # ------------------------------------------------------------------ helpers
    def showEvent(self, e):
        super().showEvent(e)
        try:
            from . import win32
            win32.set_dark_titlebar(int(self.winId()))
        except Exception:
            pass

    def closeEvent(self, e):
        self._req += 1            # ignore answers that arrive after closing
        super().closeEvent(e)

    def _genius(self):
        from .config import app_data_dir
        return GeniusClient(self.s.genius_token, app_data_dir() / "meanings_cache")

    def _ai(self):
        from .config import app_data_dir
        return AiExplainer(self.s.anthropic_key, app_data_dir() / "meanings_ai.json")

    def _language(self) -> str:
        try:
            from .translate import LANGUAGES
            return LANGUAGES.get(self.s.translate_target, ("English",))[0]
        except Exception:
            return "English"

    def _run(self, what: str, fn):
        self._req += 1
        req = self._req

        def work():
            try:
                return fn()
            except MeaningsError as e:
                return e
            except Exception as e:      # never let a background problem crash the app
                log.exception("song meanings: %s failed", what)
                return MeaningsError(f"Something went wrong ({e}).")
        self.pool.submit(work).add_done_callback(lambda f: self._done.emit(req, what, f.result()))
        return req

    def _html(self, body: str):
        self.meaning.setHtml(f"<div style='line-height:150%'>{body}</div>")

    def _intro(self):
        need = "" if self.s.genius_token else (
            "<p style='color:#fbbf24'>To see Genius explanations, add your free Genius access token in "
            "Settings → Song meanings.</p>")
        self._html("<h3>How it works</h3><p>Press <b>Now playing</b> for the song you're listening to, or "
                   "search for any song. Lines with a <span style='background:#2a2340'>&nbsp;purple "
                   "background&nbsp;</span> have an explanation on Genius – click a line to read it.</p>" + need)

    # ------------------------------------------------------------------ loading songs
    def show_now_playing(self):
        now = self.ctl.meanings_now_playing()
        if not now:
            self.status.setText("Nothing is playing in Spotify right now.")
            return
        self._load(now["title"], now["artist"], lines=now["lines"], starts=now["starts"], now=True)

    def do_search(self):
        q = self.search.text().strip()
        if not q:
            return
        self.status.setText("Searching…")
        token = bool(self.s.genius_token)

        def work():
            if token:
                return self._genius().search(q)
            items = self.ctl.client._get_json("/search", {"q": q}) or []
            seen, out = set(), []
            for it in items:
                key = (it.get("trackName"), it.get("artistName"))
                if key not in seen and key[0]:
                    seen.add(key)
                    out.append(SongHit(None, key[0], key[1] or ""))
            return out[:30]
        self._run("search", work)

    def _result_chosen(self, item):
        hit = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(hit, SongHit):
            self._load(hit.title, hit.artist, hit=hit if hit.genius_id else None)

    def _load(self, title, artist, lines=None, starts=None, now=False, hit=None):
        self.left.setCurrentWidget(self.lyrics)
        self.title_lbl.setText(title)
        self.artist_lbl.setText(artist)
        self.lyrics.clear()
        self.rows, self.line_map, self.meanings = [], [], None
        self.song = {"title": title, "artist": artist, "lines": lines, "starts": starts, "now": now}
        self.follow.setVisible(bool(now and starts))
        self._html("<p style='color:#9a9ab0'>Loading lyrics and explanations…</p>")
        self.status.setText("Loading…")
        token = bool(self.s.genius_token)

        def work():
            ls, st = lines, starts
            if not ls:
                from .lrc import parse_lrc
                from .my_lyrics import lines_for_tapping
                res = self.ctl.client.fetch(title, artist)
                if res.synced:
                    parsed = parse_lrc(res.synced)
                    ls, st = [p.text for p in parsed], None
                else:
                    ls = lines_for_tapping(None, res.plain) if res.plain else []
            out = {"lines": ls, "starts": st, "meanings": None, "genius_error": None}
            if token:
                try:
                    g = self._genius()
                    h = hit or g.find(title, artist)
                    out["meanings"] = g.meanings(h) if h else None
                    if h is None:
                        out["genius_error"] = "Genius doesn't seem to have this song."
                except MeaningsError as e:
                    out["genius_error"] = str(e)
            return out
        self._run("song", work)

    def _show_song(self, data):
        self.song["lines"] = data["lines"]
        self.song["starts"] = data["starts"] if self.song.get("starts") else None
        self.meanings = data["meanings"]
        lines = self.song["lines"] or []
        expl = self.meanings.explanations if self.meanings else []
        self.line_map = line_explanations(lines, expl)
        self.lyrics.blockSignals(True)
        self.lyrics.clear()
        self.rows = []
        explained = 0
        for i, text in enumerate(lines):
            item = QListWidgetItem(text if text.strip() else " ")
            if not text.strip():
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            elif self.line_map[i]:
                item.setBackground(MARK)
                explained += 1
            self.lyrics.addItem(item)
            self.rows.append(i)
        self.lyrics.setCurrentRow(-1)
        self.lyrics.blockSignals(False)
        self._now_row = -1
        if not lines:
            self._html("<p>No lyrics found for this song.</p>")
        else:
            self._show_about(data.get("genius_error"))
        parts = []
        if self.meanings:
            parts.append(f"{explained} of {sum(1 for ln in lines if ln.strip())} lines explained on Genius")
        elif data.get("genius_error"):
            parts.append(data["genius_error"])
        self.status.setText(" · ".join(parts))

    def _show_about(self, genius_error=None):
        m = self.meanings
        bits = []
        if m and m.about:
            bits.append(f"<h3>About this song</h3><p>{html.escape(m.about).replace(chr(10), '<br>')}</p>")
        elif m:
            bits.append("<p style='color:#9a9ab0'>Genius has no description for this song.</p>")
        if genius_error:
            bits.append(f"<p style='color:#fbbf24'>{html.escape(genius_error)}</p>")
        if m and m.hit.url:
            bits.append(f"<p><a style='color:#a78bfa' href='{html.escape(m.hit.url)}'>Open this song on Genius</a></p>")
        bits.append("<p style='color:#9a9ab0'>Click a line to see what it means.</p>")
        self._html("".join(bits))

    # ------------------------------------------------------------------ clicking a line
    def _line_clicked(self, row: int):
        if row < 0 or row >= len(self.rows) or not self.song:
            return
        i = self.rows[row]
        lines = self.song["lines"] or []
        if i < 0 or i >= len(lines) or not lines[i].strip():
            return
        quote = f"<p style='color:#c4b5fd; font-size:16px'><b>“{html.escape(lines[i])}”</b></p>"
        expl = self.meanings.explanations if self.meanings else []
        found = [expl[k] for k in (self.line_map[i] if i < len(self.line_map) else [])]
        if found:
            parts = [quote]
            for ex in sorted(found, key=lambda e: (not e.verified, -e.votes)):
                tags = []
                if ex.verified:
                    tags.append("<span style='color:#4ade80'>✔ Verified by the artist</span>")
                if not ex.accepted:
                    tags.append("<span style='color:#fbbf24'>Unreviewed</span>")
                if ex.votes:
                    tags.append(f"<span style='color:#9a9ab0'>▲ {ex.votes}</span>")
                frag = ""
                if _short(ex.fragment) != _short(lines[i]):
                    frag = f"<p style='color:#9a9ab0'><i>{html.escape(ex.fragment).replace(chr(10), '<br>')}</i></p>"
                link = (f" · <a style='color:#a78bfa' href='{html.escape(ex.url)}'>on Genius</a>" if ex.url else "")
                parts.append(f"{frag}<p>{html.escape(ex.text).replace(chr(10), '<br>')}</p>"
                             f"<p style='font-size:12px'>{' · '.join(tags)}{link}</p><hr>")
            parts.append("<p style='color:#6f6f88; font-size:11px'>Explanation from Genius contributors.</p>")
            self._html("".join(parts))
            return
        if self.s.meanings_ai and not self.s.anthropic_key.strip():
            self._html(quote + "<p>Genius hasn't explained this line yet.</p><p style='color:#fbbf24'>Add your "
                       "Anthropic API key in Settings → Song meanings to have the AI explain it.</p>")
            return
        if self.s.meanings_ai:
            ai = self._ai()
            got = ai.cached(self.song["title"], self.song["artist"], lines[i], self._language())
            if got:
                self._show_ai(quote, got)
                return
            self._html(quote + "<p style='color:#9a9ab0'>Genius hasn't explained this line – asking the AI…</p>")
            title, artist, lang = self.song["title"], self.song["artist"], self._language()
            self._ai_quote = quote                  # set before asking, the answer may come back fast
            self._run("ai", lambda: ai.explain(title, artist, lines, i, lang))
            return
        hint = ("Turn on the AI backup in Settings → Song meanings to have lines like this explained."
                if not self.s.meanings_ai else "")
        self._html(quote + "<p>Genius hasn't explained this line yet.</p>"
                   f"<p style='color:#9a9ab0'>{hint}</p>")

    def _show_ai(self, quote, text):
        self._html(quote + f"<p>{html.escape(text).replace(chr(10), '<br>')}</p>"
                   "<p style='color:#6f6f88; font-size:11px'>✦ AI explanation (Claude) – Genius hasn't explained "
                   "this line. It's an interpretation and can be wrong.</p>")

    # ------------------------------------------------------------------ answers from the background
    def _on_done(self, req, what, result):
        if req != self._req:
            return
        if isinstance(result, MeaningsError):
            self.status.setText(str(result))
            if what == "ai":
                self._html(self._ai_quote + f"<p style='color:#fbbf24'>{html.escape(str(result))}</p>")
            elif what == "song":
                self._html(f"<p style='color:#fbbf24'>{html.escape(str(result))}</p>")
            return
        if what == "search":
            self.results.clear()
            for hit in result:
                it = QListWidgetItem(hit.label)
                it.setData(Qt.ItemDataRole.UserRole, hit)
                self.results.addItem(it)
            if not result:
                self.results.addItem(QListWidgetItem("No songs found."))
            self.left.setCurrentWidget(self.results)
            self.title_lbl.setText("Search results")
            self.artist_lbl.setText(f"for “{self.search.text().strip()}” – click a song")
            self.follow.setVisible(False)
            self.status.setText("")
        elif what == "song":
            self._show_song(result)
        elif what == "ai":
            self._show_ai(self._ai_quote, result)
            self.status.setText("")

    # ------------------------------------------------------------------ following the song
    def _follow_changed(self, on: bool):
        self.s.meanings_follow = bool(on)
        self.s.save()

    def _follow_tick(self):
        if not (self.isVisible() and self.song and self.song.get("now") and self.song.get("starts")
                and self.follow.isChecked()):
            return
        np_ = self.ctl.meanings_now_playing()
        if not np_ or np_["title"] != self.song["title"]:
            return
        pos = self.ctl.overlay.position
        starts = self.song["starts"]
        idx = -1
        for k, t in enumerate(starts):
            if t <= pos:
                idx = k
            else:
                break
        row = idx if 0 <= idx < self.lyrics.count() else -1
        if row == self._now_row:
            return
        if 0 <= self._now_row < self.lyrics.count():
            old = self.lyrics.item(self._now_row)
            f = old.font()
            f.setBold(False)
            old.setFont(f)
            old.setForeground(QColor("#d9d9e6"))
        self._now_row = row
        if row >= 0:
            it = self.lyrics.item(row)
            f = QFont(it.font())
            f.setBold(True)
            it.setFont(f)
            it.setForeground(NOW)
            self.lyrics.scrollToItem(it, QListWidget.ScrollHint.PositionAtCenter)


def _short(s: str) -> str:
    import re
    return re.sub(r"[^\w]+", "", (s or "").lower())
