"""The AI sync window: what the AI is doing right now, what it still needs, and - for every song
it has synced - which lyric lines it heard and which it had to fill in."""

from __future__ import annotations

import html

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from .settings_dialog import STYLE

EXTRA = """
QWidget#aiRoot { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #11101a, stop:1 #0b0b10); color: #ececf3; }
QWidget#aiRoot QLabel, QWidget#aiRoot QCheckBox { background: transparent; }
QFrame#card { background: #13121a; border: 1px solid #23212e; border-radius: 16px; }
QLabel#kicker { color: #b9a6ff; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
QLabel#big { font-size: 22px; font-weight: 700; color: #ffffff; }
QLabel#state { font-size: 17px; font-weight: 600; color: #ffffff; }
QLabel#muted { color: #9a9ab0; }
QLabel#chip { background: #1f1d2a; color: #cfcfe0; border: 1px solid #2c2a3a; border-radius: 10px; padding: 3px 10px;
    font-size: 11px; }
QListWidget#songs { background: transparent; border: none; color: #d6d6e2; outline: none; font-size: 13px; }
QListWidget#songs::item { padding: 8px 10px; border-radius: 8px; }
QListWidget#songs::item:selected { background: #2e2350; color: #ffffff; }
QListWidget#songs::item:hover { background: #1c1b26; }
QTextBrowser#lines { background: transparent; border: none; color: #e6e6f0; font-size: 13px; }
"""


class AiSyncWindow(QWidget):
    _found = Signal(int, object)          # search number, list of (title, artist, has_lyrics)

    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.setWindowTitle("AI sync")
        self.setObjectName("aiRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE + EXTRA)
        self.resize(1000, 720)
        self.setMinimumSize(760, 560)
        self._songs_key = None
        self._selected = None
        self._search_no = 0
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-search")
        self._found.connect(self._on_found)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        # ---- header
        head = QHBoxLayout()
        titles = QVBoxLayout()
        k = QLabel("AI SYNC")
        k.setObjectName("kicker")
        self.title = QLabel("What the AI is doing")
        self.title.setObjectName("big")
        titles.addWidget(k)
        titles.addWidget(self.title)
        head.addLayout(titles, 1)
        self.model_chip = QLabel("")
        self.model_chip.setObjectName("chip")
        head.addWidget(self.model_chip, 0, Qt.AlignmentFlag.AlignBottom)
        self.toggle = QCheckBox("AI sync on")
        self.toggle.toggled.connect(self._toggled)
        head.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(head)

        # ---- right now + needs
        top = QHBoxLayout()
        top.setSpacing(14)
        now_card, nl = self._card("RIGHT NOW")
        self.now_song = QLabel("")
        self.now_song.setObjectName("muted")
        self.now_state = QLabel("")
        self.now_state.setObjectName("state")
        self.now_detail = QLabel("")
        self.now_detail.setObjectName("muted")
        self.now_detail.setWordWrap(True)
        self.now_bar = QProgressBar()
        self.now_bar.setRange(0, 100)
        self.now_bar.setVisible(False)
        self.working = QLabel("")
        self.working.setObjectName("muted")
        self.working.setWordWrap(True)
        for w in (self.now_song, self.now_state, self.now_detail, self.now_bar, self.working):
            nl.addWidget(w)
        nl.addStretch(1)
        row = QHBoxLayout()
        redo = QPushButton("Listen to this song again")
        redo.clicked.connect(lambda: (self.ctl.ai_redo_song(), self.refresh()))
        settings = QPushButton("AI settings…")
        settings.clicked.connect(self._open_settings)
        row.addWidget(redo)
        row.addWidget(settings)
        row.addStretch(1)
        nl.addLayout(row)
        need_card, self.need_layout = self._card("WHAT IT NEEDS")
        self.need_labels: list[QLabel] = []
        self.need_layout.addStretch(1)
        top.addWidget(now_card, 3)
        top.addWidget(need_card, 2)
        root.addLayout(top)

        # ---- sync one song
        one_card, ol = self._card("SYNC ONE SONG")
        orow = QHBoxLayout()
        self.one_search = QLineEdit()
        self.one_search.setPlaceholderText("Search a song to sync…")
        self.one_search.returnPressed.connect(self._search)
        find = QPushButton("Search")
        find.clicked.connect(self._search)
        self.one_pick = QComboBox()
        self.one_pick.setMinimumWidth(280)
        self.one_pick.setEnabled(False)
        self.one_go = QPushButton("Sync this song")
        self.one_go.setObjectName("primary")
        self.one_go.setEnabled(False)
        self.one_go.clicked.connect(self._sync_one)
        self.one_cancel = QPushButton("Cancel")
        self.one_cancel.clicked.connect(lambda: (self.ctl.cancel_one_song(), self.refresh()))
        self.one_cancel.setVisible(False)
        orow.addWidget(self.one_search, 2)
        orow.addWidget(find)
        orow.addWidget(self.one_pick, 3)
        orow.addWidget(self.one_go)
        orow.addWidget(self.one_cancel)
        ol.addLayout(orow)
        self.one_status = QLabel("Pick a song: Spotify shows it, you press play, the AI syncs just that song and "
                                 "pauses Spotify when it ends (nothing else plays).")
        self.one_status.setObjectName("muted")
        self.one_status.setWordWrap(True)
        ol.addWidget(self.one_status)
        root.addWidget(one_card)

        # ---- songs + lines it caught / missed
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        songs_card, sl = self._card("SONGS IT SYNCED")
        self.songs = QListWidget()
        self.songs.setObjectName("songs")
        self.songs.currentRowChanged.connect(self._song_picked)
        sl.addWidget(self.songs, 1)
        lines_card, ll = self._card("LINES IT HEARD  ✔   ·   MISSED  ✖")
        self.lines = QTextBrowser()
        self.lines.setObjectName("lines")
        ll.addWidget(self.lines, 1)
        self.rematch_btn = QPushButton("Re-match (no listening needed)")
        self.rematch_btn.setToolTip("Match the words the AI heard to the lyrics again, using the app's newest "
                                    "matching. Useful after an update.")
        self.rematch_btn.setVisible(False)
        self.rematch_btn.clicked.connect(self._rematch)
        ll.addWidget(self.rematch_btn, 0, Qt.AlignmentFlag.AlignLeft)
        bottom.addWidget(songs_card, 2)
        bottom.addWidget(lines_card, 3)
        root.addLayout(bottom, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.lines.setHtml("<p style='color:#9a9ab0'>Pick a song on the left to see which lyric lines the AI "
                           "heard, and which it missed and timed from the lines around them.</p>")

    # ------------------------------------------------------------------ sync one song
    def _search(self):
        q = self.one_search.text().strip()
        if not q:
            return
        self._search_no += 1
        no = self._search_no
        self.one_status.setText("Searching…")

        def work():
            try:
                items = self.ctl.client._get_json("/search", {"q": q}) or []
            except Exception:
                return None
            seen, out = set(), []
            for it in items:
                key = ((it.get("trackName") or "").strip(), (it.get("artistName") or "").strip())
                low = (key[0].lower(), key[1].lower())
                if key[0] and low not in seen:
                    seen.add(low)
                    out.append((key[0], key[1], bool(it.get("syncedLyrics") or it.get("plainLyrics"))))
            return out[:25]
        self.pool.submit(work).add_done_callback(lambda f: self._found.emit(no, f.result()))

    def _on_found(self, no, results):
        if no != self._search_no:
            return
        self.one_pick.clear()
        if results is None:
            self.one_status.setText("Couldn't search right now (internet?).")
            return
        for title, artist, has in results:
            self.one_pick.addItem(f"{title} – {artist}" + ("" if has else "   (no lyrics found)"), (title, artist))
        self.one_pick.setEnabled(bool(results))
        self.one_go.setEnabled(bool(results))
        self.one_status.setText(f"{len(results)} songs found – pick one and press Sync this song." if results
                                else "No songs found – try the song name and artist.")

    def _sync_one(self):
        data = self.one_pick.currentData()
        if data:
            self.ctl.start_one_song(*data)
            self.refresh()

    def _card(self, title: str):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(6)
        k = QLabel(title)
        k.setObjectName("kicker")
        lay.addWidget(k)
        return card, lay

    def showEvent(self, e):
        super().showEvent(e)
        try:
            from . import win32
            win32.set_dark_titlebar(int(self.winId()))
        except Exception:
            pass

    def _toggled(self, on: bool):
        if on != self.ctl.s.ai_sync_enabled:
            self.ctl.set_ai_enabled(on)
            self.refresh()

    def _open_settings(self):
        self.ctl.open_settings()
        dlg = self.ctl.settings_dlg
        if dlg is not None:
            dlg.nav.setCurrentRow(2)          # Lyrics & translation (where the AI sync settings are)

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        if not self.isVisible():
            return
        snap = self.ctl.ai_snapshot()
        self.toggle.blockSignals(True)
        self.toggle.setChecked(snap["enabled"])
        self.toggle.blockSignals(False)
        self.model_chip.setText(f"{snap['model']} · {snap['threads']} core{'s' if snap['threads'] != 1 else ''}"
                                if snap["installed"] else "No model downloaded")
        now = snap["now"]
        self.now_song.setText(f"{now['title']} – {now['artist']}" if now["title"] else "")
        prog = now.get("progress")
        self.now_state.setText(now["state"] + (f"  {int(round(prog * 100))}%" if prog is not None else ""))
        self.now_detail.setText(now["detail"])
        self.now_bar.setVisible(prog is not None)
        if prog is not None:
            self.now_bar.setValue(int(round(prog * 100)))
        w = snap["working"]
        if w:
            more = f" ({w['queued']} more waiting)" if w["queued"] else ""
            pct = f" {int(round((w['progress'] or 0) * 100))}%" if w["progress"] is not None else ""
            self.working.setText(f"Also working on the previous song{pct}{more}.")
        else:
            self.working.setText("")
        # checklist
        for lab in self.need_labels:
            lab.deleteLater()
        self.need_labels = []
        for ok, text in snap["need"]:
            mark, col = ("✔", "#4ade80") if ok else ("✖", "#f87171") if ok is False else ("•", "#a78bfa")
            lab = QLabel(f"<span style='color:{col}'>{mark}</span>&nbsp; {html.escape(text)}")
            lab.setWordWrap(True)
            self.need_layout.insertWidget(self.need_layout.count() - 1, lab)
            self.need_labels.append(lab)
        one = snap.get("one_song")
        if one:
            states = {"waiting": "Waiting for it to play", "syncing": "Syncing", "processing": "Working out the timing"}
            self.one_status.setText(f"<b>{html.escape(one['title'])}</b> – {states.get(one['state'], '')}. "
                                    f"{html.escape(one.get('note') or '')}")
            self.one_cancel.setVisible(True)
            self.one_go.setEnabled(False)
        else:
            self.one_cancel.setVisible(False)
            self.one_go.setEnabled(self.one_pick.count() > 0)
        self._fill_songs(snap)

    def _fill_songs(self, snap):
        entries = []
        for r in snap["recent"]:
            if not r.get("ok"):
                entries.append({"title": r["title"], "artist": r["artist"], "failed": True, "note": r["message"],
                                "details": {"lines": r.get("lines") or [], "heard": r.get("heard") or []}
                                if r.get("lines") else None, "modified": ""})
        for e in sorted(snap["songs"], key=lambda e: e.get("modified", ""), reverse=True):
            entries.append({**e, "failed": False})
        key = tuple((e["title"], e.get("modified", ""), e.get("failed")) for e in entries)
        if key == self._songs_key:
            return
        self._songs_key = key
        self._entries = entries
        self.songs.blockSignals(True)
        self.songs.clear()
        for e in entries:
            d = e.get("details")
            if e["failed"]:
                tag = "✖ didn't sync"
            elif d and d.get("total"):
                tag = f"heard {d.get('matched', 0)}/{d.get('total', 0)}"
            else:
                tag = e.get("note") or "synced"
            self.songs.addItem(QListWidgetItem(f"{e['title']}\n{e.get('artist') or ''}  ·  {tag}"))
        if not entries:
            self.songs.addItem(QListWidgetItem("No songs yet – play a song to the end with AI sync on."))
        self.songs.blockSignals(False)

    def _rematch(self):
        e = getattr(self, "_picked", None)
        if e and e.get("path"):
            self.ctl.ai_rematch(e["path"], e["title"], e.get("artist") or "")
            self._songs_key = None
            self.refresh()
            for k, x in enumerate(getattr(self, "_entries", [])):
                if x.get("path") == e["path"]:
                    self.songs.setCurrentRow(k)
                    self._song_picked(k)

    def _song_picked(self, row: int):
        entries = getattr(self, "_entries", [])
        if not (0 <= row < len(entries)):
            return
        e = entries[row]
        d = e.get("details")
        self._picked = e
        self.rematch_btn.setVisible(bool(not e.get("failed") and d and d.get("words") and e.get("path")))
        head = (f"<p style='font-size:16px; font-weight:600; color:#ffffff'>{html.escape(e['title'])}</p>"
                f"<p style='color:#9a9ab0'>{html.escape(e.get('note') or '')}</p>")
        if not d or not d.get("lines"):
            self.lines.setHtml(head + "<p style='color:#9a9ab0'>The heard / missed list is saved for songs synced "
                                      "from version 1.18 on. Use “Listen to this song again” while it plays to get "
                                      "it for this song.</p>")
            return
        rows = []
        missed = 0
        for text, heard in zip(d["lines"], d["heard"] + [False] * (len(d["lines"]) - len(d["heard"]))):
            if not text.strip():
                rows.append("<tr><td colspan='2' height='8'></td></tr>")
                continue
            if heard:
                rows.append(f"<tr><td width='26' style='color:#4ade80'>✔</td>"
                            f"<td style='color:#d6d6e2'>{html.escape(text)}</td></tr>")
            else:
                missed += 1
                rows.append(f"<tr><td width='26' style='color:#fbbf24'>✖</td>"
                            f"<td style='color:#fbbf24'>{html.escape(text)}</td></tr>")
        summary = (f"<p style='color:#c4b5fd'>Missed {missed} line{'s' if missed != 1 else ''} – those were timed "
                   "from the lines around them (or the website's timing).</p>" if missed else
                   "<p style='color:#4ade80'>The AI heard every line.</p>")
        self.lines.setHtml(head + summary + "<table cellspacing='0' cellpadding='3'>" + "".join(rows) + "</table>")
