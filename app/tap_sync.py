"""The "Sync this song myself" window.

Plain English:
* Every lyric line is listed with the time it starts. If the song already had
  timing, it's filled in - you only fix the lines that are wrong.
* Press SPACE when the highlighted line starts; the cursor moves to the next line.
* Click any line to re-time it (if Spotify allows it, the song jumps back to just
  before that line so you can tap it right away).
* Select a line and press ← / → to move it 0.1 s (hold Shift for 0.5 s).
* Your changes show live in the overlay. Save writes them to your lyrics folder.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget)

from .my_lyrics import TapSession
from .overlay import format_time
from .settings_dialog import STYLE


def _t(t) -> str:
    if t is None:
        return "  –:––.–"
    m = int(t // 60)
    return f"{m:3d}:{t - m * 60:04.1f}"


class Timeline(QWidget):
    """The whole song as a bar: where you are, and a tick for every timed line."""

    def __init__(self, dialog):
        super().__init__()
        self.d = dialog
        self.setMinimumHeight(34)

    def paintEvent(self, _):
        d = self.d
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(4, 8, -4, -8)
        times = [t for t in d.session.times if t is not None]
        total = max([d.duration or 0.0, (max(times) + 5) if times else 0.0, float(d.position_fn()) + 5, 30.0])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#22222d"))
        p.drawRoundedRect(QRectF(r.left(), r.center().y() - 3, r.width(), 6), 3, 3)
        pos = max(0.0, min(1.0, float(d.position_fn()) / total))
        p.setBrush(QColor("#8b5cf6"))
        p.drawRoundedRect(QRectF(r.left(), r.center().y() - 3, r.width() * pos, 6), 3, 3)
        for i, t in enumerate(d.session.times):
            if t is None:
                continue
            x = r.left() + r.width() * max(0.0, min(1.0, t / total))
            cur = i == d.session.cursor
            p.setPen(QPen(QBrush(QColor("#ffffff" if cur else "#6f6f88")), 2.0 if cur else 1.0))
            p.drawLine(QPointF(x, r.top() + (0 if cur else 4)), QPointF(x, r.bottom() - (0 if cur else 4)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(r.left() + r.width() * pos, r.center().y()), 6, 6)
        p.end()


class TapSyncDialog(QDialog):
    saved = Signal(str)          # the finished .lrc text
    preview = Signal(str)        # the timing so far, for the live overlay preview

    def __init__(self, title: str, artist: str, lines: list[str], position_fn, restart_fn=None,
                 duration: float = 0.0, parent=None, initial_times=None, seek_fn=None, can_seek_fn=None,
                 shift: float = 0.0):
        super().__init__(parent)
        self.title_, self.artist_ = title, artist
        self.session = TapSession(list(lines), list(initial_times or []))
        self.position_fn = position_fn
        self.restart_fn = restart_fn
        self.seek_fn = seek_fn
        self.can_seek_fn = can_seek_fn or (lambda: False)
        self.duration = duration
        self.shift = shift
        self.setWindowTitle(f"Sync this song – {title}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(STYLE + "QListWidget{background:#14141b;color:#d8d8e4;border:1px solid #24242f;"
                                   "border-radius:12px;padding:6px;font-family:'Cascadia Mono',Consolas,monospace;"
                                   "font-size:13px;outline:0;}"
                                   "QListWidget::item{padding:6px 8px;border-radius:8px;}"
                                   "QListWidget::item:selected{background:#2a1f47;color:#fff;}")
        self.setMinimumSize(620, 560)

        root = QVBoxLayout(self)
        head = QLabel(f"<b>{title}</b> — {artist}")
        head.setStyleSheet("font-size:14px;")
        root.addWidget(head)
        how = QLabel("Press <b>SPACE</b> when the line marked ▶ starts. "
                     "<b>Click</b> a wrong line to re-time it from there. "
                     "<b>← / →</b> moves the selected line 0.1 s (Shift = 0.5 s). "
                     "<b>Backspace</b> = undo. Changes show live in the overlay.")
        how.setWordWrap(True)
        how.setStyleSheet("color:#b8b8cc; font-size:12px;")
        root.addWidget(how)

        self.timeline = Timeline(self)
        root.addWidget(self.timeline)

        self.next_lbl = QLabel()
        self.next_lbl.setWordWrap(True)
        self.next_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_lbl.setStyleSheet("color:#fff; font-size:22px; font-weight:600; padding:14px;"
                                    "border:1px solid #6d4bd8; border-radius:14px; background:#1b1530;")
        root.addWidget(self.next_lbl)

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)        # keys go to the window, not the list
        self.list.itemClicked.connect(self._clicked)
        root.addWidget(self.list, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#c9b8ff; font-size:12px;")
        root.addWidget(self.status)

        row = QHBoxLayout()
        self.restart_btn = QPushButton("Restart song")
        self.tap_btn = QPushButton("TAP  (Space)")
        self.undo_btn = QPushButton("Undo")
        self.save_btn = QPushButton("Save")
        cancel = QPushButton("Cancel")
        self.save_btn.setObjectName("primary")
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

        self._build_list()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_status)
        self.timer.start(100)
        self._refresh()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------ keys
    def keyPressEvent(self, e):
        key = e.key()
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Space:
            self.tap()
        elif key == Qt.Key.Key_Backspace:
            self.undo()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.nudge_selected((-1 if key == Qt.Key.Key_Left else 1) * (0.5 if shift else 0.1))
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            r = max(0, min(self.list.count() - 1, self.list.currentRow() + (-1 if key == Qt.Key.Key_Up else 1)))
            self.list.setCurrentRow(r)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.save()
        else:
            super().keyPressEvent(e)

    # ------------------------------------------------------------------ actions
    def tap(self):
        i = self.session.cursor
        if self.session.tap(float(self.position_fn())):
            self.list.setCurrentRow(i)
            self._changed()

    def undo(self):
        self.session.undo()
        self._jump_before(self.session.cursor)
        self._changed()

    def go_to(self, index: int):
        """Re-time from this line: move the cursor there (and jump the song back if possible)."""
        self.session.go_to(index)
        self._jump_before(index)
        self._refresh()

    def nudge_selected(self, delta: float):
        r = self.list.currentRow()
        if r >= 0:
            self.session.nudge(r, delta)
            self._changed()

    def _clicked(self, item):
        self.go_to(self.list.row(item))

    def _jump_before(self, index: int):
        if self.seek_fn is not None and self.can_seek_fn():
            self.seek_fn(self.session.rewind_target(index))

    def _restart(self):
        if self.restart_fn:
            self.session.go_to(0)
            self.restart_fn()
        self._refresh()

    def save(self):
        if not self.session.timed_count:
            return
        self.saved.emit(self.session.to_lrc(self.title_, self.artist_, self.duration, self.shift))
        self.accept()

    # ------------------------------------------------------------------ display
    def _changed(self):
        self._refresh()
        self.preview.emit(self.session.to_lrc(self.title_, self.artist_, self.duration, self.shift))

    def _build_list(self):
        self.list.clear()
        for _ in self.session.lines:
            self.list.addItem(QListWidgetItem(""))

    def _refresh(self):
        s = self.session
        for i, line in enumerate(s.lines):
            mark = "▶" if i == s.cursor else " "
            self.list.item(i).setText(f"{mark} {_t(s.times[i])}  {line}")
        if 0 <= s.cursor < self.list.count():
            self.list.scrollToItem(self.list.item(s.cursor))
        self.next_lbl.setText("All lines timed – press Save (or click a line to fix it)." if s.done
                              else s.lines[s.cursor])
        self.undo_btn.setEnabled(s.cursor > 0)
        self.save_btn.setEnabled(s.timed_count > 0)
        self.tap_btn.setEnabled(not s.done)
        self._refresh_status()

    def showEvent(self, e):
        super().showEvent(e)
        try:
            from . import win32
            win32.set_dark_titlebar(int(self.winId()))
        except Exception:
            pass

    def _refresh_status(self):
        self.timeline.update()
        seek = ("Clicking a line jumps the song to just before it." if self.can_seek_fn()
                else "Your player doesn't allow jumping, so after clicking a line wait for it to come "
                     "round (or press Restart song).")
        self.status.setText(f"Song at {format_time(self.position_fn())} · {self.session.timed_count} of "
                            f"{len(self.session.lines)} lines timed · {seek}")
