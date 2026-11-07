"""Sleek notification cards that slide in above the taskbar (instead of plain Windows balloons).

They never take keyboard focus away from your game, fade out by themselves, and
can carry one action button (for example "Update now").
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

KIND_COLOURS = {"info": "#8B5CF6", "warning": "#F59E0B", "success": "#22C55E"}


class Toast(QWidget):
    clicked = Signal()            # the card (or its button) was clicked

    WIDTH = 360

    def __init__(self, icon=None):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.icon = icon
        self.title = ""
        self.text = ""
        self.action = ""
        self.kind = "info"
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.dismiss)
        self._anim = None
        self._fade = None

    # ------------------------------------------------------------------
    def show_message(self, title: str, text: str, kind: str = "info", ms: int = 4000, action: str = ""):
        self.title, self.text, self.kind, self.action = title, text, kind, action
        self.setFixedSize(self.WIDTH, self._needed_height())
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        g = scr.availableGeometry()                       # above the taskbar
        end = QPoint(g.right() - self.width() - 16, g.bottom() - self.height() - 16)
        start = QPoint(end.x() + 40, end.y())
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()
        self.update()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(260)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(220)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._anim.start()
        self._fade.start()
        self._hide_timer.start(max(1500, ms))

    def dismiss(self):
        if not self.isVisible():
            return
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(260)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.hide)
        self._fade.start()

    # ------------------------------------------------------------------
    def _fonts(self):
        title = QFont("Segoe UI", -1)
        title.setPixelSize(14)
        title.setBold(True)
        body = QFont("Segoe UI", -1)
        body.setPixelSize(13)
        return title, body

    def _wrap(self, text: str, font: QFont, width: float) -> list[str]:
        fm = QFontMetricsF(font)
        out = []
        for para in text.split("\n"):
            line = ""
            for word in para.split(" "):
                trial = (line + " " + word).strip()
                if fm.horizontalAdvance(trial) <= width or not line:
                    line = trial
                else:
                    out.append(line)
                    line = word
            out.append(line)
        return out[:5]

    def _text_width(self) -> float:
        return self.WIDTH - 16 - 52 - 16

    def _needed_height(self) -> int:
        tf, bf = self._fonts()
        lines = self._wrap(self.text, bf, self._text_width())
        h = 16 + QFontMetricsF(tf).height() + 4 + len(lines) * QFontMetricsF(bf).height() + 16
        if self.action:
            h += 36
        return int(max(76, h))

    def _button_rect(self) -> QRectF:
        return QRectF(68, self.height() - 16 - 28, 130, 28)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        card = QPainterPath()
        card.addRoundedRect(r, 14, 14)
        p.fillPath(card, QColor(22, 22, 30, 245))
        p.setPen(QPen(QBrush(QColor(255, 255, 255, 28)), 1.0))
        p.drawPath(card)
        accent = QColor(KIND_COLOURS.get(self.kind, KIND_COLOURS["info"]))
        # accent bar on the left
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(r.left() + 8, r.top() + 14, 3, r.height() - 28), 1.5, 1.5)
        p.fillPath(bar, accent)
        # icon
        if self.icon is not None:
            pm = self.icon.pixmap(36, 36)
            p.drawPixmap(22, 18, pm)
        tf, bf = self._fonts()
        x = 68
        y = 16 + QFontMetricsF(tf).ascent()
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QPointF(x, y), self.title)
        p.setFont(bf)
        p.setPen(QColor(200, 200, 214))
        y += QFontMetricsF(tf).descent() + 4
        for line in self._wrap(self.text, bf, self._text_width()):
            y += QFontMetricsF(bf).height()
            p.drawText(QPointF(x, y - QFontMetricsF(bf).descent()), line)
        if self.action:
            br = self._button_rect()
            btn = QPainterPath()
            btn.addRoundedRect(br, 8, 8)
            p.fillPath(btn, accent)
            p.setPen(QColor(255, 255, 255))
            bold = QFont(bf)
            bold.setBold(True)
            p.setFont(bold)
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, self.action)
        p.end()

    def mousePressEvent(self, e):
        self.clicked.emit()
        self.dismiss()


MENU_STYLE = """
QMenu { background: #16161e; color: #ececf3; border: 1px solid #2a2a36; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 8px 28px 8px 14px; border-radius: 8px; margin: 1px 2px; }
QMenu::item:selected { background: #2a1f47; color: #fff; }
QMenu::item:disabled { color: #6f6f88; }
QMenu::separator { height: 1px; background: #262632; margin: 6px 10px; }
QMenu::indicator { width: 14px; height: 14px; margin-left: 6px; }
QMenu::indicator:checked { background: #8b5cf6; border-radius: 4px; }
"""


def style_menu(menu) -> None:
    """Dark, rounded tray menu."""
    menu.setStyleSheet(MENU_STYLE)
    menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
