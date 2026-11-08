"""The "Song meanings" window: search a song (or use the one playing), click a lyric line,
read what it means - from Genius, or the AI backup if you turned it on."""

from __future__ import annotations

import html
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap, QTextDocument
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QStackedWidget, QStyle, QStyledItemDelegate, QTextBrowser, QVBoxLayout,
                               QWidget)

from .meanings import (AiExplainer, GeniusClient, MeaningsError, SongHit, fetch_x_post, image_urls,
                       line_explanations, x_posts)

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SpotifyLyricsOverlay"
from .settings_dialog import STYLE

log = logging.getLogger(__name__)

DEFAULT_ACCENT = "#8b5cf6"
TEXT_SIZES = {"normal": 14, "large": 17, "huge": 21}     # Settings → Song meanings → Meaning text size
EXPLAINED = Qt.ItemDataRole.UserRole + 1
SINGING = Qt.ItemDataRole.UserRole + 2


def _style(accent: str, text_px: int = 14) -> str:
    a = QColor(accent)
    h = accent.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else (139, 92, 246)
    rgba = f"rgba({r}, {g}, {b}, 35%)"
    return STYLE + f"""
QWidget#meaningsRoot {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #11101a, stop:1 #0b0b10);
    color: #ececf3; }}
QWidget#meaningsRoot QLabel, QWidget#meaningsRoot QCheckBox {{ background: transparent; }}
QLineEdit#searchBox {{ background: #17161f; border: 1px solid #2a2836; border-radius: 19px; padding: 9px 16px;
    font-size: 14px; color: #ececf3; }}
QLineEdit#searchBox:focus {{ border: 1px solid {accent}; }}
QPushButton#pill {{ background: {accent}; color: white; border: none; border-radius: 19px; padding: 9px 18px;
    font-weight: 600; }}
QPushButton#pill:hover {{ background: {a.lighter(115).name()}; }}
QPushButton#ghost {{ background: #1b1a24; color: #d6d6e2; border: 1px solid #2a2836; border-radius: 19px;
    padding: 9px 16px; }}
QFrame#hero {{ border-radius: 18px; border: 1px solid #262433;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {rgba}, stop:0.65 #14131c, stop:1 #111018); }}
QLabel#kicker {{ color: {a.lighter(140).name()}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
QLabel#heroTitle {{ font-size: 26px; font-weight: 700; color: #ffffff; }}
QLabel#heroArtist {{ font-size: 14px; color: #b5b5c8; }}
QLabel#chip {{ background: #1f1d2a; color: #cfcfe0; border: 1px solid #2c2a3a; border-radius: 10px;
    padding: 3px 10px; font-size: 11px; }}
QLabel#chipAccent {{ background: {rgba}; color: #ffffff; border-radius: 10px; padding: 3px 10px; font-size: 11px; }}
QFrame#card {{ background: #13121a; border: 1px solid #23212e; border-radius: 18px; }}
QListWidget#lyrics, QListWidget#results {{ background: transparent; border: none; color: #cfcfdc;
    font-size: 16px; outline: none; padding: 6px; }}
QListWidget#lyrics::item, QListWidget#results::item {{ padding: 8px 14px; border-radius: 10px; }}
QListWidget#lyrics::item:hover, QListWidget#results::item:hover {{ background: #1c1b26; }}
QListWidget#lyrics::item:selected, QListWidget#results::item:selected {{ background: transparent; color: #ffffff; }}
QTextBrowser#meaning {{ background: transparent; border: none; color: #e6e6f0; font-size: {text_px}px; }}
QLabel#credit {{ color: #6f6f88; font-size: 11px; }}
"""


def _rounded(img_bytes: bytes | None, side: int, radius: int = 14) -> QPixmap | None:
    if not img_bytes:
        return None
    img = QImage.fromData(img_bytes)
    if img.isNull():
        return None
    src = QPixmap.fromImage(img).scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                         Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(side, side)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src)
    p.end()
    return out


def _accent_from(img_bytes: bytes | None) -> str:
    try:
        from .overlay import OverlayWindow
        pal = OverlayWindow._palette_from(img_bytes) if img_bytes else None
        return (pal or {}).get("highlight") or DEFAULT_ACCENT
    except Exception:
        return DEFAULT_ACCENT


class _PictureBrowser(QTextBrowser):
    """A QTextBrowser that shows pictures from the internet: the window downloads them,
    and this hands them to the page (a blank space shows until a picture has arrived)."""

    def __init__(self, images: dict):
        super().__init__()
        self.images = images

    def loadResource(self, kind, url):
        if kind == QTextDocument.ResourceType.ImageResource and url.scheme() in ("http", "https"):
            img = self.images.get(url.toString())
            if img is not None:
                return img
            blank = QImage(1, 1, QImage.Format.Format_ARGB32)
            blank.fill(Qt.GlobalColor.transparent)
            return blank
        return super().loadResource(kind, url)


class _LineDelegate(QStyledItemDelegate):
    """Draws the lyric lines: a soft tint + accent bar for explained lines, a stronger one for the
    selected line and a glow for the line being sung."""

    def __init__(self, win):
        super().__init__(win.lyrics)
        self.win = win

    def paint(self, painter: QPainter, option, index):
        accent = QColor(self.win.accent)
        r = option.rect.adjusted(3, 2, -3, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        explained = bool(index.data(EXPLAINED))
        singing = bool(index.data(SINGING))
        if explained or selected or singing:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            fill = QColor(accent)
            fill.setAlpha(110 if selected else 70 if singing else 34)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(r), 10, 10)
            if explained:
                bar = QColor(accent)
                bar.setAlpha(255 if selected else 200)
                painter.setBrush(bar)
                painter.drawRoundedRect(QRectF(r.left() + 1, r.top() + 8, 3, max(4, r.height() - 16)), 1.5, 1.5)
            painter.restore()
        super().paint(painter, option, index)


class MeaningsWindow(QWidget):
    _done = Signal(int, str, object)       # request number, what, result (or MeaningsError)
    _art = Signal(int, object)             # request number, image bytes
    _pic = Signal(str, object)             # picture url, image bytes (pictures inside explanations)
    _xpost = Signal(str, object)           # X post url, post info dict (or None)

    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.s = ctl.s
        self.accent = DEFAULT_ACCENT
        self.setWindowTitle("Song meanings")
        self.setObjectName("meaningsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # a normal window: it opens in front, but never covers your game
        self.setStyleSheet(_style(self.accent, self._text_px()))
        self.resize(1100, 760)
        self.setMinimumSize(820, 560)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="meanings")
        self._req = 0
        self._art_req = 0
        self._done.connect(self._on_done)
        self._art.connect(self._on_art)
        self._pic.connect(self._on_picture)
        self._xpost.connect(self._on_x_post)
        self._pictures: dict[str, QImage] = {}
        self._pictures_pending: set[str] = set()
        self._pictures_failed: set[str] = set()
        self._x_cache: dict[str, dict | None] = {}
        self._x_pending: set[str] = set()
        self._shown_row = -1
        self.song: dict | None = None        # title, artist, lines, starts, now
        self.meanings = None
        self.line_map: list[list[int]] = []
        self.rows: list[int] = []
        self._now_row = -1
        self._ai_quote = ""
        self._last_body = ""
        self._thumbs: dict[str, QIcon] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(14)

        # ---- search bar
        top = QHBoxLayout()
        top.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("🔍   Search any song or artist…")
        self.search.returnPressed.connect(self.do_search)
        go = QPushButton("Search")
        go.setObjectName("ghost")
        go.clicked.connect(self.do_search)
        self.now_btn = QPushButton("♪  Now playing")
        self.now_btn.setObjectName("pill")
        self.now_btn.clicked.connect(self.show_now_playing)
        top.addWidget(self.search, 1)
        top.addWidget(go)
        top.addWidget(self.now_btn)
        root.addLayout(top)

        # ---- hero: album art, title, artist, chips
        self.hero = QFrame()
        self.hero.setObjectName("hero")
        hl = QHBoxLayout(self.hero)
        hl.setContentsMargins(18, 16, 18, 16)
        hl.setSpacing(18)
        self.art = QLabel()
        self.art.setFixedSize(96, 96)
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setStyleSheet("background:#1d1b28; border-radius:14px; color:#6f6f88; font-size:34px;")
        self.art.setText("💬")
        hl.addWidget(self.art)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.kicker = QLabel("SONG MEANINGS")
        self.kicker.setObjectName("kicker")
        self.title_lbl = QLabel("What do the lyrics mean?")
        self.title_lbl.setObjectName("heroTitle")
        self.artist_lbl = QLabel("Press Now playing, or search any song.")
        self.artist_lbl.setObjectName("heroArtist")
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_a = QLabel("")
        self.chip_a.setObjectName("chipAccent")
        self.chip_b = QLabel("")
        self.chip_b.setObjectName("chip")
        self.chip_a.hide()
        self.chip_b.hide()
        chips.addWidget(self.chip_a)
        chips.addWidget(self.chip_b)
        chips.addStretch(1)
        info.addWidget(self.kicker)
        info.addWidget(self.title_lbl)
        info.addWidget(self.artist_lbl)
        info.addLayout(chips)
        hl.addLayout(info, 1)
        self.follow = QCheckBox("Follow the song")
        self.follow.setChecked(self.s.meanings_follow)
        self.follow.toggled.connect(self._follow_changed)
        self.follow.hide()
        hl.addWidget(self.follow, 0, Qt.AlignmentFlag.AlignBottom)
        root.addWidget(self.hero)

        # ---- body: lyrics card + meaning card
        body = QHBoxLayout()
        body.setSpacing(14)
        left_card = QFrame()
        left_card.setObjectName("card")
        ll = QVBoxLayout(left_card)
        ll.setContentsMargins(10, 12, 10, 10)
        self.left_kicker = QLabel("LYRICS")
        self.left_kicker.setObjectName("kicker")
        self.left_kicker.setContentsMargins(14, 0, 0, 4)
        ll.addWidget(self.left_kicker)
        self.left = QStackedWidget()
        self.lyrics = QListWidget()
        self.lyrics.setObjectName("lyrics")
        self.lyrics.setWordWrap(True)
        self.lyrics.setItemDelegate(_LineDelegate(self))
        self.lyrics.currentRowChanged.connect(self._line_clicked)
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.setIconSize(QSize(48, 48))
        self.results.itemActivated.connect(self._result_chosen)
        self.results.itemClicked.connect(self._result_chosen)
        self.left.addWidget(self.lyrics)
        self.left.addWidget(self.results)
        ll.addWidget(self.left, 1)

        right_card = QFrame()
        right_card.setObjectName("card")
        rl = QVBoxLayout(right_card)
        rl.setContentsMargins(22, 14, 22, 14)
        k2 = QLabel("MEANING")
        k2.setObjectName("kicker")
        rl.addWidget(k2)
        self.meaning = _PictureBrowser(self._pictures)
        self.meaning.setObjectName("meaning")
        self.meaning.setOpenExternalLinks(True)
        rl.addWidget(self.meaning, 1)
        body.addWidget(left_card, 11)
        body.addWidget(right_card, 10)
        root.addLayout(body, 1)

        # ---- footer
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
        self._last_body = body
        px = self._text_px()
        self.meaning.document().setDefaultStyleSheet(
            f"a {{ color: {self.accent}; text-decoration: none; }} "
            "blockquote { color: #c9c9da; margin-left: 12px; font-style: italic; }")
        shown = body
        for url in self._pictures_failed:          # pictures that wouldn't load: offer a link instead
            shown = shown.replace(f'<img src="{url}">',
                                  f"<a href='{html.escape(url)}'>🖼 Open picture</a>")
        self.meaning.setHtml(f"<div style='line-height:155%; font-size:{px}px'>{shown}</div>")
        for url in image_urls(shown):
            if url not in self._pictures and url not in self._pictures_pending:
                self._fetch_picture(url)

    def _fetch_picture(self, url: str):
        self._pictures_pending.add(url)

        def work():
            try:
                r = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*"})
                with urllib.request.urlopen(r, timeout=15) as resp:
                    return resp.read(8_000_000)
            except Exception:
                return None
        self.pool.submit(work).add_done_callback(lambda f: self._pic.emit(url, f.result()))

    def _on_picture(self, url: str, data):
        self._pictures_pending.discard(url)
        img = QImage.fromData(data) if data else None
        if img is None or img.isNull():
            log.info("song meanings: picture didn't load: %s", url)
            self._pictures_failed.add(url)
            if url in self._last_body:
                self._html(self._last_body)
            return
        try:
            view = int(self.meaning.viewport().width())
        except Exception:
            view = 480
        widest = max(160, min(420, view - 60))
        if img.width() > widest:
            img = img.scaledToWidth(widest, Qt.TransformationMode.SmoothTransformation)
        if len(self._pictures) >= 60:                    # keep memory small: forget the oldest pictures
            for old in list(self._pictures)[:20]:
                self._pictures.pop(old, None)
        self._pictures[url] = img
        if url in self._last_body:          # still on screen: show it now
            bar = self.meaning.verticalScrollBar().value()
            self._html(self._last_body)
            self.meaning.verticalScrollBar().setValue(bar)

    def _set_accent(self, colour: str):
        if colour != self.accent:
            self.accent = colour
            self.setStyleSheet(_style(colour, self._text_px()))
            self.lyrics.viewport().update()

    def _text_px(self) -> int:
        return TEXT_SIZES.get(self.s.meanings_text_size, 14)

    def apply_text_size(self):
        """The text-size setting changed: restyle and redraw what's on the right."""
        self.setStyleSheet(_style(self.accent, self._text_px()))
        if self._last_body:
            self._html(self._last_body)

    def _set_art(self, img_bytes: bytes | None):
        pm = _rounded(img_bytes, 96)
        if pm is None:
            self.art.setPixmap(QPixmap())
            self.art.setText("💬")
            self._set_accent(DEFAULT_ACCENT)
            return
        self.art.setText("")
        self.art.setPixmap(pm)
        self._set_accent(_accent_from(img_bytes))

    def _load_art(self, url: str):
        """Download a small cover picture (Genius artwork) in the background."""
        self._art_req += 1
        req = self._art_req
        if not url:
            return

        def work():
            try:
                r = urllib.request.Request(url, headers={"User-Agent": "SpotifyLyricsOverlay"})
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.read(3_000_000)
            except Exception:
                return None
        self.pool.submit(work).add_done_callback(lambda f: self._art.emit(req, f.result()))

    def _on_art(self, req, data):
        if req == self._art_req and data:
            self._set_art(data)

    def _chips(self, a: str = "", b: str = ""):
        self.chip_a.setText(a)
        self.chip_a.setVisible(bool(a))
        self.chip_b.setText(b)
        self.chip_b.setVisible(bool(b))

    def _quote(self, text: str) -> str:
        return (f"<table width='100%' cellspacing='0' cellpadding='0' style='margin:4px 0 14px 0'><tr>"
                f"<td width='4' bgcolor='{self.accent}'></td><td width='14'></td>"
                f"<td><span style='font-size:{self._text_px() + 5}px; font-weight:600; color:#ffffff'>"
                f"{html.escape(text)}</span></td>"
                "</tr></table>")

    def _card(self, inner: str, bg: str = "#1b1926") -> str:
        return (f"<table width='100%' cellspacing='0' cellpadding='16' bgcolor='{bg}' "
                f"style='margin-top:10px; font-size:{self._text_px()}px'><tr><td>{inner}</td></tr></table>")

    def _empty(self, icon: str, title: str, text: str) -> str:
        return (f"<p align='center' style='margin-top:60px; font-size:40px'>{icon}</p>"
                f"<p align='center' style='font-size:17px; font-weight:600; color:#ffffff'>{title}</p>"
                f"<p align='center' style='color:#9a9ab0'>{text}</p>")

    def _intro(self):
        need = "" if self.s.genius_token else (
            "<p align='center' style='color:#fbbf24'>Add your free Genius access token in "
            "Settings → Song meanings to see explanations.</p>")
        self._html(self._empty("🎧", "Find out what the lyrics mean",
                               "Press <b>Now playing</b> for the song you're listening to, or search any song. "
                               "Highlighted lines have an explanation – click one to read it.") + need)

    # ------------------------------------------------------------------ loading songs
    def show_now_playing(self):
        now = self.ctl.meanings_now_playing()
        if not now:
            self.status.setText("Nothing is playing in Spotify right now.")
            return
        self._load(now["title"], now["artist"], lines=now["lines"], starts=now["starts"], now=True)
        self._set_art(getattr(self.ctl.overlay, "_art_bytes", None))

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
            self._set_art(None)
            if hit.thumb:
                self._load_art(hit.thumb)

    def _load(self, title, artist, lines=None, starts=None, now=False, hit=None):
        self.left.setCurrentWidget(self.lyrics)
        self.left_kicker.setText("LYRICS")
        self.kicker.setText("NOW PLAYING" if now else "SONG")
        self.title_lbl.setText(title)
        self.artist_lbl.setText(artist)
        self._chips("Loading…", "")
        self.lyrics.clear()
        self.rows, self.line_map, self.meanings = [], [], None
        self.song = {"title": title, "artist": artist, "lines": lines, "starts": starts, "now": now}
        self.follow.setVisible(bool(now and starts))
        self._html(self._empty("⏳", "Loading…", "Getting the lyrics and explanations."))
        self.status.setText("")
        token = bool(self.s.genius_token)

        def work():
            ls, st = lines, starts
            if not ls:
                from .lrc import parse_lrc
                from .my_lyrics import lines_for_tapping
                res = self.ctl.client.fetch(title, artist)
                if res.synced:
                    ls, st = [p.text for p in parse_lrc(res.synced)], None
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
            item = QListWidgetItem(text if text.strip() else "")
            if not text.strip():
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setSizeHint(QSize(10, 14))
            elif self.line_map[i]:
                item.setData(EXPLAINED, True)
                explained += 1
            self.lyrics.addItem(item)
            self.rows.append(i)
        self.lyrics.setCurrentRow(-1)
        self.lyrics.blockSignals(False)
        self._now_row = -1
        total = sum(1 for ln in lines if ln.strip())
        if self.meanings:
            self._chips(f"✦ {explained} of {total} lines explained", "Genius")
            if (not self.song.get("now") or self.art.pixmap() is None or self.art.pixmap().isNull()) \
                    and self.meanings.hit.thumb:
                self._load_art(self.meanings.hit.thumb)
        else:
            self._chips(f"{total} lines" if total else "", "")
        if not lines:
            self._html(self._empty("🔇", "No lyrics found", "LRCLIB doesn't have lyrics for this song."))
        else:
            self._show_about(data.get("genius_error"))
        self.status.setText(data.get("genius_error") or "")

    def _show_about(self, genius_error=None):
        m = self.meanings
        bits = []
        if m and m.about:
            about = m.about_html or html.escape(m.about).replace(chr(10), "<br>")
            bits.append(f"<p style='font-size:{self._text_px() + 2}px; font-weight:600; color:#ffffff'>"
                        "About this song</p>"
                        f"<div style='color:#d6d6e2'>{about}</div>")
        elif m:
            bits.append("<p style='color:#9a9ab0'>Genius has no description for this song.</p>")
        if genius_error:
            bits.append(f"<p style='color:#fbbf24'>{html.escape(genius_error)}</p>")
        if m and m.hit.url:
            bits.append(f"<p><a style='color:{self.accent}; text-decoration:none' href='{html.escape(m.hit.url)}'>"
                        "Open this song on Genius ↗</a></p>")
        bits.append("<p style='color:#8a8aa3; margin-top:18px'>👈  Click a line to see what it means.</p>")
        self._html("".join(bits))

    # ------------------------------------------------------------------ clicking a line
    def _line_clicked(self, row: int):
        if row < 0 or row >= len(self.rows) or not self.song:
            return
        i = self.rows[row]
        lines = self.song["lines"] or []
        if i < 0 or i >= len(lines) or not lines[i].strip():
            return
        self._shown_row = row
        quote = self._quote(lines[i])
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
                if ex.url:
                    tags.append(f"<a style='color:{self.accent}; text-decoration:none' "
                                f"href='{html.escape(ex.url)}'>Genius ↗</a>")
                frag = ""
                if _short(ex.fragment) != _short(lines[i]):
                    frag = (f"<p style='color:#9a9ab0'><i>{html.escape(ex.fragment).replace(chr(10), '<br>')}"
                            "</i></p>")
                text = (f"<div style='color:#ececf3'>{ex.html}</div>" if ex.html else
                        f"<p style='color:#ececf3'>{html.escape(ex.text).replace(chr(10), '<br>')}</p>")
                text += "".join(self._x_card(u) for u in x_posts(ex.html or ex.text))
                parts.append(self._card(f"{frag}{text}"
                                        f"<p style='font-size:12px'>{' &nbsp;·&nbsp; '.join(tags)}</p>"))
            parts.append("<p style='color:#6f6f88; font-size:11px; margin-top:12px'>"
                         "Explanation from Genius contributors.</p>")
            self._html("".join(parts))
            return
        if self.s.meanings_ai and not self.s.anthropic_key.strip():
            self._html(quote + self._card("<p>Genius hasn't explained this line yet.</p><p style='color:#fbbf24'>"
                                          "Add your Anthropic API key in Settings → Song meanings to have the AI "
                                          "explain it.</p>"))
            return
        if self.s.meanings_ai:
            ai = self._ai()
            got = ai.cached(self.song["title"], self.song["artist"], lines[i], self._language())
            if got:
                self._show_ai(quote, got)
                return
            self._html(quote + self._card("<p style='color:#9a9ab0'>✦ Genius hasn't explained this line – "
                                          "asking the AI…</p>"))
            title, artist, lang = self.song["title"], self.song["artist"], self._language()
            self._ai_quote = quote                  # set before asking, the answer may come back fast
            self._run("ai", lambda: ai.explain(title, artist, lines, i, lang))
            return
        self._html(quote + self._card("<p>Genius hasn't explained this line yet.</p>"
                                      "<p style='color:#9a9ab0'>Turn on the AI backup in Settings → Song meanings to "
                                      "have lines like this explained.</p>"))

    def _x_card(self, url: str) -> str:
        """A post on X shown as a little card (X doesn't let apps show the post's picture for free)."""
        open_link = (f"<a href='{html.escape(url.replace('twitter.com', 'x.com'))}'>Open on X ↗</a>")
        if url not in self._x_cache:
            if url not in self._x_pending:
                self._fetch_x(url)
            inner = f"<p style='color:#9a9ab0'>𝕏 Loading post…</p><p style='font-size:12px'>{open_link}</p>"
        elif self._x_cache[url] is None:
            inner = f"<p style='color:#cfcfe0'>𝕏 A post on X</p><p style='font-size:12px'>{open_link}</p>"
        else:
            p = self._x_cache[url]
            pics = "".join(f'<p><img src="{u}"></p>' for u in p.get("pictures") or [])
            note = (" &nbsp;·&nbsp; <span style='color:#9a9ab0'>🖼 has a picture – open it on X</span>"
                    if p.get("picture") and not pics else "")
            text = (f"<p style='color:#e6e6f0'>{html.escape(p['text']).replace(chr(10), '<br>')}</p>"
                    if p.get("text") else "")
            inner = (f"<p style='color:#ffffff'><b>𝕏 {html.escape(p['author'])}</b> "
                     f"<span style='color:#9a9ab0'>@{html.escape(p['handle'])}</span></p>"
                     f"{text}{pics}<p style='font-size:12px'>{open_link}{note}</p>")
        return (f"<table width='100%' cellspacing='0' cellpadding='12' bgcolor='#101017' "
                f"style='margin-top:10px'><tr><td>{inner}</td></tr></table>")

    def _fetch_x(self, url: str):
        self._x_pending.add(url)

        def work():
            try:
                return fetch_x_post(url)
            except Exception:
                return None
        self.pool.submit(work).add_done_callback(lambda f: self._xpost.emit(url, f.result()))

    def _on_x_post(self, url: str, post):
        self._x_pending.discard(url)
        self._x_cache[url] = post
        if self._shown_row >= 0 and url.split("/status/")[-1] in self._last_body:
            bar = self.meaning.verticalScrollBar().value()
            self._line_clicked(self._shown_row)
            self.meaning.verticalScrollBar().setValue(bar)

    def _show_ai(self, quote, text):
        self._html(quote + self._card(f"<p style='color:#ececf3'>{html.escape(text).replace(chr(10), '<br>')}</p>"
                                      "<p style='font-size:12px; color:#c4b5fd'>✦ AI explanation (Claude)</p>",
                                      bg="#1d1830")
                   + "<p style='color:#6f6f88; font-size:11px; margin-top:12px'>Genius hasn't explained this line. "
                     "The AI's answer is an interpretation and can be wrong.</p>")

    # ------------------------------------------------------------------ answers from the background
    def _on_done(self, req, what, result):
        if req != self._req:
            return
        if isinstance(result, MeaningsError):
            self.status.setText(str(result))
            if what == "ai":
                self._html(self._ai_quote + self._card(f"<p style='color:#fbbf24'>{html.escape(str(result))}</p>"))
            elif what in ("song", "search"):
                self._chips("", "")
                self._html(self._empty("⚠️", "Something went wrong", html.escape(str(result))))
            return
        if what == "search":
            self.results.clear()
            for hit in result:
                it = QListWidgetItem(f"{hit.title}\n{hit.artist}")
                it.setData(Qt.ItemDataRole.UserRole, hit)
                if hit.thumb:                       # Genius results have cover pictures, LRCLIB ones don't
                    it.setIcon(self._thumb_icon(hit.thumb))
                self.results.addItem(it)
            if not result:
                self.results.addItem(QListWidgetItem("No songs found."))
            self.left.setCurrentWidget(self.results)
            self.left_kicker.setText("RESULTS")
            self.kicker.setText("SEARCH")
            self.title_lbl.setText(f"“{self.search.text().strip()}”")
            self.artist_lbl.setText("Pick a song from the list.")
            self._chips(f"{len(result)} songs" if result else "", "Genius" if self.s.genius_token else "LRCLIB")
            self._set_art(None)
            self.follow.setVisible(False)
            self._html(self._empty("👈", "Pick a song", "Choose one of the results to see its lyrics and meanings."))
            self.status.setText("")
            for hit in result:
                if hit.thumb and hit.thumb not in self._thumbs:
                    self._fetch_thumb(hit.thumb)
        elif what == "song":
            self._show_song(result)
        elif what == "ai":
            self._show_ai(self._ai_quote, result)
            self.status.setText("")
        elif what == "thumb":
            url, data = result
            pm = _rounded(data, 48, 8)
            if pm is not None:
                self._thumbs[url] = QIcon(pm)
                for k in range(self.results.count()):
                    it = self.results.item(k)
                    hit = it.data(Qt.ItemDataRole.UserRole)
                    if isinstance(hit, SongHit) and hit.thumb == url:
                        it.setIcon(self._thumbs[url])

    def _thumb_icon(self, url: str) -> QIcon:
        if url in self._thumbs:
            return self._thumbs[url]
        pm = QPixmap(48, 48)
        pm.fill(QColor("#1d1b28"))
        return QIcon(pm)

    def _fetch_thumb(self, url: str):
        req = self._req

        def work():
            try:
                r = urllib.request.Request(url, headers={"User-Agent": "SpotifyLyricsOverlay"})
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return url, resp.read(1_500_000)
            except Exception:
                return url, None
        self.pool.submit(work).add_done_callback(lambda f: self._done.emit(req, "thumb", f.result()))

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
        idx = -1
        for k, t in enumerate(self.song["starts"]):
            if t <= pos:
                idx = k
            else:
                break
        row = idx if 0 <= idx < self.lyrics.count() else -1
        if row == self._now_row:
            return
        if 0 <= self._now_row < self.lyrics.count():
            old = self.lyrics.item(self._now_row)
            old.setData(SINGING, False)
            f = old.font()
            f.setBold(False)
            old.setFont(f)
        self._now_row = row
        if row >= 0:
            it = self.lyrics.item(row)
            it.setData(SINGING, True)
            f = QFont(it.font())
            f.setBold(True)
            it.setFont(f)
            self.lyrics.scrollToItem(it, QListWidget.ScrollHint.PositionAtCenter)


def _short(s: str) -> str:
    return re.sub(r"[^\w]+", "", (s or "").lower())
