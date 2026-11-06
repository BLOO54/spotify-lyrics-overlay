"""The "Sync this song myself" window.

Plain English: play the song and tap SPACE each time a new line starts. The
window shows which line is next. Backspace undoes the last tap. When you press
Save, a lyrics file with your timing is written to your "my lyrics" folder and
used for this song from then on.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .my_lyrics import TapSession
from .overlay import format_time
from .settings_dialog import STYLE


class TapSyncDialog(QDialog):
    saved = Signal(str)          # the finished .lrc text

    def __init__(self, title: str, artist: str, lines: list[str], position_fn, restart_fn=None,
                 duration: float = 0.0, parent=None):
        super().__init__(parent)
        self.title_, self.artist_ = title, artist
        self.session = TapSession(list(lines))
        self.position_fn = position_fn
        self.restart_fn = restart_fn
        self.duration = duration
        self.setWindowTitle(f"Sync this song – {title}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        head = QLabel(f"<b>{title}</b> — {artist}")
        head.setStyleSheet("font-size:14px;")
        root.addWidget(head)
        how = QLabel("1. Click <b>Restart song</b> (or play it from the beginning).<br>"
                     "2. Press <b>SPACE</b> the moment each line below starts being sung.<br>"
                     "3. <b>Backspace</b> undoes a tap. Press <b>Save</b> when you're done.")
        how.setWordWrap(True)
        how.setStyleSheet("color:#b8b8cc; font-size:12px;")
        root.addWidget(how)

        self.prev_lbl = QLabel()
        self.next_lbl = QLabel()
        self.after_lbl = QLabel()
        for lbl, css in ((self.prev_lbl, "color:#6c6c88; font-size:13px;"),
                         (self.next_lbl, "color:#ffffff; font-size:22px; font-weight:bold; padding:8px;"
                                         "border:1px solid #a855f7; border-radius:8px; background:#1c1530;"),
                         (self.after_lbl, "color:#9a9ab0; font-size:14px;")):
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(css)
            root.addWidget(lbl)
        self.status = QLabel()
        self.status.setStyleSheet("color:#c9b8ff; font-size:12px;")
        root.addWidget(self.status)

        row = QHBoxLayout()
        self.restart_btn = QPushButton("Restart song")
        self.tap_btn = QPushButton("TAP  (Space)")
        self.undo_btn = QPushButton("Undo  (Backspace)")
        self.save_btn = QPushButton("Save")
        cancel = QPushButton("Cancel")
        self.save_btn.setStyleSheet("background:#7c3aed; border-color:#a855f7;")
        for b in (self.restart_btn, self.tap_btn, self.undo_btn, self.save_btn, cancel):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # Space must tap, not press a button
            row.addWidget(b)
        root.addLayout(row)
        self.restart_btn.clicked.connect(self._restart)
        self.tap_btn.clicked.connect(self.tap)
        self.undo_btn.clicked.connect(self.undo)
        self.save_btn.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        self.restart_btn.setEnabled(restart_fn is not None)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(100)
        self._refresh()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------
    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key.Key_Space:
            self.tap()
        elif key == Qt.Key.Key_Backspace:
            self.undo()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.save()
        else:
            super().keyPressEvent(e)

    def tap(self):
        self.session.tap(float(self.position_fn()))
        self._refresh()

    def undo(self):
        self.session.undo()
        self._refresh()

    def _restart(self):
        if self.restart_fn:
            self.session.taps.clear()
            self.restart_fn()
        self._refresh()

    def save(self):
        if not self.session.taps:
            return
        self.saved.emit(self.session.to_lrc(self.title_, self.artist_, self.duration))
        self.accept()

    def _refresh(self):
        s = self.session
        i = s.next_index
        lines = s.lines
        self.prev_lbl.setText(f"✓ {lines[i - 1]}" if i > 0 else "")
        if s.done:
            self.next_lbl.setText("All lines timed! Press Save.")
            self.after_lbl.setText("")
        else:
            self.next_lbl.setText(lines[i])
            self.after_lbl.setText(lines[i + 1] if i + 1 < len(lines) else "(last line)")
        self.status.setText(f"Line {min(i + 1, len(lines))} of {len(lines)} · song at "
                            f"{format_time(self.position_fn())}"
                            + ("  ·  you can Save now – untimed lines are left out" if 0 < i < len(lines) else ""))
        self.undo_btn.setEnabled(i > 0)
        self.save_btn.setEnabled(i > 0)
        self.tap_btn.setEnabled(not s.done)
