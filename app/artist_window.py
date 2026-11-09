"""The "About the artist" window - photo, bio, facts, popular songs and links (like Spotify's "About")."""
from __future__ import annotations

import html
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout,
                               QWidget)

from .artist import ArtistInfo, about_artist
from .meanings_window import DEFAULT_ACCENT, _accent_from, _rounded, _style

log = logging.getLogger(__name__)
PHOTO = 150


def _views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views"
    if n >= 1000:
        return f"{n / 1000:.0f}K views"
    return f"{n} views" if n else ""


class ArtistWindow(QWidget):
    _done = Signal(int, object)        # request number, ArtistInfo
    _photo = Signal(int, object)       # request number, image bytes

    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.s = ctl.s
        self.accent = DEFAULT_ACCENT
        self.info: ArtistInfo | None = None
        self.setWindowTitle("About the artist")
        self.setObjectName("meaningsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_style(self.accent))
        self.resize(900, 720)
        self.setMinimumSize(640, 520)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="artist")
        self._req = 0
        self._done.connect(self._on_done)
        self._photo.connect(self._on_photo)
        self._side = PHOTO

        root = QVBoxLayout(self)
        self._root = root
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("🔍   Search any artist…")
        self.search.returnPressed.connect(lambda: self.load(self.search.text().strip()))
        go = QPushButton("Search")
        go.setObjectName("ghost")
        go.clicked.connect(lambda: self.load(self.search.text().strip()))
        now = QPushButton("♫  Playing now")
        now.setObjectName("pill")
        now.clicked.connect(self.show_now_playing)
        top.addWidget(self.search, 1)
        top.addWidget(go)
        top.addWidget(now)
        root.addLayout(top)

        hero = QFrame()
        hero.setObjectName("hero")
        hl = QHBoxLayout(hero)
        self._hero_layout = hl
        hl.setContentsMargins(20, 18, 20, 18)
        hl.setSpacing(20)
        self.photo = QLabel("🎤")
        self.photo.setFixedSize(PHOTO, PHOTO)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setStyleSheet("font-size: 54px; background: #1b1a24; border-radius: 18px;")
        hl.addWidget(self.photo)
        col = QVBoxLayout()
        col.setSpacing(6)
        kicker = QLabel("ABOUT THE ARTIST")
        kicker.setObjectName("kicker")
        self.name = QLabel("")
        self.name.setObjectName("heroTitle")
        self.name.setWordWrap(True)
        self.tagline = QLabel("")
        self.tagline.setObjectName("heroArtist")
        self.tagline.setWordWrap(True)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chips.addStretch(1)
        self._chip_widgets: list[QLabel] = []
        col.addStretch(1)
        col.addWidget(kicker)
        col.addWidget(self.name)
        col.addWidget(self.tagline)
        col.addLayout(self.chips)
        col.addStretch(1)
        hl.addLayout(col, 1)
        root.addWidget(hero)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 16, 22, 16)
        self.body = QTextBrowser()
        self.body.setObjectName("meaning")
        self.body.setOpenExternalLinks(True)
        cl.addWidget(self.body)
        root.addWidget(card, 1)
        self.credit = QLabel("")
        self.credit.setObjectName("credit")
        self.credit.setWordWrap(True)
        root.addWidget(self.credit)
        self._message("🎤", "About the artist", "Play a song, or search any artist above.")

    def set_compact(self, on: bool = True):
        """Inside the Settings window: a smaller photo and less padding, so the bio has room."""
        self._side = 92 if on else PHOTO
        self.photo.setFixedSize(self._side, self._side)
        self._root.setContentsMargins(*((0, 0, 0, 0) if on else (22, 18, 22, 14)))
        self._root.setSpacing(10 if on else 14)
        self._hero_layout.setContentsMargins(*((14, 12, 14, 12) if on else (20, 18, 20, 18)))

    # ------------------------------------------------------------ window
    def showEvent(self, e):
        super().showEvent(e)
        try:
            from . import win32
            win32.set_dark_titlebar(int(self.winId()))
        except Exception:
            pass

    def closeEvent(self, e):
        self._req += 1
        super().closeEvent(e)

    # ------------------------------------------------------------ loading
    def show_now_playing(self):
        st = self.ctl.track
        if not (st and st.artist):
            self._message("♫", "Nothing playing", "Play a song in Spotify, or search any artist above.")
            return
        main = st.artist.split(",")[0].split(" feat")[0].split(" & ")[0].strip() or st.artist
        self.load(main, st.title)

    def load(self, artist: str, song_title: str = ""):
        if not artist:
            return
        self._req += 1
        req = self._req
        self.search.setText(artist)
        self._set_header(artist, "Looking them up…", [])
        self._message("⏳", f"Finding {artist}…", "Asking Genius, Wikipedia and MusicBrainz.")
        from .config import app_data_dir
        token, cache = self.s.genius_token, app_data_dir() / "artist_cache"

        def work():
            try:
                return about_artist(artist, song_title, token, cache)
            except Exception as e:                 # never let a background problem crash the app
                log.exception("about the artist failed")
                return e
        self.pool.submit(work).add_done_callback(lambda f: self._done.emit(req, f.result()))

    def _on_done(self, req, info):
        if req != self._req:
            return
        if isinstance(info, Exception) or not info.found:
            name = self.search.text()
            why = ("Something went wrong – try again in a moment." if isinstance(info, Exception) else
                   "Couldn't reach Genius, Wikipedia or MusicBrainz (internet?)." if info.problems and not info.sources
                   else "Genius, Wikipedia and MusicBrainz don't know this artist yet.")
            self._set_header(name, "", [])
            self._message("🤷", "Nothing found", why + (
                "" if self.s.genius_token else "<br><br>Tip: add a Genius token in Settings → Song meanings "
                                               "for bios, photos and popular songs of smaller artists."))
            return
        self.info = info
        chips = []
        if info.followers:
            chips.append(f"{info.followers:,} followers on Genius")
        chips += info.genres[:3]
        self._set_header(info.name, info.tagline, chips)
        self.body.setHtml(self._page(info))
        bits = [s for s in ("Genius", "Wikipedia", "MusicBrainz") if s in info.sources]
        credit = "Info from " + ", ".join(bits) if bits else ""
        if "Wikipedia" in info.sources and info.bio_source == "Wikipedia":
            credit += " (Wikipedia text: CC BY-SA)"
        if info.problems:
            credit += f" · couldn't reach {', '.join(info.problems)}"
        self.credit.setText(credit)
        self._load_photo(req, info.photo_url)

    def _load_photo(self, req, url):
        if not url:
            return

        def work():
            try:
                r = urllib.request.Request(url, headers={"User-Agent": "SpotifyLyricsOverlay (about the artist)"})
                with urllib.request.urlopen(r, timeout=12) as resp:
                    return resp.read(6_000_000)
            except Exception:
                return None
        self.pool.submit(work).add_done_callback(lambda f: self._photo.emit(req, f.result()))

    def _on_photo(self, req, data):
        if req != self._req or not data:
            return
        pm = _rounded(data, self._side, 18)
        if pm is not None:
            self.photo.setText("")
            self.photo.setStyleSheet("background: transparent;")
            self.photo.setPixmap(pm)
            self.accent = _accent_from(data)
            self.setStyleSheet(_style(self.accent))
            if self.info is not None:
                self.body.setHtml(self._page(self.info))

    # ------------------------------------------------------------ drawing
    def _set_header(self, name: str, tagline: str, chips: list):
        self.name.setText(html.escape(name))
        self.tagline.setText(html.escape(tagline[:1].upper() + tagline[1:]) if tagline else "")
        self.tagline.setVisible(bool(tagline))
        self.photo.setPixmap(QPixmap())
        self.photo.setText("🎤")
        self.photo.setStyleSheet("font-size: 54px; background: #1b1a24; border-radius: 18px;")
        for w in self._chip_widgets:
            self.chips.removeWidget(w)
            w.deleteLater()
        self._chip_widgets = []
        for i, c in enumerate(chips):
            lab = QLabel(c)
            lab.setObjectName("chipAccent" if i == 0 else "chip")
            self.chips.insertWidget(i, lab)               # before the stretch, so chips sit on the left
            self._chip_widgets.append(lab)

    def _message(self, icon: str, title: str, text: str):
        self.body.setHtml(f"<div style='margin-top:40px' align='center'><div style='font-size:40px'>{icon}</div>"
                          f"<p style='font-size:18px; color:#ffffff'><b>{html.escape(title)}</b></p>"
                          f"<p style='color:#9a9ab0'>{text}</p></div>")
        self.credit.setText("")

    def _section(self, title: str) -> str:
        return (f"<p style='margin-top:18px; margin-bottom:6px; color:{self.accent}; font-size:11px; "
                f"font-weight:700; letter-spacing:2px'>{html.escape(title.upper())}</p>")

    def _page(self, info: ArtistInfo) -> str:
        out = [f"<style>a {{ color: {self.accent}; text-decoration: none; }}</style>"
               "<div style='line-height:155%; font-size:14px; color:#e6e6f0'>"]
        if info.bio_html:
            out.append(self._section("About"))
            import re
            out.append(re.sub(r"<img\b[^>]*>", "", info.bio_html))      # pictures inside the bio: left out
            if info.bio_url:
                out.append(f"<p><a href='{html.escape(info.bio_url)}'>Read more on {info.bio_source} ↗</a></p>")
        if info.popular:
            out.append(self._section("Popular on Genius"))
            rows = []
            for i, (title, url, views) in enumerate(info.popular[:8], 1):
                rows.append(f"<tr><td width='28' style='color:#7c7c94'>{i}</td>"
                            f"<td><a href='{html.escape(url)}' style='color:#ececf3'>{html.escape(title)}</a></td>"
                            f"<td align='right' style='color:#7c7c94'>{_views(views)}</td></tr>")
            out.append("<table width='100%' cellspacing='0' cellpadding='5'>" + "".join(rows) + "</table>")
        facts = list(info.facts)
        if info.genres:
            facts.append(("Genres", ", ".join(info.genres)))
        if facts:
            out.append(self._section("Facts"))
            out.append("<table cellspacing='0' cellpadding='4'>" + "".join(
                f"<tr><td width='110' style='color:#8a8aa3'>{html.escape(k)}</td>"
                f"<td style='color:#ececf3'>{html.escape(str(v))}</td></tr>" for k, v in facts) + "</table>")
        if info.links:
            out.append(self._section("Links"))
            out.append("<p>" + " &nbsp;·&nbsp; ".join(
                f"<a href='{html.escape(u)}'>{html.escape(l)} ↗</a>" for l, u in info.links) + "</p>")
        out.append("</div>")
        return "".join(out)
