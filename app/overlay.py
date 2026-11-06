"""The floating, transparent lyrics window.

Plain English: this is a frameless window with a see-through background that
stays above other windows. We draw everything ourselves (rounded dark panel,
song title, lyrics, karaoke highlight) about 60 times per second so the
highlight glides smoothly across each word.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QLinearGradient,
                           QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import QApplication, QWidget

from . import win32
from .config import Settings
from .lrc import Line, Lyrics, word_state

LINE_ANIM_SECONDS = 0.28


def _qc(hex_str: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_str)
    if not c.isValid():
        c = QColor("#FFFFFF")
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t,
                           a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t,
                           a.alphaF() + (b.alphaF() - a.alphaF()) * t)


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class OverlayWindow(QWidget):
    moved = Signal(int, int)               # user dragged the overlay
    context_menu_requested = Signal(object)  # QPoint (global)
    settings_requested = Signal()

    def __init__(self, settings: Settings):
        super().__init__(None)
        self.s = settings
        self.setWindowTitle("Spotify Lyrics Overlay")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # What to show (set by the controller)
        self.title = ""
        self.artist = ""
        self.playing = False
        self.status = "Waiting for Spotify…"
        self.lyrics: Lyrics | None = None
        self.position = 0.0
        self.word_mode_label = ""

        self._line_idx = -2
        self._anim_start = 0.0
        self._drag_offset = None
        self._hover = False
        self.apply_settings()

    # ------------------------------------------------------------ settings
    def fonts(self):
        main = QFont(self.s.font_family, -1)
        main.setPixelSize(self.s.font_size)
        main.setBold(self.s.bold)
        ctx = QFont(main)
        ctx.setPixelSize(max(8, int(self.s.font_size * 0.62)))
        ctx.setBold(False)
        head = QFont(self.s.font_family, -1)
        head.setPixelSize(max(9, int(self.s.font_size * 0.45)))
        head.setBold(True)
        return main, ctx, head

    def apply_settings(self):
        main, ctx, head = self.fonts()
        self._pad = max(10, int(self.s.font_size * 0.45))
        self._main_h = QFontMetricsF(main).height() * 1.25
        self._ctx_h = QFontMetricsF(ctx).height() * 1.25 if self.s.show_context_lines else 0
        self._head_h = QFontMetricsF(head).height() * 1.6 if self.s.show_title else 0
        h = int(self._pad * 2 + self._head_h + self._main_h + self._ctx_h * 2)
        self.setFixedSize(self.s.width, h)
        self.setWindowOpacity(self.s.window_opacity)
        self.reposition()
        self.apply_click_through()
        self.update()

    def target_screen(self):
        screens = QApplication.screens()
        if not screens:
            return None
        idx = self.s.screen_index if 0 <= self.s.screen_index < len(screens) else 0
        return screens[idx]

    def reposition(self):
        scr = self.target_screen()
        if scr is None:
            return
        g = scr.geometry()   # full screen, so it lines up over fullscreen games
        w, h, m = self.width(), self.height(), self.s.margin
        pos = self.s.position
        if pos == "custom":
            x, y = self.s.custom_x, self.s.custom_y
        else:
            if pos.endswith("left"):
                x = g.left() + m
            elif pos.endswith("right"):
                x = g.right() - w - m
            else:
                x = g.left() + (g.width() - w) // 2
            if pos.startswith("top"):
                y = g.top() + m
            elif pos.startswith("bottom"):
                y = g.bottom() - h - m
            else:
                y = g.top() + (g.height() - h) // 2
        self.move(int(x), int(y))

    def apply_click_through(self):
        if win32.IS_WINDOWS:
            if self.isVisible():
                win32.set_overlay_styles(int(self.winId()), self.s.click_through)
        else:
            # Portable Qt fallback (used when testing on other systems)
            was_visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, self.s.click_through)
            if was_visible:
                self.show()

    def showEvent(self, e):
        super().showEvent(e)
        if win32.IS_WINDOWS:
            win32.set_overlay_styles(int(self.winId()), self.s.click_through)

    def keep_on_top(self):
        if self.isVisible():
            hwnd = int(self.winId())
            win32.ensure_styles(hwnd, self.s.click_through)   # games can't undo click-through
            win32.force_topmost(hwnd)

    # ------------------------------------------------------------ state
    def set_track(self, title: str, artist: str):
        self.title, self.artist = title, artist
        self._line_idx = -2

    def set_lyrics(self, lyrics: Lyrics | None, status: str = ""):
        self.lyrics = lyrics
        self.status = status
        self._line_idx = -2
        if lyrics and lyrics.synced:
            if lyrics.has_word_timing:
                self.word_mode_label = "word sync"
            elif self.s.estimate_word_timing:
                self.word_mode_label = "line sync · word estimate"
            else:
                self.word_mode_label = "line sync"
        else:
            self.word_mode_label = ""
        self.update()

    def set_status(self, status: str):
        self.status = status
        self.update()

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.MouseButton.RightButton:
            self.context_menu_requested.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self.moved.emit(self.x(), self.y())

    def mouseDoubleClickEvent(self, e):
        self.settings_requested.emit()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_background(p)
        y = self._pad
        main_f, ctx_f, head_f = self.fonts()
        if self.s.show_title:
            self._paint_header(p, head_f, y)
            y += self._head_h
        self._paint_lyrics(p, main_f, ctx_f, y)
        p.end()

    def _paint_background(self, p: QPainter):
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(18.0, r.height() / 4)
        bg = _qc(self.s.background_color, self.s.background_opacity)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, radius, radius)
        if self.s.background_opacity > 0.02 or self._hover:
            # thin neon edge, brighter while hovering (shows you can drag it)
            g = QLinearGradient(r.topLeft(), r.topRight())
            a = 0.55 if self._hover and not self.s.click_through else 0.28
            g.setColorAt(0, _qc(self.s.highlight_color, a))
            g.setColorAt(1, _qc(self.s.highlight_color2, a))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QBrush(g), 1.4))
            p.drawRoundedRect(r, radius, radius)

    def _paint_header(self, p: QPainter, f: QFont, y: float):
        fm = QFontMetricsF(f)
        x = self._pad * 1.4
        avail = self.width() - x * 2
        if self.title:
            icon = "♪ " if self.playing else "❚❚ "
            text = f"{icon}{self.title}  —  {self.artist}" if self.artist else f"{icon}{self.title}"
        else:
            text = "♪ Spotify Lyrics Overlay"
        right = self.word_mode_label
        right_w = fm.horizontalAdvance(right) + 12 if right else 0
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(50, avail - right_w))
        base = y + fm.ascent()
        p.setFont(f)
        p.setPen(_qc(self.s.text_color, 0.72))
        p.drawText(QPointF(x, base), text)
        if right:
            p.setPen(_qc(self.s.highlight_color, 0.75))
            p.drawText(QPointF(self.width() - x - fm.horizontalAdvance(right), base), right)

    # -- text helpers
    def _text_path(self, f: QFont, x: float, base: float, text: str) -> QPainterPath:
        path = QPainterPath()
        path.addText(QPointF(x, base), f, text)
        return path

    def _draw_text(self, p: QPainter, path: QPainterPath, brush, outline_alpha=0.55, glow: QColor | None = None):
        if glow is not None and self.s.glow:
            for width, a in ((10, 0.10), (6, 0.18), (3, 0.30)):
                c = QColor(glow)
                c.setAlphaF(a * glow.alphaF())
                p.strokePath(path, QPen(QBrush(c), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        # dark outline keeps text readable over bright game scenes
        p.strokePath(path, QPen(QBrush(QColor(0, 0, 0, int(255 * outline_alpha))), 2.2,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, brush)

    def _fit_font(self, f: QFont, text: str, avail: float) -> QFont:
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text)
        if w <= avail or w == 0:
            return f
        nf = QFont(f)
        nf.setPixelSize(max(8, int(f.pixelSize() * max(0.5, avail / w))))
        return nf

    def _paint_simple_line(self, p, f: QFont, text: str, cy: float, alpha: float):
        if not text or alpha <= 0.01:
            return
        avail = self.width() - self._pad * 3
        f = self._fit_font(f, text, avail)
        fm = QFontMetricsF(f)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
        x = (self.width() - fm.horizontalAdvance(text)) / 2
        base = cy + (fm.ascent() - fm.descent()) / 2
        self._draw_text(p, self._text_path(f, x, base, text), _qc(self.s.text_color, alpha), 0.4 * alpha)

    def _paint_lyrics(self, p: QPainter, main_f: QFont, ctx_f: QFont, top: float):
        ly = self.lyrics
        prev_cy = top + self._ctx_h / 2
        main_cy = top + self._ctx_h + self._main_h / 2
        next_cy = top + self._ctx_h + self._main_h + self._ctx_h / 2

        if not ly or not ly.synced:
            msg = self.status or ("Instrumental ♪" if ly and ly.instrumental else "")
            self._paint_simple_line(p, ctx_f if len(msg) > 40 else main_f, msg, main_cy, 0.7)
            return

        t = self.position
        idx = ly.line_index_at(t)
        if idx != self._line_idx:
            self._line_idx = idx
            self._anim_start = time.monotonic()
        a = _ease((time.monotonic() - self._anim_start) / LINE_ANIM_SECONDS)

        lines = ly.lines
        ctx = self.s.show_context_lines
        slide = (1 - a) * (self._ctx_h or self._main_h * 0.4)

        if ctx and idx - 1 >= 0:
            self._paint_simple_line(p, ctx_f, lines[idx - 1].text, prev_cy + slide, 0.38 * a)
        if ctx:
            nxt = lines[idx + 1] if idx + 1 < len(lines) else None
            if nxt is not None:
                self._paint_simple_line(p, ctx_f, nxt.text or "♪", next_cy + slide * 0.5, 0.5)

        if idx < 0:
            # intro before the first lyric: little pulsing notes + countdown feel
            first = lines[0].start if lines else 0
            remaining = max(0.0, first - t)
            dots = "♪  ♪  ♪" if remaining > 1.0 else "♪"
            pulse = 0.45 + 0.25 * abs(((time.monotonic() * 1.2) % 2) - 1)
            self._paint_simple_line(p, main_f, dots, main_cy, pulse)
            return

        line = lines[idx]
        if line.is_break:
            pulse = 0.45 + 0.25 * abs(((time.monotonic() * 1.2) % 2) - 1)
            self._paint_simple_line(p, main_f, "♪  ♪  ♪", main_cy + slide, pulse * a)
            return
        self._paint_karaoke_line(p, main_f, line, t, main_cy + slide, a)

    def _paint_karaoke_line(self, p: QPainter, f: QFont, line: Line, t: float, cy: float, alpha: float):
        avail = self.width() - self._pad * 3
        f = self._fit_font(f, line.text, avail)
        fm = QFontMetricsF(f)
        base = cy + (fm.ascent() - fm.descent()) / 2
        text_c = _qc(self.s.text_color, alpha)
        hi1 = _qc(self.s.highlight_color, alpha)
        hi2 = _qc(self.s.highlight_color2, alpha)

        if not line.words:
            # ---- line-by-line mode: whole line lit, thin progress bar underneath
            text = fm.elidedText(line.text, Qt.TextElideMode.ElideRight, avail)
            w = fm.horizontalAdvance(text)
            x = (self.width() - w) / 2
            self._draw_text(p, self._text_path(f, x, base, text), text_c, 0.55 * alpha, glow=hi1)
            dur = max(line.end - line.start, 0.1)
            prog = max(0.0, min(1.0, (t - line.start) / dur))
            bar_y = base + fm.descent() + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(self.s.text_color, 0.15 * alpha))
            p.drawRoundedRect(QRectF(x, bar_y, w, 3), 1.5, 1.5)
            g = QLinearGradient(x, 0, x + w, 0)
            g.setColorAt(0, hi1)
            g.setColorAt(1, hi2)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x, bar_y, w * prog, 3), 1.5, 1.5)
            return

        # ---- word-by-word karaoke mode
        space = fm.horizontalAdvance(" ")
        widths = [fm.horizontalAdvance(w.text) for w in line.words]
        total = sum(widths) + space * (len(widths) - 1)
        x = (self.width() - total) / 2
        st = word_state(line, t)
        sung_c = _mix(text_c, hi1, 0.45)
        top = base - fm.ascent()
        height = fm.height()

        for i, (word, w) in enumerate(zip(line.words, widths)):
            path = self._text_path(f, x, base, word.text)
            grad = QLinearGradient(x, 0, x + max(w, 1), 0)
            grad.setColorAt(0, hi1)
            grad.setColorAt(1, hi2)
            if i < st.index or (i == st.index and st.progress >= 1.0):
                # already sung: soft tint, fading from the full highlight
                fade = min(1.0, max(0.0, (t - word.end) / 0.35))
                c = _mix(hi1, sung_c, fade)
                self._draw_text(p, path, c, 0.55 * alpha, glow=hi1 if fade < 1 else None)
            elif i == st.index:
                # the word being sung right now: smooth left-to-right colour wipe
                self._draw_text(p, path, text_c, 0.55 * alpha, glow=hi1)
                p.save()
                p.setClipRect(QRectF(x - 2, top - 4, (w + 4) * st.progress, height + 8))
                p.fillPath(path, QBrush(grad))
                p.restore()
            else:
                self._draw_text(p, path, _qc(self.s.text_color, 0.9 * alpha), 0.55 * alpha)
            x += w + space
